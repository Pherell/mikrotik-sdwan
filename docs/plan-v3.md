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

**`load_balance`** — done. On RouterOS it means PCC:
`per-connection-classifier=both-addresses:N/M` marking connections into
buckets, one bucket per weight share. Weights are bucket *counts*, because
RouterOS has no number it understands as a weight — a member with weight 3 owns
three of the N buckets. Total buckets are capped at 16 and weights scaled into
it, so 100-versus-1 renders sixteen rules rather than a hundred and one, with
every member keeping at least one bucket.

Two rules per bucket set, in order: `mark-connection` with `passthrough=yes`
(the connection, not the packet, so one TCP stream is never split mid-transfer)
and then `mark-routing` keyed on the connection mark. One routing table per
*member*, not per bucket, or a 7/3 split would build ten identical tables.

Each table prefers its own member at distance 1 and carries the others at 2.
Without that fallback, a member going down blackholes every connection hashed
to it — balancing without failover is worse than no balancing, because the
failure is partial and looks random. The SLA scripts became table-aware for the
same reason: one gateway sits at a different distance in every table, so a
script setting one distance everywhere would flatten the balance into whichever
path recovered last.

**The ceiling, stated up front:** RouterOS balances *connections*, not packets.
One download never uses two links. Sophos's weighted round-robin assigns
sessions too — this is not MikroTik being worse, but nobody should choose
`load_balance` expecting a single transfer to go faster.

---

## Part 4 — Everything else, honestly sized

### User-defined tunnels rather than templates — done

> user can config the vpn using their own prefrence not setup by template

The mechanism already existed: `Fabric.transport_params` is merged over each
transport's defaults at render time. What was missing was that the field was an
untyped `dict` nothing validated, and no way to reach it from the UI.

Each transport now declares its options — key, allowed values, and a sentence
saying what the option is for — and the fabric schema validates against that.
Both failures this prevents are silent ones. A misspelled key was *ignored*, so
the fabric built with the default exactly as if nothing had been set. A bad
value rendered, applied cleanly, and the tunnel never established, because IKE
mismatches do not report themselves as configuration errors. Either way it
surfaced as "the VPN is broken", days later, with nothing pointing at the
cause.

The UI draws the Advanced section from the same declaration rather than
carrying a second list: a copy of a list of ciphers goes stale the first time
one is added, and a stale list silently hides a setting. Only values changed
away from the default are stored, so a fabric created today still follows this
build's chosen default tomorrow.

GRE, IPIP and WireGuard declare no options, and the UI says so rather than
drawing an empty section that looks broken — they have no ciphers to agree on,
and WireGuard's are not selectable by design.

Still template-driven: bringing your own certificate. The PSK is generated per
link and stored encrypted; certificate-based IKE is a different credential
lifecycle (issue, distribute, renew, revoke) and belongs with a decision about
where the CA lives.

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

### Web console — done, as a command console

What shipped is a **command console**: type a RouterOS command, see what it
said. Reads and probes, server-side allowlist, every command audited including
the refused ones. It replaced a dropdown of menus that was safe and nearly
useless — it could only answer questions somebody had anticipated, and the
whole reason to open a console is a question nobody anticipated.

What did **not** ship is a PTY proxied to SSH, and the reason is two reasons.

The security one is the one the plan already stated: it turns the controller
into a jump host with a shell on every device it manages, taking a controller
compromise from "can push configuration, with a diff and a rollback and an
audit row" to "has root on the estate".

The product one would bite first. Configuration changed by hand is drift the
reconciler does not know about, and the next apply reverts it — silently,
because reverting drift is exactly its job. A console that lets you change
things behind the reconciler's back does not give you a faster way to work; it
gives you changes that disappear.

If a real PTY is wanted later it is a websocket channel to `asyncssh`, plus
xterm.js in the browser, plus a decision that the drift problem above is
acceptable. That is a deliberate product decision, not a missing feature.

### Interface dropdowns

> auto list port, do dropdown

Straightforward and overdue. The port panel already reads every interface; the
uplink form should offer that list instead of a free-text field where a typo
produces a policy that silently matches nothing.

### Logging — done

Three different things wearing one word:

- **audit** — who changed what in the controller. Existed in the database with
  no UI; now `GET /audit`, admin only, on a Logs page.
- **job log** — what happened during an apply. Existed; the Logs page links to
  it rather than showing a worse copy.
- **device log** — RouterOS's own log, read from `/log`. New.

Three tabs, not one merged stream: they answer different questions and belong
to different owners, and merging them produces a feed where nothing can be
found.

Two things the work turned up. The audit trail is **admin only** — it carries
source addresses and, because failed logins are audited, the email addresses of
accounts that do and do not exist, which makes it an account-enumeration
endpoint in anyone else's hands. And audit rows needed a Python-generated
`created_at`: `func.now()` is `CURRENT_TIMESTAMP`, which SQLite resolves to
whole seconds, so two events in the same second came back in UUID order — in no
order at all. For an append-only trail the order *is* the content.

### API documentation and tokens — done

The plan said "FastAPI already serves OpenAPI at `/docs`". It served it at a
path Caddy does not proxy — `/api/*` goes to the API and everything else to the
UI — so the reference was reachable by nobody. It now lives at
`/api/v1/docs`, under the prefix that is actually routed.

**Tokens** carry a **role**, not a separate scope vocabulary. The product
already has three roles that mean something, and a second orthogonal permission
system beside them is two models that have to agree and eventually do not. What
a token adds is the rest of a credential's life cycle: a name, an owner, an
expiry, a last-used time, and revocation.

Three properties are worth stating because they are what stop a token becoming
a way around the permission system:

- The effective permission is the **lesser** of the token's role and its
  owner's, so demoting a person weakens their tokens without anyone having to
  remember to revoke them.
- A token **cannot mint another token**. A token that could would outlive its
  own revocation.
- Revocation is a **timestamp, not a delete**, because the audit trail refers
  to tokens by id and a trail full of dangling ids is not a trail.

The audit trail now records which credential acted, not only which person:
"admin did this" stops being the whole answer the moment automation exists.

Alongside it, an in-app **API** page with the tokens, a getting-started
tutorial whose curl examples are built from the browser's own origin (so they
are copy-pasteable rather than aspirational), and a link to the reference.

---

## Sequencing

```
F1a  Rule ordering in the reconciler      correctness; unblocks everything below
F1b  NAT and filter management            correctness; makes steering actually work
V1   Vocabulary and menu split            cheap, and every later screen inherits it
U1   Interface dropdowns                  small, removes a whole class of typo
S1   SD-WAN groups + traffic rules        the model change; needs V1's words
D1   Diagnostics                          done
L1   Logs                                 done
A1   API tokens and scopes                done
S2   Load balancing via PCC               done
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
