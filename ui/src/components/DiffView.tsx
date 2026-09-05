import type { Plan } from "../lib/api";

/**
 * Renders a plan the way the differ produced it: one line per change, prefixed
 * `+` add, `-` remove, `~` update, `!` a menu that could not be read. Secrets
 * are already masked server-side; nothing here needs to redact.
 */
export function DiffView({ plan }: { plan: Plan }) {
  if (plan.empty && Object.keys(plan.unreadable).length === 0) {
    return <p className="muted">No changes — the device already matches intent.</p>;
  }

  return (
    <>
      <p className="muted">
        {plan.counts.add} to add · {plan.counts.set} to change · {plan.counts.remove} to
        remove
      </p>

      {Object.keys(plan.unreadable).length > 0 && (
        <div className="error">
          <strong>Some menus could not be read.</strong> Applying now would look like a
          request to delete everything the controller manages in them, so apply is
          blocked.
          <ul>
            {Object.entries(plan.unreadable).map(([path, err]) => (
              <li key={path}>
                <code>{path}</code>: {err}
              </li>
            ))}
          </ul>
        </div>
      )}

      {plan.sections.map((section) => (
        <div key={section.path} style={{ marginBottom: 12 }}>
          <div className="muted" style={{ marginBottom: 4 }}>
            {section.path}
          </div>
          <pre className="diff">
            {section.lines.map((line, i) => (
              <div key={i} className={`diff-line ${lineClass(line)}`}>
                {line}
              </div>
            ))}
          </pre>
        </div>
      ))}
    </>
  );
}

function lineClass(line: string): string {
  const marker = line.trimStart()[0];
  if (marker === "+") return "add";
  if (marker === "-") return "remove";
  if (marker === "~") return "change";
  if (marker === "!") return "problem";
  return "";
}
