# Tutorial

A complete walkthrough, from an empty install to a working SD-WAN fabric with
application steering. Every command here is real and every payload matches the
API as it actually is.

Work through it in order — each part builds on the last.

---

## The scenario

You run the network for a logistics company with a datacentre and two branches.

```
                        ┌─────────────────┐
                        │       dc1       │  hub · route reflector
                        │  198.51.100.5   │  10.10.0.0/16
                        └────────┬────────┘
                                 │
                ┌────────────────┴────────────────┐
                │                                 │
      ┌─────────┴─────────┐             ┌─────────┴─────────┐
      │    oslo           │             │    bergen         │
      │  fibre 203.0.113.1│  ← "mpls"   │  broadband, CGNAT │
      │  LTE   (CGNAT)    │  ← "lte"    │  dial-out only    │
      │  10.20.1.0/24     │             │  10.20.2.0/24     │
      └───────────────────┘             └───────────────────┘
```

This exercises nearly everything: a hub, a dual-homed spoke, a spoke with no
public address, uplink tags, steering, and SLA failover.

**Substitute your own addresses as you go.** The RFC 5737 documentation ranges
used here (`198.51.100.0/24`, `203.0.113.0/24`) will not route anywhere.

---

## Part 0 — Install

On Debian or Ubuntu, install Docker with the compose v2 plugin first. **Debian's
own `docker.io` package does not include it** — `docker compose` will simply not
exist:

```bash
curl -fsSL https://get.docker.com | sh
docker compose version   # must print v2.x
```

Then:

```bash
git clone https://github.com/Pherell/mikrotik-sdwan.git
cd mikrotik-sdwan
cp .env.example .env
make secrets
```

`make secrets` prints four generated values. Paste them into `.env`:

```ini
POSTGRES_PASSWORD=<generated>
SDWAN_SECRET_KEY=<generated>
SDWAN_JWT_SECRET=<generated>
SDWAN_BOOTSTRAP_ADMIN_PASSWORD=<generated>
```

> **`SDWAN_SECRET_KEY` encrypts every device credential and link key.** Changing
> it makes all of them undecryptable — there is no recovery. Back it up
> somewhere you would back up a root password, and rotate it deliberately, never
> casually.
>
> `SDWAN_JWT_SECRET` only signs API tokens. Rotating that just logs everyone out.

> **No `make` or no `python3`?** `make secrets` is only a convenience. Generate
> the four values however you like — this works on any host with openssl:
>
> ```bash
> for k in POSTGRES_PASSWORD SDWAN_SECRET_KEY SDWAN_JWT_SECRET SDWAN_BOOTSTRAP_ADMIN_PASSWORD; do
>   echo "$k=$(openssl rand -hex 32)"
> done
> ```
>
> `make doctor` reports which tools it can find.

Bring it up:

```bash
docker compose up -d --build
```

Then check it is alive:

```bash
curl -fsS http://localhost:8080/healthz
```

### Serving on an IP instead of a domain

Most installs have no DNS name pointing at the controller. `SDWAN_DOMAIN` takes
an IP directly:

```ini
SDWAN_DOMAIN=192.168.1.50
HTTPS_PORT=8443
```

Caddy cannot get a public certificate for a bare IP — no CA issues those — so it
signs one with its own internal CA. The site works at
`https://192.168.1.50:8443`, and your browser warns once until you trust
Caddy's root. Export it from the container if you want the warning gone:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

To skip TLS entirely, prefix the scheme:

```ini
SDWAN_DOMAIN=http://192.168.1.50
```

> Prefer the self-signed HTTPS over plain HTTP. This controller can decrypt the
> credentials of every router you manage. Sending its login in cleartext, even
> on a management LAN, means anyone on that LAN can take the fleet.

**You do not need to touch `SDWAN_CORS_ORIGINS`.** Caddy serves the UI and
proxies `/api/*` from the *same* origin, so the browser never makes a
cross-origin request and the setting has no effect here. It only matters if you
put a separate frontend on a different host.

Open <http://localhost:8080> and sign in with `admin@local` and the bootstrap
password. That account is seeded **only when the user table is empty** — changing
the variable later does nothing.

### The UI at a glance

| Section | What it is for |
|---|---|
| **Overview** | Fleet health, anything needing attention, bulk apply, drift sweep |
| **Sites** | Devices, their uplinks, plan/apply, drift, device console |
| **Fabrics** | Overlays: transport, topology, members, links, topology graph |
| **Policies** | Steering rules and SLA profiles |
| **Jobs** | Every apply, with its diff and log |
| **Settings** | Export/import intent, application groups |
| **Users** | Accounts and roles (admin only) |

Everything the API can do is reachable from those pages; the API examples below
exist because automation needs them, not because the UI is missing anything.

