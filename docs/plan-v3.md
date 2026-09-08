# Plan v3 — firewall, vocabulary, and the SD-WAN objects

Seventeen requests came in at once. They are not seventeen things. This sorts
them, names what is a defect rather than a feature, and sequences the rest.

---

## Part 1 — The firewall gap, which is a defect

**Verified, not assumed.** The controller owns 23 RouterOS menus. `/ip/firewall/nat`
and `/ip/firewall/filter` are not among them:

```
/ip/address              /ip/ipsec/peer           /interface/gre
/ip/route                /ip/ipsec/identity       /interface/ipip
/ip/firewall/address-list /ip/ipsec/policy        /interface/wireguard
/ip/firewall/mangle      /ip/ipsec/profile        /interface/wireguard/peers
/routing/table           /ip/ipsec/proposal       /interface/vxlan
/routing/bgp/template    /tool/netwatch           /interface/vxlan/vteps
/routing/bgp/connection                           /interface/eoip
/routing/bgp/network                              /interface/bridge
                                                  /interface/bridge/port
```

Three things break because of it.

### 1. Steering can silently blackhole traffic

A normal RouterOS site masquerades on its uplink:

```
/ip/firewall/nat add chain=srcnat action=masquerade out-interface=ether1
```

A policy that steers traffic to `ether2` sends it out an interface **no NAT rule
matches**. The packet leaves with a private source address and is dropped by the
first upstream router. Nothing in the UI says so; the plan applies cleanly, the
routes are correct, and the traffic disappears.

This is the most serious problem in the product. Steering, the headline feature,
is unsafe on any device with an ordinary firewall.

### 2. Tunnel traffic gets masqueraded

Traffic to a peer's public address matches the same masquerade rule. IPsec in
transport mode needs an exception above it:

```
/ip/firewall/nat add chain=srcnat action=accept dst-address=<peer> place-before=0
```

### 3. A default-drop input chain blocks tunnel establishment

Nothing opens UDP 500/4500, protocol 50 (ESP), protocol 47 (GRE) or the
WireGuard port from peer addresses. On a hardened device the tunnel never comes
up and the error is a timeout with no explanation.

### Why this is not a small addition

**Firewall rules are ordered and the reconciler has no concept of order.**

`ConfigOp.place_before` exists and is plumbed through both drivers —
`ros7_rest.py:215` and `ros6_ssh.py:228` — and **nothing anywhere sets it**. It
is dead capability. Every create is a bare `PUT`, which appends to the end of
the chain.

That means an `accept` rule the controller adds lands *after* the operator's
existing `masquerade`, where it can never match. Adding a NAT section without
ordering would produce config that looks right in the diff and does nothing.

It also means the mangle rules the controller already writes are appended to the
bottom of `prerouting`. That happens to work when nothing above them marks the
same traffic, which is luck rather than design.

**So the firewall work is really two pieces:**

- **F1a — ordering.** Sections gain a position intent; the diff compares
  position as well as properties; `place_before` starts being set. This is the
  harder half and it improves mangle correctness too.
- **F1b — the rules themselves.** NAT bypass for fabric peers, masquerade that
  follows steering, and input accepts for whichever transport a fabric uses.
  All ownership-tagged, so a hand-written firewall is never touched.

**Done when:** a site with `masquerade out-interface=ether1` and a default-drop
input chain forms tunnels and steers traffic to a second uplink without anyone
editing its firewall by hand — and a second apply is still a no-op.

---

## Part 2 — Vocabulary, because six of the requests are one request

> policies is so confusing · separate the menu · make sure not use ambiguous
> meaning for word · separate the policy because this thing make me confuse more

These are the same complaint. The words overload each other:

| Today | What it actually means | Why it confuses |
|---|---|---|
| **Site** | one device | Nothing else lives at a "site". It is a device. |
| **Fabric** | the VPN overlay joining devices | Invented word; "fabric" means nothing to a network engineer coming from Sophos or Cisco. |
| **Link** | one tunnel between two uplinks | "Link" also means a physical port, and an uplink. Three meanings. |
| **Policy** | match + link preference + SLA + fallback | Four concepts in one noun. |
| **prefer_tags** | which uplinks, in order | Not a tag in any normal sense. |

### The renaming

| From | To | Why |
|---|---|---|
| Site | **Device** | It is one device. Rename when multi-device sites exist, not before. |
| Fabric | **Tunnel network** | Says what it is. |
| Link | **Tunnel** | One word, one meaning. |
| Policy | split → **SD-WAN group** + **Traffic rule** | See Part 3. |
| prefer_tags | group membership | Disappears as a user-facing idea. |

### The menu

Separating SD-WAN from tunnels, as asked:

```
SD-WAN
    Uplinks            every WAN link, across all devices
    SD-WAN groups      which uplinks, weighted, with a strategy and an SLA
    Traffic rules      match traffic -> send it to a group
    Diagnostics        ping, traceroute, path test

TUNNELS
    Tunnel networks    was: fabrics
    Tunnels            was: links. Computed, never authored.

DEVICES
    Devices            was: sites
    Console            command runner

SYSTEM
    Jobs               every configuration push
    Logs               controller and device logs
    Users & tokens     accounts, roles, API tokens and scopes
    API                the reference, in-app
    Settings           actual preferences
```

Tunnels and SD-WAN become separate things you look at, which is what was asked
and is also true: the overlay and the link-selection logic are independent.

