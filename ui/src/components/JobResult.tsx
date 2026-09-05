import type { Job, JobState } from "../lib/api";

const STATE_CLASS: Record<JobState, string> = {
  queued: "",
  running: "",
  succeeded: "reachable",
  failed: "unreachable",
  rolled_back: "drifted",
  cancelled: "",
};

export function JobResult({ job }: { job: Job }) {
  const armed = Boolean(job.rollback_token);

  return (
    <div>
      <p>
        <span className={`badge ${STATE_CLASS[job.state]}`}>{job.state}</span>{" "}
        <span className="muted">
          {String(job.result?.applied ?? 0)} of {String(job.result?.planned ?? 0)}{" "}
          change(s) applied
        </span>
      </p>

      {armed && (
        <div className="error">
          <strong>The rollback is still armed on this device.</strong> The controller
          could not confirm management access after the push, so the router will restore{" "}
          <code>{job.backup_name}</code> and reboot. Do not re-apply until it comes back.
        </div>
      )}

      {job.error && !armed && <div className="error">{job.error}</div>}

      {job.diff?.text && (
        <>
          <div className="muted" style={{ marginBottom: 4 }}>
            What was pushed
          </div>
          <pre className="diff">{job.diff.text}</pre>
        </>
      )}

      {job.log && (
        <>
          <div className="muted" style={{ marginBottom: 4 }}>
            Job log
          </div>
          <pre className="diff">{job.log}</pre>
        </>
      )}
    </div>
  );
}
