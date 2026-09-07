/**
 * The diagram that answers "fabric — what is this?".
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
    id: "sites",
    label: "Sites",
    to: "/sites",
    made: "A location and its router. Each has one or more uplinks.",
    gives: "The things that need connecting.",
  },
  {
    id: "fabric",
    label: "Fabric",
    to: "/fabrics",
    made: "Sites you pick, a transport (IPsec, WireGuard, GRE…) and a topology.",
    gives: "One overlay network joining those sites.",
  },
  {
    id: "links",
    label: "Links",
    to: "/fabrics",
    made: "Computed, never written. Every uplink pair the topology allows.",
    gives: "The individual tunnels, with addresses and keys allocated.",
  },
  {
    id: "policies",
    label: "Policies",
    to: "/policies",
    made: "Traffic to match, uplinks to prefer, and how bad a link must get.",
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

export function HowItWorks() {
  const [open, setOpen] = useState<string | null>(null);
  const chosen = NODES.find((n) => n.id === open);

  return (
    <div className="card">
      <h2>How this fits together</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Each stage is built from the one before it. Select any of them.
      </p>

      <div className="pipeline">
        {NODES.map((node, i) => (
          <div className="pipeline-node" key={node.id}>
            <button
              type="button"
              className={`pipe${open === node.id ? " selected" : ""}${
                node.id === "links" ? " derived" : ""
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
          <strong>Links are the one you do not author.</strong> They are derived from a
          fabric's members and topology, which is why there is no button to create one.
        </p>
      )}
    </div>
  );
}