---

## Part 3 — SD-WAN groups and traffic rules

Modelled on Sophos, as requested. Sophos has an **SD-WAN profile** (gateways,
each with a weight; a strategy; an SLA; health-check targets) and an **SD-WAN
route** (match traffic, point at a profile). Two objects, and the hard half is
named once and reused.

### SdwanGroup

```
name                "internet-balanced"
members[]           { uplink, weight, order }        max 8, as Sophos
strategy            failover | load_balance
sla_profile         what "healthy enough" means
health_targets[]    what to ping. Defaults to each uplink's gateway.
```

### TrafficRule

```
name, priority, enabled
match               source, destination, application group, ports, DSCP
group               the SdwanGroup to use
fallback            main routing table | drop
```

A rule is then readable as one sentence: *"Teams traffic goes to the
voice-primary group."* That is the whole point.

### What each strategy renders to

**`failover`** is what exists today and works: one routing table per group,
routes ordered by member position via `distance`, `check-gateway=ping`, and
netwatch demoting a member that breaches its SLA by +100. Weights are ignored
and the UI must say so rather than accept a number that does nothing.

**`load_balance`** does not exist. On RouterOS it means PCC —
`per-connection-classifier=both-addresses:N/M` marking connections into buckets,
one bucket per weight share. That collides with the mangle rules traffic rules
already emit, and needs the ordering work from F1a to be safe. Own milestone.

**The ceiling, stated up front:** RouterOS balances *connections*, not packets.
One download never uses two links. Sophos's weighted round-robin assigns
sessions too — this is not MikroTik being worse, but nobody should choose
`load_balance` expecting a single transfer to go faster.

---

## Part 4 — Everything else, honestly sized

### User-defined tunnels rather than templates

> user can config the vpn using their own prefrence not setup by template

Fair. Today a tunnel network picks a transport and the crypto comes from a
profile. Exposing IKE proposal, DH group, PFS, lifetimes, and letting someone
bring their own pre-shared key or certificate, is a real need for anyone with a
security standard to meet. It is also the fastest way to build a fabric that
silently fails to establish, so it belongs behind Advanced with validation and a
clear "these must match on both ends" warning.

### Diagnostics — done

Ping, traceroute and a path test from a device. Plus, per tunnel: is the SA up,
is BGP established, what does netwatch currently measure. This is the tool that
answers "why is this tunnel down" without SSH.

The plan said "run through the existing allowlisted command runner". There was
no command runner -- `READABLE_PATHS` is a *read* allowlist, and the device
console is menu browsing, not command execution. So ping and traceroute are the
first endpoints that ask a device to do anything outside the reconciler. They
are safe because RouterOS ping writes no configuration and leaves no rows, but
the target still gets a strict validator in front of both drivers: the REST
driver sends it as JSON while the SSH driver builds a console line out of it,
and only one rule in front of both stays true when a third driver arrives.

Three-state everywhere, not two. A GRE fabric has no IPsec SA; a device that
was never applied has no interface; an unreachable device has no opinion about
any of its tunnels. All three are `null`, drawn grey. Collapsing them into
"down" turns one unreachable router into what looks like a total outage.

### Web console

A read-only allowlisted command runner already exists (`DeviceConsole`). A real
interactive SSH terminal in the browser is a different thing: a websocket PTY
proxy, and a serious security surface — it turns the controller into a jump host
for every device it manages, with the controller's own auth in front of it.
Worth having, worth doing deliberately, and worth being explicit that it widens
the blast radius of a controller compromise from "can push config" to "has a
shell everywhere".

### Interface dropdowns

> auto list port, do dropdown

Straightforward and overdue. The port panel already reads every interface; the
uplink form should offer that list instead of a free-text field where a typo
produces a policy that silently matches nothing.

### Logging

Three different things wearing one word:

- **audit** — who changed what in the controller. Exists in the database, has no
  UI.
- **job log** — what happened during an apply. Exists, shown per job.
- **device log** — RouterOS's own log, read from `/log`. Does not exist.

All three should be visible; only the third is new work.

### API documentation and tokens

FastAPI already serves OpenAPI at `/docs`. What is missing is **API tokens with
scopes** — today the only credential is a user's login JWT, so any automation
runs as a person with that person's full rights. Tokens want: a name, a role, an
expiry, revocation, and an audit trail.

---

## Sequencing

```
F1a  Rule ordering in the reconciler      correctness; unblocks everything below
F1b  NAT and filter management            correctness; makes steering actually work
V1   Vocabulary and menu split            cheap, and every later screen inherits it
U1   Interface dropdowns                  small, removes a whole class of typo
S1   SD-WAN groups + traffic rules        the model change; needs V1's words
D1   Diagnostics                          done
L1   Logs                                 independent
A1   API tokens and scopes                independent
S2   Load balancing via PCC               needs F1a and S1
C1   Interactive SSH console              last; largest security decision
```

F1 first because everything else is polish on a product whose main feature can
blackhole traffic. V1 before S1 because renaming after building the new objects
means renaming twice.

## What this does not include

- **Multi-device sites.** "Device" is the honest word precisely because one
  device per site is enforced. Revisit when that changes.
- **Per-packet load balancing or FEC.** RouterOS cannot.
- **Hardware validation.** Still none. Every milestone here lands against
  `tests/fakeros` and containerlab, and the honest position does not change
  until this runs on real MikroTiks.
