/**
 * Three different things wearing one word.
 *
 * "The log" means the controller's audit trail (who changed what here), an
 * apply's job log (what happened when we pushed), or the router's own log
 * (what the device thinks went wrong). They answer different questions and
 * belong to different owners, so they are three tabs rather than one merged
 * stream — merging them produces a feed where nothing can be found.
 *
 * The job log already has a page of its own; this links to it rather than
 * showing a worse copy.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { endpoints, type AuditEvent, type DeviceLogEntry } from "../lib/api";

type Tab = "activity" | "device";

export function LogsPage() {
  // Already in the cache -- App fetches it on every render of the shell, so
  // this is a read, not a second request.
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });

  // The audit trail is admin-only on the server. Showing an operator a tab
  // that can only ever return 403 is worse than not showing it.
  const canSeeAudit = me.data?.role === "admin";

  // Derived rather than initialised, because the role arrives after the first
  // render: useState would have frozen an admin onto the device tab.
  const [picked, setTab] = useState<Tab | null>(null);
  const tab: Tab = picked ?? (canSeeAudit ? "activity" : "device");

  return (
    <>
      <PageHeader
        title="Logs"
        description="Who changed the controller, and what the routers themselves have to say. What happened during a particular push lives with that job."
      >
        <Link to="/jobs" className="button">
          Job history
        </Link>
      </PageHeader>

      <div className="tabs">
        {canSeeAudit && (
          <button
            className={`tab${tab === "activity" ? " active" : ""}`}
            onClick={() => setTab("activity")}
          >
            Controller activity
          </button>
        )}
        <button
          className={`tab${tab === "device" ? " active" : ""}`}
          onClick={() => setTab("device")}
        >
          Device log
        </button>
      </div>

      {tab === "activity" && canSeeAudit && <ActivityPanel />}
      {tab === "device" && <DeviceLogPanel />}
    </>
  );
}

// -- controller activity ----------------------------------------------------

function ActivityPanel() {
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");

  const actions = useQuery({
    queryKey: ["audit-actions"],
    queryFn: endpoints.auditActions,
  });
  const events = useQuery({
    queryKey: ["audit", action, actor],
    queryFn: () => endpoints.audit({ action, actor_email: actor, limit: 200 }),
  });

  return (
    <div className="card">
      <div className="row">
        <label>
          Action
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="">everything</option>
            {/* The list comes from the data. A hardcoded one goes stale the
                first time an endpoint is added, and a stale filter hides
                events rather than failing. */}
            {actions.data?.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Who
          <input
            placeholder="any signed-in user"
            value={actor}
            onChange={(e) => setActor(e.target.value)}
          />
        </label>
      </div>

      {events.isLoading && <Skeleton rows={5} />}
      {events.isError && <div className="error">{(events.error as Error).message}</div>}

      {events.data?.length === 0 && (
        <p className="muted">Nothing matches. Every state-changing call is recorded here.</p>
      )}

      {events.data && events.data.length > 0 && (
        <table className="stack">
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>Did what</th>
              <th>To</th>
              <th>From</th>
            </tr>
          </thead>
          <tbody>
            {events.data.map((event) => (
              <AuditRow key={event.id} event={event} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function AuditRow({ event }: { event: AuditEvent }) {
  return (
    <tr>
      <td data-label="When" className="muted nowrap">
        {new Date(event.created_at).toLocaleString()}
      </td>
      <td data-label="Who">{event.actor_email ?? <span className="muted">system</span>}</td>
      <td data-label="Did what">
        <code>{event.action}</code>
        {event.detail && Object.keys(event.detail).length > 0 && (
          <div className="muted">{summarise(event.detail)}</div>
        )}
      </td>
      <td data-label="To" className="muted">
        {event.object_type ?? "—"}
      </td>
      <td data-label="From" className="muted">
        {event.source_ip ?? "—"}
      </td>
    </tr>
  );
}

/**
 * The detail blob is deliberately untyped on the server — an audit trail keeps
 * what happened, not what a schema anticipated. So render it generically
 * rather than pretending to know its shape.
 */
function summarise(detail: Record<string, unknown>): string {
  return Object.entries(detail)
    .slice(0, 4)
    .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`)
    .join(" · ");
}

// -- the router's own log ---------------------------------------------------

function DeviceLogPanel() {
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });
  const [siteId, setSiteId] = useState("");
  const [topic, setTopic] = useState("");
  const [contains, setContains] = useState("");

  const chosen = siteId || sites.data?.[0]?.id || "";

  const log = useQuery({
    queryKey: ["device-log", chosen, topic, contains],
    queryFn: () => endpoints.deviceLog(chosen, { topic, contains }),
    enabled: chosen !== "",
    retry: false,
  });

  return (
    <div className="card">
      <div className="row">
        <label>
          Device
          <select
            value={chosen}
            onChange={(e) => setSiteId(e.target.value)}
            disabled={!sites.data?.length}
          >
            {sites.data?.length === 0 && <option value="">No devices yet</option>}
            {sites.data?.map((site) => (
              <option key={site.id} value={site.id}>
                {site.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Topic
          <select value={topic} onChange={(e) => setTopic(e.target.value)}>
            <option value="">everything</option>
            <option value="error">error</option>
            <option value="warning">warning</option>
            <option value="ipsec">ipsec</option>
            <option value="dhcp">dhcp</option>
            <option value="route">route</option>
            <option value="firewall">firewall</option>
            <option value="system">system</option>
            <option value="script">script</option>
          </select>
        </label>
        <label>
          Containing
          <input
            placeholder="any text"
            value={contains}
            onChange={(e) => setContains(e.target.value)}
          />
        </label>
      </div>

      <p className="muted" style={{ marginTop: 0 }}>
        RouterOS keeps this in memory, so it is short and it starts again at
        every reboot. It is the only place that says <em>why</em> the device did
        something — a rejected IPsec proposal appears here and nowhere else.
      </p>

      {log.isLoading && <Skeleton rows={6} />}
      {log.isError && <div className="error">{(log.error as Error).message}</div>}

      {log.data?.length === 0 && (
        <p className="muted">
          Nothing in the log matches. A freshly rebooted router has very little
          in it.
        </p>
      )}

      {log.data && log.data.length > 0 && (
        <table className="stack">
          <thead>
            <tr>
              <th>When</th>
              <th>Topics</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
            {log.data.map((entry, i) => (
              <LogRow key={`${entry.time}-${i}`} entry={entry} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function LogRow({ entry }: { entry: DeviceLogEntry }) {
  const tone =
    entry.severity === "error"
      ? "unreachable"
      : entry.severity === "warning"
        ? "drifted"
        : "";
  return (
    <tr>
      <td data-label="When" className="muted nowrap">
        {entry.time ?? "—"}
      </td>
      <td data-label="Topics">
        {/* The worst topic decides the colour: "ipsec,error" is an error that
            happens to be about ipsec, not the other way round. */}
        <span className={`badge ${tone}`}>{entry.topics.join(", ") || "—"}</span>
      </td>
      <td data-label="Message">{entry.message}</td>
    </tr>
  );
}
