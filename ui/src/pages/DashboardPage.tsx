import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { endpoints, type Job, type Site } from "../lib/api";

/**
 * The front door: what the fleet is doing, and the two bulk actions worth
 * having. Polls rather than using a WebSocket — a controller managing tens of
 * sites does not generate enough change to justify one, and polling survives a
 * proxy that buffers.
 */
export function DashboardPage() {
  const sites = useQuery({
    queryKey: ["sites"],
    queryFn: endpoints.sites,
    refetchInterval: 15_000,
  });
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => endpoints.jobs(),
    refetchInterval: 15_000,
  });

  const all = sites.data ?? [];
  const byStatus = count(all.map((s) => s.status));
  const drifted = all.filter((s) => s.status === "drifted");
  const unreachable = all.filter(
    (s) => s.status === "unreachable" || s.status === "error",
  );
  const armed = (jobs.data ?? []).filter((j) => j.rollback_token);

  return (
    <>
      {armed.length > 0 && <ArmedBanner jobs={armed} sites={all} />}

      <div className="tiles">
        <Tile label="Sites" value={all.length} to="/sites" />
        <Tile
          label="Reachable"
          value={byStatus.reachable ?? 0}
          tone={all.length && byStatus.reachable === all.length ? "ok" : undefined}
        />
        <Tile
          label="Drifted"
          value={drifted.length}
          tone={drifted.length ? "warn" : undefined}
        />
        <Tile
          label="Unreachable"
          value={unreachable.length}
          tone={unreachable.length ? "bad" : undefined}
        />
        <Tile
          label="Not yet applied"
          value={byStatus.unprovisioned ?? 0}
          tone={byStatus.unprovisioned ? "warn" : undefined}
        />
        <Tile
          label="Rollbacks armed"
          value={armed.length}
          tone={armed.length ? "bad" : undefined}
        />
      </div>

      <FleetActions sites={all} />

      {(drifted.length > 0 || unreachable.length > 0) && (
        <NeedsAttention drifted={drifted} unreachable={unreachable} />
      )}

      <RecentJobs jobs={jobs.data ?? []} sites={all} />
    </>
  );
}

