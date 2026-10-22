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
  });

  const save = useMutation({
    mutationFn: () => {
      const body = {
        ...form,
        description: form.description || null,
        jitter_ms: form.jitter_ms ? Number(form.jitter_ms) : null,
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

        <p className="muted">
          Detection takes roughly {form.probe_interval_seconds * 2}s. Tightening
          the interval speeds that up and costs router CPU; below a second or
          two you start failing over on ordinary jitter.
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
