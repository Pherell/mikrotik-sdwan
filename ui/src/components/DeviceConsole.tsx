import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { DEVICE_MENUS, endpoints } from "../lib/api";

/**
 * Read one RouterOS menu straight off the device.
 *
 * Read-only and allowlisted server-side, so this cannot become a way to run
 * commands. The menu list here mirrors READABLE_PATHS in the API; anything
 * outside it is refused with a 400 regardless of what the UI sends.
 */
export function DeviceConsole({ siteId }: { siteId: string }) {
  const [menu, setMenu] = useState<string>("ip/ipsec/active-peers");
  const [open, setOpen] = useState(false);

  const rows = useQuery({
    queryKey: ["device", siteId, menu],
    queryFn: () => endpoints.deviceRead(siteId, menu),
    enabled: false,
    retry: false,
  });

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>Device console</h2>
        <div style={{ flex: 0 }}>
          <button onClick={() => setOpen(!open)}>{open ? "Hide" : "Open"}</button>
        </div>
      </div>

      {open && (
        <>
          <p className="muted">
            Reads one menu directly from the router. Never writes, and secret
            properties are stripped before they leave the controller.
          </p>
          <div className="row" style={{ justifyContent: "flex-start" }}>
            <label style={{ flex: "0 0 320px", margin: 0 }}>
              Menu
              <select value={menu} onChange={(e) => setMenu(e.target.value)}>
                {DEVICE_MENUS.map((m) => (
                  <option key={m} value={m}>
                    /{m}
                  </option>
                ))}
              </select>
            </label>
            <div style={{ flex: 0, alignSelf: "flex-end" }}>
              <button onClick={() => rows.refetch()} disabled={rows.isFetching}>
                {rows.isFetching ? "Reading…" : "Read"}
              </button>
            </div>
          </div>

          {rows.isError && (
            <div className="error">{(rows.error as Error).message}</div>
          )}
          {rows.data && <DeviceTable rows={rows.data} />}
        </>
      )}
    </div>
  );
}

function DeviceTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (rows.length === 0) return <p className="muted">Menu is empty.</p>;

  // RouterOS rows are ragged: not every row carries every property. Take the
  // union so nothing is silently hidden, but keep .id last since it is noise.
  const columns = [
    ...new Set(rows.flatMap((r) => Object.keys(r))),
  ].sort((a, b) => Number(a.startsWith(".")) - Number(b.startsWith(".")));

  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c} className={c.startsWith(".") ? "muted" : undefined}>
                  {format(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function format(value: unknown): string {
  if (value === undefined || value === null) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

/**
 * Rollback schedulers still armed on a device.
 *
 * A controller that dies between arming and disarming leaves one behind, and it
 * will restore a perfectly good configuration and reboot the router when it
 * fires. This is how an operator finds out before that happens — previously it
 * required curl, which is the wrong place to put a safety valve.
 */
export function RollbackPanel({ siteId }: { siteId: string }) {
  const queryClient = useQueryClient();
  const armed = useQuery({
    queryKey: ["rollbacks", siteId],
    queryFn: () => endpoints.rollbacks(siteId),
    // Cheap, and the answer matters urgently when it is not empty.
    refetchInterval: 30_000,
    retry: false,
  });

  const clear = useMutation({
    mutationFn: (name: string) => endpoints.clearRollback(siteId, name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["rollbacks", siteId] }),
  });

  if (armed.isError || !armed.data || armed.data.length === 0) return null;

  return (
    <div className="card">
      <h2>Armed rollbacks</h2>
      <div className="error">
        <strong>This device is scheduled to restore a backup and reboot.</strong> If
        the configuration on it is good, clear the entry before it fires.
      </div>
      <table>
        <thead>
          <tr>
            <th>Scheduler</th>
            <th>Fires in</th>
            <th>Next run</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {armed.data.map((r) => (
            <tr key={r.name}>
              <td>{r.name}</td>
              <td>{r.interval ?? "—"}</td>
              <td className="muted">{r.next_run ?? "—"}</td>
              <td>
                <button onClick={() => clear.mutate(r.name)} disabled={clear.isPending}>
                  Disarm
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {clear.isError && <div className="error">{(clear.error as Error).message}</div>}
    </div>
  );
}
