import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";


import { endpoints, type Policy, type SlaProfile,
  type SdwanGroup,
} from "../lib/api";
import { Skeleton } from "../components/Skeleton";
import { PageHeader } from "../components/PageHeader";

export function PoliciesPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);

  const policies = useQuery({ queryKey: ["policies"], queryFn: endpoints.policies });
  const slas = useQuery({ queryKey: ["slas"], queryFn: endpoints.slaProfiles });
  const sdwanGroups = useQuery({
    queryKey: ["sdwan-groups"],
    queryFn: endpoints.sdwanGroups,
  });
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

  // Priority and enabled are what actually get changed day to day -- silencing
  // a rule during an incident, or reordering two that overlap. Both edit in
  // place rather than behind a form.
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: unknown }) =>
      endpoints.updatePolicy(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["policies"] }),
  });

  return (
    <>
      <PageHeader
        title="Traffic rules"
        description="Match some traffic and send it to an SD-WAN group. Rules are evaluated top to bottom by priority and the first match wins — edit a priority or untick a rule right in the table."
      >
        <button className="primary" onClick={() => setAdding(true)}>
          New rule
        </button>
      </PageHeader>

      <div className="card">
        <p className="muted" style={{ margin: 0 }}>
          Matching is by prefix, port and DSCP. RouterOS has no usable application
          classifier, so an application group is a prefix list, not deep packet
          inspection.
        </p>
      </div>

      {adding && (
        <NewPolicyForm
          groups={sdwanGroups.data ?? []}
          onDone={() => {
            setAdding(false);
            queryClient.invalidateQueries({ queryKey: ["policies"] });
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="card">
        {policies.isLoading && <Skeleton rows={3} />}
        {policies.data?.length === 0 && (
          <p className="muted">
            No rules yet, so every packet follows the device's own routing table.
            A rule overrides that for traffic you name, sending it to an SD-WAN
            group which decides the uplink and moves it when one degrades.
          </p>
        )}
        {policies.data && policies.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Priority</th>
                <th>Name</th>
                <th>Match</th>
                <th>Group</th>
                <th>Uplinks</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {policies.data.map((p) => (
                <tr key={p.id} style={{ opacity: p.enabled ? 1 : 0.55 }}>
                  <td>
                    <input
                      type="number"
                      style={{ width: 74 }}
                      defaultValue={p.priority}
                      onBlur={(e) => {
                        const next = Number(e.target.value);
                        if (next !== p.priority)
                          update.mutate({ id: p.id, body: { priority: next } });
                      }}
                    />
                  </td>
                  <td>
                    <label style={{ margin: 0, display: "inline-flex", gap: 6 }}>
                      <input
                        type="checkbox"
                        style={{ width: "auto" }}
                        checked={p.enabled}
                        onChange={(e) =>
                          update.mutate({
                            id: p.id,
                            body: { enabled: e.target.checked },
                          })
                        }
                      />
                      <span style={{ color: "var(--text)" }}>{p.name}</span>
                    </label>
                  </td>
                  <td className="muted">{describeMatch(p)}</td>
                  <td>
                    {sdwanGroups.data?.find((g) => g.id === p.sdwan_group_id)?.name ?? (
                      <span className="muted">none — this rule steers nothing</span>
                    )}
                  </td>
                  <td className="muted">
                    {(() => {
                      const g = sdwanGroups.data?.find(
                        (x) => x.id === p.sdwan_group_id,
                      );
                      return g ? g.members.map((m: { uplink: string }) => m.uplink).join(" → ") : "—";
                    })()}
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
        {update.isError && <div className="error">{(update.error as Error).message}</div>}
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
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);

  const remove = useMutation({
    mutationFn: (id: string) => endpoints.deleteSla(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["slas"] }),
  });

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>SLA profiles</h2>
        <div className="no-grow">
          <button onClick={() => setAdding(!adding)}>
            {adding ? "Cancel" : "New profile"}
          </button>
        </div>
      </div>
      {adding && (
        <SlaForm
          onDone={() => {
            setAdding(false);
            queryClient.invalidateQueries({ queryKey: ["slas"] });
          }}
        />
      )}
      {remove.isError && <div className="error">{(remove.error as Error).message}</div>}
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
              <th />
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
                <td>
                  <button onClick={() => remove.mutate(s.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function NewPolicyForm({
  groups,
  onDone,
  onCancel,
}: {
  groups: SdwanGroup[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    name: "",
    priority: 100,
    dst_prefixes: "",
    protocol: "",
    dst_ports: "",
    sdwan_group_id: "",
    fallback: "any",
  });

  const create = useMutation({
    mutationFn: () =>
      endpoints.createPolicy({
        name: form.name,
        priority: form.priority,
        dst_prefixes: splitList(form.dst_prefixes),
        protocol: form.protocol || null,
        dst_ports: form.dst_ports || null,
        sdwan_group_id: form.sdwan_group_id,
        fallback: form.fallback,
      }),
    onSuccess: onDone,
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    // Guarded here as well as on the button: requestSubmit() and implicit
    // submission both bypass a disabled control.
    if (!form.sdwan_group_id) return;
    create.mutate();
  }

  return (
    <div className="card">
      <h2>New traffic rule</h2>
      {create.isError && <div className="error">{(create.error as Error).message}</div>}

      <p className="muted" style={{ marginTop: 0 }}>
        A rule matches traffic and sends it to an SD-WAN group. The group decides
        which uplinks and in what order — that part is named once and shared.
      </p>

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
            Send it to
            <select
              required
              value={form.sdwan_group_id}
              onChange={(e) => setForm({ ...form, sdwan_group_id: e.target.value })}
            >
              <option value="" disabled>
                Choose an SD-WAN group…
              </option>
              {groups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name} — {g.members.map((m: { uplink: string }) => m.uplink).join(" → ")}
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

        {groups.length === 0 && (
          <div className="warn">
            No SD-WAN groups yet. A rule needs one to know where to send traffic —
            create a group first.
          </div>
        )}

        <label>
          When no uplink in the group is healthy
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
          <div className="no-grow">
            <button
              className="primary"
              type="submit"
              disabled={create.isPending || !form.sdwan_group_id}
            >
              {create.isPending ? "Creating…" : "Create"}
            </button>
          </div>
          <div className="no-grow">
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

function SlaForm({ onDone }: { onDone: () => void }) {
  const [form, setForm] = useState({
    name: "",
    loss_percent: 20,
    latency_ms: 300,
    jitter_ms: "",
    probe_interval_seconds: 10,
    probe_count: 10,
    recovery_seconds: 60,
  });

  const create = useMutation({
    mutationFn: () =>
      endpoints.createSla({
        ...form,
        jitter_ms: form.jitter_ms ? Number(form.jitter_ms) : null,
      }),
    onSuccess: onDone,
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
      style={{ margin: "12px 0" }}
    >
      {create.isError && <div className="error">{(create.error as Error).message}</div>}
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
          Loss %
          <input
            type="number"
            min={1}
            max={100}
            value={form.loss_percent}
            onChange={(e) => setForm({ ...form, loss_percent: Number(e.target.value) })}
          />
        </label>
        <label>
          Latency (ms)
          <input
            type="number"
            min={1}
            value={form.latency_ms}
            onChange={(e) => setForm({ ...form, latency_ms: Number(e.target.value) })}
          />
        </label>
        <label>
          Jitter (ms)
          <input
            type="number"
            placeholder="optional"
            value={form.jitter_ms}
            onChange={(e) => setForm({ ...form, jitter_ms: e.target.value })}
          />
        </label>
      </div>
      <div className="row">
        <label>
          Probe every (s)
          <input
            type="number"
            min={1}
            value={form.probe_interval_seconds}
            onChange={(e) =>
              setForm({ ...form, probe_interval_seconds: Number(e.target.value) })
            }
          />
        </label>
        <label>
          Packets per probe
          <input
            type="number"
            min={1}
            value={form.probe_count}
            onChange={(e) => setForm({ ...form, probe_count: Number(e.target.value) })}
          />
        </label>
        <label>
          Recovery hold (s)
          <input
            type="number"
            min={0}
            value={form.recovery_seconds}
            onChange={(e) => setForm({ ...form, recovery_seconds: Number(e.target.value) })}
          />
        </label>
      </div>
      <p className="muted">
        Detection takes roughly {form.probe_interval_seconds * 2}s. Tightening the
        interval speeds that up and costs router CPU; below a second or two you start
        failing over on ordinary jitter.
      </p>
      <button className="primary" type="submit" disabled={create.isPending}>
        {create.isPending ? "Creating…" : "Create"}
      </button>
    </form>
  );
}
