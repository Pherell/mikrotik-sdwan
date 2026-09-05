# Plan v2 — the three pillars

[plan.md](plan.md) built the data plane: overlays, routing, steering, safe apply.
This plan closes the gap between "an overlay orchestrator" and "an SD-WAN
product", measured against the three things the category is usually defined by.

## Where the project actually stands

| Pillar | Today | Honest verdict |
|---|---|---|
| **Zero-touch provisioning** | Nothing. Enable `www-ssl`, make a certificate, make a user, then type credentials into the UI | **Absent.** This is high-touch onboarding |
| **Application-aware routing** | Prefix, port and DSCP matching → mangle → routing tables → SLA demotion | **Half there.** The steering works; the *identification* is crude and static |
| **Real-time telemetry** | `app/telemetry/__init__.py`, zero lines | **Absent.** You can steer by SLA but cannot see the SLA |

That last row is the strangest place to have stopped. Netwatch is already
measuring loss, jitter and RTT on every device, and the controller throws all of
it away.

---

## The finding that shapes this plan

**RouterOS netwatch already exposes the measurements.** For `type=icmp` it
reports, per probe:

```
status  since  sent-count  response-count  loss-percent
rtt-avg  rtt-min  rtt-max  rtt-jitter  rtt-stdev
```

The controller writes those probes today (one per tunnel, thresholds from the
SLA profile) and never reads them back. So real SLA telemetry costs **one extra
read per site per interval** — no synthetic probes from the controller, no agent,
no SNMP, no additional load on the device. The measurement already happened.

That inverts the usual build order. Telemetry is normally the expensive pillar;
here it is the cheapest, and everything else gets more useful once it exists.
So it goes first.

---

## M7 — Telemetry

**Goal:** see what the network is doing, over time, per link.

### What is collected

One poller pass per site, every 30 seconds by default:

| Source | Gives |
|---|---|
| `/tool/netwatch` | **loss %, rtt avg/min/max, jitter, stdev, up/down** — per tunnel |
| `/interface` (WAN + tunnel) | rx/tx bytes and packets, errors, drops, running |
| `/ip/ipsec/active-peers` | SA established, uptime, last seen |
| `/routing/bgp/session` | established, prefix counts, uptime |
| `/ip/firewall/mangle` | **per-policy byte and packet counters** — traffic per application rule |
| `/system/resource` | CPU, free memory, uptime |

Counters are monotonic and reset on reboot; the poller stores deltas and
discards any negative delta as a reset rather than recording a spike.

### Storage

**Plain Postgres, no new extension.** The volume does not justify one: 50 sites
× 4 links × 2 samples/minute is ~576k rows/day, which Postgres handles without
comment. TimescaleDB would be nicer at ten times that scale and is a dependency
to add later, not now.

Three tables, downsampled by a nightly job:

```
sample_raw      30s resolution, kept 48 hours
sample_5m       5-minute rollups (avg/min/max/p95), kept 90 days
sample_1h       hourly rollups, kept 2 years
```

Rollups store percentiles, not just averages. A link that is fine on average and
terrible at p95 is the one causing complaints.

### Exposed as

- `GET /api/v1/links/{id}/series?metric=loss&from=…&to=…` for the UI
- Per-link Prometheus gauges on `/metrics`, so it drops into existing monitoring
- Sparklines on the fabric page; a link detail page with loss/latency/jitter over
  time and the SLA threshold drawn as a line, so "why did it fail over" is one
  glance rather than an investigation

### Alerting

Rules on the series, evaluated by the worker: link down, SLA breached for N
intervals, drift detected, rollback armed, site unreachable. Delivered by
webhook (Slack/Teams/generic) and SMTP. Deduplicated and with a resolve
notification — an alerting system that only fires is one people mute.

**Done when:** impairing a lab link shows up as a loss spike in the UI within a
minute, the failover is visible on the same chart, and a webhook fires once for
the breach and once for the recovery.

---

## M8 — Provisioning

**Goal:** get a device from factory-default to fabric member without a human
typing configuration into it.

### Be honest about what "zero-touch" can mean here

MikroTik has no vendor cloud that hands a controller its serial numbers. True
zero-touch — unbox, power on, it joins — is only achievable in specific setups.
So this ships **three tiers**, and the docs say plainly which one you are in.

**Tier 1 — one-touch enrollment (the realistic default).**
An operator creates an enrollment token in the UI. A field tech pastes **one
line** into the router's terminal:

```
/tool fetch url="https://sdwan.example.com/enroll/7f3a9c2e" output=file \
  dst-path=enroll.rsc; /import enroll.rsc
```

The bootstrap script the controller generates:

1. creates a certificate and enables `www-ssl`
2. creates a dedicated `sdwan` user with a controller-generated password,
   restricted to the controller's address
3. disables `www` and `api`
4. calls back to the controller to confirm

The controller then probes, pins the device identity, discovers uplinks, and —
if the token carried a site template — applies the fabric config. Nobody ever
types or sees a device password; it is generated, encrypted, and never displayed.

**Tier 2 — true zero-touch on a staging LAN.**
Where you control DHCP, hand out option 66/67 pointing at the controller's
bootstrap endpoint. A factory-default RouterOS box takes a DHCP lease, fetches,
and enrolls with nobody touching it. This is how you stage a batch before
shipping them to branches.

**Tier 3 — pre-staged image.**
Bake the bootstrap into a netinstall image or a `.rsc` on the device before it
ships. The box enrolls the first time it reaches the internet.

### Token security

This is the part to get right, because an enrollment endpoint is
unauthenticated by definition:

