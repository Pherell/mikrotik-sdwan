# Live CHR tests (plan C1, step 1)

Tests in this directory run against a **real RouterOS CHR**, through the
repo's own `Ros7RestDriver` and reconcile planner — not against the
`tests/fakeros` fake and not through raw HTTP. They exist to catch the
class of bug a fake cannot: RouterOS behaviours that only a real device
exhibits.

This is step 1 of plan item **C1** (CHR integration harness + chaos
drills): a single node, covering identity, one harmless write
round-trip, and reconcile idempotency. The hub + 2-spoke topology, the
per-release chaos drills (kill the controller mid-apply, drop
management during Safe Mode, flap a WAN, test a backup restore), and
CI integration are later steps and are **not** implemented here.

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

## Next empirical questions (TODO, not answered yet)

The harness is the vehicle for two questions the research behind plan
C1 flagged as unresolvable from documentation alone. Both are still
open:

1. **BFD `check-gateway` for static routes** — MikroTik's docs
   contradict each other (the BFD page says enabling BFD for IP route
   gateways is not yet supported; the IP Routing page says
   `check-gateway` can check BFD sessions). Must be settled on a CHR
   at the target RouterOS version before plan A6 is designed.
2. **Safe Mode held over SSH** — RouterOS Safe Mode is the only
   commit-confirm mechanism, REST cannot hold it (an SSH session must
   hold it around REST mutations), and it auto-disengages after 100
   history actions. Plan B1 (transactional apply) depends on verifying
   this behaviour on a real device first.