### Create real accounts

The bootstrap admin is for setup. Make per-person accounts with the least role
that works:

| Role | Can |
|---|---|
| `viewer` | Read everything, run `plan` and `export` — never changes a device |
| `operator` | Everything above, plus apply, expand, and edit policies |
| `admin` | Everything above, plus manage users and delete sites and fabrics |

```bash
TOKEN=$(curl -sX POST http://localhost:8080/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@local","password":"<bootstrap>"}' | jq -r .access_token)

curl -sX POST http://localhost:8080/api/v1/users \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"email":"nina@example.com","password":"a-long-passphrase","role":"operator"}'
```

`plan` is deliberately available to `viewer`: reading a diff is how someone
learns what a change would do without being able to make it.

> **Logins are throttled.** Five failures for the same account from the same
> address locks that combination out for five minutes, and a `429` is returned
> with `Retry-After`. The correct password is refused during the lockout too --
> otherwise it would only slow down someone who is already guessing wrong.
> Locked out of your own controller? Wait it out, or restart the API container:
> the counters are in memory and a restart forgives them.

---

## Part 1 — Prepare a RouterOS device

The controller is agentless and speaks the REST API, which needs `www-ssl`
running. On each RouterOS 7.1+ device:

```
/certificate add name=sdwan common-name=$[/system identity get name] key-size=2048 days-valid=3650
/certificate sign sdwan
/ip service set www-ssl certificate=sdwan disabled=no
/ip service set www disabled=yes
/ip service set api disabled=yes

/user add name=sdwan password=<strong> group=full address=<controller-ip>/32
```

Two things there matter:

- **`address=<controller-ip>/32`** restricts the account to the controller. Do
  not skip it — this account can rewrite the router.
- **Disable `www` and `api`.** The controller only needs `www-ssl`; the others
  are attack surface.

### How the controller decides it is talking to the right device

RouterOS ships a self-signed certificate and an unmanaged SSH host key, so
ordinary certificate validation is not available and `verify_tls` defaults to
off. On its own that would leave the connection encrypted but the far end
*unauthenticated* — anyone able to intercept traffic to the router would collect
its credentials and every pre-shared key pushed through it.

So the controller **pins** the device identity instead:

1. On first contact it records the device's TLS certificate fingerprint (or SSH
   host key, for RouterOS 6) against the site.
2. Every later connection checks it **before authenticating**. Checking after
   the handshake would mean the password had already gone to whoever answered.
3. A mismatch is refused with `409` and both values shown.

You will see the pin appear on the site after the first probe:

```bash
curl -s $API/sites/$DC1 -H "$AUTH" | jq .tls_fingerprint
# "3B:1F:...:9C"
```

> **First contact is trusted blindly.** That is inherent to trust-on-first-use
> and cannot be avoided without a CA. Onboard devices over a network you trust.

If you legitimately rebuild or re-key a device, clear the pin so it re-learns:

```bash
curl -sX PATCH $API/sites/$DC1 -H "$AUTH" -H "$JSON"   -d '{"tls_fingerprint": null}'
```

Do that only when you know why the identity changed. Turn pinning off entirely
with `SDWAN_PIN_DEVICE_IDENTITY=false` if you must, but then nothing
authenticates the device end at all.

**Better than any of this:** sign your device certificates with your own CA, set
`"verify_tls": true` on the site, and mount the CA into the API container. Then
it is real validation rather than pinning.

---

## Part 2 — Add your first site

### Through the UI

**Sites → Add site.** Fill in name, management address, and credentials, then
**Create and probe**.

Probing is strictly read-only. It reports the RouterOS version, board,
architecture, and whether WireGuard and netwatch thresholds are available — then
proposes uplinks it found by looking at default routes and DHCP clients.

Tick the ones that are real, untick the rest, and **Add uplinks**.

### Through the API

```bash
API=http://localhost:8080/api/v1
AUTH="Authorization: Bearer $TOKEN"
JSON='Content-Type: application/json'

DC1=$(curl -sX POST $API/sites -H "$AUTH" -H "$JSON" -d '{
  "name": "dc1",
  "description": "Primary datacentre",
  "region": "eu-north",
  "role": "hub",
  "mgmt_host": "198.51.100.5",
  "username": "sdwan",
  "password": "<device password>",
  "loopback_ip": "10.254.0.1",
  "local_prefixes": ["10.10.0.0/16"],
  "wans": [
    {"name": "wan1", "interface": "ether1", "public_ip": "198.51.100.5",
     "cost": 1, "provider": "Telenor", "tags": {"mpls": "yes"}}
  ]
}' | jq -r .id)

curl -sX POST $API/sites/$DC1/probe -H "$AUTH" | jq
```

The probe response tells you what the device can do:

