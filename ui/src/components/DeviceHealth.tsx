/**
 * How the box itself is doing.
 *
 * One read of /system/resource, on demand. This is not the telemetry from
 * plan-v2 M7 -- there is no history here, no thresholds and no alerting,
 * because those need storage and retention decisions that have not been made.
 * It answers "is this device healthy right now", which is the question you have
 * while you are already looking at its page.
 */

import { useQuery } from "@tanstack/react-query";

import { endpoints, type DeviceHealth as Health } from "../lib/api";

function gib(bytes: number | null): string {
  if (bytes === null) return "—";
  const mb = bytes / 1024 / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GiB` : `${Math.round(mb)} MiB`;
}

function Meter({
  label,
  used,
  total,
  detail,
}: {
  label: string;
  used: number | null;
  total: number | null;
  detail: string;
}) {
  const pct = used !== null && total ? Math.round((used / total) * 100) : null;
  // Colour by pressure, not by taste. Under half is unremarkable.
  const tone = pct === null ? "" : pct >= 90 ? " bad" : pct >= 75 ? " warn" : "";
  return (
    <div className="meter">
      <div className="meter-head">
        <span>{label}</span>
        <span className="muted">{pct === null ? "—" : `${pct}%`}</span>
      </div>
      <div className="meter-track">
        <div className={`meter-fill${tone}`} style={{ width: `${pct ?? 0}%` }} />
      </div>
      <div className="muted meter-detail">{detail}</div>
    </div>
  );
}

export function DeviceHealthCard({ siteId }: { siteId: string }) {
  const health = useQuery<Health>({
    queryKey: ["health", siteId],
    queryFn: () => endpoints.health(siteId),
    staleTime: 30_000,
    retry: false,
  });

  const h = health.data;
  const usedMemory =
    h?.total_memory_bytes != null && h?.free_memory_bytes != null
      ? h.total_memory_bytes - h.free_memory_bytes
      : null;
  const usedDisk =
    h?.total_disk_bytes != null && h?.free_disk_bytes != null
      ? h.total_disk_bytes - h.free_disk_bytes
      : null;

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>Device health</h2>
        <div className="no-grow">
          <button
            className="sm"
            onClick={() => health.refetch()}
            data-busy={health.isFetching}
            disabled={health.isFetching}
          >
            {health.isFetching ? "Reading…" : "Refresh"}
          </button>
        </div>
      </div>

      {health.isError && (
        <div className="error">{(health.error as Error).message}</div>
      )}

      {h && (
        <>
          <div className="meters">
            <Meter
              label="CPU"
              used={h.cpu_load_percent}
              total={100}
              detail={
                h.cpu_count
                  ? `${h.cpu_count} core${h.cpu_count > 1 ? "s" : ""}${
                      h.cpu_frequency_mhz ? ` at ${h.cpu_frequency_mhz} MHz` : ""
                    }`
                  : "—"
              }
            />
            <Meter
              label="Memory"
              used={usedMemory}
              total={h.total_memory_bytes}
              detail={`${gib(usedMemory)} of ${gib(h.total_memory_bytes)}`}
            />
            <Meter
              label="Storage"
              used={usedDisk}
              total={h.total_disk_bytes}
              detail={`${gib(usedDisk)} of ${gib(h.total_disk_bytes)}`}
            />
          </div>
          <dl className="kv">
            <dt>Uptime</dt>
            <dd>{h.uptime ?? "—"}</dd>
            <dt>RouterOS</dt>
            <dd>
              {h.version ?? "—"}
              {h.board_name && <span className="muted"> · {h.board_name}</span>}
              {h.architecture && <span className="muted"> · {h.architecture}</span>}
            </dd>
          </dl>
        </>
      )}
    </div>
  );
}
