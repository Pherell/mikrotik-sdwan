import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { ApplyPanel } from "../components/ApplyPanel";
import { endpoints } from "../lib/api";

export function SiteDetailPage() {
  const { siteId = "" } = useParams();
  const queryClient = useQueryClient();

  const site = useQuery({
    queryKey: ["site", siteId],
    queryFn: () => endpoints.site(siteId),
    enabled: Boolean(siteId),
  });

  const probe = useMutation({
    mutationFn: () => endpoints.probe(siteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["site", siteId] });
      queryClient.invalidateQueries({ queryKey: ["sites"] });
    },
  });

  const drift = useMutation({
    mutationFn: () => endpoints.driftCheck(siteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["site", siteId] });
      queryClient.invalidateQueries({ queryKey: ["sites"] });
    },
  });

  if (site.isLoading) return <p className="muted">Loading…</p>;
  if (site.isError) return <div className="error">{(site.error as Error).message}</div>;
  if (!site.data) return null;

  const s = site.data;

  return (
    <>
      <p>
        <Link to="/sites">← All sites</Link>
      </p>

      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>
            {s.name} <span className={`badge ${s.status}`}>{s.status}</span>
          </h2>
          <div style={{ flex: 0 }}>
            <button onClick={() => probe.mutate()} disabled={probe.isPending}>
              {probe.isPending ? "Probing…" : "Probe device"}
            </button>
          </div>
          <div style={{ flex: 0 }}>
            <button onClick={() => drift.mutate()} disabled={drift.isPending}>
              {drift.isPending ? "Checking…" : "Check for drift"}
            </button>
          </div>
        </div>

        {s.last_error && <div className="error" style={{ marginTop: 12 }}>{s.last_error}</div>}
        {drift.data && !drift.data.result?.drifted && (
          <p className="muted" style={{ marginTop: 12 }}>
            Checked just now — the device matches intent.
          </p>
        )}
        {drift.data?.diff?.text && drift.data.result?.drifted ? (
          <pre className="diff" style={{ marginTop: 12 }}>{drift.data.diff.text}</pre>
        ) : null}

        <dl className="kv" style={{ marginTop: 16 }}>
          <dt>Role</dt>
          <dd>{s.role}</dd>
          <dt>Management</dt>
          <dd>
            {s.mgmt_host}
            {s.mgmt_port ? `:${s.mgmt_port}` : ""} as {s.username}
          </dd>
          <dt>Credentials</dt>
          <dd>{s.has_credentials ? "stored (encrypted)" : "none stored"}</dd>
          <dt>RouterOS</dt>
          <dd>{s.ros_version ?? "unknown"}</dd>
          <dt>Board</dt>
          <dd>{s.board_name ? `${s.board_name} (${s.architecture})` : "unknown"}</dd>
          <dt>Identity</dt>
          <dd>{s.identity ?? "unknown"}</dd>
          <dt>Last seen</dt>
          <dd>{s.last_seen_at ?? "never"}</dd>
          <dt>Local prefixes</dt>
          <dd>{s.local_prefixes.length ? s.local_prefixes.join(", ") : "none"}</dd>
          <dt>On drift</dt>
          <dd>{s.drift_action === "auto-remediate" ? "re-apply automatically" : "alert only"}</dd>
        </dl>
      </div>

      <ApplyPanel siteId={s.id} />

      <div className="card">
        <h2>Uplinks</h2>
        {s.wans.length === 0 ? (
          <p className="muted">No uplinks recorded. Probe the device to discover them.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Interface</th>
                <th>Public IP</th>
                <th>Gateway</th>
                <th>Cost</th>
                <th>Reachability</th>
              </tr>
            </thead>
            <tbody>
              {s.wans.map((w) => (
                <tr key={w.id}>
                  <td>{w.name}</td>
                  <td>{w.interface}</td>
                  <td>{w.public_ip ?? <span className="muted">none</span>}</td>
                  <td className="muted">{w.gateway ?? "—"}</td>
                  <td>{w.cost}</td>
                  <td>
                    {w.dial_out_only ? (
                      <span className="badge drifted">dial-out only</span>
                    ) : (
                      <span className="badge reachable">can accept tunnels</span>
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