```json
{
  "reachable": true,
  "version": "7.14.3 (stable)",
  "board_name": "CCR2004-1G-12S+2XS",
  "ros_major": 7,
  "has_wireguard": true,
  "has_netwatch_thresholds": true,
  "suggested_wans": [
    {"name": "wan1", "interface": "ether1", "public_ip": "198.51.100.5",
     "dynamic": false, "nat_behind": false}
  ]
}
```

### Uplink fields that change behaviour

| Field | Effect |
|---|---|
| `public_ip` | `null` means the uplink cannot accept an inbound tunnel |
| `nat_behind` | Same effect — forces this end to be the IKE initiator |
| `cost` | Tie-break between uplinks carrying the same tag; lower wins |
| `tags` | What policies name when they say "prefer this kind of link" |
| `enabled` | `false` excludes it from the fabric without deleting it |

**`public_ip: null` or `nat_behind: true` makes a WAN dial-out only.** Two
dial-out-only endpoints can never form a tunnel with each other — neither can
accept one — so the controller refuses that pair and tells you to go via a hub.
This is checked before anything is rendered, so you get a message in the UI
rather than two routers retrying an impossible negotiation forever.

---

## Part 3 — Plan and apply

**Always plan first.** It renders intent, reads the device, and shows a diff. It
writes nothing.

```bash
curl -sX POST $API/sites/$DC1/plan -H "$AUTH" | jq -r .text
```

```
+ /ip/firewall/address-list sdwan-local-dc1,10.10.0.0/16  list=sdwan-local-dc1 …
+ /interface/bridge lo-sdwan  name=lo-sdwan protocol-mode=none
+ /ip/address 10.254.0.1/32  address=10.254.0.1/32 interface=lo-sdwan
```

Read the markers as `+` add, `-` remove, `~` change, `!` a menu that could not be
read.

Then apply:

```bash
curl -sX POST $API/sites/$DC1/apply -H "$AUTH" -H "$JSON" \
  -d '{"confirm": true}' | jq '{state, result, backup_name}'
```

`confirm: true` is mandatory, and here is why.

### What safe apply does

1. **Back up** the device.
2. **Arm** a `/system/scheduler` entry that restores that backup after N seconds
   (120 by default, `rollback_timeout_seconds` per site). The backup was taken
   *before* the scheduler existed, so restoring it also removes the scheduler.
3. **Push** the configuration.
4. **Verify** on a brand-new connection. An already-open socket proves nothing
   about a firewall rule that just changed.
5. **Disarm** — only after verification succeeds.

If step 3 costs the controller its management access, step 5 never happens, the
scheduler fires, and **the router restores its previous configuration and
reboots**. That reboot is why `confirm` exists.

Use `{"dry_run": true}` to record a plan as a job without pushing anything.

### Applying twice does nothing

```bash
curl -sX POST $API/sites/$DC1/apply -H "$AUTH" -H "$JSON" -d '{"confirm":true}' \
  | jq '.result.applied, .result.message'
# 0
# "no changes"
```

That is the property the whole diff layer exists to guarantee. If a second apply
ever reports changes, something is wrong — open an issue with the diff.

### Config you wrote by hand is never touched

Every row the controller manages carries a `comment` starting with `sdwan:`. The
reconciler only ever adds, changes, or removes rows with that prefix. Your
existing bridges, address lists, and firewall rules are invisible to it.

You can verify this on a real device: `/export` before and after an apply, and
diff. The only new lines will carry `sdwan:` comments.

### If something goes wrong

A push that leaves the rollback armed reports:

```json
{
  "state": "rolled_back",
  "error": "Device stopped answering after the push. The rollback is armed: the
            router will restore sdwan-pre-job-a1b2 in about 120s and reboot."
}
```

If the *controller* crashed between arming and disarming, a scheduler is left
behind that will restore a perfectly good configuration. Find and clear it:

```bash
curl -s $API/sites/$DC1/rollbacks -H "$AUTH" | jq
curl -sX DELETE $API/sites/$DC1/rollbacks/sdwan-rollback-job-a1b2 -H "$AUTH"
```

That endpoint refuses anything not named `sdwan-rollback-*`, so it can never
become a generic "delete any scheduler."

---

## Part 4 — Add the remaining sites

**Oslo — dual-homed, one uplink on CGNAT:**

```bash
OSLO=$(curl -sX POST $API/sites -H "$AUTH" -H "$JSON" -d '{
  "name": "oslo",
  "role": "spoke",
  "mgmt_host": "203.0.113.1",
  "username": "sdwan",
  "password": "<device password>",
  "loopback_ip": "10.254.0.2",
  "local_prefixes": ["10.20.1.0/24"],
  "wans": [
    {"name": "fibre", "interface": "ether1", "public_ip": "203.0.113.1",
     "cost": 1, "bandwidth_mbps": 500, "tags": {"mpls": "yes"}},
    {"name": "lte", "interface": "lte1", "public_ip": null, "nat_behind": true,
     "dynamic": true, "cost": 10, "bandwidth_mbps": 50, "tags": {"lte": "yes"}}
  ]
}' | jq -r .id)
```

