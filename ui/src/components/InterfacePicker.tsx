/**
 * Choose an interface from the ones the device actually has.
 *
 * It was a free-text box. A typo produced an uplink pointing at an interface
 * that does not exist, which renders configuration that applies cleanly and
 * routes nothing — the failure only shows up as traffic that never arrives.
 *
 * Falls back to free text when the device cannot be read. An unreachable
 * device is a reason to be careful, not a reason to stop someone entering the
 * uplink they already know is there.
 */

import { useQuery } from "@tanstack/react-query";

import { endpoints, type PortRole } from "../lib/api";

const ROLE_NOTE: Record<PortRole, string> = {
  wan: "already an uplink",
  candidate: "looks like an uplink",
  lan: "LAN",
  unused: "no cable",
  bridge: "bridge",
  tunnel: "tunnel — managed by the controller",
  other: "",
};

export function InterfacePicker({
  siteId,
  value,
  onChange,
}: {
  siteId: string;
  value: string;
  onChange: (name: string) => void;
}) {
  const ports = useQuery({
    queryKey: ["ports", siteId],
    queryFn: () => endpoints.ports(siteId),
    retry: false,
    staleTime: 30_000,
  });

  if (ports.isLoading) {
    return (
      <label>
        Interface
        <select disabled>
          <option>Reading the device…</option>
        </select>
      </label>
    );
  }

  if (ports.isError || !ports.data?.length) {
    return (
      <label>
        Interface
        <input
          required
          placeholder="ether1"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        <span className="muted field-note">
          Could not read the device, so this is not a list. Check the name
          against the router.
        </span>
      </label>
    );
  }

  // A saved uplink can name an interface that has since been renamed or
  // removed. Keep it selectable and say so, rather than silently switching
  // the value to whatever happens to be first.
  const known = ports.data.some((p) => p.name === value);
  const missing = value !== "" && !known;

  return (
    <label>
      Interface
      <select required value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="" disabled>
          Choose an interface…
        </option>
        {missing && <option value={value}>{value} — not on this device</option>}
        {ports.data.map((port) => {
          const note = ROLE_NOTE[port.role];
          const state = port.disabled ? "disabled" : port.running ? "" : "no link";
          const suffix = [note, state].filter(Boolean).join(", ");
          return (
            <option key={port.name} value={port.name}>
              {port.name}
              {suffix && ` — ${suffix}`}
            </option>
          );
        })}
      </select>
      {missing && (
        <span className="muted field-note">
          The device has no interface called <code>{value}</code>. Configuration
          for this uplink will apply and carry no traffic.
        </span>
      )}
    </label>
  );
}
