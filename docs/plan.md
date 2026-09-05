# MikroTik SD-WAN Controller — Implementation Plan

## Context

There is no vendor-neutral, self-hostable SD-WAN control plane for MikroTik. Building a fabric across RouterOS sites today means hand-writing IPsec peers, identities, proposals, GRE tunnels, BGP connections, mangle rules and netwatch scripts on every box — then repeating it, by hand, whenever a site is added or a policy changes. It does not scale, it drifts, and a typo on a WAN edge locks you out of the router.

This project builds **an intent-based SD-WAN controller for RouterOS, shipped as a Docker Compose stack with a web UI.** You describe sites, links, and policies once in a declarative model; the controller renders per-device RouterOS configuration, shows you a diff, applies it inside a dead-man rollback, then continuously verifies that the device still matches intent.

The current working directory (`C:\Users\avare\Documents\docking-code`) contains an unrelated Arduino docking project. **This is a greenfield build in a new repository** — nothing here is reused.

### Decisions locked in

| Decision | Choice |
|---|---|
| Device access | Agentless — RouterOS REST API (`/rest`, v7.1+) with SSH fallback |
| Overlay transport | IPsec/IKEv2 default, **pluggable** — WireGuard, GRE, IPIP, VXLAN, EoIP selectable per fabric/link |
| Topology | Hub-and-spoke with dynamic spoke-to-spoke mesh |
| RouterOS support | v7 primary (REST), v6 legacy (SSH, separate template set) |
| Backend | Python 3.12 / FastAPI / Postgres / Redis / React + TypeScript |
| Cloud hub | Optional strongSwan + FRR container — a real hub/route-reflector without buying a CHR |
| Safety | Dry-run diff → safe-apply → auto-rollback → drift detection |
| Tenancy | Single org, RBAC (admin/operator/viewer), `tenant_id` in schema for later |

---

## Architecture

```
                          ┌──────────── Docker Compose ────────────┐
   Browser ──HTTPS──►  Caddy ──► React SPA
                          │        │
                          │        └──► FastAPI  ──► Postgres  (intent, state, audit)
                          │                 │   └──► Redis     (queue, cache, locks)
                          │                 ▼
                          │            ARQ workers ──┬─► RouterOS REST (v7)  ─┐
                          │            + scheduler   └─► SSH/paramiko (v6)   │
                          │                                                   │
                          │            softhub (optional)                     │
                          │            strongSwan + FRR ◄─── IPsec + iBGP ────┤
                          └───────────────────────────────────────────────────┘
                                                                              │
                             ┌──────────────┬──────────────┬─────────────────┘
                          Hub CHR       Spoke hEX      Spoke RB5009   ... (RouterOS)
                             └──── dynamic spoke-to-spoke tunnels ────┘
```

**Control plane is out-of-band.** The controller never sits in the data path. If the controller dies, the fabric keeps forwarding — BGP and netwatch on the routers handle convergence. This is the single most important design constraint and it drives everything below.

### Repository layout

```
mikrotik-sdwan/
├── docker-compose.yml            # api, worker, scheduler, db, redis, caddy, ui
├── docker-compose.softhub.yml    # optional strongSwan + FRR hub
├── docker-compose.dev.yml        # hot reload, containerlab test fabric
├── .env.example
├── backend/
│   ├── app/
│   │   ├── main.py               # FastAPI app factory
│   │   ├── api/v1/               # sites, links, fabrics, policies, jobs, auth, ws
│   │   ├── models/               # SQLAlchemy 2.0 ORM
│   │   ├── schemas/              # Pydantic v2 — the intent model
│   │   ├── drivers/              # ← device access abstraction
│   │   │   ├── base.py           #   DeviceDriver protocol
│   │   │   ├── ros7_rest.py
│   │   │   ├── ros6_ssh.py
│   │   │   └── softhub.py
│   │   ├── transports/           # ← overlay abstraction (the versatility layer)
│   │   │   ├── base.py           #   TransportDriver protocol
│   │   │   ├── ipsec_gre.py      #   default
│   │   │   ├── ipsec_policy.py
│   │   │   ├── wireguard.py
│   │   │   ├── gre.py / ipip.py
│   │   │   └── vxlan.py / eoip.py
│   │   ├── render/
│   │   │   ├── engine.py         # intent → ConfigSection[] → RouterOS commands
│   │   │   └── templates/ros7/, templates/ros6/
│   │   ├── reconcile/            # diff, plan, apply, rollback, drift
│   │   ├── telemetry/            # SNMP + REST pollers, link scoring
│   │   └── tasks/                # ARQ jobs
│   ├── alembic/
│   └── tests/
├── ui/                           # Vite + React + TS + TanStack Query + Tailwind
├── softhub/                      # strongSwan + FRR image + entrypoint
├── labs/                         # containerlab topologies for CI
└── docs/
```