**Bergen — single uplink, entirely behind CGNAT:**

```bash
BERGEN=$(curl -sX POST $API/sites -H "$AUTH" -H "$JSON" -d '{
  "name": "bergen",
  "role": "spoke",
  "mgmt_host": "203.0.113.2",
  "username": "sdwan",
  "password": "<device password>",
  "loopback_ip": "10.254.0.3",
  "local_prefixes": ["10.20.2.0/24"],
  "wans": [
    {"name": "broadband", "interface": "ether1", "public_ip": null,
     "nat_behind": true, "dynamic": true, "cost": 3, "tags": {"broadband": "yes"}}
  ]
}' | jq -r .id)
```

Both of bergen's and oslo's NAT'd uplinks now show `dial_out_only: true` in the
UI. That is the controller telling you they can only initiate.

---

## Part 5 — Build the fabric

A **fabric** is one overlay: a transport, a topology, an address pool, and the
sites that take part.

```bash
FABRIC=$(curl -sX POST $API/fabrics -H "$AUTH" -H "$JSON" -d "{
  \"name\": \"core\",
  \"transport\": \"ipsec_gre\",
  \"topology\": \"hub_spoke_dynamic\",
  \"ip_pool\": \"10.255.0.0/16\",
  \"loopback_pool\": \"10.254.0.0/24\",
  \"asn\": 65000,
  \"mtu\": 1400,
  \"member_site_ids\": [\"$DC1\", \"$OSLO\", \"$BERGEN\"]
}" | jq -r .id)
```

### Choosing a topology

| Topology | Builds | Use when |
|---|---|---|
| `hub_spoke` | Hub↔hub, hub↔spoke | Simplest; all spoke traffic transits a hub |
| `hub_spoke_dynamic` | Same, plus spoke↔spoke on demand | **Default.** Direct paths appear when traffic justifies them |
| `full_mesh` | Everything to everything | Small fabrics where latency matters everywhere |

### Choosing a transport

```bash
curl -s $API/fabrics/transports -H "$AUTH" | jq
```

| Transport | RouterOS | Encrypted | Notes |
|---|---|---|---|
| `ipsec_gre` | 6, 7 | Yes | **Default.** IKEv2 + GRE. Works everywhere |
| `wireguard` | 7 only | Yes | Faster, simpler keys, lower CPU |
| `gre` | 6, 7 | **No** | For an already-private underlay (MPLS L3VPN) |
| `ipip` | 6, 7 | **No** | Same, lower overhead, IPv4 payloads only |
| `vxlan` | 7 only | No (rides a parent) | L2 stretch |
| `eoip` | 6, 7 | No (rides a parent) | L2 stretch, MikroTik-proprietary |

`vxlan` and `eoip` extend one broadcast domain across sites. Occasionally
necessary, always a liability — a broadcast storm at one site becomes a storm
everywhere. They bind to the overlay address, so their payload inherits the
parent tunnel's IPsec SA rather than crossing the internet in the clear.

### Tuning the crypto

`transport_params` overrides the defaults per fabric:

```bash
curl -sX PATCH $API/fabrics/$FABRIC -H "$AUTH" -H "$JSON" -d '{
  "transport_params": {
    "enc_algorithm": "aes-256-cbc",
    "auth_algorithm": "sha256",
    "dh_group": "modp2048",
    "pfs_group": "modp2048",
    "lifetime": "4h"
  }
}'
```

Defaults are `aes-256-gcm` / `ecp256`, which suits modern hardware. Older boards
without AES-NI do better on CBC.

### Expand the topology into links

```bash
curl -sX POST $API/fabrics/$FABRIC/expand -H "$AUTH" | jq
```

```json
{"created": 3, "kept": 0, "removed": 0, "skipped": 0, "problems": [],
 "affected_site_ids": ["…"]}
```

Three links: dc1↔oslo-fibre, dc1↔oslo-lte, dc1↔bergen. **No oslo↔bergen** — this
is `hub_spoke_dynamic`, so that one appears only when traffic justifies it.

Expansion allocated a /31 per link from the pool, generated and encrypted a PSK
per link, and decided which end dials.

```bash
curl -s $API/fabrics/$FABRIC/links -H "$AUTH" | jq -r \
  '.[] | "\(.slug)  \(.subnet)  dials=\(.initiator)  keys=\(.has_secrets)"'
```

