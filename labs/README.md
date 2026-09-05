# Lab

The only layer that proves the rendered RouterOS syntax actually establishes.
Everything in `backend/tests/` runs against a fake device and can prove the
request shapes, the diff, and the ownership rules — but not that RouterOS
accepts the configuration.

## What you need

- [containerlab](https://containerlab.dev) on Linux
- A CHR image imported as `vrnetlab/mikrotik_ros:7.14.3`, built with
  [vrnetlab](https://github.com/hellt/vrnetlab). CHR's free tier is capped at
  1 Mbps — enough for control-plane assertions, useless for throughput tests.
- The controller running and reachable from the host.

## Topology

```
              ┌──────────── internet (L2 bridge) ────────────┐
              │                    │                         │
          hub1 (RR)            spoke1                    impair ── spoke2
        198.51.100.5       198.51.100.11               (netem)  198.51.100.12
         10.1.0.0/24        10.2.0.0/24                          10.3.0.0/24
```

`internet` is a plain bridge standing in for the public internet; the addresses
on it are what the fabric treats as public. `impair` sits between spoke2 and the
bridge so a test can add loss and latency without touching the routers.

Each router also has a management interface on `172.30.30.0/24`, which is how
the controller reaches it.

## Running it

```bash
sudo clab deploy -t labs/hub-spoke.clab.yml
python labs/verify_fabric.py --api http://localhost:8000 --password "$SDWAN_BOOTSTRAP_ADMIN_PASSWORD"
sudo clab destroy -t labs/hub-spoke.clab.yml --cleanup
```

`verify_fabric.py` builds the fabric through the API, applies it, and asserts:

- expansion produces two links and skips nothing;
- every site applies cleanly and disarms its rollback;
- re-planning each site is empty — the idempotency property, on real RouterOS;
- both IPsec SAs come up on the hub;
- both BGP sessions establish;
- spoke1 learns the hub's and spoke2's prefixes over the overlay;
- the hand-written lab configuration is still there afterwards.

It exits non-zero on the first failure, so CI can gate on it.

## Injecting a fault

Brownout on spoke2's uplink:

```bash
docker exec clab-sdwan-impair tc qdisc add dev eth1 root netem loss 30% delay 200ms
```

Netwatch on the hub should mark that tunnel down inside the configured window
(10 s interval × 10 packets ≈ 10–15 s by default). Remove it with:

```bash
docker exec clab-sdwan-impair tc qdisc del dev eth1 root
```

## The rollback test

The one that matters most, and the one that cannot be faked. On a live device,
apply a configuration that cuts the controller off from `www-ssl` — for example
add a firewall rule dropping TCP 443 from the management subnet. The controller
should fail to verify, leave the scheduler armed, and report that the router is
restoring itself. Roughly two minutes later the router reboots and comes back
with its pre-apply configuration.

Do this in the lab before you ever do it on hardware.