---

## The intent model

Everything the user configures is one of six objects. Keep this small — it is what makes the system easy to configure.

```python
Site       # a location: name, region, role=hub|spoke, router credentials,
           # ros_version, loopback_ip, local_prefixes[], tags{}
Wan        # an uplink on a site: name, interface, public_ip|dynamic, nat_behind,
           # cost, bandwidth_mbps, provider, tags{}
Fabric     # the overlay: name, transport=ipsec_gre|wireguard|..., transport_params,
           # topology=hub_spoke|hub_spoke_dynamic|full_mesh, ip_pool (tunnel /31s),
           # asn, crypto profile, members[Site]
Link       # DERIVED, not authored: one tunnel between two WANs on two sites.
           # The controller computes the full Wan×Wan cross product per fabric.
Policy     # application/prefix steering: match{src,dst,app,dscp,port} →
           # action{prefer=[wan_tags], sla=SlaProfile, fallback=drop|any}
SlaProfile # loss_pct, latency_ms, jitter_ms thresholds + hold-down timers
```

The controller expands `Fabric` + `Site` + `Wan` into concrete `Link` objects, allocates tunnel IPs from `ip_pool`, generates keys/PSKs, and renders per-device config. **The user never writes a tunnel definition.** Adding a site with two WANs to a two-hub fabric creates four tunnels automatically.

---

## Transport abstraction — the versatility layer

Every overlay technology implements one protocol. This is the seam that keeps IPsec from being hardcoded.

```python
class TransportDriver(Protocol):
    name: str
    supported_ros: set[int]          # {6, 7} or {7}
    requires_static_public_ip: bool  # gates initiator/responder role assignment
    supports_dynamic_mesh: bool

    def allocate(self, link: Link) -> LinkSecrets: ...
        # PSK / keypair / SPI — generated controller-side, stored encrypted

    def render(self, link: Link, side: Literal["a","b"],
               ros: int) -> list[ConfigSection]: ...
        # returns declarative sections; the render engine turns them into
        # RouterOS commands or REST payloads

    def health_checks(self, link: Link) -> list[HealthCheck]: ...
        # what to poll to decide the link is up + its quality score
```

### `ipsec_gre` (default)

RouterOS has **no true VTI**. Route-based IPsec on RouterOS means *GRE (or IPIP) tunnel + transport-mode IPsec policy*. The driver renders, per link:

- `/ip/ipsec/profile` — IKEv2, dh-group 19/20, sha256, configurable
- `/ip/ipsec/proposal` — AES-256-GCM (or CBC+SHA256 for old hardware), PFS on
- `/ip/ipsec/peer` — `exchange-mode=ike2`, `address=<remote WAN>`, `passive` on the responder side
- `/ip/ipsec/identity` — `auth-method=pre-shared-key` (v1) or `digital-signature` with controller-issued certs (v2)
- `/ip/ipsec/policy` — `protocol=gre`, `level=unique`, `src/dst-address=<WAN ips>/32`
- `/interface/gre` — `local-address`, `remote-address`, `keepalive`, `mtu=1400`
- `/ip/address` — tunnel /31 from the fabric pool

The alternative one-liner form (`/interface/gre ... ipsec-secret=<psk>`, which auto-generates peer+policy) is exposed as `transport_params.mode=simple`. It is fewer moving parts but gives no control over IKEv2, PFS or certs — offer it, don't default to it.

