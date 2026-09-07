import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints, type Job, type Plan } from "../lib/api";
import { DiffView } from "./DiffView";
import { JobResult } from "./JobResult";

/**
 * Plan-then-apply. The operator never applies blind: the diff must be fetched
 * and shown first, and the reboot consequence is stated next to the button that
 * causes it.
 */
export function ApplyPanel({ siteId }: { siteId: string }) {
  const queryClient = useQueryClient();
  const [plan, setPlan] = useState<Plan | null>(null);
  const [job, setJob] = useState<Job | null>(null);

  const doPlan = useMutation({
    mutationFn: () => endpoints.plan(siteId),
    onSuccess: (result) => {
      setPlan(result);
      setJob(null);
    },
  });

  const doApply = useMutation({
    mutationFn: () => endpoints.apply(siteId, { confirm: true }),
    onSuccess: (result) => {
      setJob(result);
      setPlan(null);
      queryClient.invalidateQueries({ queryKey: ["jobs", siteId] });
      queryClient.invalidateQueries({ queryKey: ["site", siteId] });
    },
  });

  const blocked = plan ? Object.keys(plan.unreadable).length > 0 : false;
  const nothingToDo = plan?.empty ?? false;

  return (
    <div className="card">
      <h2>Configuration</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        <strong>Plan</strong> reads the device and shows the exact difference between
        what it runs and what your configuration says. Nothing is written.{" "}
        <strong>Apply</strong> saves a backup on the device, schedules it to restore
        that backup shortly, pushes the change, then reconnects to confirm management
        still works and cancels the restore. If it cannot reconnect, the device rolls
        itself back and reboots.
      </p>

      <div className="row" style={{ justifyContent: "flex-start", marginBottom: 12 }}>
        <div className="no-grow">
          <button onClick={() => doPlan.mutate()} disabled={doPlan.isPending}>
            {doPlan.isPending ? "Planning…" : "Plan changes"}
          </button>
        </div>
        {plan && !nothingToDo && !blocked && (
          <div className="no-grow">
            <button
              className="primary"
              disabled={doApply.isPending}
              onClick={() => doApply.mutate()}
            >
              {doApply.isPending ? "Applying…" : "Apply these changes"}
            </button>
          </div>
        )}
      </div>

      {doPlan.isError && <div className="error">{(doPlan.error as Error).message}</div>}
      {doApply.isError && <div className="error">{(doApply.error as Error).message}</div>}

      {plan && !nothingToDo && !blocked && (
        <p className="muted">
          The push runs inside a dead-man rollback. If it breaks management access the
          router restores its pre-apply backup — <strong>which reboots it</strong>.
        </p>
      )}

      {plan && <DiffView plan={plan} />}
      {job && <JobResult job={job} />}
    </div>
  );
}
