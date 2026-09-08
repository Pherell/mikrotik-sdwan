/**
 * The diagram that answers "what is this thing?".
 *
 * The empty states were accurate and assumed the answer: "a fabric is one
 * overlay: a transport, a topology, and the sites that take part" is a good
 * sentence only if overlay, transport and topology are words you already have.
 * This shows the pipeline instead, and defines each noun by what it is made
 * from and what it produces.
 */

import { useState } from "react";
import { Link } from "react-router-dom";

const NODES = [
  {
    id: "devices",
    label: "Devices",
    to: "/devices",
    made: "One RouterOS router each.",
    gives: "Uplinks — the internet connections everything else chooses between.",
  },
  {
    id: "network",
    label: "Tunnel network",
    to: "/tunnel-networks",
    made: "Devices you pick, and how they should connect (IPsec, WireGuard, GRE…).",
    gives: "One encrypted network joining those devices.",
  },
  {
    id: "tunnels",
    label: "Tunnels",
    to: "/tunnel-networks",
    made: "Computed, never written. Every uplink pair the network's shape allows.",
    gives: "The individual tunnels, with addresses and keys allocated.",
  },
  {
    id: "rules",
    label: "Traffic rules",
    to: "/traffic-rules",
    made: "Traffic to match, uplinks to prefer, and how bad one must get before moving.",
    gives: "Which path traffic takes, and when it moves.",
  },
  {
    id: "apply",
    label: "Apply",
    to: "/jobs",
    made: "All of the above, rendered to RouterOS config and diffed against the device.",
    gives: "A reviewed change, pushed inside a rollback that fires if it goes wrong.",
  },
];

export function HowItWorks({ defaultOpen = true }: { defaultOpen?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const chosen = NODES.find((n) => n.id === open);

  return (
    <details className="card explainer" open={defaultOpen}>
      <summary>
        <h2>How this fits together</h2>
      </summary>
      <p className="muted" style={{ marginTop: 0 }}>
        Each stage is built from the one before it. Select any of them.
      </p>

      <div className="pipeline">
        {NODES.map((node, i) => (
          <div className="pipeline-node" key={node.id}>
            <button
              type="button"
              className={`pipe${open === node.id ? " selected" : ""}${
                node.id === "tunnels" ? " derived" : ""
              }`}
              onClick={() => setOpen(open === node.id ? null : node.id)}
              aria-pressed={open === node.id}
            >
              {node.label}
            </button>
            {i < NODES.length - 1 && (
              <span className="pipe-arrow" aria-hidden="true">
                →
              </span>
            )}
          </div>
        ))}
      </div>

      {chosen ? (
        <div className="pipe-detail">
          <dl className="kv">
            <dt>Made from</dt>
            <dd>{chosen.made}</dd>
            <dt>Gives you</dt>
            <dd>{chosen.gives}</dd>
          </dl>
          <Link className="navlink" to={chosen.to}>
            Go to {chosen.label} →
          </Link>
        </div>
      ) : (
        <p className="muted pipe-hint">
          <strong>Tunnels are the one thing you do not author.</strong> They are
          worked out from a network's members and shape, which is why there is no
          button to create one.
        </p>
      )}
    </details>
  );
}
