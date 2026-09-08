/**
 * What a fresh install opens on, in place of six tiles reading zero.
 *
 * The order here *is* the product, and it appeared nowhere in the interface:
 * sites, then a fabric over them, then links computed from that, then an apply
 * you review first, then optional steering. Someone who does not know what a
 * fabric is cannot learn it from a navigation bar listing seven equal peers.
 */

import { Link } from "react-router-dom";

import type { Fabric, Job, Site } from "../lib/api";

function Step({
  index,
  done,
  title,
  what,
  to,
  cta,
}: {
  index: number;
  done: boolean;
  title: string;
  what: string;
  to: string;
  cta: string;
}) {
  return (
    <li className={`step${done ? " done" : ""}`}>
      <span className="step-mark" aria-hidden="true">
        {done ? "✓" : index}
      </span>
      <div className="step-body">
        <div className="step-title">{title}</div>
        <div className="step-what muted">{what}</div>
      </div>
      <div className="no-grow">
        <Link className="navlink step-cta" to={to}>
          {done ? "Review" : cta} →
        </Link>
      </div>
    </li>
  );
}

export function GettingStarted({
  sites,
  fabrics,
  jobs,
}: {
  sites: Site[];
  fabrics: Fabric[];
  jobs: Job[];
}) {
  const hasSite = sites.length > 0;
  const hasFabric = fabrics.length > 0;
  const hasLinks = fabrics.some((f) => f.link_count > 0);
  const hasApplied = jobs.some((j) => j.kind === "apply" && j.state === "succeeded");

  return (
    <div className="card getting-started">
      <h2>Get started</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Five steps, in this order. Each one produces what the next one needs.
      </p>
      <ol className="steps">
        <Step
          index={1}
          done={hasSite}
          title="Add a device"
          what="One RouterOS router. The controller connects to it, reads its interfaces, and works out which of them reach the internet — those become its uplinks."
          to="/devices"
          cta="Add a device"
        />
        <Step
          index={2}
          done={hasFabric}
          title="Create a tunnel network"
          what="An encrypted network joining your devices to each other over whatever internet they have. You choose how they connect — IPsec, WireGuard — and which devices take part."
          to="/tunnel-networks"
          cta="Create one"
        />
        <Step
          index={3}
          done={hasLinks}
          title="Build the tunnels"
          what="One tunnel joins two uplinks. You never write these: the controller works out every pair from the network's members and shape."
          to="/tunnel-networks"
          cta="Build them"
        />
        <Step
          index={4}
          done={hasApplied}
          title="Review and apply"
          what="You see the exact configuration before it is pushed. The device takes a backup and arms a rollback first, so a push that costs you management access undoes itself."
          to="/devices"
          cta="Open a device"
        />
        <Step
          index={5}
          done={false}
          title="Add traffic rules"
          what="Optional. Which traffic prefers which uplink, and when to move it. Start from a preset — voice, SaaS, bulk — rather than from thresholds in milliseconds."
          to="/traffic-rules"
          cta="Add a rule"
        />
      </ol>
    </div>
  );
}
