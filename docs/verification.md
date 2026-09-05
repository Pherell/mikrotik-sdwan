# Verification

Three layers, cheapest first. Only the third proves RouterOS syntax is correct.

## 1. Unit and integration (no hardware, runs in CI)

```bash
make test
```

Covers coercion, the REST driver against `tests/fakeros/`, WAN discovery,
auth/RBAC, credential encryption, and that migrations import and apply.

Also, added after each bit them reached a real deployment:

- `test_security.py` — identity pinning, login throttling, key derivation, and
  that values written under the old derivation still decrypt. Each test names
  the attack it prevents.
- `test_config.py` — settings built from the *environment*, the way a container
  gets them, including the whole compose environment block together. This
  exists because `SDWAN_CORS_ORIGINS` broke every container while 294 tests
  passed: none of them set that variable, so the environment source was never
  exercised and the suite only proved the defaults parse.

## 2. End-to-end lab (containerlab + CHR)  — from M3

Not yet built. `labs/` will bring up 2 hubs, 3 spokes, and a WAN-impairment
container, then assert tunnels establish, BGP converges, prefixes are exchanged,
a link failure fails over inside the SLA window, and a deliberately broken push
self-rolls-back.

CHR's free tier is capped at 1 Mbps — enough for control-plane assertions, not
for throughput tests.

## 3. Hardware smoke test (required before any release tag)

At least one RouterOS 7 device (hEX / hAP / RB5009) and, from M6, one RouterOS 6
device.

---

## M1 checklist — complete

Automated:

- [x] `make test` green (47 tests)
- [x] `make lint` clean
- [x] `alembic upgrade head` creates every model table

Manual, against a real RouterOS 7 device:

- [ ] Enable `www-ssl` and create a restricted `sdwan` user on the device
- [ ] `docker compose up -d --build` reaches a healthy `api` container
- [ ] Sign in with the bootstrap admin; the password from `.env` works
- [ ] Add the site through the wizard; probe returns version, board, and identity
- [ ] Discovered uplinks match reality, and a NAT'd uplink is marked **dial-out only**
- [ ] The device's configuration is unchanged after probing (`/export` before and
      after are identical) — probing must be strictly read-only
- [ ] `GET /api/v1/sites` never contains the device password
- [ ] A `viewer` account gets 403 on site creation
- [ ] Audit rows exist for `auth.login`, `site.create`, and `site.probe`

## M2 checklist — complete

Automated (39 new tests in `test_reconcile.py` and `test_api_jobs.py`):

- [x] A rendered section applies, then re-plans clean — no phantom diff
      (`test_apply_then_replan_is_a_no_op`, `test_second_apply_is_a_no_op`)
- [x] Hand-built config on the same device is untouched
      (`test_apply_does_not_disturb_unmanaged_config`)
- [x] A push that costs management access leaves the rollback armed, and the
      error says so first (`test_safe_apply_leaves_rollback_armed_...`)
- [x] The scheduler is armed *before* the first mutating op
      (`test_safe_apply_arms_before_pushing`)
- [x] The rollback's `on-event` string is asserted verbatim
      (`test_rollback_scheduler_restores_the_backup_it_was_given`)
- [x] A failed backup aborts before anything is pushed
- [x] An unreadable menu blocks apply rather than diffing as "delete everything"
- [x] Device credentials never reach a job record

Manual, against a real RouterOS 7 device:

- [ ] `POST /sites/{id}/plan` on a fresh device lists 3 additions and changes nothing
- [ ] Apply, then `/export` and confirm only `sdwan:`-tagged rows appeared
- [ ] Re-plan returns `empty: true`
- [ ] Add an unrelated bridge and address-list by hand, apply again, confirm both survive
- [ ] **The real test:** add a firewall rule dropping the controller's access to
      the `www-ssl` service as part of an apply. Confirm the router restores its
      backup, reboots, and comes back reachable with the pre-apply config
- [ ] Kill the API container mid-apply; confirm `GET /sites/{id}/rollbacks`
      surfaces the orphaned scheduler and `DELETE` clears it
- [ ] Confirm `DELETE /sites/{id}/rollbacks/{name}` refuses a non-controller entry

## M3–M6 checklists

Automated coverage now stands at **269 tests**, none needing hardware.

### M3 — IPsec fabric

- [x] Hub-and-spoke expansion produces the right links and skips impossible pairs
- [x] Expansion is idempotent; re-running never renumbers a live overlay
- [x] Both ends of a link agree on interface name, /31 and initiator
- [x] Applying every member converges, and re-planning is empty
- [x] Hub renders as `ibgp-rr`, spokes as `ibgp-rr-client`
- [x] Link PSKs are encrypted at rest and never appear in a plan or the API
- [ ] **Hardware/lab:** `labs/verify_fabric.py` against three CHRs — IPsec SAs
      up, BGP established, prefixes learned, lab config untouched

### M4 — Transport plugins

- [x] Six transports registered: ipsec_gre, wireguard, gre, ipip, vxlan, eoip
- [x] Every transport tags what it renders and declares every path it writes
- [x] X25519 verified against the RFC 7748 test vector
- [x] v7-only transports refuse a v6 site with a clear message
- [x] IPsec → WireGuard switch re-keys every link, sweeps the old stack, converges
- [ ] **Lab:** the same switch on real CHRs, with traffic loss measured

### M5 — Steering and SLA

- [x] Policies render address-lists, mangle marks, routing tables, routes, probes
- [x] Preference order becomes route distance; cheaper uplink wins within a tag
- [x] A policy naming no uplink present at a site is skipped, not blackholed
- [x] Breaching the SLA demotes the path rather than removing it
- [x] Demotion is absolute, not cumulative — two down events do not compound
- [ ] **Lab:** `tc netem loss 30%` on spoke2's uplink moves matched traffic to
      the backup inside the SLA window, and it returns after the impairment lifts

### M6 — Mesh, v6, softhub, hardening

- [x] Dynamic mesh builds on traffic, tears down on idle, never flaps a young tunnel
- [x] Two CGNAT spokes are never paired directly
- [x] Drift detection flags edits; auto-remediate reverts them; hand-built config
      is never reported as drift
- [x] Intent round-trips through YAML with no credentials in the file
- [x] Import is dry-run by default and never deletes what a document omits
- [x] RouterOS 6 console quoting blocks command injection
- [x] Metrics carry counts and states only — no names or addresses
- [ ] **Hardware:** one RouterOS 6 device driven over SSH end to end
- [ ] **Hardware:** softhub terminating a real tunnel from a CHR
