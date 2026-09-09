/**
 * The manual, in the product.
 *
 * The empty dashboard already had onboarding — a five-step checklist and a
 * pipeline diagram — but both disappear the moment you have one device, which
 * is roughly when the questions start. And neither covered the things people
 * actually get stuck on: that a tunnel needs one end able to accept it, that
 * the address pool cannot be changed later, or what to do when a device's
 * certificate changes.
 *
 * So this is permanent, reachable from the sidebar, and deeper than a
 * checklist. It reuses the pipeline diagram rather than describing it a second
 * time, because a second description is a second thing to keep true.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import { HowItWorks } from "../components/HowItWorks";
import { PageHeader } from "../components/PageHeader";

type Section = "setup" | "builds" | "words" | "steering" | "trouble";

const SECTIONS: { id: Section; label: string }[] = [
  { id: "setup", label: "Set up a tunnel network" },
  { id: "builds", label: "What it builds on the router" },
  { id: "words", label: "What the words mean" },
  { id: "steering", label: "Steering traffic" },
  { id: "trouble", label: "When something is wrong" },
];

export function GuidePage() {
  const [section, setSection] = useState<Section>("setup");

  return (
    <>
      <PageHeader
        title="Guide"
        description="How this controller works, in the order you meet it."
      />

      <div className="tabs">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            className={`tab${section === s.id ? " active" : ""}`}
            onClick={() => setSection(s.id)}
          >
            {s.label}
          </button>
        ))}
      </div>

      {section === "setup" && <Setup />}
      {section === "builds" && <Builds />}
      {section === "words" && <Words />}
      {section === "steering" && <Steering />}
      {section === "trouble" && <Trouble />}
    </>
  );
}

function Step({
  n,
  title,
  children,
}: {
  n: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="card">
      <h2>
        <span className="guide-step">{n}</span> {title}
      </h2>
      {children}
    </div>
  );
}

// -- setting one up ---------------------------------------------------------

function Setup() {
  return (
    <>
      <HowItWorks defaultOpen={false} />

      <Step n={1} title="Add your devices">
        <p>
          <Link to="/devices">Devices</Link> → <strong>Add device</strong>. It
          needs the management address and an account on the router that can
          read and write configuration.
        </p>
        <p>
          Then <strong>Probe</strong> it. That reads the RouterOS version and
          records the device's identity, and the later steps need the version to
          know which transports it can run.
        </p>
      </Step>

      <Step n={2} title="Confirm each device's uplinks">
        <p>
          <Link to="/uplinks">Uplinks</Link>, or the device's own page. This is
          the step that is easiest to skip and the one that decides whether
          anything can be built at all. Two fields do the deciding:
        </p>
        <ul>
          <li>
            <strong>Public IP</strong> — leave it empty if this uplink has no
            address the far side could reach.
          </li>
          <li>
            <strong>NAT behind</strong> — tick it when the router sits behind
            someone else's NAT.
          </li>
        </ul>
        <p>
          Either one makes the uplink <strong>dial-out only</strong>: it can
          start a tunnel but cannot accept one. The Uplinks page says which each
          uplink is, in those words.
        </p>
      </Step>

      <Step n={3} title="Create the tunnel network">
        <p>
          <Link to="/tunnel-networks">Tunnel networks</Link> →{" "}
          <strong>New</strong>.
        </p>
        <table className="stack">
          <thead>
            <tr>
              <th>Field</th>
              <th>What it decides</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td data-label="Field">Transport</td>
              <td data-label="What it decides">
                How each tunnel is built. <code>ipsec_gre</code> is the default
                and encrypts. <code>gre</code> and <code>ipip</code> do not
                encrypt at all. <code>wireguard</code> needs RouterOS 7 at both
                ends.
              </td>
            </tr>
            <tr>
              <td data-label="Field">Topology</td>
              <td data-label="What it decides">
                Which pairs of uplinks get a tunnel — see below.
              </td>
            </tr>
            <tr>
              <td data-label="Field">Tunnel pool</td>
              <td data-label="What it decides">
                Every tunnel takes a <code>/31</code> out of it.{" "}
                <strong>Cannot be changed once tunnels exist</strong>, because
                renumbering drops every one of them. Pick a range nothing else
                uses.
              </td>
            </tr>
            <tr>
              <td data-label="Field">AS number</td>
              <td data-label="What it decides">
                Used by the routing that carries your prefixes over the tunnels.
                The default is fine unless you already run BGP.
              </td>
            </tr>
            <tr>
              <td data-label="Field">Advanced</td>
              <td data-label="What it decides">
                Ciphers, key exchange group, lifetimes. Only touch these if a
                security standard tells you to; every one of them must match at
                both ends, and the controller keeps them matching for the ends
                it configures.
              </td>
            </tr>
          </tbody>
        </table>

        <h3>Topology</h3>
        <ul>
          <li>
            <strong>Hub and spoke</strong> — a tunnel for every hub-to-hub and
            hub-to-spoke pair. Spokes never link to each other directly.
          </li>
          <li>
            <strong>Hub and spoke + dynamic mesh</strong> — the same permanent
            set, plus spoke-to-spoke tunnels built on demand when traffic wants
            one.
          </li>
          <li>
            <strong>Full mesh</strong> — every pair. Count first: ten devices is
            forty-five tunnels.
          </li>
        </ul>
      </Step>

      <Step n={4} title="Add members, and give at least one of them the hub role">
        <p>
          Open the network and add devices to it. Under hub and spoke, a pair
          with no hub in it gets no tunnel — which is how you end up with a
          network full of spokes and zero tunnels.
        </p>
        <div className="warn">
          A tunnel needs at least one end that can <em>accept</em> it. Your hub
          therefore wants a real public IP with "NAT behind" unticked. If every
          device is behind NAT, no tunnel can be built between any of them.
        </div>
      </Step>

      <Step n={5} title="Expand, then apply">
        <p>
          <strong>Expand</strong> works out the tunnels. It writes nothing to any
          device. Read what it returns — especially <strong>skipped</strong>,
          which names each pair it could not link and why.
        </p>
        <p>
          Then apply each device: its page → <strong>Plan changes</strong> to see
          the exact difference, then <strong>Apply</strong>. Apply saves a backup
          on the router, schedules it to restore that backup shortly, pushes the
          change, then reconnects to confirm management still works and cancels
          the restore. If it cannot reconnect, the router puts itself back and
          reboots.
        </p>
        <p className="muted">
          Nothing reaches a device until you apply it. Creating, expanding and
          editing are all controller-side.
        </p>
      </Step>
    </>
  );
}

// -- vocabulary -------------------------------------------------------------

const WORDS: { word: string; is: string; isNot: string; to?: string }[] = [
  {
    word: "Device",
    is: "One RouterOS router. It has credentials, a status, and uplinks.",
    isNot: "Not a site or a location. One device, one router — if a location has two routers, that is two devices.",
    to: "/devices",
  },
  {
    word: "Uplink",
    is: "One internet connection on one device. Tunnels are built per uplink, not per device.",
    isNot: "Not an interface. An interface becomes an uplink when you tell the controller it is one.",
    to: "/uplinks",
  },
  {
    word: "Tunnel network",
    is: "A set of devices, a way of connecting them, and a shape. The tunnels are computed from it.",
    isNot: "Not a tunnel, and not a VPN server. You never write a tunnel by hand here.",
    to: "/tunnel-networks",
  },
  {
    word: "SD-WAN group",
    is: "A named set of uplinks, in the order traffic should prefer them, with the standard each must meet to stay in use.",
    isNot: "Not a match rule. It says which way traffic goes, never which traffic.",
    to: "/sdwan-groups",
  },
  {
    word: "Traffic rule",
    is: "Which traffic, and which SD-WAN group carries it.",
    isNot: "Not a firewall rule. It steers; it does not permit or deny.",
    to: "/traffic-rules",
  },
  {
    word: "Health standard",
    is: "Loss, latency and jitter an uplink must stay within to keep carrying traffic that asked for it.",
    isNot: "Not a guarantee. It is a threshold that moves traffic when crossed.",
  },
  {
    word: "Apply",
    is: "Push this device's configuration, inside a rollback that fires if management breaks.",
    isNot: "Not a save. Everything is saved as you edit; apply is when a router changes.",
  },
  {
    word: "Drift",
    is: "The device no longer matches what the controller intends, because somebody changed it by hand.",
    isNot: "Not an error. It is a fact, and you decide whether to re-apply over it.",
  },
];

function Words() {
  return (
    <div className="card">
      <h2>What the words mean</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Each of these has a narrow meaning here, and the second column is the
        one worth reading — most confusion comes from a word meaning something
        slightly different elsewhere.
      </p>
      <table className="stack">
        <thead>
          <tr>
            <th>Word</th>
            <th>What it is</th>
            <th>What it is not</th>
          </tr>
        </thead>
        <tbody>
          {WORDS.map((w) => (
            <tr key={w.word}>
              <td data-label="Word">
                {w.to ? (
                  <Link to={w.to}>
                    <strong>{w.word}</strong>
                  </Link>
                ) : (
                  <strong>{w.word}</strong>
                )}
              </td>
              <td data-label="What it is">{w.is}</td>
              <td data-label="What it is not" className="muted">
                {w.isNot}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// -- steering ---------------------------------------------------------------

function Steering() {
  return (
    <>
      <div className="card">
        <h2>Two halves, on purpose</h2>
        <p>
          Deciding where traffic goes was one confusing form. It is now two
          objects, because it was always two decisions:
        </p>
        <ul>
          <li>
            <Link to="/sdwan-groups">
              <strong>SD-WAN group</strong>
            </Link>{" "}
            — <em>which way</em>. The uplinks, their order or their shares, and
            the health standard they must meet.
          </li>
          <li>
            <Link to="/traffic-rules">
              <strong>Traffic rule</strong>
            </Link>{" "}
            — <em>which traffic</em>. Addresses, ports, protocol — and the group
            that carries it.
          </li>
        </ul>
        <p className="muted">
          Build the group first. Several rules can point at one group, which is
          the whole reason it is separate: "the voice path" becomes a thing you
          name once instead of a shape you retype and hope matches.
        </p>
      </div>

      <div className="card">
        <h2>One at a time, or all at once</h2>
        <p>A group does one of two things with its uplinks.</p>
        <table className="stack">
          <thead>
            <tr>
              <th></th>
              <th>What happens</th>
              <th>Use it for</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td data-label="">
                <strong>One at a time</strong>
              </td>
              <td data-label="What happens">
                Traffic takes the first uplink in the list that is present and
                meeting the standard. When it degrades, traffic moves to the
                next; when it recovers, traffic moves back.
              </td>
              <td data-label="Use it for">
                Anything where one path is genuinely better — voice over a
                leased line, with broadband behind it.
              </td>
            </tr>
            <tr>
              <td data-label="">
                <strong>All at once</strong>
              </td>
              <td data-label="What happens">
                Each new connection is assigned an uplink in proportion to the
                shares you set. A failing uplink stops receiving new ones.
              </td>
              <td data-label="Use it for">
                Bulk traffic across two similar links.
              </td>
            </tr>
          </tbody>
        </table>
        <div className="warn">
          "All at once" spreads <strong>connections, not packets</strong>. One
          download still uses one uplink and will not go faster. That is true of
          RouterOS and of every other router that balances this way — two 100M
          links do not make a 200M link.
        </div>
      </div>

      <div className="card">
        <h2>The order of rules matters</h2>
        <p>
          Rules are evaluated by priority and the first match wins, so a broad
          rule above a narrow one hides it. Give the specific rules the lower
          priority numbers.
        </p>
      </div>
    </>
  );
}

// -- troubleshooting --------------------------------------------------------

const PROBLEMS: { q: string; a: React.ReactNode }[] = [
  {
    q: "Expand created no tunnels",
    a: (
      <>
        Under hub and spoke, a pair with no hub in it gets no tunnel. Check that
        at least one member has the hub role. Then read the{" "}
        <strong>skipped</strong> list — it names each pair and the reason,
        usually that neither end can accept a tunnel.
      </>
    ),
  },
  {
    q: "Neither end is publicly reachable",
    a: (
      <>
        Both uplinks are dial-out only, so neither can accept the tunnel. Give
        one end a public IP with "NAT behind" unticked and make it the hub, or
        link those two devices through a hub instead of to each other.
      </>
    ),
  },
  {
    q: "The tunnel is up but nothing routes",
    a: (
      <>
        <Link to="/diagnostics">Diagnostics</Link> → Tunnels on this device. It
        shows the interface, the encryption and the routing separately, so you
        can see which of the three stopped. "Encrypted" with "no routes" means
        the tunnel is fine and the routing session is not.
      </>
    ),
  },
  {
    q: "Apply refused to run",
    a: (
      <>
        The controller refuses to apply when it could not read a menu it
        manages, because applying then would look like a request to delete
        everything in it. The message names the menus. Usually the device is
        missing a package, or the account cannot read that menu.
      </>
    ),
  },
  {
    q: "The certificate does not match the one pinned",
    a: (
      <>
        The controller records a device's identity on first contact and refuses
        anything different, because a changed certificate and an interceptor
        look identical. If you know the device was reset, rebuilt or re-keyed,
        open it and use <strong>Forget this identity</strong>. If you cannot
        explain the change, treat that device's credentials as compromised.
      </>
    ),
  },
  {
    q: "I changed something on the router and it went back",
    a: (
      <>
        That is drift, and reverting it is the reconciler's job. Configuration
        this controller manages belongs in the controller — change it here and
        apply. The <Link to="/devices">device page</Link> can check for drift and
        show you exactly what differs.
      </>
    ),
  },
  {
    q: "Why is this device unreachable?",
    a: (
      <>
        <Link to="/diagnostics">Diagnostics</Link> pings and traces from a
        device, and you can send those out of one chosen uplink — which is the
        only way to tell "the internet is down" from "this one uplink is down".{" "}
        <Link to="/logs">Logs</Link> → Device log shows what the router itself
        thinks went wrong, which is the only place a rejected IPsec proposal
        appears.
      </>
    ),
  },
];

function Trouble() {
  return (
    <>
      <div className="card">
        <h2>When something is wrong</h2>
        {/* Not .kv: that is a 160px/1fr grid, and wrapping each pair in a div
            to keep them together would put the whole pair in the narrow
            column. These stack instead. */}
        <dl>
          {PROBLEMS.map((p) => (
            <div key={p.q} className="guide-qa">
              <dt>{p.q}</dt>
              <dd>{p.a}</dd>
            </div>
          ))}
        </dl>
      </div>

      <div className="card">
        <h2>Where to look</h2>
        <ul>
          <li>
            <Link to="/diagnostics">Diagnostics</Link> — is this tunnel up, and
            can this device reach that address.
          </li>
          <li>
            <Link to="/logs">Logs</Link> — what the router said, and who changed
            what in the controller.
          </li>
          <li>
            <Link to="/jobs">Jobs</Link> — what happened during a particular
            apply, including the exact configuration pushed.
          </li>
        </ul>
      </div>
    </>
  );
}

