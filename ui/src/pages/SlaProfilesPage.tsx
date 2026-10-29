/**
 * SLA profiles: the health standard an SD-WAN group holds its uplinks to.
 *
 * This used to be a section at the bottom of the traffic-rules page, which
 * buried a first-class object under an unrelated one — a profile is named
 * once and shared by groups, so it gets its own page, like the groups do.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import { endpoints, type SlaProfile } from "../lib/api";

export function SlaProfilesPage() {
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const toast = useToast();

  const profiles = useQuery({ queryKey: ["slas"], queryFn: endpoints.slaProfiles });

  const remove = useMutation({
    mutationFn: (id: string) => endpoints.deleteSla(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["slas"] });
      toast.ok("Profile deleted.");
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["slas"] });
    setAdding(false);
    setEditing(null);
  };

  return (
    <>
      <PageHeader
        title="SLA profiles"
        description="The standard a path has to meet: how much loss, latency and jitter are tolerable, how hard the controller probes to find out, and how far back compliance is judged. SD-WAN groups name a profile instead of repeating the numbers."
      >
        <button className="primary" onClick={() => setAdding(true)}>
          New profile
        </button>
      </PageHeader>

      {adding && <SlaForm onDone={refresh} onCancel={() => setAdding(false)} />}

      <div className="card">
        {profiles.isLoading && <Skeleton rows={3} />}
        {profiles.isError && (
          <div className="error">{(profiles.error as Error).message}</div>
        )}
        {profiles.data?.length === 0 && !adding && (
          <p className="muted">
            None defined. Groups without one use 20% loss / 300 ms, probed
            every 10 s.
          </p>
        )}
        {profiles.data && profiles.data.length > 0 && (
          <table className="stack">
            <thead>
              <tr>
                <th>Name</th>
                <th>Loss</th>
                <th>Latency</th>
                <th>Jitter</th>
                <th>Probe</th>
                <th>Eval window</th>
                <th>Detects in</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {profiles.data.map((s) => (
                <tr key={s.id}>
                  <td data-label="Name">
                    <strong>{s.name}</strong>
                    {s.description && <div className="muted">{s.description}</div>}
                  </td>
                  <td data-label="Loss">{s.loss_percent}%</td>
                  <td data-label="Latency">{s.latency_ms} ms</td>
                  <td data-label="Jitter">
                    {s.jitter_ms ? `${s.jitter_ms} ms` : "—"}
                  </td>
                  <td data-label="Probe" className="muted">
                    {s.probe_count} × {s.probe_interval_seconds}s
                  </td>
                  <td data-label="Eval window" className="muted">
                    {s.eval_window_minutes} min
                  </td>
                  <td data-label="Detects in" className="muted">
                    ~{s.detection_seconds}s
                  </td>
                  <td data-label="">
                    <button
                      className="sm"
                      onClick={() => setEditing(editing === s.id ? null : s.id)}
                    >
                      {editing === s.id ? "Close" : "Edit"}
                    </button>{" "}
                    <button
                      className="sm danger"
                      onClick={() => remove.mutate(s.id)}
                      disabled={remove.isPending}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {editing && profiles.data && (
        <SlaForm
          profile={profiles.data.find((s) => s.id === editing)}
          onDone={refresh}
          onCancel={() => setEditing(null)}
        />
      )}
    </>
  );
}

function SlaForm({
  profile,
  onDone,
  onCancel,
}: {
  profile?: SlaProfile;
  onDone: () => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState({
    name: profile?.name ?? "",
    description: profile?.description ?? "",
    loss_percent: profile?.loss_percent ?? 20,
    latency_ms: profile?.latency_ms ?? 300,
    jitter_ms: profile?.jitter_ms != null ? String(profile.jitter_ms) : "",
    probe_interval_seconds: profile?.probe_interval_seconds ?? 10,
    probe_count: profile?.probe_count ?? 10,
    recovery_seconds: profile?.recovery_seconds ?? 60,
    eval_window_minutes: profile?.eval_window_minutes ?? 30,
    fail_count: profile?.fail_count ?? 5,
    restore_count: profile?.restore_count ?? 5,
    failback_latency_margin_ms: profile?.failback_latency_margin_ms ?? 10,
    failback_jitter_margin_ms: profile?.failback_jitter_margin_ms ?? 5,
    probe_type: profile?.probe_type ?? "icmp",
    probe_port: profile?.probe_port != null ? String(profile.probe_port) : "",
    probe_dns_name: profile?.probe_dns_name ?? "",
    probe_targets: (profile?.probe_targets ?? []).join(", "),
  });

  const save = useMutation({
    mutationFn: () => {
      const body = {
        ...form,
        description: form.description || null,
        jitter_ms: form.jitter_ms ? Number(form.jitter_ms) : null,
        probe_port: form.probe_port ? Number(form.probe_port) : null,
        probe_dns_name: form.probe_dns_name.trim() || null,
        probe_targets: form.probe_targets
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      };
      return profile
        ? endpoints.updateSla(profile.id, body)
        : endpoints.createSla(body);
    },
    onSuccess: () => {
      toast.ok(profile ? "Profile updated." : "Profile created.");
      onDone();
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <div className="card">
      <h2>{profile ? `Edit ${profile.name}` : "New SLA profile"}</h2>
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
            Description
            <input
              placeholder="optional"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </label>
        </div>

        <div className="row">
          <label>
            Loss %
            <input
              type="number"
              min={1}
              max={100}
              value={form.loss_percent}
              onChange={(e) =>
                setForm({ ...form, loss_percent: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Latency (ms)
            <input
              type="number"
              min={1}
              value={form.latency_ms}
              onChange={(e) =>
                setForm({ ...form, latency_ms: Number(e.target.value) })
              }
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
            <span className="muted">
              RouterOS measures jitter as max − min RTT within one probe test.
            </span>
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
                setForm({
                  ...form,
                  probe_interval_seconds: Number(e.target.value),
                })
              }
            />
          </label>
          <label>
            Packets per probe
            <input
              type="number"
              min={1}
              value={form.probe_count}
              onChange={(e) =>
                setForm({ ...form, probe_count: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Recovery hold (s)
            <input
              type="number"
              min={0}
              value={form.recovery_seconds}
              onChange={(e) =>
                setForm({ ...form, recovery_seconds: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Eval window (minutes)
            <input
              type="number"
              min={1}
              max={1440}
              value={form.eval_window_minutes}
              onChange={(e) =>
                setForm({
                  ...form,
                  eval_window_minutes: Number(e.target.value),
                })
              }
            />
            <span className="muted">
              How far back the central evaluator looks when it judges a path
              against this profile — the window behind every verdict on the
              SLA compliance page. Longer windows forgive brief spikes;
              shorter ones react to them.
            </span>
          </label>
        </div>

        <div className="row">
          <label>
            Failures before demotion
            <input
              type="number"
              min={1}
              max={100}
              value={form.fail_count}
              onChange={(e) =>
                setForm({ ...form, fail_count: Number(e.target.value) })
              }
            />
            <span className="muted">
              Consecutive failed probe tests before a path is demoted, so
              one twitchy test moves nothing. 1 demotes on the first failure.
            </span>
          </label>
          <label>
            Passes before restore
            <input
              type="number"
              min={1}
              max={100}
              value={form.restore_count}
              onChange={(e) =>
                setForm({ ...form, restore_count: Number(e.target.value) })
              }
            />
            <span className="muted">
              Consecutive passed tests before traffic returns — whichever of
              this and the recovery hold takes longer is the one that binds.
            </span>
          </label>
          <label>
            Failback margin, latency (ms)
            <input
              type="number"
              min={0}
              value={form.failback_latency_margin_ms}
              onChange={(e) =>
                setForm({
                  ...form,
                  failback_latency_margin_ms: Number(e.target.value),
                })
              }
            />
            <span className="muted">
              A recovered path must beat the latency threshold by this much
              before it takes traffic back, not just duck under it.
            </span>
          </label>
          <label>
            Failback margin, jitter (ms)
            <input
              type="number"
              min={0}
              value={form.failback_jitter_margin_ms}
              onChange={(e) =>
                setForm({
                  ...form,
                  failback_jitter_margin_ms: Number(e.target.value),
                })
              }
            />
          </label>
        </div>

        <div className="row">
          <label>
            Probe type
            <select
              value={form.probe_type}
              onChange={(e) => setForm({ ...form, probe_type: e.target.value })}
            >
              <option value="icmp">ICMP (loss / latency / jitter)</option>
              <option value="tcp-conn">TCP connect</option>
              <option value="http-get">HTTP GET</option>
              <option value="dns">DNS</option>
            </select>
            <span className="muted">
              Service probes measure a real transaction and survive ICMP
              being filtered — but judge up/down only: the loss, latency and
              jitter thresholds above apply to ICMP.
            </span>
          </label>
          <label>
            Probe port
            <input
              type="number"
              min={1}
              max={65535}
              placeholder="required for TCP connect"
              value={form.probe_port}
              onChange={(e) => setForm({ ...form, probe_port: e.target.value })}
            />
          </label>
          <label>
            DNS probe name
            <input
              placeholder="required for DNS, e.g. one.one.one.one"
              value={form.probe_dns_name}
              onChange={(e) =>
                setForm({ ...form, probe_dns_name: e.target.value })
              }
            />
            <span className="muted">
              A DNS probe tests that this name resolves — the device will
              not accept an address there.
            </span>
          </label>
          <label>
            Extra probe targets
            <input
              placeholder="e.g. 1.1.1.1, 8.8.8.8"
              value={form.probe_targets}
              onChange={(e) =>
                setForm({ ...form, probe_targets: e.target.value })
              }
            />
            <span className="muted">
              Comma-separated addresses probed alongside the path's next hop.
              A demotion needs every target down at once, so one dead target
              cannot demote a healthy path. Not used by DNS probes, which
              resolve the name above instead.
            </span>
          </label>
        </div>

        <p className="muted">
          Detection takes roughly {form.probe_interval_seconds * 2}s, plus the
          failure-count window above before a demotion lands. Tightening the
          interval speeds that up and costs router CPU; below a second or two
          you start failing over on ordinary jitter.
        </p>

        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div className="no-grow">
            <button className="primary" type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : profile ? "Save" : "Create"}
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
