# Plan — the interface

Two complaints started this, and they are different in kind.

**"Make it readable and professional."** That one has a measurable cause. Every
primary button on every page is roughly 67 pixels wide and wraps onto two or
three lines. It is one CSS mistake, repeated 44 times.

**"Fabric — what is this?"** That one does not. The vocabulary is correct and
the pages are complete; nothing tells you what order to do things in or why
these four nouns exist. The interface is a reference manual for someone who
already understands the product.

They need different fixes, so this plan keeps them apart.

---

## Part 1 — What is measurably broken

Measured in a browser at 1440×900 against the real API, not read off a
screenshot.

### The 67-pixel button

```
  control                 width   lines   page
  Check all for drift      67px     3      /
  Apply all (0)            67px     2      /
  All jobs →               24px     3      /
  Import for real          74px     3      /settings
  Dry run                  51px     2      /settings
  New policy               68px     2      /policies
  New profile              70px     2      /policies
  New user                 59px     2      /users
  Add site / New fabric      —      2      /sites, /fabrics
```

Every one sits inside `<div style={{ flex: 0 }}>`.

**`flex: 0` does not mean "do not flex".** It is shorthand for
`flex: 0 1 0%` — grow `0`, **shrink `1`**, basis **`0%`**. The wrapper starts at
zero width and is permitted to stay there. The only thing stopping it from
collapsing entirely is `min-width: auto`, which resolves to the widest
unbreakable word in the label. So a button reading *Check all for drift* comes
to rest at the width of the word *Check*.

The value that was meant is `flex: none` — `0 0 auto`. Grow nothing, **shrink
nothing**, size to content.

There are **44 occurrences across 14 files**. This is not a design problem; it
is one misunderstanding of a shorthand, copy-pasted.

Related, in `styles.css`:

```css
.row > * { flex: 1; }   /* = 1 1 0% */
```

Every direct child of a `.row` is forced into an equal share of the width,
buttons included. That is why form rows put a 40-character text input and a
Save button on the same footing.

### The page is 1100 pixels wide, forever

`main { max-width: 1100px }` regardless of content. At 1440 that wastes 340px;
on the 1920 display these screenshots came from, roughly 820px. The pages that
need width are exactly the ones being squeezed — Sites, Users, Policies and
Jobs are all tables — while the pages that would read better narrow, the login
and the single-column forms, get the same 1100.

### The header does not collapse

`header.topbar` needs **811px of min-content** and contains nothing that wraps,
truncates, or folds into a menu. Above ~900px it is fine — at 1440 it has 629px
of slack, so the top bar is *not* broken on a desktop, and replacing it is a
design decision rather than a bug fix. Below that it degrades with no fallback.

### The type scale is two pixels

`h2` is 16px against 14px body text. A heading 14% larger than its paragraph is
not a heading. Section titles, card titles and body copy are currently
distinguishable only by weight.

### Focus is invisible on anything that is not a text field

`styles.css` defines `:focus` for `input`, `select` and `textarea`. Buttons and
links fall back to the browser default, which on this dark palette is close to
unreadable. The app is keyboard-navigable in principle and untraceable in
practice.

### What is *not* broken — leave it alone

The palette is fine. `--muted` on `--surface` measures **6.4:1**, comfortably
past WCAG AA for body text, and the dark theme is coherent. A redesign that
opens by changing colours would spend its budget in the one place that does not
need it.

---

## Part 2 — Why the product is confusing

None of this is a rendering bug. These are information-architecture problems.

**There is no first run.** A new install opens on six tiles reading zero and
two fleet actions with nothing to act on. The screen carrying the least
information is the one everybody sees first.

**The vocabulary is asserted, not taught.** The empty states are accurate and
they assume the answer:

> *A fabric is one overlay: a transport, a topology, and the sites that take part.*

If you already know what an overlay, a transport and a topology are, that is a
good sentence. If "fabric" is the word you are stuck on, every word explaining
it is also new.

**Nothing states the order.** Sites, then a fabric containing them, then
expanding it into links, then applying, then policies — that sequence is the
entire product and it appears nowhere. The navigation lists seven peers in a
row and implies you may start anywhere.