Expansion is **idempotent** — running it again reports `created: 0, kept: 3`. It
never renumbers a live overlay.

> **Changing `ip_pool` after links exist is refused.** Renumbering drops every
> tunnel on the fabric. Delete the links first if you really mean it.

### Push it

Nothing is on the devices yet. Apply each member:

```bash
for SITE in $DC1 $OSLO $BERGEN; do
  curl -sX POST $API/sites/$SITE/apply -H "$AUTH" -H "$JSON" \
    -d '{"confirm": true}' | jq -r '"\(.state)  \(.result.applied) changes"'
done
```

Then confirm convergence — every site should re-plan empty:

```bash
for SITE in $DC1 $OSLO $BERGEN; do
  curl -sX POST $API/sites/$SITE/plan -H "$AUTH" | jq -r .empty
done
# true true true
```

---

## Part 6 — Verify the overlay

The controller exposes a **read-only** device passthrough for troubleshooting.
It is allowlisted and strips secret properties, so it can never be used to read
`/user` or leak a PSK.

```bash
# IPsec security associations — should show one per tunnel
curl -s $API/sites/$DC1/device/ip/ipsec/active-peers -H "$AUTH" | jq -r \
  '.[] | "\(.["remote-address"])  state=\(.state)  uptime=\(.uptime)"'

# BGP sessions
curl -s $API/sites/$DC1/device/routing/bgp/session -H "$AUTH" | jq -r \
  '.[] | "\(.["remote.address"])  established=\(.established)"'

# Did the spoke learn the other prefixes?
curl -s $API/sites/$OSLO/device/ip/route -H "$AUTH" | jq -r \
  '.[] | select(.["dst-address"] | startswith("10.")) |
   "\(.["dst-address"]) via \(.gateway)"'
```

Oslo should show `10.10.0.0/16` (from dc1) and `10.20.2.0/24` (from bergen,
reflected by the hub) over the tunnel.

### The BGP design

Hubs are route reflectors (`local.role=ibgp-rr`), spokes are clients
(`ibgp-rr-client`), all in one AS. Spokes have no session with each other, so
the reflector is what makes spoke-to-spoke routing work at all. Loopbacks are
advertised so a direct spoke-to-spoke tunnel can later be built to an address
that does not move when an uplink flaps.

---

## Part 7 — Application steering

This is where SD-WAN stops being "a VPN with automation."

### Define an SLA profile

```bash
VOICE_SLA=$(curl -sX POST $API/sla-profiles -H "$AUTH" -H "$JSON" -d '{
  "name": "voice",
  "description": "Tolerances for real-time audio",
  "loss_percent": 2,
  "latency_ms": 150,
  "jitter_ms": 30,
  "probe_interval_seconds": 5,
  "probe_count": 10,
  "recovery_seconds": 60
}' | jq -r .id)
```

Detection takes roughly `probe_interval × 2`, so this notices a breach in about
10 seconds. Tightening the interval speeds that up and costs router CPU — and
below a second or two you start failing over on ordinary jitter.

### Define what the traffic is

RouterOS has no usable application classifier, so an "app group" is a prefix and
port list, not DPI. The docs say so plainly because it matters: traffic that
moves to a new range is missed until the list is updated.

```bash
TEAMS=$(curl -sX POST $API/app-groups -H "$AUTH" -H "$JSON" -d '{
  "name": "teams-media",
  "description": "Microsoft Teams real-time media (prefix-based, not DPI)",
  "prefixes": ["52.112.0.0/14", "52.122.0.0/15"],
  "ports": [3478, 3479, 3480, 3481],
  "protocol": "udp",
  "dscp": 46
}' | jq -r .id)
```

### Write the policy

```bash
curl -sX POST $API/policies -H "$AUTH" -H "$JSON" -d "{
  \"name\": \"voice\",
  \"description\": \"Teams media prefers fibre, falls back to LTE\",
  \"priority\": 10,
  \"app_group_id\": \"$TEAMS\",
  \"prefer_tags\": [\"mpls\", \"lte\"],
  \"sla_profile_id\": \"$VOICE_SLA\",
  \"fallback\": \"any\"
}"
```

And a lower-priority rule pushing bulk traffic the other way:

```bash
curl -sX POST $API/policies -H "$AUTH" -H "$JSON" -d '{
  "name": "backups",
  "priority": 900,
  "dst_prefixes": ["10.10.50.0/24"],
  "protocol": "tcp",
  "dst_ports": "873,22",
  "prefer_tags": ["broadband", "lte"],
  "fallback": "any"
}'
```

Apply the affected sites, then look at what landed:

