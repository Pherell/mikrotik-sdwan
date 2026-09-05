# Architecture notes

The reasoning behind decisions that are not obvious from the code, and the bugs
that shaped them.

## RouterOS realities the design is built around

**There is no VTI.** "Route-based IPsec" on RouterOS means a GRE (or IPIP)
tunnel plus a transport-mode IPsec policy matching `protocol=gre` between the two
public addresses. `app/transports/ipsec_gre.py` renders both as one unit.

**Every REST value is a string.** `"1400"`, `"true"`. Compare intent against that
naively and the reconciler pushes identical config forever.
`app/drivers/coerce.py` is the only module allowed to reason about RouterOS value
encoding: `coerce()` on read, `canonical()` for diffing and writing. The differ
compares `canonical(intent)` to `canonical(device)`, so the two sides cannot
disagree about representation alone.

**`yes`/`no` are not booleans.** They are members of RouterOS enums — most
importantly `generate-policy` on an IPsec identity, whose values are
`no | port-override | port-strict`. An early version of `coerce` mapped `"no"` to
`False`; canonicalising that back produced `"false"`, and the property diffed
dirty on every run. Only `true`/`false` are booleans.

**Requests are cut off at 60 seconds.** Applies are chunked so a timeout is ours,
not the router's.

**Interface names are capped at 31 characters.** Link slugs are truncated hard
and asserted in tests, because the prefix (`gre-`, `wg-`, `vxl-`) eats into it.

**IKE does not take AEAD cipher names.** A fabric on `aes-256-gcm` still
negotiates phase 1 as `aes-256`, and a proposal using GCM must omit
`auth-algorithms` entirely.

## Ownership, and why it makes the controller safe

Every managed row carries `comment="sdwan:<scope>:<name>"`. The reconciler only
adds, changes or removes rows whose comment starts with the section's scope.
Configuration a human wrote on the same device is invisible to it.

Two consequences that took a bug each to find:

**Sections must be merged per menu.** Several renderers write to `/ip/address`.
Diffed separately, rows from a *deleted* link match no section at all and the
tunnel runs forever. `reconcile/merge.py` unions the items and widens the
ownership tag to the shared scope, and `render_device` always emits an empty
section for every menu any transport can write — so there is always something to
sweep with.

**Ownership scope must not include a name that can change.** Site baseline rows
are scoped `sdwan:site:`, not `sdwan:site:<name>`: a device belongs to exactly one
site, and scoping by name would orphan every row the moment someone renames it.

**Two renderers claiming the same row is an error, not a merge.** The guard found
a genuine conflict: both the fabric and the policy renderer wrote `/tool/netwatch`
keyed on host, and the device would have flapped between two intents on alternate
applies. Netwatch now belongs to policy alone — link liveness is already covered
by GRE keepalives and the BGP hold timer, while netwatch exists for the different
job of spotting a path that is up but degraded.

## Safe apply

1. Back up the device.
2. Add a `/system/scheduler` entry restoring that backup after N seconds. The
   backup predates the scheduler, so restoring it also removes the scheduler —
   there is no second firing to clean up.
3. Push.
4. Reconnect on a **fresh** connection and read `/system/resource`. An
   established socket proves nothing about a firewall rule that just changed.
5. Only then disarm.

If step 3 costs management access, step 5 never runs and the router restores
itself. The error message leads with that fact even when the push also failed —
it is what the operator needs first.

A menu that cannot be read is never treated as empty for a section with items to
write; that would diff as "delete everything managed in it". An *empty* cleanup
section for an absent menu is skipped quietly, because there is nothing there.

## Steering

Preference order becomes route `distance`. Netwatch scripts set an **absolute**
distance, not a relative penalty: an earlier version added to the current value,
so two down events compounded and the path never recovered its preference.

Demotion lands below every other preference but above the `any` fallback at 250,
so a fully degraded site still forwards.

A policy naming no uplink that exists at a site is skipped there rather than
pushed. Marking traffic into an empty routing table blackholes it, which is far
worse than leaving it on the main table.

## Enums from the database are plain strings

`SiteRole` is a `StrEnum`. A value loaded from SQLAlchemy is a `str`, so
`role is SiteRole.hub` is `False` even though `==` is `True`. That cost one silent
"expansion produced zero links" bug. Compare enums by value.
