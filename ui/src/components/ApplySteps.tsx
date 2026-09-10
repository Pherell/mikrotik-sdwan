/**
 * An apply, read as a process rather than a wall of text.
 *
 * The job log already records every phase and every row the controller wrote
 * -- but as one undifferentiated blob, which is exactly the wrong shape for
 * the question an operator actually has: how far did it get, and which step
 * broke. Two line shapes come back:
 *
 *   14:02:11 backup saved as sdwan-pre-4ec18e1e   <- a timestamped phase
 *   ok   add /ip/ipsec/profile                    <- one row, written
 *   FAIL add /tool/netwatch: unknown parameter …  <- one row, refused
 *
 * so they are split into steps and the failing one is given the weight it
 * deserves. The raw log stays available underneath: when something is wrong in
 * a way this parser did not anticipate, the unparsed text is the evidence.
 */

type StepKind = "ok" | "fail" | "info";

export interface Step {
  kind: StepKind;
  time: string | null;
  text: string;
  detail?: string;
}

const TIMESTAMPED = /^(\d{2}:\d{2}:\d{2})\s+(.*)$/;

export function parseApplyLog(log: string): Step[] {
  return log
    .split("\n")
    .map((line) => line.trimEnd())
    .filter((line) => line.length > 0)
    .map((line): Step => {
      if (line.startsWith("ok ")) {
        return { kind: "ok", time: null, text: line.slice(3).trim() };
      }
      if (line.startsWith("FAIL ")) {
        const rest = line.slice(5).trim();
        // "add /tool/netwatch: <device said this>" -- keep the row and the
        // reason apart so the reason can be shown as the reason.
        const split = rest.indexOf(": ");
        return split === -1
          ? { kind: "fail", time: null, text: rest }
          : {
              kind: "fail",
              time: null,
              text: rest.slice(0, split),
              detail: rest.slice(split + 2),
            };
      }
      const stamped = TIMESTAMPED.exec(line);
      return stamped
        ? { kind: "info", time: stamped[1] ?? null, text: stamped[2] ?? line }
        : { kind: "info", time: null, text: line };
    });
}

const MARK: Record<StepKind, string> = { ok: "✓", fail: "✕", info: "·" };

export function ApplySteps({ log }: { log: string }) {
  const steps = parseApplyLog(log);
  if (steps.length === 0) return null;

  const written = steps.filter((s) => s.kind === "ok").length;
  const refused = steps.filter((s) => s.kind === "fail").length;

  return (
    <div style={{ marginTop: 12 }}>
      <div className="muted" style={{ marginBottom: 6 }}>
        What the controller did — {written} row(s) written
        {refused > 0 ? `, ${refused} refused by the device` : ""}
      </div>

      <ol
        style={{
          listStyle: "none",
          margin: 0,
          padding: 0,
          display: "flex",
          flexDirection: "column",
          gap: 2,
        }}
      >
        {steps.map((step, index) => (
          <li
            key={index}
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 8,
              fontSize: 13.5,
              lineHeight: 1.5,
            }}
          >
            <span
              aria-hidden="true"
              className={step.kind === "fail" ? "" : "muted"}
              style={{
                width: 14,
                flex: "none",
                textAlign: "center",
                color: step.kind === "fail" ? "var(--bad)" : undefined,
              }}
            >
              {MARK[step.kind]}
            </span>
            {step.time && (
              <span className="muted" style={{ flex: "none", fontFamily: "monospace" }}>
                {step.time}
              </span>
            )}
            <span style={{ minWidth: 0 }}>
              <span
                style={{
                  fontFamily: step.kind === "info" ? undefined : "monospace",
                  wordBreak: "break-word",
                }}
              >
                {step.text}
              </span>
              {step.detail && (
                <span
                  style={{
                    display: "block",
                    color: "var(--bad)",
                    wordBreak: "break-word",
                  }}
                >
                  {step.detail}
                </span>
              )}
            </span>
          </li>
        ))}
      </ol>

      <details style={{ marginTop: 10 }}>
        <summary className="muted" style={{ cursor: "pointer" }}>
          Raw job log
        </summary>
        <pre className="diff">{log}</pre>
      </details>
    </div>
  );
}
