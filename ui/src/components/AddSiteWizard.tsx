import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { endpoints, type ProbeResult, type Site } from "../lib/api";

/**
 * Two-step onboarding: create the site, then probe it and adopt the uplinks it
 * reports. Probing is read-only, so the operator sees what the controller found
 * before anything is written to the device.
 */
export function AddSiteWizard({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    name: "",
    mgmt_host: "",
    username: "admin",
    password: "",
    role: "spoke",
    region: "",
  });
  const [site, setSite] = useState<Site | null>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const create = useMutation({
    mutationFn: () =>
      endpoints.createSite({
        name: form.name,
        mgmt_host: form.mgmt_host,
        username: form.username,
        password: form.password || null,
        role: form.role,
        region: form.region || null,
      }),
    onSuccess: async (created) => {
      setSite(created);
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      const result = await endpoints.probe(created.id);
      setProbe(result);
      // Pre-select every discovered uplink; the operator unticks what is wrong.
      setSelected(new Set(result.suggested_wans.map((_, i) => i)));
    },
  });

  const adopt = useMutation({
    mutationFn: async () => {
      if (!site || !probe) return;
      for (const [index, wan] of probe.suggested_wans.entries()) {
        if (selected.has(index)) await endpoints.addWan(site.id, wan);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      onClose();
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  function toggle(index: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  if (site && probe) {
    return (
      <div className="card">
        <h2>Probe result — {site.name}</h2>
        {!probe.reachable ? (
          <>
            <div className="error">{probe.error}</div>
            <p className="muted">
              The site was created. Fix credentials or reachability, then probe again
              from its detail page.
            </p>
            <button onClick={onClose}>Close</button>
          </>
        ) : (
          <>
            <dl className="kv">
              <dt>RouterOS</dt>
              <dd>{probe.version}</dd>
              <dt>Board</dt>
              <dd>
                {probe.board_name} ({probe.architecture})
              </dd>
              <dt>Identity</dt>
              <dd>{probe.identity}</dd>
              <dt>WireGuard</dt>
              <dd>{probe.has_wireguard ? "available" : "not available"}</dd>
              <dt>Netwatch thresholds</dt>
              <dd>
                {probe.has_netwatch_thresholds
                  ? "available"
                  : "not available (needs RouterOS 7.7+)"}
              </dd>
            </dl>

            <h2 style={{ marginTop: 20 }}>Discovered uplinks</h2>
            {probe.suggested_wans.length === 0 ? (
              <p className="muted">
                No default route or DHCP client found. Add uplinks by hand on the site
                page.
              </p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 40 }} />
                    <th>Name</th>
                    <th>Interface</th>
                    <th>Public IP</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {probe.suggested_wans.map((wan, index) => (
                    <tr key={wan.interface}>
                      <td>
                        <input
                          type="checkbox"
                          style={{ width: "auto" }}
                          checked={selected.has(index)}
                          onChange={() => toggle(index)}
                        />
                      </td>
                      <td>{wan.name}</td>
                      <td>{wan.interface}</td>
                      <td>{wan.public_ip ?? <span className="muted">none</span>}</td>
                      <td className="muted">
                        {[
                          wan.dynamic ? "DHCP" : null,
                          wan.nat_behind ? "behind NAT — dial-out only" : null,
                        ]
                          .filter(Boolean)
                          .join(" · ") || "static, publicly reachable"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {adopt.isError && <div className="error">{(adopt.error as Error).message}</div>}
            <div className="row" style={{ marginTop: 16, justifyContent: "flex-start" }}>
              <div style={{ flex: 0 }}>
                <button
                  className="primary"
                  disabled={adopt.isPending}
                  onClick={() => adopt.mutate()}
                >
                  {adopt.isPending ? "Adding…" : `Add ${selected.size} uplink(s)`}
                </button>
              </div>
              <div style={{ flex: 0 }}>
                <button onClick={onClose}>Skip</button>
              </div>
            </div>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Add a site</h2>
      {create.isError && <div className="error">{(create.error as Error).message}</div>}
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
              onChange={(e) => setForm({ ...form, role: e.target.value })}
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
              placeholder="203.0.113.10"
              value={form.mgmt_host}
              onChange={(e) => setForm({ ...form, mgmt_host: e.target.value })}
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
          <label>
            Password
            <input
              type="password"
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
            />
          </label>
        </div>
        <p className="muted">
          The device needs the <code>www-ssl</code> service enabled for the REST API.
          Probing only reads; nothing is written to the router.
        </p>
        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div style={{ flex: 0 }}>
            <button className="primary" type="submit" disabled={create.isPending}>
              {create.isPending ? "Creating and probing…" : "Create and probe"}
            </button>
          </div>
          <div style={{ flex: 0 }}>
            <button type="button" onClick={onClose}>
              Cancel
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}