**Derived objects look authored.** Links are computed from fabric membership
and never written by hand. In the UI they sit in a list looking exactly like
something you create, beside things you do create.

**"Settings" is not settings.** It is intent export, intent import, and the
application-group library. Someone looking for "where do I configure this"
finds a YAML textarea. That is the most misleading label in the app, and it is
the one the complaint named.

**Nothing says what Apply does.** The backup, the armed scheduler, the reboot
if verification fails — the most consequential thing this software does — is
explained in `docs/`, not at the button.

---

## Part 3 — The work

### U1 — The shell: a collapsible sidebar

Replace `header.topbar` with a left rail.

- 240px expanded, 56px collapsed to icons, toggled from its foot
- state in `localStorage`; a per-viewer preference, not shared state
- below 900px it becomes an overlay drawer, closed by default
- grouped, so the order carries meaning instead of listing seven peers:

```
  NETWORK          Overview · Sites · Fabrics · Policies
  OPERATIONS       Jobs · Drift
  LIBRARY          App groups · SLA profiles
  ADMIN            Users · Import & export
```

- account and sign-out move to the rail's foot
- content gets a slim page header: title, one-line description, primary action
  pinned right

**Width becomes per-page, not global.** `--content-wide` (1600px) for tables and
the topology graph, `--content-form` (760px) for single-column forms. Tables
that can breathe are most of the readability complaint.

**Done when:** the rail collapses and remembers it, no page scrolls sideways
between 360px and 2560px, and tables use the width the display actually has.

### U2 — Primitives, so this cannot recur

- replace all 44 `flex: 0` with `flex: none`, and give the button base
  `flex-shrink: 0` and `white-space: nowrap` so a future wrong wrapper cannot
  collapse a label again
- delete `.row > * { flex: 1 }`; make `.row` a plain flex row and let callers
  opt into growth with an explicit `.grow`
- a real type scale — 12 / 13 / 14 / 16 / 20 / 24 with weights, so headings are
  headings
- a 4px spacing scale, replacing ad-hoc inline `marginTop: 16`
- button variants (primary, secondary, ghost, danger), two sizes, and a
  `loading` state — every mutating button currently just sits there
- `:focus-visible` on every interactive element
- one `<PageHeader>`, one `<Card>`, one `<EmptyState>`, one `<Toolbar>`. Cards
  are hand-assembled on every page today, which is why they drift.

**A test that fails on regression.** UI CI runs `tsc` and `vite build`; neither
would catch a 67px button. Add a headless check that renders each route and
asserts no interactive element wraps — the measurement from Part 1, automated.

**Done when:** no control on any page wraps at any width ≥360px, and CI fails
if one does.

### U3 — Make the model legible

**A first-run checklist on Overview**, shown while the fleet is empty, in place
of six zeros:

```
  1  Add a site            a location and the router that serves it
  2  Create a fabric       the overlay that joins your sites
  3  Expand it into links  the controller computes the tunnels for you
  4  Review and apply      you see the exact config before it is pushed
  5  Add policies          optional — which traffic prefers which uplink
```

Each step links to where it is done, ticks when satisfied, and says what it
produces. The six tiles appear once there is a fleet to summarise.

**A "How this works" panel**, permanently reachable, with the pipeline drawn:

```
  Sites ─┐
         ├─► Fabric ─► Links ─► Render ─► Diff ─► Apply ─► Device
  WANs ──┘                                 ▲
                                       Policies
```

Each node explains itself in one sentence and links to its page. This is the
diagram that answers "fabric — what is this?" without requiring the reader to
already know.

**Rewrite empty states to lead with the concrete.** Not *"a fabric is one
overlay: a transport, a topology, and the sites that take part"* but *"A fabric
connects your sites to each other over the internet. Choose how they connect —
IPsec, WireGuard — and which sites take part; the controller works out the
tunnels."* The precise definition stays, one level down.

**Mark derived things as derived.** Links get a "computed from fabric
membership" banner and no create button, so nobody hunts for the one that ought
to be there.

**Say what Apply does, at the Apply button.** One line — takes a backup, arms a
rollback, pushes, verifies, disarms; if verification fails the device restores
itself and reboots — linking to the detail.

