import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";

import { GettingStarted } from "../components/GettingStarted";
import { HowItWorks } from "../components/HowItWorks";
import { Skeleton } from "../components/Skeleton";
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
  const fabrics = useQuery({ queryKey: ["fabrics"], queryFn: endpoints.fabrics });

  const all = sites.data ?? [];
  const byStatus = count(all.map((s) => s.status));
  const drifted = all.filter((s) => s.status === "drifted");
  const unreachable = all.filter(
    (s) => s.status === "unreachable" || s.status === "error",
  );
  const armed = (jobs.data ?? []).filter((j) => j.rollback_token);

  // The checklist stays until the fleet has actually been configured once, not
  // until the first site exists -- otherwise it disappears the moment step one
  // is done and its ticks are never seen.
  const hasApplied = (jobs.data ?? []).some(
    (j) => j.kind === "apply" && j.state === "succeeded",
  );

  // First load: hold the layout with placeholders instead of flashing zeros,
  // then swapping in the real numbers once the request lands.
  if (sites.isLoading) {
    return (
      <>
        <div className="tiles" aria-busy="true" aria-label="Loading dashboard">
          {Array.from({ length: 6 }, (_, i) => (
            <div className="tile" key={i}>
              <div
                className="skeleton"
                style={{ height: 30, width: "55%", marginBottom: 8 }}
              />
              <div
                className="skeleton"
                style={{ height: 12, width: "80%", marginBottom: 0 }}
              />
            </div>
          ))}
        </div>
        <div className="card">
          <Skeleton rows={3} label="Loading fleet actions" />
        </div>
        <div className="card">
          <Skeleton rows={5} label="Loading recent activity" />
        </div>
      </>
    );
  }

  // The whole page is the device list; a failed request must say so loudly and
  // offer a way back, not silently fall through to the empty-onboarding flow.
  if (sites.isError) {
    return (
      <div className="card">
        <div className="error">
          <strong>Couldn't load the device list.</strong> The dashboard needs
          it to show anything.
          <br />
          <span className="muted">{(sites.error as Error)?.message}</span>
        </div>
        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div className="no-grow">
            <button className="primary" onClick={() => sites.refetch()}>
              Try again
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (all.length === 0) {
    return (
      <>
        <GettingStarted
          sites={all}
          fabrics={fabrics.data ?? []}
          jobs={jobs.data ?? []}
        />
        <HowItWorks />
        {/* The checklist above is the happy path. This is where someone goes
            when a step did not do what they expected -- and unlike these two
            cards, it does not disappear once the first device exists. */}
        <div className="card">
          <h2>Stuck on a step?</h2>
          <p className="muted" style={{ marginTop: 0 }}>
            The <Link to="/guide">guide</Link> covers each of these in detail,
            what every word here means, and what to do when something does not
            come up.
          </p>
        </div>
      </>
    );
  }

  return (
    <>
      {armed.length > 0 && <ArmedBanner jobs={armed} sites={all} />}

      <div className="tiles">
        <Tile label="Devices" value={all.length} to="/devices" />
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

      {/* Jobs and fabrics are secondary data: their failures should be visible
          with a retry, not silent gaps in the sections that depend on them. */}
      {(jobs.isError || fabrics.isError) && (
        <div className="card">
          <div className="error">
            <strong>Couldn't load some dashboard data:</strong>{" "}
            {[
              jobs.isError && "recent activity",
              fabrics.isError && "fabric data",
            ]
              .filter(Boolean)
              .join(" and ")}
            . Showing everything else.
          </div>
          <div className="row" style={{ justifyContent: "flex-start" }}>
            <div className="no-grow">
              <button
                onClick={() => {
                  if (jobs.isError) jobs.refetch();
                  if (fabrics.isError) fabrics.refetch();
                }}
              >
                Retry
              </button>
            </div>
          </div>
        </div>
      )}

      {!hasApplied && (
        <GettingStarted
          sites={all}
          fabrics={fabrics.data ?? []}
          jobs={jobs.data ?? []}
        />
      )}

      <FleetActions sites={all} />

      {(drifted.length > 0 || unreachable.length > 0) && (
        <NeedsAttention drifted={drifted} unreachable={unreachable} />
      )}

      <RecentJobs jobs={jobs.data ?? []} sites={all} />

      <HowItWorks defaultOpen={false} />
    </>
  );
}

function ToneIcon({ tone }: { tone?: "ok" | "warn" | "bad" }) {
  if (tone === "ok")
    return (
      <svg
        className="tile-icon"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        aria-hidden="true"
      >
        <circle cx="8" cy="8" r="6.4" />
        <path d="M5.2 8.3 7.3 10.4 11 6.2" strokeLinecap="round" />
      </svg>
    );
  if (tone === "warn")
    return (
      <svg
        className="tile-icon"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        aria-hidden="true"
      >
        <path d="M8 2.2 14.6 13.4H1.4L8 2.2Z" strokeLinejoin="round" />
        <path d="M8 6.6v3" strokeLinecap="round" />
        <circle cx="8" cy="11.4" r="1" fill="currentColor" stroke="none" />
      </svg>
    );
  if (tone === "bad")
    return (
      <svg
        className="tile-icon"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        aria-hidden="true"
      >
        <circle cx="8" cy="8" r="6.4" />
        <path d="M8 4.8v3.6" strokeLinecap="round" />
        <circle cx="8" cy="11.2" r="1" fill="currentColor" stroke="none" />
      </svg>
    );
  return null;
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
      <div className="tile-label">
        <ToneIcon tone={tone} />
        {label}
      </div>
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
        configuration is actually fine, disarm the rollback from the device page
        before it fires.
        <ul>
          {jobs.map((j) => (
            <li key={j.id}>
              <Link to={`/devices/${j.site_id}`}>{name(j.site_id)}</Link> —{" "}
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
  const stopRef = useRef(false);

  const candidates = sites.filter((s) => s.has_credentials);

  const applyAll = useMutation({
    mutationFn: async () => {
      setResults([]);
      stopRef.current = false;
      const out: { site: string; state: string }[] = [];
      for (const site of candidates) {
        if (stopRef.current) break;
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
        <div className="no-grow">
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
          <div className="no-grow">
            <button onClick={() => (stopRef.current = true)}>Stop after this one</button>
          </div>
        )}
        <div className="no-grow">
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
                <Link to={`/devices/${s.id}`}>{s.name}</Link>
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
        <div className="no-grow">
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
