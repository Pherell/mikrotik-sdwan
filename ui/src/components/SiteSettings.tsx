import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { endpoints, type Site } from "../lib/api";

/**
 * Edit a site after creation.
 *
 * Credentials are write-only: the field is always blank on load, and an empty
 * value means "leave unchanged" rather than "clear". Rendering a masked
 * placeholder would invite someone to save it back as the literal password.
 */
export function SiteSettings({ site, onDone }: { site: Site; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    name: site.name,
    description: site.description ?? "",
    region: site.region ?? "",
    role: site.role,
    mgmt_host: site.mgmt_host,
    mgmt_port: site.mgmt_port?.toString() ?? "",
    username: site.username,
    password: "",
    loopback_ip: site.loopback_ip ?? "",
    local_prefixes: site.local_prefixes.join(", "),
    drift_action: site.drift_action,
    rollback_timeout_seconds: "",
  });

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: form.name,
        description: form.description || null,
        region: form.region || null,
        role: form.role,
        mgmt_host: form.mgmt_host,
        mgmt_port: form.mgmt_port ? Number(form.mgmt_port) : null,
        username: form.username,
        loopback_ip: form.loopback_ip || null,
        local_prefixes: splitList(form.local_prefixes),
        drift_action: form.drift_action,
      };
      if (form.password) body.password = form.password;
      if (form.rollback_timeout_seconds) {
        body.rollback_timeout_seconds = Number(form.rollback_timeout_seconds);
      }
      return endpoints.updateSite(site.id, body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["site", site.id] });
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      onDone();
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <div className="card">
      <h2>Edit {site.name}</h2>
      {save.isError && <div className="error">{(save.error as Error).message}</div>}
      <form onSubmit={submit}>
        <div className="row">
          <label>
            Name
            <input
              required
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <label>
            Region
            <input
              value={form.region}
              onChange={(e) => setForm({ ...form, region: e.target.value })}
            />
          </label>
          <label>
            Role
            <select
              value={form.role}
              onChange={(e) =>
                setForm({ ...form, role: e.target.value as Site["role"] })
              }
            >
              <option value="spoke">spoke</option>
              <option value="hub">hub</option>
            </select>
          </label>
        </div>

        <div className="row">
          <label>
            Management address
            <input
              required
              value={form.mgmt_host}
              onChange={(e) => setForm({ ...form, mgmt_host: e.target.value })}
            />
          </label>
          <label>
            Port
            <input
              type="number"
              placeholder="443"
              value={form.mgmt_port}
              onChange={(e) => setForm({ ...form, mgmt_port: e.target.value })}
            />
          </label>
          <label>
            Username
            <input
              required
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
            />
          </label>
        </div>

        <div className="row">
          <label>
            New password
            <input
              type="password"
              autoComplete="new-password"
              placeholder={site.has_credentials ? "unchanged" : "none stored"}
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          </label>
          <label>
            Loopback
            <input
              placeholder="10.254.0.7"
              value={form.loopback_ip}
              onChange={(e) => setForm({ ...form, loopback_ip: e.target.value })}
            />
          </label>
          <label>
            Local prefixes
            <input
              placeholder="192.168.10.0/24, 192.168.11.0/24"
              value={form.local_prefixes}
              onChange={(e) => setForm({ ...form, local_prefixes: e.target.value })}
            />
          </label>
        </div>

        <div className="row">
          <label>
            On drift
            <select
              value={form.drift_action}
              onChange={(e) => setForm({ ...form, drift_action: e.target.value })}
            >
              <option value="alert">alert only</option>
              <option value="auto-remediate">re-apply automatically</option>
            </select>
          </label>
          <label>
            Rollback timeout (seconds)
            <input
              type="number"
              placeholder="120"
              value={form.rollback_timeout_seconds}
              onChange={(e) =>
                setForm({ ...form, rollback_timeout_seconds: e.target.value })
              }
            />
          </label>
        </div>

        {form.drift_action === "auto-remediate" && (
          <p className="warn">
            Auto-remediate silently reverts anything an engineer changes on this
            device, including mid-troubleshooting. Use it only where nobody logs in
            by hand.
          </p>
        )}

        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div style={{ flex: 0 }}>
            <button className="primary" type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save"}
            </button>
          </div>
          <div style={{ flex: 0 }}>
            <button type="button" onClick={onDone}>
              Cancel
            </button>
          </div>
        </div>
        <p className="muted">
          Changing addressing or prefixes only updates intent. Apply the site to put
          it on the device.
        </p>
      </form>
    </div>
  );
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}
