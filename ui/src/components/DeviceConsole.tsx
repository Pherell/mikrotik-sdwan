import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { endpoints } from "../lib/api";

/**
 * Type a RouterOS command, see what it says.
 *
 * This replaced a dropdown of menus. The dropdown was safe and nearly useless:
 * it could only answer questions somebody had anticipated, and the whole
 * reason to open a console is a question nobody anticipated.
 *
 * It is a command console, not a shell. The server keeps the allowlist and
 * decides what runs; this sends what was typed and shows what came back,
 * refusals included — a refusal that explains itself is how somebody learns
 * where the boundary is.
 */

type Entry =
  | { kind: "command"; text: string }
  | { kind: "resolved"; text: string }
  | { kind: "rows"; rows: Record<string, unknown>[] }
  | { kind: "error"; text: string }
  | { kind: "note"; text: string };

const EXAMPLES = [
  "/ip/route/print",
  "/ip/address/print",
  "/interface/print",
  "/ip/ipsec/active-peers/print",
  "/routing/bgp/session/print",
  "/ping address=8.8.8.8 count=3",
];

export function DeviceConsole({ siteId }: { siteId: string }) {
  const [open, setOpen] = useState(false);
  const [command, setCommand] = useState("");
  const [history, setHistory] = useState<Entry[]>([]);
  // Shell-style recall. Without it you retype a long command to change one
  // character, which is most of what anyone does at a console.
  const [recall, setRecall] = useState<string[]>([]);
  const [recallAt, setRecallAt] = useState<number | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const run = useMutation({
    mutationFn: (text: string) => endpoints.console(siteId, text),
    onSuccess: (result, text) => {
      const lines: Entry[] = [];
      // Only when it differs from what was typed. An echo of every command
      // halves how much output fits on the screen.
      if (result.resolved !== text.trim()) {
        lines.push({ kind: "resolved", text: result.resolved });
      }
      if (result.error) {
        lines.push({ kind: "error", text: result.error });
      } else if (result.rows.length === 0) {
        lines.push({ kind: "note", text: "no rows" });
      } else {
        lines.push({ kind: "rows", rows: result.rows });
      }
      setHistory((prev) => [...prev, ...lines]);
    },
    onError: (e) =>
      setHistory((prev) => [...prev, { kind: "error", text: (e as Error).message }]),
  });

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [history]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const text = command.trim();
    if (!text) return;
    setHistory((prev) => [...prev, { kind: "command", text }]);
    setRecall((prev) => [...prev, text]);
    setRecallAt(null);
    setCommand("");
    run.mutate(text);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    if (recall.length === 0) return;
    event.preventDefault();
    const next =
      event.key === "ArrowUp"
        ? recallAt === null
          ? recall.length - 1
          : Math.max(0, recallAt - 1)
        : recallAt === null || recallAt + 1 >= recall.length
          ? null
          : recallAt + 1;
    setRecallAt(next);
    // ?? "" because noUncheckedIndexedAccess makes recall[next] possibly
    // undefined even though next was derived from the array's own length.
    setCommand(next === null ? "" : (recall[next] ?? ""));
  }

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>Console</h2>
        <div className="no-grow">
          <button onClick={() => setOpen(!open)}>{open ? "Hide" : "Open"}</button>
        </div>
      </div>

      {open && (
        <>
          <p className="muted">
            Runs one RouterOS command on the device and shows what it said.
            Reads and probes only — the controller keeps an allowlist and says
            why when it refuses. Changes go through plan and apply, where they
            leave a diff, a backup and a rollback behind them.
          </p>

          <div className="console-out">
            {history.length === 0 && (
              <div className="console-note">
                Try one of these, or type your own:
                <div className="console-examples">
                  {EXAMPLES.map((example) => (
                    <button
                      key={example}
                      type="button"
                      className="sm"
                      onClick={() => setCommand(example)}
                    >
                      {example}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {history.map((entry, i) => (
              <Line key={i} entry={entry} />
            ))}
            {run.isPending && <div className="console-note">running…</div>}
            <div ref={endRef} />
          </div>

          <form onSubmit={submit}>
            <div className="console-input">
              <span aria-hidden="true">&gt;</span>
              <input
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="/ip/route/print"
                spellCheck={false}
                autoComplete="off"
                aria-label="RouterOS command"
              />
              <div className="no-grow">
                <button className="primary" type="submit" disabled={run.isPending}>
                  Run
                </button>
              </div>
            </div>
          </form>
        </>
      )}
    </div>
  );
}

function Line({ entry }: { entry: Entry }) {
  if (entry.kind === "command") {
    return (
      <div className="console-cmd">
        <span aria-hidden="true">&gt; </span>
        {entry.text}
      </div>
    );
  }
  if (entry.kind === "resolved") {
    return <div className="console-note">ran {entry.text}</div>;
  }
  if (entry.kind === "error") {
    return <div className="console-err">{entry.text}</div>;
  }
  if (entry.kind === "note") {
    return <div className="console-note">{entry.text}</div>;
  }
  return <Rows rows={entry.rows} />;
}

/**
 * Rows as a table rather than raw JSON.
 *
 * Columns are the union of the keys present, in first-seen order. RouterOS
 * rows are ragged — not every row carries every property — so a union is the
 * only way nothing is silently hidden. `.id` goes last: a column of `*7` on
 * every row pushes the useful fields off the right-hand edge.
 */
function Rows({ rows }: { rows: Record<string, unknown>[] }) {
  const columns: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!columns.includes(key)) columns.push(key);
    }
  }
  columns.sort((a, b) => Number(a.startsWith(".")) - Number(b.startsWith(".")));

  return (
    <div className="console-rows">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((column) => (
                <td key={column} className={column.startsWith(".") ? "muted" : undefined}>
                  {format(row[column])}
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
  if (typeof value === "object") return JSON.stringify(value);
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
