import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { JobResult } from "../components/JobResult";
import { endpoints, type Job } from "../lib/api";
import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";

export function JobsPage() {
  const [open, setOpen] = useState<string | null>(null);
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => endpoints.jobs() });

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Every configuration push, with the diff it applied and how it ended. This is the audit trail."
      />
      <div className="card">
        {jobs.isLoading && <Skeleton rows={4} />}
        {jobs.isError && <div className="error">{(jobs.error as Error).message}</div>}
        {jobs.data?.length === 0 && (
          <p className="muted">Nothing has been applied yet.</p>
        )}
        {jobs.data && jobs.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>State</th>
                <th>Changes</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.data.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  open={open === job.id}
                  onToggle={() => setOpen(open === job.id ? null : job.id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function JobRow({
  job,
  open,
  onToggle,
}: {
  job: Job;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr>
        <td className="muted">{new Date(job.created_at).toLocaleString()}</td>
        <td>{job.kind}</td>
        <td>
          <span className={`badge ${job.state === "succeeded" ? "reachable" : job.state === "rolled_back" ? "drifted" : "unreachable"}`}>
            {job.state}
          </span>
        </td>
        <td className="muted">
          {String(job.result?.applied ?? 0)} applied
          {job.rollback_token ? " · rollback armed" : ""}
        </td>
        <td>
          <button onClick={onToggle}>{open ? "Hide" : "Details"}</button>
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={5}>
            <JobResult job={job} />
          </td>
        </tr>
      )}
    </>
  );
}