// -- what lands on the router -----------------------------------------------

/**
 * The RouterOS side, menu by menu.
 *
 * "Why is there nothing in my IPsec Policies tab" is only answerable if you
 * know what was supposed to be there. Nothing else in the product says what a
 * tunnel actually *is* once it reaches a router, so this is the page you open
 * next to WinBox.
 *
 * The rows are the real ones, taken from a rendered plan rather than written
 * from memory.
 */
const BUILDS: { menu: string; row: string; why: string }[] = [
  {
    menu: "/ip/ipsec/profile",
    row: "prof-<tunnel>",
    why: "Phase 1: how the two routers agree on keys — cipher, hash, DH group, lifetime, dead-peer detection.",
  },
  {
    menu: "/ip/ipsec/proposal",
    row: "prop-<tunnel>",
    why: "Phase 2: how the traffic itself is encrypted, and how often the key is replaced.",
  },
  {
    menu: "/ip/ipsec/peer",
    row: "peer-<tunnel>",
    why: "The far router's public address, and which of the two dials. This is the destination — it comes from the far uplink's Public IP.",
  },
  {
    menu: "/ip/ipsec/identity",
    row: "peer-<tunnel>",
    why: "The pre-shared key. Generated per tunnel, stored encrypted, never shown in a plan or a log.",
  },
  {
    menu: "/ip/ipsec/policy",
    row: "<local public>/32 → <far public>/32, gre",
    why: "What to encrypt: the GRE between these two public addresses, and nothing else. If this tab holds only the built-in template row, no tunnel has been pushed here.",
  },
  {
    menu: "/interface/gre",
    row: "gre-<tunnel>",
    why: "The tunnel itself, carrying your traffic inside the encryption above. Keepalives take it down when the far end vanishes.",
  },
  {
    menu: "/ip/address",
    row: "the /31 on gre-<tunnel>",
    why: "One address at each end of the tunnel, out of the network's pool. This is the inside; the peer above is the outside.",
  },
  {
    menu: "/routing/bgp/template · connection · network",
    row: "sdwan-<network> · bgp-<tunnel>",
    why: "How each end learns the other's networks. Your Local prefixes are advertised here — a device with none builds a working tunnel that no traffic enters.",
  },
  {
    menu: "/ip/firewall/filter",
    row: "three accepts per tunnel",
    why: "UDP 500 and 4500, ESP, and GRE, from the far public address only. Without these a default-drop input chain blocks the tunnel from ever establishing.",
  },
  {
    menu: "/ip/firewall/nat",
    row: "bypass, then masquerade",
    why: "The bypass stops tunnel traffic being masqueraded, which would break it. It must sit above the masquerade rule, and the controller keeps it there.",
  },
  {
    menu: "/interface/bridge",
    row: "lo-sdwan",
    why: "A loopback, once per device. It gives routing a stable identity that does not move when an uplink flaps.",
  },
  {
    menu: "/ip/firewall/address-list",
    row: "sdwan-local-<device>",
    why: "This device's own prefixes, for traffic rules to match against.",
  },
];

