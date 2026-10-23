/**
 * SLA compliance: what the central evaluator actually judged, over time.
 *
 * The thresholds in an SLA profile used to drive only momentary failover
 * decisions — a path was in or out, and the moment passed unrecorded. The
 * evaluator now writes a verdict per bucket per path; this page is those
 * verdicts, aggregated. A share is a fraction of judged buckets, and "no
 * data" is shown as exactly that: an unjudged path is not a compliant one,
 * and it is not 0% either.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import {
  endpoints,
  type SlaClass,
  type SlaComplianceWindow,
  type SlaLinkCompliance,
} from "../lib/api";

const WINDOWS: Array<{ value: SlaComplianceWindow; label: string }> = [
  { value: "24h", label: "Last 24 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
];

const CLASS_META: Record<SlaClass, { label: string; badge: string }> = {
  in_sla: { label: "In SLA", badge: "reachable" },
  degraded: { label: "Degraded", badge: "drifted" },
  down: { label: "Down", badge: "unreachable" },
};

function ClassBadge({ value }: { value: string | null }) {
  if (!value) return <span className="muted">—</span>;
  const meta = CLASS_META[value as SlaClass];
  if (!meta) return <span className="badge">{value}</span>;
  return <span className={`badge ${meta.badge}`}>{meta.label}</span>;
}

function classLabel(value: string | null): string {
  if (!value) return "—";
  return CLASS_META[value as SlaClass]?.label ?? value;
}

/** Shares arrive as fractions of judged buckets; null means never judged. */
function share(value: number | null): string {
  if (value === null) return "—";
  return `${Math.round(value * 1000) / 10}%`;
}

