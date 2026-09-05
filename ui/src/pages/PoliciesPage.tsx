import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { endpoints, type Policy, type SlaProfile } from "../lib/api";

export function PoliciesPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);

  const policies = useQuery({ queryKey: ["policies"], queryFn: endpoints.policies });
  const slas = useQuery({ queryKey: ["slas"], queryFn: endpoints.slaProfiles });
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });

  // Every tag any uplink carries, plus every WAN name — the set a policy can
  // actually prefer. Offering free text here is how you get a policy that
  // silently matches nothing.
  const tags = new Set<string>();
  for (const site of sites.data ?? []) {
    for (const wan of site.wans) {
      tags.add(wan.name);
      for (const tag of Object.keys(wan.tags ?? {})) tags.add(tag);
    }
  }

  const remove = useMutation({
    mutationFn: (id: string) => endpoints.deletePolicy(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["policies"] }),
  });

  return (
    <>
      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>Steering policies</h2>
          <div style={{ flex: 0 }}>
            <button className="primary" onClick={() => setAdding(true)}>
              New policy
            </button>
          </div>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Rules are evaluated top to bottom by priority and the first match wins.
          Matching is by prefix, port and DSCP — RouterOS has no usable
          application classifier, so an app group is a prefix list, not DPI.
        </p>
      </div>

      {adding && (
        <NewPolicyForm
          tags={[...tags].sort()}
          slas={slas.data ?? []}
          onDone={() => {
            setAdding(false);
            queryClient.invalidateQueries({ queryKey: ["policies"] });
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="card">
        {policies.isLoading && <p className="muted">Loading…</p>}
        {policies.data?.length === 0 && (
          <p className="muted">No policies. All traffic follows the routing table.</p>
        )}
        {policies.data && policies.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Priority</th>
                <th>Name</th>
                <th>Match</th>
                <th>Prefers</th>
                <th>SLA</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {policies.data.map((p) => (
                <tr key={p.id}>
                  <td>{p.priority}</td>
                  <td>
                    {p.name}
                    {!p.enabled && <span className="muted"> · disabled</span>}
                  </td>
                  <td className="muted">{describeMatch(p)}</td>
                  <td>{p.prefer_tags.join(" → ")}</td>
                  <td className="muted">
                    {slas.data?.find((s) => s.id === p.sla_profile_id)?.name ?? "default"}
                  </td>
                  <td>
                    <button onClick={() => remove.mutate(p.id)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {remove.isError && <div className="error">{(remove.error as Error).message}</div>}
      </div>

      <SlaProfiles profiles={slas.data ?? []} />
    </>
  );
}

function describeMatch(p: Policy): string {
  const parts: string[] = [];
  if (p.src_prefixes.length) parts.push(`from ${p.src_prefixes.join(", ")}`);
  if (p.dst_prefixes.length) parts.push(`to ${p.dst_prefixes.join(", ")}`);
  if (p.protocol) parts.push(p.protocol);
  if (p.dst_ports) parts.push(`port ${p.dst_ports}`);
  if (p.dscp !== null) parts.push(`dscp ${p.dscp}`);
  return parts.join(" · ") || "everything";
}

function SlaProfiles({ profiles }: { profiles: SlaProfile[] }) {
  return (
    <div className="card">
      <h2>SLA profiles</h2>
      {profiles.length === 0 ? (
        <p className="muted">
          None defined. Policies without one use 20% loss / 300 ms, probed every 10 s.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Loss</th>
              <th>Latency</th>
              <th>Jitter</th>
              <th>Probe</th>
              <th>Detects in</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td>{s.loss_percent}%</td>
                <td>{s.latency_ms} ms</td>
                <td>{s.jitter_ms ? `${s.jitter_ms} ms` : "—"}</td>
                <td className="muted">
                  {s.probe_count} × {s.probe_interval_seconds}s
                </td>
                <td className="muted">~{s.detection_seconds}s</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function NewPolicyForm({
  tags,
  slas,
  onDone,
  onCancel,
}: {
  tags: string[];
  slas: SlaProfile[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    name: "",
    priority: 100,
    dst_prefixes: "",
    protocol: "",
    dst_ports: "",
    sla_profile_id: "",
    fallback: "any",
  });
  const [prefer, setPrefer] = useState<string[]>([]);

  const create = useMutation({
    mutationFn: () =>
      endpoints.createPolicy({
        name: form.name,
        priority: form.priority,
        dst_prefixes: splitList(form.dst_prefixes),
        protocol: form.protocol || null,
        dst_ports: form.dst_ports || null,
        prefer_tags: prefer,
        sla_profile_id: form.sla_profile_id || null,
        fallback: form.fallback,
      }),
    onSuccess: onDone,
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  function toggle(tag: string) {
    setPrefer((prev) =>
      prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag],
    );
  }

  return (
    <div className="card">
      <h2>New policy</h2>
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
            Priority
            <input
              type="number"
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })}
            />
          </label>
          <label>
            SLA profile
            <select
              value={form.sla_profile_id}
              onChange={(e) => setForm({ ...form, sla_profile_id: e.target.value })}
            >
              <option value="">default (20% / 300 ms)</option>
              {slas.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="row">
          <label>
            Destination prefixes
            <input
              placeholder="10.1.0.0/24, 10.9.0.0/16"
              value={form.dst_prefixes}
              onChange={(e) => setForm({ ...form, dst_prefixes: e.target.value })}
            />
          </label>
          <label>
            Protocol
            <select
              value={form.protocol}
              onChange={(e) => setForm({ ...form, protocol: e.target.value })}
            >
              <option value="">any</option>
              <option value="tcp">tcp</option>
              <option value="udp">udp</option>
              <option value="icmp">icmp</option>
            </select>
          </label>
          <label>
            Destination ports
            <input
              placeholder="443 or 5060,5061"
              value={form.dst_ports}
              onChange={(e) => setForm({ ...form, dst_ports: e.target.value })}
            />
          </label>
        </div>

        <label>
          Prefer these uplinks, in order
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 6 }}>
            {tags.length === 0 && (
              <span className="muted">
                No uplinks known yet. Add sites and probe them first.
              </span>
            )}
            {tags.map((tag) => (
              <button
                type="button"
                key={tag}
                className={prefer.includes(tag) ? "primary" : ""}
                onClick={() => toggle(tag)}
              >
                {prefer.includes(tag) ? `${prefer.indexOf(tag) + 1}. ${tag}` : tag}
              </button>
            ))}
          </div>
        </label>

        <label>
          When none of them meets the SLA
          <select
            value={form.fallback}
            onChange={(e) => setForm({ ...form, fallback: e.target.value })}
          >
            <option value="any">fall back to the normal routing table</option>
            <option value="drop">drop the traffic</option>
          </select>
        </label>

        <p className="muted">
          A policy naming no uplink that exists at a site is skipped there rather than
          pushed — marking traffic into an empty routing table would blackhole it.
        </p>

        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div style={{ flex: 0 }}>
            <button
              className="primary"
              type="submit"
              disabled={create.isPending || prefer.length === 0}
            >
              {create.isPending ? "Creating…" : "Create"}
            </button>
          </div>
          <div style={{ flex: 0 }}>
            <button type="button" onClick={onCancel}>
              Cancel
            </button>
          </div>
        </div>
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
