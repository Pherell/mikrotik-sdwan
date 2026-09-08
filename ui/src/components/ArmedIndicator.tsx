/**
 * A device is scheduled to restore a backup and reboot itself.
 *
 * This is the most urgent state the system can be in, and it was visible only
 * on the Overview page. Someone who applied a change, saw it fail verification,
 * and then navigated to the site page to investigate lost the one banner
 * telling them a reboot was pending.
 *
 * It sits in the shell, so it follows you.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { endpoints, getToken } from "../lib/api";

export function ArmedIndicator() {
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => endpoints.jobs(),
    refetchInterval: 15_000,
    enabled: Boolean(getToken()),
  });

  const armed = (jobs.data ?? []).filter((j) => j.rollback_token);
  if (armed.length === 0) return null;

  return (
    <div className="armed-strip">
      <strong>
        {armed.length} device{armed.length > 1 ? "s are" : " is"} scheduled to restore a
        backup and reboot.
      </strong>{" "}
      The controller could not confirm management access after a push. If the
      configuration is actually fine, disarm it from the device page before it fires.{" "}
      <Link to="/jobs">See the jobs →</Link>
    </div>
  );
}