- single-use, short TTL (default 24h), revocable, scoped to one intended site
- optionally bound to a source address range
- the token grants *only* the bootstrap script — never fabric keys, never any
  other site's data
- every fetch is audited whether it succeeds or not
- because enrollment pins the device identity at the moment it happens, and the
  controller generated the password itself, the trust-on-first-use window closes
  as soon as the device enrols rather than the first time an operator gets round
  to probing it

### Site templates

A template carries role, fabric memberships, local prefix pattern, uplink tag
conventions, SLA and policy assignments. Enrolling with a template means a new
branch is a fabric member with steering applied before anyone opens the UI.

**Done when:** a factory-default CHR in the lab, given one pasted line, appears
in the UI as a probed, pinned, fabric-joined site with tunnels up — with no
other human input.

---

## M9 — Application awareness

**Goal:** identify traffic properly, and show what the policies are actually
doing.

### TLS SNI matching

RouterOS has had a `tls-host` firewall matcher since 6.41. It reads the SNI from
the TLS handshake — real application identification, no DPI, negligible CPU:

```
/ip/firewall/mangle add chain=prerouting protocol=tcp dst-port=443 \
  tls-host=*.teams.microsoft.com action=mark-connection \
  new-connection-mark=app-teams
/ip/firewall/mangle add chain=prerouting connection-mark=app-teams \
  action=mark-routing new-routing-mark=sdwan-voice
```

Note the two-stage shape: SNI is only visible in the handshake, so the *first*
packets are matched on `tls-host` and marked at the **connection** level, and
subsequent packets inherit it. The current renderer marks routing directly per
packet; SNI matching requires connection marks, which is a real change to the
policy renderer rather than a new field.

Limits, stated in the UI: SNI is plaintext today but Encrypted Client Hello
breaks it, and it only sees TLS. Prefix matching stays as the fallback.

### Endpoint feeds

App groups become refreshable instead of hand-typed:

- **Microsoft 365 / Teams** — Microsoft publishes a versioned JSON endpoint list
- **AWS / Azure / GCP / Cloudflare** — all publish machine-readable ranges
- a scheduled job refreshes them, diffs, and applies through the normal
  render → diff → safe-apply path, so a feed change is a reviewable diff and not
  a surprise
- feeds are pinned to a version and a refresh that shrinks a list by more than a
  configured fraction is held for review rather than applied — a bad upstream
  publish should not blackhole an application

### Per-application visibility

The mangle counters M7 already collects become the answer to "is this policy
doing anything?" Per-policy bytes over time, which path each application is
currently on, and how often it has flipped. A steering rule you cannot observe
is a steering rule nobody trusts.

**Done when:** a policy matching `*.teams.microsoft.com` steers real Teams
traffic in the lab, the UI shows its byte counters climbing on the preferred
path, and impairing that path moves both the traffic and the graph.

---

## M10 — The operationally crucial, unglamorous things

Things a production deployment needs that no marketing page lists.

### MSS clamping — a real bug, not a feature

GRE adds 24 bytes and IPsec transport-mode ESP adds ~40 more, which is why the
tunnel MTU is 1400. TCP endpoints that set DF and never see the ICMP
Fragmentation Needed reply will **blackhole silently** — the classic "small
pages load, large ones hang" fault, and it is the single most common
SD-WAN-over-IPsec support call.

The current renderer does not emit MSS clamping. It should, on every tunnel:

```
/ip/firewall/mangle add chain=forward protocol=tcp tcp-flags=syn \
  action=change-mss new-mss=clamp-to-pmtu out-interface=<tunnel>
```

This is a defect in what already ships, not a v2 feature, and it should land
first regardless of the rest of this plan.

### Controller high availability

Today: one instance, in-memory login throttle, no leader election. The data
plane survives a controller outage by design, but the controller is a single
point for *changes*. Needs: a leader lock for the worker so two replicas do not
both sweep, throttle state that is shared or accepted as per-replica, and a
documented restore-from-backup path.

### Certificate authority

Pinning closes most of the TOFU gap but a controller-run CA closes it properly:
issue a certificate per device at enrolment, distribute the root, set
`verify_tls: true`. Real validation instead of trust-on-first-use. Enrolment
(M8) is the natural moment to do it.

### The rest

- **Maintenance windows** — queue applies for an approved window rather than now
- **Token revocation** — a stolen JWT is currently valid until it expires
- **Multi-tenancy enforcement** — the schema carries `tenant_id`; nothing filters on it
- **QoS** — shape and prioritise per policy class, not just steer
- **Config backup/restore of the controller** — the intent export exists; a full
  operational restore procedure does not
- **Bulk operations** — apply a template change across N sites as one reviewable plan

---

## Sequencing, and why

```
M10a  MSS clamping                  ← a defect; do it first, it is small
M7    Telemetry                     ← makes everything else observable
M8    Provisioning                  ← the biggest operator-facing win
M9    Application awareness         ← needs M7 to prove it works
M10b  HA, CA, maintenance windows   ← hardening for a real fleet
```

Telemetry before provisioning is deliberate. Onboarding fifty sites you cannot
observe is how you end up with fifty sites in an unknown state.

## What this does not fix

Being clear about the ceiling:

- **Sub-second failover is not achievable this way.** Netwatch's floor is
  ~10–15s. Commercial SD-WAN duplicates packets across two links simultaneously
  with FEC so a loss is invisible; RouterOS cannot do that.
- **No forward error correction, no jitter buffering, no per-packet steering.**
- **SNI matching degrades as Encrypted Client Hello spreads.**
- **Still no hardware validation.** Every milestone here should land with a
  containerlab test, and the honest position stays unchanged until this runs
  against real MikroTiks.
