/**
 * The device's front panel.
 *
 * Answers, at a glance, the questions a list of eleven interface names cannot:
 * which sockets are the uplinks the fabric is built on, which have a cable in
 * them, which are free, and which the controller owns and will overwrite on the
 * next apply.
 *
 * It is read-only, and deliberately so -- see app/services/ports.py for why
 * port *configuration* is a different product.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints, type Port, type PortRole } from "../lib/api";

const ROLE_LABEL: Record<PortRole, string> = {
  wan: "Uplink",
  candidate: "Looks like an uplink",
  lan: "LAN",
  unused: "Free",
  bridge: "Bridge",
  tunnel: "Tunnel",
  other: "Other",
};

const PHYSICAL: PortRole[] = ["wan", "candidate", "lan", "unused"];

/** Why the device appears to be using a port as an uplink. */
function signals(port: Port): string {
  const found = [
    port.dhcp_client && "a DHCP client",
    port.default_route && "a default route",
  ].filter(Boolean) as string[];
  return found.join(" and ");
}

function bytes(value: number | null): string {
  if (value === null) return "—";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let n = value;
  let i = 0;
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000;
    i += 1;
  }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function Socket({
  port,
  selected,
  onSelect,
}: {
  port: Port;
  selected: boolean;
  onSelect: () => void;
}) {
  // disabled beats down: a port switched off administratively is a different
  // fact from one with no cable, and conflating them wastes a site visit.
  const state = port.disabled ? "off" : port.running ? "up" : "down";
  return (
    <button
      type="button"
      className={`port port-${port.role} port-${state}${selected ? " selected" : ""}`}
      onClick={onSelect}
      title={`${port.name} — ${ROLE_LABEL[port.role]}, ${
        port.disabled ? "disabled" : port.running ? "link up" : "no link"
      }`}
      aria-pressed={selected}
    >
      <span className="port-socket" aria-hidden="true" />
      <span className="port-name">{port.name}</span>
      <span className="port-sub">
        {port.disabled ? "off" : port.running ? (port.speed ?? "up") : "—"}
      </span>
    </button>
  );
}

function Detail({ port }: { port: Port }) {
  return (
    <dl className="kv port-detail">
      <dt>Role</dt>
      <dd>
        {ROLE_LABEL[port.role]}
        {port.role === "candidate" && (
          <div className="muted">
            The device has {signals(port)} on this port, but no uplink is declared
            for it. Add one under Uplinks below and the fabric can use it.
          </div>
        )}
        {port.wan_name && (
          <>
            {" · "}
            <strong>{port.wan_name}</strong>
            {port.wan_enabled === false && <span className="muted"> (disabled)</span>}
          </>
        )}
      </dd>
      <dt>Link</dt>
      <dd>
        {port.disabled ? "administratively off" : port.running ? "up" : "no link"}
        {port.speed && ` · ${port.speed}`}
      </dd>
      <dt>Type</dt>
      <dd>
        {port.type}
        {port.default_name && port.default_name !== port.name && (
          <span className="muted"> · factory {port.default_name}</span>
        )}
      </dd>
      <dt>Addresses</dt>
      <dd>{port.addresses.length ? port.addresses.join(", ") : "none"}</dd>
      {port.bridge && (
        <>
          <dt>Bridge</dt>
          <dd>{port.bridge}</dd>
        </>
      )}
      <dt>MAC</dt>
      <dd>{port.mac ?? "—"}</dd>
      <dt>Traffic</dt>
      <dd>
        {bytes(port.rx_bytes)} in · {bytes(port.tx_bytes)} out
      </dd>
      {port.comment && (
        <>
          <dt>Comment</dt>
          <dd>{port.comment}</dd>
        </>
      )}
    </dl>
  );
}

export function PortPanel({ siteId }: { siteId: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const ports = useQuery({
    queryKey: ["ports", siteId],
    queryFn: () => endpoints.ports(siteId),
    // The device is reached on every call, so this is not free. Manual only.
    staleTime: 30_000,
    retry: false,
  });

  const all = ports.data ?? [];
  const physical = all.filter((p) => PHYSICAL.includes(p.role));
  const logical = all.filter((p) => !PHYSICAL.includes(p.role));
  const chosen = all.find((p) => p.name === selected) ?? null;

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>Ports</h2>
        <div className="no-grow">
          <button
            className="sm"
            onClick={() => ports.refetch()}
            data-busy={ports.isFetching}
            disabled={ports.isFetching}
          >
            {ports.isFetching ? "Reading…" : "Refresh"}
          </button>
        </div>
      </div>

      {ports.isLoading && <p className="muted">Reading the device…</p>}
      {ports.isError && (
        <div className="error">{(ports.error as Error).message}</div>
      )}

      {!ports.isLoading && !ports.isError && (
        <>
          {physical.length > 0 && (
            <div className="port-faceplate">
              {physical.map((p) => (
                <Socket
                  key={p.name}
                  port={p}
                  selected={p.name === selected}
                  onSelect={() => setSelected(p.name === selected ? null : p.name)}
                />
              ))}
            </div>
          )}

          <div className="port-legend muted">
            <span>
              <i className="swatch port-wan" /> uplink
            </span>
            <span>
              <i className="swatch port-candidate" /> looks like an uplink
            </span>
            <span>
              <i className="swatch port-lan" /> LAN
            </span>
            <span>
              <i className="swatch port-unused" /> free
            </span>
            <span>no fill = no link</span>
          </div>

          {physical.some((p) => p.role === "candidate") && (
            <div className="warn">
              {physical
                .filter((p) => p.role === "candidate")
                .map((p) => p.name)
                .join(", ")}{" "}
              {physical.filter((p) => p.role === "candidate").length > 1
                ? "are carrying"
                : "is carrying"}{" "}
              internet access the controller does not know about. Declare them as
              uplinks below to use them in a fabric.
            </div>
          )}

          {logical.length > 0 && (
            <div className="port-logical">
              {logical.map((p) => (
                <button
                  key={p.name}
                  type="button"
                  className={`badge port-chip${p.name === selected ? " selected" : ""}`}
                  onClick={() => setSelected(p.name === selected ? null : p.name)}
                >
                  {p.name}
                  <span className="muted"> · {ROLE_LABEL[p.role]}</span>
                  {p.managed && <span className="port-managed" title="Managed by the controller"> ●</span>}
                </button>
              ))}
            </div>
          )}

          {chosen && (
            <>
              {chosen.managed && (
                <div className="warn">
                  The controller owns <code>{chosen.name}</code>. Editing it on the
                  device by hand will be reverted on the next apply.
                </div>
              )}
              <Detail port={chosen} />
            </>
          )}

          {all.length === 0 && (
            <p className="muted">The device reported no interfaces.</p>
          )}
        </>
      )}
    </div>
  );
}