```bash
curl -sX POST $API/sites/$OSLO/apply -H "$AUTH" -H "$JSON" -d '{"confirm":true}'
curl -s $API/sites/$OSLO/device/ip/route -H "$AUTH" | jq -r \
  '.[] | select(.["routing-table"] // "" | startswith("sdwan-")) |
   "\(.["routing-table"])  via \(.gateway)  distance=\(.distance)"'
```

```
sdwan-voice  via 10.255.0.1  distance=1     ← fibre  ("mpls")
sdwan-voice  via 10.255.0.3  distance=2     ← LTE
sdwan-voice  via main        distance=250   ← fallback
```

### How a packet gets steered

```
packet → /ip/firewall/mangle prerouting
           evaluated top→bottom by policy priority; first match wins
           → action=mark-routing new-routing-mark=sdwan-voice
       → routing lookup uses table "sdwan-voice" instead of main
       → lowest active distance wins
```

Next hops are **tunnel addresses**, never WAN gateways. Steering points at the
overlay so policy traffic stays encrypted.

### How failover actually happens

Four mechanisms at different timescales, catching different failures:

| Mechanism | Fires after | Catches |
|---|---|---|
| `check-gateway=ping` | ~1–4 s | Next hop stops answering |
| **netwatch + SLA** | ~10–15 s | Path is **up but degraded** |
| GRE keepalive `10s,3` | ~30 s | Far end silent → interface down |
| BGP hold-time `30s` | ~30 s | Session drops → prefixes withdrawn |

Only netwatch catches the brownout. A link with 30% loss and 400 ms latency is
*up* — ICMP replies come back, BGP stays established, the interface says
running. Every conventional failover mechanism is blind to it.

When netwatch fires it does **not** delete the route. It runs:

```
:foreach r in=[/ip/route/find gateway="10.255.0.1"] do={/ip/route/set $r distance=101}
```

Distance 101 sits below the LTE backup at 2, but above the fallback at 250. So
traffic moves to LTE, and a fully degraded site still forwards rather than
blackholing. When the path recovers, the up-script sets it back to `distance=1`
and traffic returns on its own.

### Policy gotchas

- **A policy naming no uplink present at a site is skipped there**, not pushed.
  Marking traffic into an empty routing table would blackhole it.
- **`prefer_tags` must be non-empty.** The API rejects a policy with none.
- **Order is the semantics.** Mangle is positional; `priority` decides it.
- **`fallback: "drop"`** means exactly that — no escape route. Use deliberately.

---

## Part 8 — Switch transport with no CLI

Say you have moved to RouterOS 7 everywhere and want WireGuard's lower CPU cost.

```bash
curl -sX PATCH $API/fabrics/$FABRIC -H "$AUTH" -H "$JSON" \
  -d '{"transport": "wireguard"}'
```

This re-keys every link — WireGuard cannot use an IPsec PSK, so each link gets a
fresh Curve25519 keypair and a preshared key. If any member cannot run the new
transport the whole switch is refused, with the offending sites named:

```json
{"detail": "bergen cannot run the wireguard transport (needs RouterOS [7]).
            Probe those sites, or move them to their own fabric."}
```

Then re-apply each member. The old IPsec/GRE stack is swept off the devices and
WireGuard replaces it; BGP keeps pointing at the same overlay addresses, so
routing does not move.

```bash
for SITE in $DC1 $OSLO $BERGEN; do
  curl -sX POST $API/sites/$SITE/apply -H "$AUTH" -H "$JSON" -d '{"confirm":true}'
done
```

---

## Part 9 — Drift detection

A device can stop matching intent without the controller doing anything: someone
logs in and edits, a reboot loses an unsaved change, a firmware upgrade rewrites
a menu.

```bash
curl -sX POST $API/sites/$OSLO/drift -H "$AUTH" | jq '{state, result, diff}'
```

```json
{
  "result": {"drifted": true, "changes": {"add": 0, "set": 1, "remove": 0},
             "action": "alert"},
  "diff": {"text": "~ /interface/bridge lo-sdwan  protocol-mode: rstp -> none"}
}
```

Two modes, set per site:

```bash
curl -sX PATCH $API/sites/$OSLO -H "$AUTH" -H "$JSON" \
  -d '{"drift_action": "auto-remediate"}'
```

- **`alert`** (default) — flag it, change nothing, let a human decide.
- **`auto-remediate`** — re-apply immediately.

Choose carefully. `auto-remediate` is right for a fleet nobody logs into by
hand, and actively hostile on one where engineers do — it silently reverts their
work mid-troubleshooting.

Configuration you wrote by hand is **never** reported as drift; only
`sdwan:`-tagged rows are compared.

Sweep everything (the worker also does this hourly at :17):

```bash
curl -sX POST $API/drift -H "$AUTH" | jq -r \
  '.[] | "\(.site_id)  drifted=\(.result.drifted)"'
```

---

## Part 10 — GitOps