**Sites behind NAT/CGNAT with no public IP** are always the IKE *initiator* and never a responder; the driver marks them `dial_out_only`, forces `nat-traversal`, and refuses to schedule spoke-to-spoke tunnels between two such sites (they must transit a hub). This check lives in `transports/base.py` so every driver inherits it.

### `wireguard`

`/interface/wireguard` + `/interface/wireguard/peers` with `persistent-keepalive=25` for NAT'd spokes. Private keys generated controller-side, only the public key is ever stored in plaintext. **RouterOS 7 only** — `supported_ros = {7}`, so a fabric containing a v6 site rejects this driver at validation time with a clear message rather than failing at apply.

### Others

`gre`, `ipip` (unencrypted, for trusted MPLS underlays), `vxlan` and `eoip` (L2 stretch, run over an `ipsec_gre` parent link). Each is ~120 lines because the abstraction does the work.

**Selecting a transport is one dropdown on the fabric.** Switching it queues a migration job: build new tunnels, wait for BGP adjacency on the new path, shift preference, tear down old.

---

## Routing and steering

**iBGP over the overlay** distributes prefixes. Hubs are route reflectors; spokes are clients. One AS for the whole fabric (`fabric.asn`, default 65000).

```
/routing/bgp/template  name=sdwan-<fabric> as=65000 address-families=ip \
                       router-id=<loopback> output.network=sdwan-local
/routing/bgp/connection name=<peer> templates=sdwan-<fabric> \
                       remote.address=<tunnel-ip> remote.as=65000 \
                       local.role=ibgp-rr-client   # or ibgp-rr on hubs
```

Loopbacks are advertised so spoke-to-spoke tunnels can be built to a stable address. Local prefixes come from `/routing/bgp/network` fed by `Site.local_prefixes`.

**RouterOS v7's BGP route-reflector and filter support has rough edges.** Mitigation: the `softhub` container runs FRR as the route reflector, which is battle-tested, and MikroTik hubs act as plain iBGP peers to it. For MikroTik-only deployments the plan falls back to full-mesh iBGP among hubs (small n) plus RR to spokes, and the integration tests in `labs/` verify RR behavior on the target RouterOS version before it ships.

**Link health** — `/tool/netwatch` with `type=icmp`, `thr-loss-percent`, `thr-latency`, and `interval` from the SLA profile. Netwatch `up`/`down` scripts adjust route distance or toggle a BGP connection. Default 10s interval × 10 packets ≈ 10–15s detection; tunable per SLA profile with a documented load/false-positive tradeoff.

**Application steering** — `Policy` objects render to:
- `/ip/firewall/address-list` for prefix groups
- `/ip/firewall/mangle` in `prerouting` — match, then `action=mark-routing new-routing-mark=via-<wan>`
- `/routing/table` + `/ip/route` per mark, with `distance` ordering the preferred WANs
- `check-gateway=ping` plus netwatch-driven distance changes for brownout failover

Layer-7 application matching on RouterOS is weak. Ship a curated **app-group library** (Office365, Teams, Zoom, Salesforce, generic-voip…) as prefix + port + DSCP address-lists refreshed by a scheduled controller job, and be explicit in the UI that this is prefix-based, not DPI.

**Dynamic spoke-to-spoke** — a worker watches telemetry for spoke pairs whose hub-transited traffic exceeds a threshold, then builds a direct tunnel between them and lets BGP local-preference pull traffic onto it. Idle tunnels are torn down after a configurable timeout. Both endpoints must have a reachable public IP or one must be dial-out-capable; otherwise the pair stays hub-transited.

---

## Device drivers

```python
class DeviceDriver(Protocol):
    async def read(self, path: str, query: dict | None) -> list[dict]: ...
    async def apply(self, ops: list[ConfigOp]) -> ApplyResult: ...
    async def run(self, command: str) -> str: ...
    async def backup(self) -> bytes: ...
    async def capabilities(self) -> DeviceCaps: ...
```

**`ros7_rest`** — `httpx.AsyncClient`, HTTP Basic auth over HTTPS to `https://<ip>/rest/<path>`. GET to read, PUT to create, PATCH to update, DELETE to remove; POST with a JSON body for console commands and `.query` filtering. Two RouterOS quirks the driver must absorb:

