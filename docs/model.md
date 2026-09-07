# The model, and where it is wrong

Four questions came from someone using the interface, which makes them evidence
rather than opinion:

> why sites instead of device, and why there hub and spoke why fabric so
> complicated and policies is so confusing to setup

Three of the four are fair. This is the honest accounting, and what to do about
each.

---

## "Why sites instead of devices?"

**The vocabulary is industry-standard and currently pointless.**

Every commercial SD-WAN names locations rather than boxes — Sophos has
firewalls at sites, Palo Alto has branches and hubs, Cisco has sites carrying
TLOCs, Aruba has branches. The reason is that a location is what the overlay
connects: two routers in the same building are one place on the map, and a
router replaced under RMA is the same site with different hardware.

**But this controller enforces one device per site.** There is no HA pair, no
second router at a hub, no device inventory separate from the site row. So the
abstraction is a bet on a future that has not arrived, and today it makes you
name a thing ("oslo") that is already named — the device has an identity, a
board name and a management address.

**What to do:** keep the model, fix the presentation. The list should lead with
what the device *is* — identity, board, address — rather than a name you had to
invent, and the word "site" should stop appearing before it means anything. If
multi-device sites ever land, the model is already right.

---

## "Why hub and spoke?"

**This one is not a design flaw, it is arithmetic.**

A full mesh of N sites needs N(N−1)/2 tunnels per uplink pair. Twenty
dual-homed sites is 760 tunnels, each with its own IPsec SA, GRE interface,
address and BGP session. RouterOS will not carry that and neither will anyone
maintaining it.

Hubs also solve a problem that has nothing to do with scale: a site behind
CGNAT cannot accept an inbound tunnel. It can only dial out. Two such sites can
never build a direct tunnel to each other, so something with a reachable
address has to sit between them. That is what a hub is.

Every product listed above does the same thing under different names.

**What is wrong is when you are asked.** Topology is the second question the
fabric form asks, before you have any sites, when you have no way to answer it.

**What to do:** default to `hub_spoke_dynamic` and infer the hubs — a site with
a reachable public IP can be one, a CGNAT site cannot. Move the choice under
Advanced, where someone who genuinely wants full mesh can find it.

---

## "Why is fabric so complicated?"

**Because it asks nine questions and seven of them have correct answers already.**

A fabric today wants: name, transport, transport parameters, topology, IP pool,
loopback pool, ASN, MTU, and members. Of those:

| Field | Honest assessment |
|---|---|
| name, members | Real. You must answer these. |
| transport | Real, but `ipsec_gre` is right for almost everyone. |
| topology | Inferable — see above. |
| ip_pool, loopback_pool | Implementation detail. The controller allocates /31s out of them and nobody looks again. Any unused RFC1918 range works. |
| asn | Implementation detail. It is iBGP inside one overlay; the number is arbitrary and never leaves. |
| mtu | Derived from the transport's overhead. Exposing it invites someone to set 1500 and blackhole large packets. |

So creating a fabric should be: **a name, and which sites take part.**
Everything else defaulted, computed, and available under Advanced for the
person who has a reason.

That is almost entirely a UI change. The model is fine; the form is the problem.

---

## "Policies are confusing to set up"

**Agreed, and there is a missing object.**

A policy currently carries the match (prefixes, ports, DSCP, app group), the
ordered `prefer_tags`, an SLA profile, and a fallback. Five concepts in one
form, and the interesting half — *which links, in what order, how healthy do
they have to be* — is retyped for every policy.

Compare Sophos, which the question pointed at. It has two objects:

- an **SD-WAN profile**: up to eight gateways, each with a **weight**, a
  routing **strategy**, an **SLA**, and health-check targets;
- an **SD-WAN route**: match some traffic, send it to a profile.

The hard part is named once and reused. That is the piece missing here.

### The proposal: uplink groups

```
UplinkGroup
    name              "internet-balanced", "voice-primary"
    members[]         { uplink tag or WAN name, weight, order }   -- max 8
    strategy          failover | load_balance
    sla_profile_id    what "healthy enough" means for this group
```

A policy then becomes **match → group**, and the presets shipped in the policy
form become presets for the *group* instead, which is where they belong.

### How each strategy maps to RouterOS — including what does not

**`failover`** is what exists today and works: one routing table per group,
routes to each member ordered by distance, `check-gateway=ping`, and netwatch
demoting a member whose SLA is breached. Weights are ignored, which must be
said in the UI rather than silently tolerated.

**`load_balance`** does not exist and is not free. Two ways on RouterOS:

- **ECMP** — one route, several gateways. RouterOS balances per source-and-
  destination pair. Weighting is only possible by listing a gateway more than
  once, which gives you ratios like 2:1 and nothing finer.
- **PCC** — `per-connection-classifier=both-addresses:N/M` in mangle, marking
  connections into buckets and routing each bucket. This is the RouterOS-
  idiomatic answer, gives real ratios, and keeps a connection on one link. It
  is also more rules, and it interacts with the mangle marking policies already
  emit — which is exactly the kind of overlap the section-merging code exists
  to catch, and exactly why this needs designing rather than adding.

**The honest ceiling:** on RouterOS, load balancing distributes *connections*,
not packets. A single download does not get two links. Sophos's weighted
round-robin assigns sessions too, so this is not a MikroTik limitation being
hidden — but nobody should choose `load_balance` expecting one flow to go
faster.

**Sequencing:** the group object and `failover` are a straightforward
refactor of what already works. `load_balance` via PCC is a milestone of its
own and should not be bundled with it.

---

## Telemetry

Device health — CPU, memory, storage, uptime — now reads live from
`/system/resource` on the site page. That is one call and no storage.

What it is not: history, thresholds, or alerting. Those need a retention model
and a rollup strategy, and they are `plan-v2.md` M7. The distinction matters
because a chart implies you can look backwards, and right now you cannot.

---

## Summary of what should change

| | Change | Size |
|---|---|---|
| Sites | Lead with device identity; drop the invented name from the primary column | Small, UI |
| Fabric | Two questions, not nine; infer topology, default the pools and ASN | Medium, UI |
| Policies | Introduce **uplink groups**; policy becomes match → group | Medium, model + UI |
| Load balancing | PCC-based weighted balancing | Large, own milestone |
| Telemetry | Live health shipped; time series remains M7 | — |

The pattern across all four: the model is mostly right and the interface asks
too many questions in the wrong order. That is a better problem to have than
the reverse, and it is the one this list fixes.
