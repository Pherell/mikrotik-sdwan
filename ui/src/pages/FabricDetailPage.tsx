import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { FabricSettings } from "../components/FabricSettings";
import { TopologyGraph } from "../components/TopologyGraph";
import {
  endpoints,
  type Expansion,
  type FabricLink,
  type Site,
  type Wan,
} from "../lib/api";
import { Skeleton } from "../components/Skeleton";

export function FabricDetailPage() {
  const { fabricId = "" } = useParams();
  const queryClient = useQueryClient();
  const [expansion, setExpansion] = useState<Expansion | null>(null);
  const [editing, setEditing] = useState(false);
  const navigate = useNavigate();
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });
  const [addSiteId, setAddSiteId] = useState("");

  const fabric = useQuery({
    queryKey: ["fabric", fabricId],
    queryFn: () => endpoints.fabric(fabricId),
    enabled: Boolean(fabricId),
  });
  const links = useQuery({
    queryKey: ["fabric-links", fabricId],
    queryFn: () => endpoints.links(fabricId),
    enabled: Boolean(fabricId),
  });
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });

  function refresh() {
    queryClient.invalidateQueries({ queryKey: ["fabric", fabricId] });
    queryClient.invalidateQueries({ queryKey: ["fabric-links", fabricId] });
  }

  const removeFabric = useMutation({
    mutationFn: () => endpoints.deleteFabric(fabricId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["fabrics"] });
      navigate("/fabrics", { replace: true });
    },
  });

  const expand = useMutation({
    mutationFn: () => endpoints.expand(fabricId),
    onSuccess: (result) => {
      setExpansion(result);
      refresh();
    },
  });

  const addMember = useMutation({
    mutationFn: () => endpoints.addMember(fabricId, addSiteId),
    onSuccess: () => {
      setAddSiteId("");
      refresh();
    },
  });

  const removeMember = useMutation({
    mutationFn: (siteId: string) => endpoints.removeMember(fabricId, siteId),
    onSuccess: refresh,
  });

  if (fabric.isLoading) return <div className="card"><Skeleton rows={5} /></div>;
  if (fabric.isError) return <div className="error">{(fabric.error as Error).message}</div>;
  if (!fabric.data) return null;

  const f = fabric.data;
  const memberIds = new Set(f.members.map((m) => m.site_id));
  const candidates = (sites.data ?? []).filter((s) => !memberIds.has(s.id));

  return (
    <>
      <p>
        <Link to="/tunnel-networks">← All tunnel networks</Link>
      </p>

      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>{f.name}</h2>
          <div className="no-grow">
            <button
              className="primary"
              onClick={() => expand.mutate()}
              disabled={expand.isPending}
            >
              {expand.isPending ? "Rebuilding…" : "Rebuild tunnels"}
            </button>
          </div>
          <div className="no-grow">
            <button onClick={() => setEditing(!editing)}>
              {editing ? "Close" : "Settings"}
            </button>
          </div>
          {me.data?.role === "admin" && (
            <div className="no-grow">
              <button
                onClick={() => {
                  if (
                    confirm(
                      `Delete fabric ${f.name}? Tunnels stay on the devices until ` +
                        `each member device is applied again.`,
                    )
                  )
                    removeFabric.mutate();
                }}
              >
                Delete
              </button>
            </div>
          )}
        </div>

        <dl className="kv" style={{ marginTop: 16 }}>
          <dt>Transport</dt>
          <dd>{f.transport}</dd>
          <dt>Topology</dt>
          <dd>{f.topology}</dd>
          <dt>Tunnel pool</dt>
          <dd>
            {f.ip_pool} — {f.link_count} of {f.pool_capacity} /31s used
          </dd>
          <dt>AS number</dt>
          <dd>{f.asn}</dd>
          <dt>Tunnel MTU</dt>
          <dd>{f.mtu}</dd>
        </dl>

        {expand.isError && <div className="error">{(expand.error as Error).message}</div>}
        {expansion && <ExpansionResult result={expansion} />}
      </div>

      {editing && <FabricSettings fabric={f} onDone={() => setEditing(false)} />}

      <div className="card">
        <h2>Topology</h2>
        <TopologyGraph fabric={f} links={links.data ?? []} sites={sites.data ?? []} />
      </div>

      <div className="card">
        <h2>Members</h2>
        {f.members.length === 0 ? (
          <p className="muted">No devices in this tunnel network yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Device</th>
                <th>Loopback</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {f.members.map((m) => (
                <tr key={m.id}>
                  <td>
                    <Link to={`/devices/${m.site_id}`}>{m.site_name}</Link>
                  </td>
                  <td className="muted">{m.loopback_ip ?? "not assigned"}</td>
                  <td>
                    <button onClick={() => removeMember.mutate(m.site_id)}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {candidates.length > 0 && (
          <div className="row" style={{ marginTop: 12, justifyContent: "flex-start" }}>
            <label style={{ flex: "0 0 260px", margin: 0 }}>
              Add a site
              <select value={addSiteId} onChange={(e) => setAddSiteId(e.target.value)}>
                <option value="">Choose…</option>
                {candidates.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name} ({s.role})
                  </option>
                ))}
              </select>
            </label>
            <div className="no-grow" style={{ alignSelf: "flex-end" }}>
              <button disabled={!addSiteId} onClick={() => addMember.mutate()}>
                Add
              </button>
            </div>
          </div>
        )}
        {addMember.isError && (
          <div className="error">{(addMember.error as Error).message}</div>
        )}
      </div>

      <div className="card">
        <h2>Tunnels</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Computed from this network's members and shape — there is no button to add
          one, and editing them by hand is not a thing. Change who is a member, or the
          shape, and rebuild them.
        </p>
        {(links.data ?? []).length === 0 ? (
          <p className="muted">
            No links yet. Recompute to build them from the topology, then apply each
            member device to push the tunnels.
          </p>
        ) : (
          <table className="stack">
            <thead>
              <tr>
                <th>Between</th>
                <th>Outside — where it dials</th>
                <th>Inside — the tunnel itself</th>
                <th>Keys</th>
              </tr>
            </thead>
            <tbody>
              {(links.data ?? []).map((l) => (
                <TunnelRow key={l.id} link={l} sites={sites.data ?? []} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function ExpansionResult({ result }: { result: Expansion }) {
  return (
    <div style={{ marginTop: 12 }}>
      <p className="muted">
        {result.created} created · {result.kept} unchanged · {result.removed} removed
      </p>
      {result.problems.length > 0 && (
        <div className="error">
          <strong>Some pairs could not be linked.</strong>
          <ul>
            {result.problems.map((p, i) => (
              <li key={i}>
                {p.a} ↔ {p.b}: {p.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(result.created > 0 || result.removed > 0) && (
        <p className="muted">
          Nothing has been pushed yet. Apply each affected device to put these changes on
          the devices.
        </p>
      )}
    </div>
  );
}

/**
 * One tunnel, described by where it goes rather than by its slug.
 *
 * The table used to show the slug, the /31 and the two tunnel addresses — all
 * of which are *inside* the tunnel. Nothing said which two devices it joined
 * (except through a truncated slug) or which public address each end dials,
 * which is the question anyone looking at this actually has, and the one that
 * explains a tunnel that never comes up.
 *
 * The join happens here rather than on the server because the sites, with
 * their uplinks, are already loaded on this page for the topology graph.
 */
function TunnelRow({ link, sites }: { link: FabricLink; sites: Site[] }) {
  const end = (wanId: string) => {
    for (const site of sites) {
      const wan = site.wans.find((w) => w.id === wanId);
      if (wan) return { site, wan };
    }
    return null;
  };

  const a = end(link.a_wan_id);
  const b = end(link.b_wan_id);
  const dialsFromA = link.initiator === "a";

  return (
    <tr>
      <td data-label="Between">
        <Endpoint end={a} /> ↔ <Endpoint end={b} />
      </td>
      <td data-label="Outside">
        {/* The destination is the *responder's* public address. There is no
            destination field on a tunnel because it is not a property of the
            tunnel -- it is the far uplink's Public IP, and this is the only
            place the two are shown together. */}
        {(() => {
          const responder = dialsFromA ? b : a;
          const dialer = dialsFromA ? a : b;
          if (!responder?.wan.public_ip) {
            return (
              <span className="badge unreachable">
                neither end has a public address
              </span>
            );
          }
          return (
            <>
              <span className="muted">{dialer?.site.name ?? "?"} dials </span>
              <code>{responder.wan.public_ip}</code>
              <div className="muted">
                on {responder.site.name}/{responder.wan.name}
              </div>
            </>
          );
        })()}
      </td>
      <td data-label="Inside" className="muted">
        <code>
          {link.a_tunnel_ip} ↔ {link.b_tunnel_ip}
        </code>
        <div>out of {link.subnet}</div>
      </td>
      <td data-label="Keys">
        {link.has_secrets ? (
          <span className="badge reachable">generated</span>
        ) : (
          <span className="badge unreachable">missing</span>
        )}
      </td>
    </tr>
  );
}

function Endpoint({ end }: { end: { site: Site; wan: Wan } | null }) {
  if (!end) return <span className="muted">unknown uplink</span>;
  return (
    <>
      <Link to={`/devices/${end.site.id}`}>{end.site.name}</Link>
      <span className="muted">/{end.wan.name}</span>
    </>
  );
}