1. **All JSON values are strings** — even numbers and booleans. Normalize on read (`"true"` → `True`) and on write, or every diff will show false positives. This belongs in one place: `drivers/ros7_rest.py::_coerce()`.
2. **60-second timeout** on long operations — chunk large applies and never let a single request carry the whole config.

`www-ssl` must be enabled on the device with a certificate. The onboarding wizard detects this and, for a device reachable over SSH, offers to enable it and install a controller-issued cert.

**`ros6_ssh`** — `asyncssh`, driving the CLI with `/export terse` for reads and command batches for writes. Reading state means parsing `:put [/ip/ipsec/peer print as-value]` output. Slower, no atomicity, no WireGuard, old BGP syntax (`/routing/bgp/instance` + `/routing/bgp/peer`). It gets its own template directory `templates/ros6/` and a `capabilities()` result that makes the planner refuse unsupported features up front.

**Credentials** are encrypted at rest with a Fernet key from `SDWAN_SECRET_KEY` (env or Docker secret), with an optional HashiCorp Vault backend behind the same interface. Per-device SSH keys preferred over passwords. Nothing sensitive is ever logged — the apply log stores rendered config with secrets masked.

---

## Safe apply

This is the feature that makes the tool usable on production WAN edges. Four stages:

1. **Render** — intent → `ConfigSection[]` per device, deterministic and ordered.
2. **Diff** — read the device's current state for only the paths the controller owns, normalize, and produce a three-way diff (intent vs last-known-applied vs live). Present as `+`/`-`/`~` lines in the UI. A section the controller doesn't own is never touched — every managed item carries a `comment="sdwan:<fabric>:<link-id>"` tag, and the reconciler only adds, changes or removes items bearing that tag. **This is what makes the controller safe to point at a router that already has hand-built config.**
3. **Apply with a dead-man switch** — before the first mutating op:
   ```
   /system/backup/save name=sdwan-pre-<job-id>
   /system/scheduler/add name=sdwan-rollback-<job-id> start-time=<now+Ns> \
     interval=0 on-event="/system/backup/load name=sdwan-pre-<job-id> password=\"\""
   ```
   Config is pushed, then the controller re-establishes a *fresh* connection and calls a confirm endpoint on the device. Only a successful post-apply health check (management reachable + tunnel/BGP state as expected) removes the scheduler. If the push broke management access, the scheduler fires and the router restores itself. Timeout defaults to 120s, configurable per site.

   Two failure modes to handle explicitly: `/system/backup/load` reboots the router (expected — the UI must say so), and a scheduler that fires while the controller is mid-verify must not race the confirm. The confirm step deletes the scheduler *first*, then reports.
4. **Verify + record** — post-apply health check, store the applied config as the new `last_known_applied`, write an audit row.

**Drift detection** runs on a schedule: read managed paths, compare to `last_known_applied`, and raise a drift alert with a diff. Configurable per site: `alert` (default) or `auto-remediate`.

**Ordering matters.** Applies are always: address-lists → crypto material → tunnels → addresses → routing → firewall/mangle → policies. Removals run in reverse. A single global lock per device (Redis) prevents concurrent jobs on the same router.

---

## Web UI

React + TypeScript, Vite, TanStack Query, Tailwind + shadcn/ui. "Easy to config" means the UI does the thinking, not the operator.

- **Dashboard** — fabric topology graph (react-flow), links colored by SLA state, live via WebSocket
- **Onboarding wizard** — enter IP + credentials → probe version/capabilities → detect WANs and their public IPs → suggest a role → preview config → apply. Target: a new spoke joins the fabric in under two minutes with no CLI.
- **Fabric designer** — pick transport, topology and IP pool; see the computed link matrix before anything is pushed
- **Policy builder** — match/action rows with a live "this is what will be steered" preview
- **Config diff viewer** — side-by-side, on every job, before and after
- **Jobs & audit** — every apply, who ran it, the rendered config, the result, one-click rollback
- **Device console** — read-only command runner for troubleshooting (allowlisted commands)

`docker compose up` must produce a working stack with a seeded admin and a demo fabric against the softhub. That first-run experience is a requirement, not a nicety.

---

## Verification

Testing a network controller without hardware is the hardest part of this project. Three layers:

