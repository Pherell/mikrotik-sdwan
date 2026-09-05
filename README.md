# MikroTik SD-WAN Controller

An intent-based SD-WAN control plane for MikroTik RouterOS, shipped as a Docker
Compose stack with a web UI.

You describe sites, uplinks, and policies once in a declarative model. The
controller renders per-device RouterOS configuration, shows you a diff, applies
it inside a dead-man rollback, then keeps checking that the device still matches
intent.

**The control plane is out of band.** It never sits in the data path. If the
controller is down, the fabric keeps forwarding — BGP and netwatch on the routers
handle convergence.

> Status: **M1–M6 feature-complete**, verified by 294 automated tests that need
> no hardware. What remains is the hardware and containerlab verification marked
> in [docs/verification.md](docs/verification.md) — the only layer that proves
> the rendered RouterOS syntax actually establishes. See [the plan](docs/plan.md).

---

## What works today

**Fabrics.** Declare a transport, a topology and some members; the controller
works out every tunnel, allocates a /31 and a loopback for each, generates and
encrypts the keys, and renders both ends consistently. Hubs become BGP route
reflectors, spokes their clients.

**Six overlays, one dropdown.** `ipsec_gre` (default), `wireguard`, `gre`,
`ipip`, `vxlan`, `eoip`. Switching a fabric's transport re-keys every link,
sweeps the old stack off the devices, and converges — no CLI.

**Steering.** Policies match on prefix, port and DSCP, then prefer uplinks in
order. An SLA profile's thresholds become netwatch probes whose scripts demote a
degraded path instead of tearing it down, so it recovers on its own.

**The UI.** Seven sections — Overview, Sites, Fabrics, Policies, Jobs, Settings,
Users. The overview is the front door: fleet health at a glance, anything needing
attention, bulk apply across every site, and a one-click drift sweep. Everything
the API can do is reachable from the browser.

**Everything else:**

- Add a RouterOS 7 device from the browser, probe it read-only, and see its
  version, board, capabilities, and discovered uplinks.
- Uplink discovery flags NAT'd and CGNAT sites as **dial-out only**, so the
  fabric planner will never try to make them tunnel responders.
- **Plan** a site: render intent, diff it against the live device, see a
  `+`/`-`/`~` changelist. Nothing is written.
- **Apply** it inside a dead-man rollback. If the push costs the controller
  management access, the router restores its pre-apply backup by itself.
- Re-applying is a no-op. The controller converges and then stops.
- Configuration a human wrote on the same device is never touched.
- Orphaned rollbacks (from a controller that crashed mid-apply) can be listed
  and cleared before they fire.
- Dynamic spoke-to-spoke tunnels that build on traffic and tear down on idle,
  with a minimum lifetime so a pair near the threshold cannot flap.
- Drift detection on a schedule: alert by default, or auto-remediate.
- The whole intent model exports to YAML and imports back, so it can live in git.
  Credentials are excluded on purpose.
- Prometheus metrics carrying counts and states — never names or addresses.
- RouterOS 6 sites over SSH, with capability gating so an unsupported feature is
  refused at validation rather than halfway through an apply.
- Device credentials are encrypted at rest and never returned by the API.
- RBAC: `viewer` reads and plans, `operator` applies, `admin` manages users.
- Every state-changing call writes an audit row; every apply writes a job record
  holding the diff, the log, and the outcome.

## Documentation

| | |
|---|---|
| **[Tutorial](docs/tutorial.md)** | Start here. Empty install → working fabric with app steering, worked end to end |
| [Architecture](docs/architecture.md) | The RouterOS behaviours the design works around, and the bugs that shaped it |
| [Verification](docs/verification.md) | What is tested, and what still needs real hardware |
| [Security](SECURITY.md) | Threat model, what is enforced, and the known limitations |
| [Plan](docs/plan.md) | The original roadmap, M1–M6 |
| [Lab](labs/README.md) | containerlab topology and the fabric verifier |

### Prerequisites on Debian / Ubuntu

Only Docker is needed to run the stack.

```bash
# Docker Engine + the compose v2 plugin.
# Debian's own docker.io package does NOT include compose v2 -- if you install
# that, `docker compose` will not exist and only the legacy `docker-compose`
# binary might. Use Docker's repository instead:
curl -fsSL https://get.docker.com | sh
docker compose version   # must print v2.x
```

For local development (`make install`, `make test`) you also need Python 3.11 or
newer. Debian splits the venv module into its own package:

```bash
sudo apt install python3 python3-venv build-essential
```

| Release | Default `python3` | Works? |
|---|---|---|
| Debian 12 bookworm | 3.11 | Yes |
| Debian 13 trixie | 3.13 | Yes |
| Ubuntu 22.04 | 3.10 | No — needs a newer Python |
| Ubuntu 24.04 | 3.12 | Yes |

`make doctor` reports what it can actually find on your host.

## Quick start

```bash
cp .env.example .env
make secrets      # paste the output into .env
docker compose up -d --build
```

`make secrets` needs `python3` or `openssl`; `make doctor` reports what it can
find. Only Docker is required for the stack itself — the venv targets are for
local development.

Open <http://localhost:8080> and sign in with `SDWAN_BOOTSTRAP_ADMIN_EMAIL` and
`SDWAN_BOOTSTRAP_ADMIN_PASSWORD`. The bootstrap admin is seeded only when the
user table is empty.

### Preparing a RouterOS device

The controller is agentless and speaks the REST API, which needs the `www-ssl`
service running:

```
/ip service set www-ssl disabled=no certificate=<your-cert>
/user add name=sdwan password=<strong> group=full
```

Restricting that user with `address=<controller-ip>` is strongly recommended.
RouterOS 7.1 or newer is required for `/rest`; RouterOS 6 support lands in M6
over SSH.

## Development

```bash
make install      # backend venv + npm install
make test         # 294 tests, no hardware needed
make lint
make api-dev      # uvicorn on :8000
make ui-dev       # vite on :5173, proxying /api to :8000
```

The test suite runs against `tests/fakeros/`, an in-process fake RouterOS REST
endpoint that reproduces the behaviours that actually bite — notably that
**RouterOS encodes every JSON value as a string**, booleans included. No device
is required.

## Architecture

```
Browser ──► Caddy ──► React SPA
              │
              └────► FastAPI ──► Postgres   (intent, state, audit)
                        │    └─► Redis      (queue, locks)
                        ▼
                    ARQ workers ──┬─► RouterOS REST (v7)
                                  └─► SSH           (v6, M6)
```

Two abstractions carry the versatility:

| Seam | File | Purpose |
|---|---|---|
| `DeviceDriver` | `backend/app/drivers/base.py` | How to reach a device: REST (v7), SSH (v6), softhub |
| `TransportDriver` | `backend/app/transports/base.py` | Which overlay: IPsec/GRE, WireGuard, GRE, IPIP, VXLAN, EoIP |

Adding an overlay technology is a driver, not a fork. Selecting one is a dropdown
on the fabric.

### Why coercion has its own module

RouterOS returns `"1400"` and `"true"`, not `1400` and `true`. Compare intent
against that naively and the reconciler pushes the same config forever.
`backend/app/drivers/coerce.py` is the only place allowed to reason about
RouterOS value encoding: `coerce()` for reading, `canonical()` for diffing and
writing. The differ compares `canonical(intent)` to `canonical(device)`, so the
two sides can never disagree about representation alone.

### Why the controller is safe to point at an existing router

Every row the controller manages carries a `comment="sdwan:<scope>:<name>"`
ownership tag. The reconciler only ever adds, changes, or removes rows bearing
that tag. Hand-built configuration on the same device is invisible to it.

A menu that cannot be read is never treated as empty — that would diff as
"delete everything managed in it" — so apply refuses until the read succeeds.

### How the dead-man rollback works

1. Back up the device.
2. Add a `/system/scheduler` entry that restores that backup after N seconds.
   The backup was taken *before* the scheduler existed, so restoring it also
   removes the scheduler — there is no second firing to clean up.
3. Push the configuration.
4. Reconnect on a **fresh** connection and read `/system/resource`. An
   established socket proves nothing about a rule that just changed.
5. Only then disarm.

If step 3 breaks management access, step 5 never happens and the router restores
itself. Restoring a backup reboots the device, which is why apply requires an
explicit `confirm` and the UI says so next to the button.

### Layers of the config pipeline

```
Site + Fabric + Policy ──► render/{site,fabric,policy}.py ──► [ConfigSection]
                           transports/*.py                    pure, no device access
                                          │
                    reconcile/merge.py ───┤   one section per menu, shared owner scope
                                          │
                    reconcile/diff.py ◄───┴──► driver.read()   ownership-filtered
                              │
                    reconcile/plan.py ──► [ConfigOp]           ordered by ORDER
                              │
                    reconcile/apply.py ──► safe_apply()        backup, arm, push, verify, disarm
```

**Why sections are merged.** Several renderers write to one RouterOS menu — the
site baseline puts a loopback in `/ip/address`, and every link puts a tunnel
address there too. Diffing them separately means rows belonging to a link that no
longer exists match no section at all, so a removed site's tunnels run forever.
Merging unions the items and widens the ownership tag to the shared scope, so one
diff covers everything the controller owns in that menu. The merger also refuses
two renderers that claim the same row — that catch is what found a real conflict
between the fabric and policy netwatch renderers, which would have made devices
flap between two intents on alternate applies.

## Layout

```
backend/app/
├── api/v1/       HTTP surface
├── models/       SQLAlchemy — the persisted intent model
├── schemas/      Pydantic — the wire contract
├── drivers/      device access (REST, SSH, softhub)
├── transports/   overlay technologies              (M3/M4)
├── render/       intent → RouterOS config           (M2)
├── reconcile/    diff, safe-apply, rollback, drift  (M2)
├── telemetry/    pollers and link scoring           (M5)
└── services/     orchestration between API and drivers
ui/               React + TypeScript
softhub/          strongSwan + FRR cloud hub         (M6)
labs/             containerlab topologies for CI     (M3)
```

## Security notes

- `SDWAN_SECRET_KEY` encrypts device credentials, via a PBKDF2-derived key.
  **Changing it makes every stored credential undecryptable.** Rotate
  deliberately.
- Device identity is pinned on first contact and enforced afterwards, because
  RouterOS ships a self-signed certificate and chain validation is not
  available. Onboard over a network you trust — first contact is the one
  connection that cannot be verified.
- Logins are locked out after 5 failures per account and source.
- `SDWAN_JWT_SECRET` signs API tokens. Rotating it only logs everyone out.
- Rendered config is redacted through `drivers/redact.py` before it reaches a
  log, a job record, or the UI.
- The API container runs as a non-root user; it holds decryptable credentials.

## Verification

See [docs/verification.md](docs/verification.md) for the per-milestone checklist,
including the hardware smoke test required before any release tag.