function Tile({
  label,
  value,
  tone,
  to,
}: {
  label: string;
  value: number;
  tone?: "ok" | "warn" | "bad";
  to?: string;
}) {
  const body = (
    <div className={`tile${tone ? ` tile-${tone}` : ""}`}>
      <div className="tile-value">{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  );
  return to ? (
    <Link to={to} style={{ textDecoration: "none" }}>
      {body}
    </Link>
  ) : (
    body
  );
}

function ArmedBanner({ jobs, sites }: { jobs: Job[]; sites: Site[] }) {
  const name = (id: string | null) =>
    sites.find((s) => s.id === id)?.name ?? id ?? "unknown";
  return (
    <div className="card">
      <div className="error">
        <strong>
          {jobs.length} device{jobs.length > 1 ? "s are" : " is"} scheduled to restore
          a backup and reboot.
        </strong>{" "}
        The controller could not confirm management access after a push. If the
        configuration is actually fine, disarm the rollback from the site page
        before it fires.
        <ul>
          {jobs.map((j) => (
            <li key={j.id}>
              <Link to={`/sites/${j.site_id}`}>{name(j.site_id)}</Link> —{" "}
              <code>{j.backup_name}</code>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/**
 * Bulk apply and a fleet drift sweep.
 *
 * Applies run one site at a time rather than in parallel: each one arms a
 * dead-man rollback on its device, and a burst of simultaneous pushes means a
 * burst of simultaneous reboots if something is systematically wrong. Serial is
 * slower and much easier to stop.
 */
function FleetActions({ sites }: { sites: Site[] }) {
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<string | null>(null);
  const [results, setResults] = useState<{ site: string; state: string }[]>([]);
  const [stop, setStop] = useState(false);

  const candidates = sites.filter((s) => s.has_credentials);

  const applyAll = useMutation({
    mutationFn: async () => {
      setResults([]);
      setStop(false);
      const out: { site: string; state: string }[] = [];
      for (const site of candidates) {
        if (stop) break;
        setProgress(site.name);
        try {
          const job = await endpoints.apply(site.id, { confirm: true });
          out.push({ site: site.name, state: job.state });
        } catch (e) {
          out.push({ site: site.name, state: (e as Error).message });
        }
        setResults([...out]);
      }
      setProgress(null);
      return out;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const sweep = useMutation({
    mutationFn: () => endpoints.driftSweep(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  return (
    <div className="card">
      <h2>Fleet actions</h2>
      <div className="row" style={{ justifyContent: "flex-start" }}>
        <div style={{ flex: 0 }}>
          <button
            className="primary"
            disabled={applyAll.isPending || candidates.length === 0}
            onClick={() => {
              if (
                confirm(
                  `Apply to all ${candidates.length} site(s), one at a time?\n\n` +
                    `Each push arms a rollback that reboots the router if management ` +
                    `access is lost.`,
                )
              )
                applyAll.mutate();
            }}
          >
            {applyAll.isPending
              ? `Applying ${progress ?? ""}…`
              : `Apply all (${candidates.length})`}
          </button>
        </div>
        {applyAll.isPending && (
          <div style={{ flex: 0 }}>
            <button onClick={() => setStop(true)}>Stop after this one</button>
          </div>
        )}
        <div style={{ flex: 0 }}>
          <button onClick={() => sweep.mutate()} disabled={sweep.isPending}>
            {sweep.isPending ? "Sweeping…" : "Check all for drift"}
          </button>
        </div>
      </div>

      {candidates.length < sites.length && (
        <p className="muted">
          {sites.length - candidates.length} site(s) have no stored credentials and
          are skipped.
        </p>
      )}

      {sweep.data && (
        <p className="muted">
          Checked {sweep.data.length} site(s);{" "}
          {sweep.data.filter((j) => (j.result as { drifted?: boolean })?.drifted).length}{" "}
          drifted.
        </p>
      )}

      {results.length > 0 && (
        <table style={{ marginTop: 12 }}>
          <tbody>
            {results.map((r) => (
              <tr key={r.site}>
                <td>{r.site}</td>
                <td>
                  <span
                    className={`badge ${r.state === "succeeded" ? "reachable" : "unreachable"}`}
                  >
                    {r.state}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function NeedsAttention({
  drifted,
  unreachable,
}: {
  drifted: Site[];
  unreachable: Site[];
}) {
  return (
    <div className="card">
      <h2>Needs attention</h2>
      <table>
        <tbody>
          {[...unreachable, ...drifted].map((s) => (
            <tr key={s.id}>
              <td>
                <Link to={`/sites/${s.id}`}>{s.name}</Link>
              </td>
              <td>
                <span className={`badge ${s.status}`}>{s.status}</span>
              </td>
              <td className="muted">{s.last_error ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RecentJobs({ jobs, sites }: { jobs: Job[]; sites: Site[] }) {
  const name = (id: string | null) => sites.find((s) => s.id === id)?.name ?? "—";
  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>Recent activity</h2>
        <div style={{ flex: 0 }}>
          <Link to="/jobs" className="navlink">
            All jobs →
          </Link>
        </div>
      </div>
      {jobs.length === 0 ? (
        <p className="muted">Nothing has run yet.</p>
      ) : (
        <table>
          <tbody>
            {jobs.slice(0, 8).map((j) => (
              <tr key={j.id}>
                <td className="muted">{new Date(j.created_at).toLocaleString()}</td>
                <td>{name(j.site_id)}</td>
                <td>{j.kind}</td>
                <td>
                  <span
                    className={`badge ${
                      j.state === "succeeded"
                        ? "reachable"
                        : j.state === "rolled_back"
                          ? "drifted"
                          : "unreachable"
                    }`}
                  >
                    {j.state}
                  </span>
                </td>
                <td className="muted">
                  {String((j.result as { applied?: number })?.applied ?? 0)} changes
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function count(values: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const v of values) out[v] = (out[v] ?? 0) + 1;
  return out;
}