The whole authored intent round-trips through YAML.

```bash
curl -s $API/intent/export -H "$AUTH" -o sdwan-intent.yaml
git add sdwan-intent.yaml && git commit -m "SD-WAN: add bergen"
```

**Credentials are excluded on purpose.** Device passwords and link keys are
environment-specific and do not belong in a file people put in git. Links are
excluded too — they are derived from topology and members, so exporting them
would let a file and its own fabric disagree.

Import is a **dry run by default**:

```bash
curl -sX POST $API/intent/import -H "$AUTH" \
  --data-binary @sdwan-intent.yaml | jq
```

```json
{"dry_run": true, "sites": 3, "fabrics": 1, "policies": 2,
 "created": ["site/bergen"], "warnings": ["site/bergen has no credentials…"]}
```

Commit it for real with `?dry_run=false`. **Nothing is ever deleted** — a
partial document is the normal case, and treating omissions as deletions would
make sharing one fabric destructive.

After importing, set credentials on the new sites and re-expand each fabric to
rebuild its links.

---

## Part 11 — Monitoring

```bash
curl -s http://localhost:8080/api/v1/metrics
```

```
sdwan_sites{status="reachable"} 3
sdwan_sites_drifted 0
sdwan_rollbacks_armed 0
sdwan_links 3
```

The two worth alerting on:

- **`sdwan_rollbacks_armed > 0`** — a device is about to restore itself and
  reboot. Page someone.
- **`sdwan_sites_drifted > 0`** — someone changed a device out of band.

The endpoint is unauthenticated by design and carries counts and states only —
no names, no addresses.

Job history is the audit trail:

```bash
curl -s "$API/jobs?site_id=$OSLO&limit=10" -H "$AUTH" | jq -r \
  '.[] | "\(.created_at)  \(.kind)  \(.state)  \(.result.applied // 0) changes"'
```

---

## RouterOS 6 sites

v6 has no REST API, so those devices are driven over SSH.

```bash
curl -sX POST $API/sites -H "$AUTH" -H "$JSON" -d '{
  "name": "legacy-branch",
  "role": "spoke",
  "device_kind": "ros6",
  "mgmt_host": "203.0.113.50",
  "mgmt_port": 22,
  "username": "sdwan",
  "password": "<device password>",
  "local_prefixes": ["10.20.9.0/24"]
}'
```

What you give up:

- **No WireGuard and no VXLAN.** The planner refuses those fabrics up front with
  a clear message rather than failing halfway through an apply.
- **No atomicity.** Commands go one at a time; a failure partway leaves the
  earlier ones applied. The dead-man rollback is the only real safety net.
- **Slower.** Reads parse console output instead of JSON.

> **Known weakness:** SSH host keys are not currently verified
> (`known_hosts=None`). Anyone who can intercept traffic to a v6 device sees the
> credentials. Treat v6 management as trusted-network-only until that is fixed.

---

## Troubleshooting

**"Refusing to apply: could not read /ip/ipsec/peer"**
The menu is missing or the package is not installed. The controller will not
guess an empty read for a menu it intends to write, because that diffs as
"delete everything managed in it." Check the device has the `security` package.

**A second apply keeps showing changes**
That is a bug — the diff should converge. Capture `plan` output and open an
issue. It usually means a property RouterOS normalises on write (a timeout
format, a case change) needs adding to a section's `ignore` list.

**Tunnels do not come up**
Walk down the stack:
```bash
curl -s $API/sites/$X/device/ip/ipsec/active-peers -H "$AUTH"   # phase 1/2?
curl -s $API/sites/$X/device/interface/gre -H "$AUTH"           # running?
curl -s $API/sites/$X/device/routing/bgp/session -H "$AUTH"     # established?
```
If IPsec is up but GRE is down, check the underlay actually passes protocol 47.
If GRE is up but BGP is not, check both ends hold the /31.

**"Neither … is publicly reachable"**
Both endpoints are `dial_out_only`. They must transit a hub. This is a fact
about NAT, not a limitation of the controller.

**`docker compose build` fails: "unable to apply apparmor profile"**

```
runc run failed: unable to start container process: error during container init:
unable to apply apparmor profile: apparmor failed to apply profile:
write fsmount:fscontext:proc/thread-self/attr/apparmor/exec: no such file or directory
```

Not a problem with this project — no container process can start on that host at
all, so whichever `RUN` step happens to come first is the one that reports it.

Almost always **Docker inside an unprivileged LXC container**, Proxmox in
particular. Confirm with `systemd-detect-virt` (prints `lxc`) and a `-pve`
kernel. runc 1.3 mounts a fresh procfs through the new mount API and then writes
the AppArmor label into it; inside unprivileged LXC that new mount does not
expose the apparmor attributes, even though the container's inherited `/proc`
does. Nothing you change inside the container fixes it.