1. **Unit** — pytest over renderers and transport drivers. Golden-file tests: intent fixture → expected RouterOS command list, per transport, per RouterOS version. These catch template regressions instantly and cost nothing to run.
2. **Integration** — a fake RouterOS REST server (`tests/fakeros/`) implementing the `/rest` semantics including the string-typed JSON quirk. Full apply/diff/rollback cycles run against it in CI.
3. **End-to-end** — `labs/` containerlab topologies running **real CHR images** (2 hubs + 3 spokes + a WAN-impairment container). CI brings the lab up, runs the controller against it, and asserts: tunnels establish, BGP converges, prefixes are exchanged, a simulated link failure fails over within the SLA window, and a deliberately broken push self-rolls-back. This is the only layer that proves the RouterOS syntax is actually correct — do not skip it.

Manual verification checklist for each milestone is in `docs/verification.md`, including a real-hardware smoke test against at least one hEX/hAP (v7) and one v6 device before any release tag.

---

## Milestones

**M1 — Foundation (weeks 1–2)**
Compose stack, FastAPI skeleton, Postgres + Alembic, auth + RBAC, `Site`/`Wan` models, `ros7_rest` driver with read + capability probe, onboarding wizard, device inventory UI.
*Done when:* you can add a real RouterOS 7 device from the browser and see its interfaces and version.

**M2 — Render and safe apply (weeks 3–4)**
`ConfigSection`/`ConfigOp` model, Jinja2 render engine, ownership tagging, three-way diff, backup + scheduler rollback, job queue, diff viewer and job log UI, fakeros test server.
*Done when:* a hand-written config section applies, diffs clean on re-run, and a deliberately broken push self-restores.

**M3 — IPsec fabric (weeks 5–7)**
`Fabric`/`Link` models and expansion, IP pool allocator, secret generation and encryption, `ipsec_gre` driver, BGP templates, hub-and-spoke topology, topology graph UI, containerlab lab + CI.
*Done when:* three CHRs in the lab form a fabric from the UI and exchange routes.

**M4 — Transport plugins (week 8)**
`wireguard`, `gre`, `ipip`, `vxlan`, `eoip` drivers; transport selection in the fabric designer; migration job; capability gating with clear validation errors.
*Done when:* the lab fabric switches IPsec → WireGuard with no manual CLI and no traffic loss beyond convergence.

**M5 — Steering and SLA (weeks 9–10)**
`Policy`/`SlaProfile`, mangle + routing-table rendering, netwatch generation, telemetry pollers, link scoring, app-group library, policy builder UI, live dashboard.
*Done when:* impairing a lab link past its SLA threshold moves matched traffic to the backup path within the configured window.

**M6 — Mesh, v6, softhub, hardening (weeks 11–13)**
Dynamic spoke-to-spoke, `ros6_ssh` driver + v6 templates, strongSwan/FRR softhub image, drift detection and auto-remediation, config export/import (YAML — the whole intent model round-trips, so the system is GitOps-able), Prometheus metrics, docs.
*Done when:* the full verification checklist passes on the lab and on real hardware.

---

## Risks

| Risk | Mitigation |
|---|---|
| RouterOS v7 BGP RR/filter gaps | FRR softhub as RR; lab-verify RR on the target version before shipping; hub full-mesh fallback |
| REST JSON string-typing causes phantom diffs | Single normalization layer + golden tests asserting a second apply is a no-op |
| Rollback reboots the router | Documented loudly in the UI; per-site timeout; opt-out for lab devices |
| v6 support doubles template surface | Strict capability gating — v6 sites get IPsec+GRE and old BGP only, and the planner refuses the rest with a clear message rather than half-working |
| CGNAT sites can't be responders | Encoded in the model (`dial_out_only`), enforced in `transports/base.py`, surfaced in the fabric designer |
| No hardware for CI | containerlab with real CHR images; hardware smoke test gate before release tags |

---

## Open items to settle during M1

- CHR licensing for the CI lab (free tier is 1Mbps — adequate for control-plane assertions, not throughput tests)
- Whether the app-group prefix library is bundled or fetched from an upstream feed
- IPv6 underlay and overlay support — deferred to post-M6 unless needed sooner
