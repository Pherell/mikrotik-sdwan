import type { Fabric, FabricLink, Site } from "../lib/api";

/**
 * The fabric as a picture: hubs in the middle, spokes on a ring around them,
 * one line per tunnel.
 *
 * Inline SVG rather than a graph library — the layout is a circle, and pulling
 * in a renderer to draw one would cost more than it explains.
 */
export function TopologyGraph({
  fabric,
  links,
  sites,
}: {
  fabric: Fabric;
  links: FabricLink[];
  sites: Site[];
}) {
  const width = 640;
  const height = 420;
  const cx = width / 2;
  const cy = height / 2;

  const byId = new Map(sites.map((s) => [s.id, s]));
  const wanOwner = new Map<string, string>();
  for (const site of sites) {
    for (const wan of site.wans) wanOwner.set(wan.id, site.id);
  }

  const memberSites = fabric.members
    .map((m) => byId.get(m.site_id))
    .filter((s): s is Site => Boolean(s));

  const hubs = memberSites.filter((s) => s.role === "hub");
  const spokes = memberSites.filter((s) => s.role !== "hub");

  const pos = new Map<string, { x: number; y: number }>();
  hubs.forEach((site, i) => {
    const spread = hubs.length === 1 ? 0 : (i - (hubs.length - 1) / 2) * 140;
    pos.set(site.id, { x: cx + spread, y: cy });
  });
  spokes.forEach((site, i) => {
    const angle = (i / Math.max(spokes.length, 1)) * Math.PI * 2 - Math.PI / 2;
    pos.set(site.id, {
      x: cx + Math.cos(angle) * 165,
      y: cy + Math.sin(angle) * 155,
    });
  });

  if (memberSites.length === 0) {
    return <p className="muted">No members yet. Add sites to see the topology.</p>;
  }

  // Several links can join the same pair of sites (one per WAN). Collapse them
  // for drawing, but keep the count so a dual-homed site reads as such.
  const edges = new Map<string, { a: string; b: string; count: number }>();
  for (const link of links) {
    const a = wanOwner.get(link.a_wan_id);
    const b = wanOwner.get(link.b_wan_id);
    if (!a || !b || !pos.has(a) || !pos.has(b)) continue;
    const key = [a, b].sort().join("|");
    const existing = edges.get(key);
    if (existing) existing.count += 1;
    else edges.set(key, { a, b, count: 1 });
  }

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      style={{ width: "100%", height: "auto", maxHeight: 420 }}
      role="img"
      aria-label={`Topology of fabric ${fabric.name}`}
    >
      {[...edges.values()].map(({ a, b, count }) => {
        const pa = pos.get(a)!;
        const pb = pos.get(b)!;
        return (
          <g key={`${a}|${b}`}>
            <line
              x1={pa.x}
              y1={pa.y}
              x2={pb.x}
              y2={pb.y}
              stroke="var(--accent)"
              strokeWidth={count > 1 ? 3 : 1.5}
              strokeOpacity={0.65}
            />
            {count > 1 && (
              <text
                x={(pa.x + pb.x) / 2}
                y={(pa.y + pb.y) / 2 - 6}
                textAnchor="middle"
                fontSize="11"
                fill="var(--muted)"
              >
                ×{count}
              </text>
            )}
          </g>
        );
      })}

      {memberSites.map((site) => {
        const p = pos.get(site.id)!;
        const isHub = site.role === "hub";
        return (
          <g key={site.id}>
            <circle
              cx={p.x}
              cy={p.y}
              r={isHub ? 26 : 19}
              fill="var(--surface-2)"
              stroke={statusColor(site.status)}
              strokeWidth={2}
            />
            <text
              x={p.x}
              y={p.y + 4}
              textAnchor="middle"
              fontSize="10"
              fill="var(--muted)"
            >
              {isHub ? "hub" : "spoke"}
            </text>
            <text
              x={p.x}
              y={p.y + (isHub ? 42 : 35)}
              textAnchor="middle"
              fontSize="12"
              fill="var(--text)"
            >
              {site.name}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function statusColor(status: Site["status"]): string {
  if (status === "reachable") return "var(--ok)";
  if (status === "drifted") return "var(--warn)";
  if (status === "unprovisioned") return "var(--border)";
  return "var(--bad)";
}
