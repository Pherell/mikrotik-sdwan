import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { FabricSettings } from "../components/FabricSettings";
import { TopologyGraph } from "../components/TopologyGraph";
import { endpoints, type Expansion } from "../lib/api";

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

  if (fabric.isLoading) return <p className="muted">Loading…</p>;
  if (fabric.isError) return <div className="error">{(fabric.error as Error).message}</div>;
  if (!fabric.data) return null;

  const f = fabric.data;
  const memberIds = new Set(f.members.map((m) => m.site_id));
  const candidates = (sites.data ?? []).filter((s) => !memberIds.has(s.id));

  return (
    <>
      <p>
        <Link to="/fabrics">← All fabrics</Link>
      </p>

      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>{f.name}</h2>
          <div style={{ flex: 0 }}>
            <button
              className="primary"
              onClick={() => expand.mutate()}
              disabled={expand.isPending}
            >
              {expand.isPending ? "Expanding…" : "Recompute links"}
            </button>
          </div>
          <div style={{ flex: 0 }}>
            <button onClick={() => setEditing(!editing)}>
              {editing ? "Close" : "Settings"}
            </button>
          </div>
          {me.data?.role === "admin" && (
            <div style={{ flex: 0 }}>
              <button
                onClick={() => {
                  if (
                    confirm(
                      `Delete fabric ${f.name}? Tunnels stay on the devices until ` +
                        `each member site is applied again.`,
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
          <p className="muted">No sites in this fabric yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Site</th>
                <th>Loopback</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {f.members.map((m) => (
                <tr key={m.id}>
                  <td>
                    <Link to={`/sites/${m.site_id}`}>{m.site_name}</Link>
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
            <div style={{ flex: 0, alignSelf: "flex-end" }}>
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
        <h2>Links</h2>
        {(links.data ?? []).length === 0 ? (
          <p className="muted">
            No links yet. Recompute to build them from the topology, then apply each
            member site to push the tunnels.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Link</th>
                <th>Subnet</th>
                <th>Addresses</th>
                <th>Dials</th>
                <th>Keys</th>
              </tr>
            </thead>
            <tbody>
              {(links.data ?? []).map((l) => (
                <tr key={l.id}>
                  <td>{l.slug}</td>
                  <td className="muted">{l.subnet}</td>
                  <td className="muted">
                    {l.a_tunnel_ip} ↔ {l.b_tunnel_ip}
                  </td>
                  <td>{l.initiator === "a" ? "side A" : "side B"}</td>
                  <td>
                    {l.has_secrets ? (
                      <span className="badge reachable">generated</span>
                    ) : (
                      <span className="badge unreachable">missing</span>
                    )}
                  </td>
                </tr>
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
          Nothing has been pushed yet. Apply each affected site to put these changes on
          the devices.
        </p>
      )}
    </div>
  );
}