Fix on the **Proxmox host**, not in the container:

```bash
pct stop <vmid>
pct set <vmid> --features nesting=1,keyctl=1
echo "lxc.apparmor.profile: unconfined" >> /etc/pve/lxc/<vmid>.conf
pct start <vmid>
```

> `lxc.apparmor.profile: unconfined` removes AppArmor confinement **of the LXC
> container itself**, weakening its isolation from the Proxmox host. That is a
> real cost, and this controller can decrypt the credentials of every router you
> manage. If the choice is available, run it in a VM rather than an LXC
> container — Docker-in-LXC is a long-standing awkward combination and this is
> not the last edge you will hit.

To confirm the cause before touching the host, `systemctl stop apparmor &&
systemctl restart docker` inside the container will let the build through. That
is a diagnostic, not a fix — it drops confinement for everything on the box.

**`409` — "the TLS certificate does not match the one pinned for this site"**

Either the device was rebuilt or re-keyed, or something is intercepting traffic
to it. The message shows both fingerprints. If you know why it changed, clear
the pin (`{"tls_fingerprint": null}` for RouterOS 7, `{"ssh_host_key": null}`
for RouterOS 6) and connect again. If you do not know why it changed, treat that
device's credentials as compromised and rotate them.

**`429` — "Too many failed attempts"**

Five failed logins for that account from that address. Wait for `Retry-After`,
or restart the API container — the counters are in memory.

**A device rebooted after an apply**
The rollback fired: the controller could not reach it after the push. The device
is on its pre-apply configuration. Check the job's diff for what would have
broken management access — usually a firewall or `/ip service` change.

---

## API reference

All paths are prefixed `/api/v1`. Roles are the minimum required.

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/login` | — | Obtain a token |
| GET | `/auth/me` | any | Current user |
| GET/POST | `/users` | admin | Manage users |
| PATCH | `/users/{id}` | admin | Change role, password, active |
| GET/POST | `/sites` | viewer/operator | List, create |
| GET/PATCH | `/sites/{id}` | viewer/operator | Read, update |
| DELETE | `/sites/{id}` | admin | Delete (refused while in a fabric) |
| POST | `/sites/{id}/probe` | operator | Read-only capability probe |
| POST | `/sites/{id}/wans` | operator | Add an uplink |
| PATCH/DELETE | `/sites/{id}/wans/{wan}` | operator | Edit, remove |
| POST | `/sites/{id}/plan` | **viewer** | Diff intent vs device, writes nothing |
| POST | `/sites/{id}/apply` | operator | Push inside the rollback |
| POST | `/sites/{id}/drift` | operator | Drift check |
| GET | `/sites/{id}/rollbacks` | viewer | Armed rollbacks on the device |
| DELETE | `/sites/{id}/rollbacks/{name}` | operator | Disarm a stale one |
| GET | `/sites/{id}/device/{path}` | operator | Read-only allowlisted passthrough |
| GET | `/fabrics/transports` | viewer | What this build can render |
| GET/POST | `/fabrics` | viewer/operator | List, create |
| GET/PATCH | `/fabrics/{id}` | viewer/operator | Read, update, switch transport |
| DELETE | `/fabrics/{id}` | admin | Delete |
| POST | `/fabrics/{id}/members` | operator | Add a site |
| DELETE | `/fabrics/{id}/members/{site}` | operator | Remove a site |
| POST | `/fabrics/{id}/expand` | operator | Recompute links |
| GET | `/fabrics/{id}/links` | viewer | List links (never secrets) |
| GET/POST | `/policies` | viewer/operator | List, create |
| PATCH/DELETE | `/policies/{id}` | operator | Edit, delete |
| GET/POST | `/sla-profiles` | viewer/operator | List, create |
| DELETE | `/sla-profiles/{id}` | operator | Delete (refused while in use) |
| GET/POST | `/app-groups` | viewer/operator | List, create |
| POST | `/drift` | operator | Sweep every site |
| GET | `/intent/export` | viewer | YAML export |
| POST | `/intent/import` | admin | YAML import (`?dry_run=false` to commit) |
| GET | `/jobs` | viewer | Job history |
| GET | `/jobs/{id}` | viewer | One job with diff and log |
| GET | `/metrics` | — | Prometheus |

Interactive docs are at <http://localhost:8080/docs>.

---

## Before production

Read [verification.md](verification.md) for what still needs real hardware, and
[architecture.md](architecture.md) for the RouterOS behaviours the design works
around.

The single most important thing to rehearse in the lab before trusting this on a
production edge: **make a push that deliberately breaks management access**, and
watch the router restore itself. `labs/README.md` has the recipe. If that does
not work on your hardware, nothing else here is safe.