function Builds() {
  return (
    <>
      <div className="card">
        <h2>How a tunnel gets made</h2>
        <ol>
          <li>
            <strong>You declare</strong> devices, their uplinks, and a network
            with a transport and a shape. Nothing is computed yet.
          </li>
          <li>
            <strong>Expand</strong> pairs the uplinks the shape calls for, takes
            a <code>/31</code> from the pool for each pair, generates that
            tunnel's key, and decides which end dials. Still nothing on a router
            — this all lives in the controller.
          </li>
          <li>
            <strong>Plan</strong> turns that into RouterOS rows, reads what the
            device currently has, and shows you the difference. Also writes
            nothing.
          </li>
          <li>
            <strong>Apply</strong> takes a backup, schedules the router to
            restore it shortly, pushes the difference, then reconnects to prove
            management still works and cancels the restore. If it cannot
            reconnect, the router puts itself back.
          </li>
        </ol>
        <p className="muted">
          Both ends need applying. A tunnel configured on one side only never
          comes up.
        </p>
      </div>

      <div className="card">
        <h2>What appears on the router</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          For the default <code>ipsec_gre</code> transport — GRE carrying your
          traffic, IPsec encrypting the GRE. Open this next to WinBox.
        </p>
        <table className="stack">
          <thead>
            <tr>
              <th>Menu</th>
              <th>Row</th>
              <th>What it is for</th>
            </tr>
          </thead>
          <tbody>
            {BUILDS.map((b) => (
              <tr key={b.menu}>
                <td data-label="Menu">
                  <code>{b.menu}</code>
                </td>
                <td data-label="Row" className="muted">
                  <code>{b.row}</code>
                </td>
                <td data-label="What it is for">{b.why}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Why it will not touch your own configuration</h2>
        <p>
          Every row above carries a comment beginning{" "}
          <code>sdwan:</code>. That comment is how the controller knows what it
          owns.
        </p>
        <ul>
          <li>
            Rows with that comment are managed: created, corrected, and removed
            when they are no longer wanted.
          </li>
          <li>
            Rows without it are left alone entirely. Your existing firewall,
            addresses and routes are not read as things to delete.
          </li>
        </ul>
        <p className="muted">
          The corollary is that editing a managed row by hand does not stick —
          the next apply puts it back. Change it in the controller instead.
        </p>
      </div>
    </>
  );
}
