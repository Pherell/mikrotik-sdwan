# Lab

The only layer that proves the rendered RouterOS syntax actually establishes.
Everything in `backend/tests/` runs against a fake device and can prove the
request shapes, the diff, and the ownership rules — but not that RouterOS
accepts the configuration.

## What you need

- [containerlab](https://containerlab.dev) on Linux
- A CHR image imported as `vrnetlab/mikrotik_ros:7.14.3`, built with
  [vrnetlab](https://github.com/srl-labs/vrnetlab). `labs/build_chr_image.sh`
  does the download, build and tag in one go (see below). CHR's free tier is
  capped at 1 Mbps — enough for control-plane assertions, useless for
  throughput tests.
- KVM (`/dev/kvm`): each CHR is a QEMU VM inside its container.
- The controller running and able to reach `172.30.30.0/24`
  (`labs/docker-compose.lab.yml` attaches it to the lab network).

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

Interface names: vrnetlab gives RouterOS `ether1` as its management port, so
the clab link `hub1:eth1` is `ether2` inside the router. The uplinks in
`configs/*.rsc` and the WANs `verify_fabric.py` registers are on `ether2`.
Because vrnetlab NATs management traffic into the VM, the controller's
requests arrive from `172.31.255.29`; the `sdwan` user allows that /30 as well
as the management subnet.

## Building the CHR image

```bash
labs/build_chr_image.sh                         # vrnetlab/mikrotik_ros:7.14.3, local only
labs/build_chr_image.sh --version 7.16.2        # another release
echo "$GHCR_TOKEN" | docker login ghcr.io -u <you> --password-stdin
labs/build_chr_image.sh --push ghcr.io/<owner>/mikrotik_ros   # also push :7.14.3
```

It downloads `chr-<version>.vmdk.zip` from MikroTik (`--url`, `--sha256` to
override or pin), builds it with vrnetlab at a pinned ref (`--vrnetlab-ref`,
default `v0.21.0`), tags it as the name the topology uses and, with `--push`,
pushes `<repo>:<version>`. Building needs Docker but not KVM. Keep a pushed
image private unless you have checked MikroTik's licence terms.

## Running it

```bash
sudo clab deploy -t labs/hub-spoke.clab.yml
docker compose -f docker-compose.yml -f labs/docker-compose.lab.yml up -d --build --wait
python3 labs/verify_fabric.py --api http://localhost:8000 \
    --password "$SDWAN_BOOTSTRAP_ADMIN_PASSWORD" --json-out verify.json
docker compose -f docker-compose.yml -f labs/docker-compose.lab.yml down -v
sudo clab destroy -t labs/hub-spoke.clab.yml --cleanup
```

Order matters: the compose overlay joins the `sdwan-mgmt` network that
containerlab creates, so deploy the lab first and stop compose before
destroying it. `verify_fabric.py` only needs `httpx` (`pip install httpx`).

`verify_fabric.py` builds the fabric through the API, applies it, and asserts:

- expansion produces two links and skips nothing;
- every site applies cleanly and disarms its rollback;
- re-planning each site is empty — the idempotency property, on real RouterOS;
- both IPsec SAs come up on the hub;
- both BGP sessions establish;
- spoke1 learns the hub's and spoke2's prefixes over the overlay;
- with a failover steering policy applied to the spokes (all three LANs via
  the overlay, `recovery_seconds=30`): both spokes apply and re-plan clean;
  the policy LAN guard (`accept dst-address-list=sdwan-<site>-lan`) is present
  and above the first mark rule, and its address-list is populated; every
  netwatch up-script holds down with `:delay 30s`; every PCC classifier (if
  any rendered) has `connection-state=new connection-mark=no-mark` — the lab
  has one uplink per site, so that one is reported as *skipped*;
- the overlay still converges with steering on;
- the hand-written lab configuration is still there afterwards.

Every check runs and is reported; the exit code is `0` if all passed, `1` if
any failed, `2` if the run could not complete (controller unreachable, API
error). Convergence checks poll for up to `--converge-timeout` seconds
(default 180), the controller's `/healthz` is awaited for `--api-wait`
(default 120), and connection errors / 502–504 are retried (`--retries`).
`--json-out PATH` writes a summary (`ok`, counts, `error`, every check with
phase and detail), also on an abort. `--skip-policy` runs only the fabric
checks. Re-running against a controller that already has the sites reuses
them instead of failing on 409.

## Running in CI

`.github/workflows/lab.yml` runs the whole thing on GitHub Actions:
KVM enabled via a udev rule → containerlab (pinned) installed → CHR image
pulled or built → lab deployed → routers' REST API awaited → controller started
with `labs/docker-compose.lab.yml` → `verify_fabric.py` → logs, router exports
and `verify.json` uploaded as an artifact → compose and lab torn down. The
check table is written to the job summary.

When it runs:

| Trigger | Runs when |
|---|---|
| `workflow_dispatch` | always (inputs: force a vrnetlab build, RouterOS version, convergence timeout) |
| nightly (02:17 UTC) | always |
| `pull_request` | the PR has the `lab` label, **or** it changes `backend/app/{render,transports,reconcile,drivers}/`, `labs/`, or the workflow |

A `paths:` filter cannot express "label OR paths", so a small `gate` job
decides and the lab job is skipped otherwise. PRs from forks are skipped (they
get no secrets or package access).

Configuration (Settings → Secrets and variables → Actions):

| Name | Kind | Purpose |
|---|---|---|
| `LAB_CHR_IMAGE` | variable (or secret) | e.g. `ghcr.io/<owner>/mikrotik_ros:7.14.3`. Pulled and retagged to the topology's image name. **Preferred.** |
| `LAB_REGISTRY_USERNAME` / `LAB_REGISTRY_PASSWORD` | secrets | registry login. Not needed for GHCR: `GITHUB_TOKEN` is used, so give the repository read access to the package (package settings → Manage Actions access). |
| `LAB_CHR_BUILD` | variable | `true` to build with vrnetlab each run when no `LAB_CHR_IMAGE` is set (~5 min extra, downloads from MikroTik). |
| `LAB_CHR_VERSION` | variable | version to build; default is the tag in `hub-spoke.clab.yml` (7.14.3). |
| `LAB_CHR_URL` / `LAB_CHR_SHA256` | variables | override / pin the CHR download. |
| `LAB_RUNNER` | variable | `runs-on` label; default `ubuntu-latest`. |

With neither `LAB_CHR_IMAGE` nor `LAB_CHR_BUILD`, the job ends with a
"Lab skipped" notice instead of failing, so forks and fresh clones stay green.

**KVM.** GitHub's hosted Linux runners (standard `ubuntu-latest` and the
larger Linux runners) expose `/dev/kvm`; the workflow makes it world-accessible
with GitHub's documented udev rule and fails with a clear error if the device
is missing. A self-hosted runner needs hardware virtualisation (nested, if it
is itself a VM), Docker, passwordless `sudo`, and the label set in
`LAB_RUNNER`. Three CHRs at 512 MB plus the controller fit comfortably in a
standard public-repository runner (4 vCPU / 16 GB); a private repository's
standard runner (2 vCPU / 7 GB) is tighter, so raise `--converge-timeout` or
use a larger runner there.

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
