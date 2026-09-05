import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApplyPanel } from "../components/ApplyPanel";
import { DeviceConsole, RollbackPanel } from "../components/DeviceConsole";
import { SiteSettings } from "../components/SiteSettings";
import { WanEditor } from "../components/WanEditor";
import { endpoints } from "../lib/api";

export function SiteDetailPage() {
  const { siteId = "" } = useParams();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);

  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });

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

  const remove = useMutation({
    mutationFn: () => endpoints.deleteSite(siteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      navigate("/sites", { replace: true });
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
          <div style={{ flex: 0 }}>
            <button onClick={() => setEditing(!editing)}>
              {editing ? "Close" : "Edit"}
            </button>
          </div>
          {me.data?.role === "admin" && (
            <div style={{ flex: 0 }}>
              <button
                onClick={() => {
                  if (confirm(`Delete site ${s.name}? This does not remove anything from the device.`))
                    remove.mutate();
                }}
              >
                Delete
              </button>
            </div>
          )}
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

      {remove.isError && <div className="error">{(remove.error as Error).message}</div>}

      {editing && <SiteSettings site={s} onDone={() => setEditing(false)} />}

      <RollbackPanel siteId={s.id} />

      <ApplyPanel siteId={s.id} />

      <WanEditor site={s} />

      <DeviceConsole siteId={s.id} />

    </>
  );
}