function when(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

/** A measured value, or the dash that means it was never measured. */
function metric(value: number | null, unit: string): string {
  return value != null ? `${Math.round(value * 10) / 10}${unit}` : "—";
}

export function SlaCompliancePage() {
  const [window, setWindow] = useState<SlaComplianceWindow>("24h");
  const [selectedLinkId, setSelectedLinkId] = useState<string | null>(null);

  const compliance = useQuery({
    queryKey: ["sla-compliance", window],
    queryFn: () => endpoints.slaCompliance(window),
  });

  const totals = compliance.data?.totals;
  const noData = totals !== undefined && totals.buckets === 0;

  return (
    <>
      <PageHeader
        title="SLA compliance"
        description="How each path scored against the SLA profile its group names, as judged centrally from the telemetry samples — not just whether traffic failed over in the moment."
      />

      <div className="tabs">
        {WINDOWS.map((w) => (
          <button
            key={w.value}
            type="button"
            className={`tab${window === w.value ? " active" : ""}`}
            onClick={() => setWindow(w.value)}
          >
            {w.label}
          </button>
        ))}
      </div>

      {compliance.isLoading && (
        <div className="card">
          <Skeleton rows={4} />
        </div>
      )}
      {compliance.isError && (
        <div className="error">{(compliance.error as Error).message}</div>
      )}

      {compliance.data && noData && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            No SLA evaluations have been recorded in this window yet. The
            evaluator runs with the telemetry sweep and judges each path
            against its profile in buckets — until it has run, and until
            devices have reported samples for it to judge, there is nothing
            honest to show here. This is not a clean bill of health; it is
            an empty record.
          </p>
        </div>
      )}

      {compliance.data && !noData && totals && (
        <>
          <div className="row">
            <div className="card" style={{ textAlign: "center" }}>
              <div style={{ fontSize: 26, fontWeight: 600 }}>{totals.buckets}</div>
              <div className="muted">buckets judged</div>
            </div>
            <div className="card" style={{ textAlign: "center" }}>
              <div style={{ fontSize: 26, fontWeight: 600 }}>
                {share(totals.shares.in_sla)}
              </div>
              <div className="muted">in SLA</div>
            </div>
            <div className="card" style={{ textAlign: "center" }}>
              <div style={{ fontSize: 26, fontWeight: 600 }}>
                {share(totals.shares.degraded)}
              </div>
              <div className="muted">degraded</div>
            </div>
            <div className="card" style={{ textAlign: "center" }}>
              <div style={{ fontSize: 26, fontWeight: 600 }}>
                {share(totals.shares.down)}
              </div>
              <div className="muted">down</div>
            </div>
            <div className="card" style={{ textAlign: "center" }}>
              <div style={{ fontSize: 26, fontWeight: 600 }}>
                {totals.violation_buckets}
              </div>
              <div className="muted">violation buckets</div>
            </div>
            <div className="card" style={{ textAlign: "center" }}>
              <div style={{ fontSize: 26, fontWeight: 600 }}>
                {totals.violation_episodes}
              </div>
              <div className="muted">violation episodes</div>
            </div>
            <div className="card" style={{ textAlign: "center" }}>
              <div style={{ fontSize: 26, fontWeight: 600 }}>
                {totals.links_with_data} / {totals.links_total}
              </div>
              <div className="muted">links with data</div>
            </div>
          </div>

          <div className="card">
            <h2>By site</h2>
            <table className="stack">
              <thead>
                <tr>
                  <th>Site</th>
                  <th>Current</th>
                  <th>Buckets</th>
                  <th>In SLA</th>
                  <th>Violation episodes</th>
                </tr>
              </thead>
              <tbody>
                {compliance.data.sites.map((s) => (
                  <tr key={s.site_id}>
                    <td data-label="Site">
                      <strong>{s.site_name}</strong>
                    </td>
                    {s.has_data ? (
                      <>
                        <td data-label="Current">
                          <ClassBadge value={s.current_class} />
                        </td>
                        <td data-label="Buckets">{s.buckets}</td>
                        <td data-label="In SLA">{share(s.shares.in_sla)}</td>
                        <td data-label="Violation episodes">
                          {s.violation_episodes}
                        </td>
                      </>
                    ) : (
                      <td data-label="Current" className="muted" colSpan={4}>
                        No data yet
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="card">
            <h2>By link</h2>
            <p className="muted" style={{ marginTop: 0 }}>
              Choose a link to see the individual judgements underneath these
              numbers.
            </p>
            <table className="stack">
              <thead>
                <tr>
                  <th>Link</th>
                  <th>Current</th>
                  <th>Buckets</th>
                  <th>In SLA / Degraded / Down</th>
                  <th>Violation episodes</th>
                  <th>Last judged</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {compliance.data.links.map((l) => (
                  <tr key={l.link_id}>
                    <td data-label="Link">
                      <strong>{l.slug}</strong>
                      <div className="muted">
                        {l.fabric_name} · {l.a_site_name} {l.a_wan_name} ↔{" "}
                        {l.b_site_name} {l.b_wan_name}
                      </div>
                    </td>
                    {l.has_data ? (
                      <>
                        <td data-label="Current">
                          <ClassBadge value={l.current_class} />
                        </td>
                        <td data-label="Buckets">{l.buckets}</td>
                        <td data-label="Shares" className="muted">
                          {share(l.shares.in_sla)} / {share(l.shares.degraded)}{" "}
                          / {share(l.shares.down)}
                        </td>
                        <td data-label="Violation episodes">
                          {l.violation_episodes}
                        </td>
                        <td data-label="Last judged" className="muted">
                          {when(l.last_bucket_start)}
                        </td>
                      </>
                    ) : (
                      <>
                        <td data-label="Current" className="muted" colSpan={4}>
                          No data yet
                        </td>
                        <td data-label="Last judged" className="muted">
                          —
                        </td>
                      </>
                    )}
                    <td data-label="">
                      <button
                        className="sm"
                        onClick={() =>
                          setSelectedLinkId(
                            selectedLinkId === l.link_id ? null : l.link_id,
                          )
                        }
                      >
                        {selectedLinkId === l.link_id ? "Close" : "Evaluations"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {selectedLinkId && (
            <LinkEvaluations
              link={compliance.data.links.find(
                (l) => l.link_id === selectedLinkId,
              )}
              onClose={() => setSelectedLinkId(null)}
            />
          )}
        </>
      )}
    </>
  );
}

function LinkEvaluations({
  link,
  onClose,
}: {
  link?: SlaLinkCompliance;
  onClose: () => void;
}) {
  const evaluations = useQuery({
    queryKey: ["link-evaluations", link?.link_id],
    queryFn: () => endpoints.linkEvaluations(link!.link_id),
    enabled: !!link,
  });

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>
          Evaluations{link ? ` — ${link.slug}` : ""}
        </h2>
        <div className="no-grow">
          <button className="sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {link && (
        <p className="muted" style={{ marginTop: 0 }}>
          {link.fabric_name} · {link.a_site_name} {link.a_wan_name} ↔{" "}
          {link.b_site_name} {link.b_wan_name}. Newest first — each row is one
          bucket the evaluator judged, and what it changed from.
        </p>
      )}
      {evaluations.isLoading && <Skeleton rows={3} />}
      {evaluations.isError && (
        <div className="error">{(evaluations.error as Error).message}</div>
      )}
      {evaluations.data && evaluations.data.evaluations.length === 0 && (
        <p className="muted">
          No evaluations recorded for this link yet.
        </p>
      )}
      {evaluations.data && evaluations.data.evaluations.length > 0 && (
        <table className="stack">
          <thead>
            <tr>
              <th>Bucket</th>
              <th>Class</th>
              <th>Loss</th>
              <th>Latency</th>
              <th>Jitter</th>
              <th>Previously</th>
            </tr>
          </thead>
          <tbody>
            {evaluations.data.evaluations.map((ev) => (
              <tr key={ev.id}>
                <td data-label="Bucket" className="muted">
                  {when(ev.bucket_start)}
                </td>
                <td data-label="Class">
                  <ClassBadge value={ev.classification} />
                </td>
                <td data-label="Loss">{metric(ev.loss_percent, "%")}</td>
                <td data-label="Latency">{metric(ev.latency_ms, " ms")}</td>
                <td data-label="Jitter">{metric(ev.jitter_ms, " ms")}</td>
                <td data-label="Previously" className="muted">
                  {classLabel(ev.previous_class)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
