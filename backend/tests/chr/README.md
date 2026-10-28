# Live CHR tests (plan C1)

Tests in this directory run against a **real RouterOS CHR**, through the
repo's own `Ros7RestDriver` and reconcile planner — not against the
`tests/fakeros` fake and not through raw HTTP. They exist to catch the
class of bug a fake cannot: RouterOS behaviours that only a real device
exhibits.

Layout:

* `test_chr_live.py` — C1 step 1: a single node, covering identity, one
  harmless write round-trip, and reconcile idempotency.
* `lab_topology.py` — shared helpers for the multi-node tests: the lab
  node map, topology seeding in the real controller DB, applies through
  `services/reconcile.apply_site`, and device-state readers/wipers.
* `test_chr_topology.py` — the rest of C1: three CHRs registered as
  sites, a hub + 2-spoke WireGuard fabric expanded and applied for
  real (with Safe Mode held by `drivers/safemode.py` — its first live
  run), tunnel/BGP verification, and convergence (a second apply is a
  no-op).
* `test_chr_drills.py` + `_kill_drill.py` — the chaos drills: the
  controller SIGKILLed mid-apply, management cut during held Safe Mode
  (healthy close and silent partition), a WAN-flap baseline, and a
  restore from the C4 scoped pre-image.

The multi-node tests need the three-node lab (QEMU socket links and
host forwards as described in the harness STATUS.md outside this repo),
the controller Postgres DB (`CHR_DB_URL`, default
`postgresql+asyncpg://sdwan:sdwan@127.0.0.1:5432/sdwan`), and the lab
device timeouts the runner exports. CI integration is still future
work.

## Prerequisites

- A throwaway lab CHR running RouterOS 7.x. Do not point these tests at
  a production device. The write tests only ever create and delete
  address-list rows tagged `sdwan:chr-harness` (address `192.0.2.99`,
  TEST-NET-1), and each test cleans up after itself, but the device
  should still be disposable.
- REST reachable over HTTPS (`www-ssl` enabled on the device with a
  certificate assigned — RouterOS ships it disabled with
  `certificate=none`). The harness connects with `verify_tls=False`
  and the driver's default TOFU certificate pinning, matching how the
  controller talks to a device presenting a self-signed certificate.
- How you run the CHR is up to you (QEMU, a hypervisor, …). In the
  original PoC the CHR ran under QEMU on the development machine via a
  run script kept **outside this repo** (together with the CHR disk
  image and the lab password); nothing about that setup is required by
  the tests themselves — only the REST endpoint and credentials are.

## Running

Either export the environment yourself:

```sh
cd backend
CHR_LIVE=1 \
CHR_PASSWORD='<lab admin password>' \
.venv/bin/python -m pytest tests/chr -v
```

or use the runner, which reads the password from `CHR_PASSWORD` if set,
else from the file named by `CHR_PASSWORD_FILE` (default:
`~/workspace/chr-harness/.chr-password`, the PoC location), sets the
variables above, and bypasses any proxy environment for the local
endpoint:

```sh
scripts/chr-test.sh
```

Environment variables:

| Variable       | Default                     | Meaning                          |
| -------------- | --------------------------- | -------------------------------- |
| `CHR_LIVE`     | *(unset)*                   | Must be `1` for the tests to run |
| `CHR_BASE_URL` | `https://127.0.0.1:18443`   | CHR base URL (no `/rest` suffix needed) |
| `CHR_USER`     | `admin`                     | REST user                        |
| `CHR_PASSWORD` | *(required when live)*      | REST password; never commit it   |

## Default-skip behaviour

Without `CHR_LIVE=1` (and a password), every test here is skipped, so
the normal suite — `pytest` in `backend/` — and CI are unaffected. The
tests are also collected under the `chr` pytest marker:

```sh
.venv/bin/python -m pytest -m "not chr"   # explicit opt-out
```

## Empirical questions the harness has answered

The harness was built to settle two questions the research behind plan
C1 flagged as unresolvable from documentation alone. Both are now
answered on RouterOS 7.24.5:

1. **BFD `check-gateway` for static routes** — ACCEPTED-BUT-IGNORED:
   the route accepts the setting and a BFD session is demand-created,
   but the session never establishes between two CHRs and the route
   stays active regardless. Do not rely on it for static/policy routes;
   withdrawal stays on ICMP/netwatch probes. (Lab findings, 2026-10-22.)
2. **Safe Mode held over SSH** — works: Ctrl+X over an SSH PTY holds it
   around REST mutations, a second Ctrl+X commits, and a healthy socket
   close rolls the held changes back in about a second (the documented
   ~9 minutes is only the silent-partition TCP timeout). One catch only
   a real device showed: the fresh console first asks the terminal for
   its cursor position (ESC [ 6 n) and waits for the reply — a session
   that does not answer never sees a prompt, and Safe Mode is never
   granted. `drivers/safemode.py` answers it in its drain step.