**Done when:** someone who has never read `docs/` can add a site, build a
fabric and apply it without leaving the UI to find out what a fabric is.

### U4 — Configuration: one, or many

The complaint asked for both, and the app does one and a half.

**Single, cleaned up.** The site page is a wall of fields. Group it — Identity,
Access, Uplinks, Fabric membership, Safety — with a sticky bar showing the
unsaved-change count, Save and Discard. Nothing is written per keystroke, and
whether something is pending is always visible.

**Bulk, properly.**

- a checkbox column on Sites, Fabrics, Policies and Users, with a selection
  bar: *N selected · Edit · Plan · Apply · Export*
- **Bulk edit** — choose fields, set values, apply to the selection. Only
  fields that make sense across sites: role, tags, drift mode, rollback
  timeout, SLA profile. Never credentials, which are per-device by definition.
- **Bulk plan before bulk apply.** Today "Apply all" pushes to every site with
  credentials, one after another, and you watch results land. It should compute
  every diff first, present them as one reviewable set with a total — *"14
  sites, 3 with changes, 1 error"* — and only then push. Reviewing a
  fleet-wide change after it has happened is not reviewing it.
- **Templates** — define role, fabric membership, SLA and policy assignments
  once, stamp onto many. This is the same object `plan-v2.md` M8 needs for
  enrolment; build it here and provisioning inherits it.

**Implementation note, so this is not mistaken for a bigger job than it is.**
`PATCH /api/v1/sites/{id}` already exists, so bulk edit is N sequential
requests from the client — the pattern `FleetActions` already uses for apply,
with the same progress list and stop button. A dedicated bulk endpoint is a
later optimisation, not a prerequisite. Bulk *plan* needs no new endpoint
either: `POST /sites/{id}/plan` is already non-mutating.

**And rename the page.** "Settings" becomes **Import & export** under ADMIN;
the app-group library moves to LIBRARY beside SLA profiles. "Settings" either
disappears as a label or comes to mean actual preferences.

**Done when:** twenty sites can have their drift mode changed in one action,
with one confirmation and one reviewable summary.

### U5 — Feedback

- toasts on every mutation, success and failure, the failure quoting the API's
  message rather than a generic string
- skeletons instead of the word "Loading…"
- inline validation before submit, not a 422 rendered as a red box
- a persistent indicator whenever any site has a rollback armed. It is the most
  urgent state in the system and it currently appears only on Overview.

### U6 — Responsive and accessible

- works from 360px up; tables become stacked cards on narrow screens
- `:focus-visible` throughout (from U2), skip-to-content, landmark roles
- keyboard: `g s` / `g f` jumps, `/` to focus search, Escape closes drawers
- honour `prefers-reduced-motion` on the rail transition

---

## Sequencing

```
  U2  Primitives          ← the 44-line fix is an afternoon and repairs every screenshot
  U1  Shell / sidebar     ← the structural change, and it needs U2's primitives
  U3  Model legibility    ← the actual complaint; cheap once the shell has room
  U4  Single + bulk       ← the largest, and the only one needing new UI concepts
  U5  Feedback
  U6  Responsive / a11y
```

U2 before U1 deliberately. The button fix is small, self-contained, and repairs
every screenshot that prompted this. Shipping it first means the sidebar work
happens on a codebase that is no longer visibly broken — and if the sidebar
takes a while, the interface is better in the meantime.

## What this does not fix

- **The topology graph.** `TopologyGraph.tsx` is 146 hand-rolled lines. Adequate
  for a handful of sites; it will not survive fifty. Replacing it with a real
  graph library is its own piece of work and is not in this plan.
- **Telemetry has nothing to show.** U3 makes the model legible; it cannot make
  the dashboard *useful*, because there is still no time-series data behind it.
  That is `plan-v2.md` M7, and the dashboard stays thin until it lands.
- **No design-system dependency is being added.** The existing CSS is 334 lines
  and coherent. This plan extends it rather than importing Tailwind or a
  component kit, because the problem is 44 wrong values and a missing scale,
  not a missing framework.
