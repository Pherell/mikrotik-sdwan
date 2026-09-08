/**
 * SD-WAN groups: which uplinks, in what order, and how healthy they must be.
 *
 * This is the half of a steering decision worth naming once. Before it existed
 * every rule retyped its own uplink order and its own SLA, so "the voice path"
 * was not a thing you could point at — it was a shape you re-entered and hoped
 * matched the last time.
 *
 * The presets live here rather than on the rule, because what they actually
 * described was always a path, never a match.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import {
  endpoints,
  type GroupStrategy,
  type SdwanGroup,
  type SlaProfile,
} from "../lib/api";
import { POLICY_PRESETS, type PolicyPreset } from "../lib/presets";

export function SdwanGroupsPage() {
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const toast = useToast();

  const groups = useQuery({ queryKey: ["sdwan-groups"], queryFn: endpoints.sdwanGroups });
  const slas = useQuery({ queryKey: ["slas"], queryFn: endpoints.slaProfiles });
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });

  // Every tag any uplink carries, plus every uplink name. Free text here is how
  // you get a group that silently matches nothing.
  const uplinks = new Set<string>();
  for (const site of sites.data ?? []) {
    for (const wan of site.wans) {
      uplinks.add(wan.name);
      for (const tag of Object.keys(wan.tags ?? {})) uplinks.add(tag);
    }
  }

  const remove = useMutation({
    mutationFn: (id: string) => endpoints.deleteSdwanGroup(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sdwan-groups"] });
      toast.ok("Group deleted.");
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["sdwan-groups"] });
    setAdding(false);
    setEditing(null);
  };

  return (
    <>
      <PageHeader
        title="SD-WAN groups"
        description="A named set of uplinks, in the order traffic should prefer them, with the standard each must meet to stay in use. Traffic rules point at a group instead of repeating all of that."
      >
        <button className="primary" onClick={() => setAdding(true)}>
          New group
        </button>
      </PageHeader>

      {adding && (
        <GroupForm
          uplinks={[...uplinks].sort()}
          slas={slas.data ?? []}
          onDone={refresh}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="card">
        {groups.isLoading && <Skeleton rows={3} />}
        {groups.isError && (
          <div className="error">{(groups.error as Error).message}</div>
        )}
        {groups.data?.length === 0 && !adding && (
          <p className="muted">
            No groups yet. A group answers "which internet connection should this
            traffic take, and what happens when it degrades" — once, by name, so
            every rule that wants the same answer can share it.
          </p>
        )}
        {groups.data && groups.data.length > 0 && (
          <table className="stack">
            <thead>
              <tr>
                <th>Group</th>
                <th>Uplinks, in order</th>
                <th>Strategy</th>
                <th>Health standard</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {groups.data.map((g) => (
                <tr key={g.id}>
                  <td data-label="Group">
                    <strong>{g.name}</strong>
                    {g.description && <div className="muted">{g.description}</div>}
                  </td>
                  <td data-label="Uplinks">
                    {g.strategy === "failover"
                      ? g.members.map((m) => m.uplink).join(" → ")
                      : g.members
                          .map((m) => `${m.uplink} ×${m.weight}`)
                          .join(" + ")}
                  </td>
                  <td data-label="Strategy" className="muted">
                    {g.strategy === "failover"
                      ? "first healthy one wins"
                      : "connections spread across all of them"}
                  </td>
                  <td data-label="Health" className="muted">
                    {slas.data?.find((s) => s.id === g.sla_profile_id)?.name ??
                      "default"}
                  </td>
                  <td data-label="">
                    <button
                      className="sm"
                      onClick={() => setEditing(editing === g.id ? null : g.id)}
                    >
                      {editing === g.id ? "Close" : "Edit"}
                    </button>{" "}
                    <button
                      className="sm danger"
                      onClick={() => remove.mutate(g.id)}
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

      {editing && groups.data && (
        <GroupForm
          group={groups.data.find((g) => g.id === editing)}
          uplinks={[...uplinks].sort()}
          slas={slas.data ?? []}
          onDone={refresh}
          onCancel={() => setEditing(null)}
        />
      )}
    </>
  );
}

function GroupForm({
  group,
  uplinks,
  slas,
  onDone,
  onCancel,
}: {
  group?: SdwanGroup;
  uplinks: string[];
  slas: SlaProfile[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(group?.name ?? "");
  const [description, setDescription] = useState(group?.description ?? "");
  const [chosen, setChosen] = useState<string[]>(
    group?.members.map((m) => m.uplink) ?? [],
  );
  const [slaId, setSlaId] = useState(group?.sla_profile_id ?? "");
  const [preset, setPreset] = useState<PolicyPreset | null>(null);
  const [strategy, setStrategy] = useState<GroupStrategy>(
    group?.strategy ?? "failover",
  );
  // Weight per uplink, kept for every uplink rather than only the chosen ones,
  // so toggling one off and back on does not lose the number you typed.
  const [weights, setWeights] = useState<Record<string, number>>(
    Object.fromEntries((group?.members ?? []).map((m) => [m.uplink, m.weight])),
  );

  function applyPreset(option: PolicyPreset | null) {
    setPreset(option);
    if (!option) return;
    setName((current) => current || option.id);
    const existing = option.sla ? slas.find((s) => s.name === option.sla!.name) : undefined;
    setSlaId(existing?.id ?? "");
  }

  function toggle(uplink: string) {
    setChosen((prev) =>
      prev.includes(uplink) ? prev.filter((u) => u !== uplink) : [...prev, uplink],
    );
  }

  const save = useMutation({
    mutationFn: async () => {
      // A preset may name an SLA nobody has created yet. Making it here is the
      // point: someone picked "voice", not a jitter budget in milliseconds.
      let sla = slaId || null;
      if (!sla && preset?.sla) {
        const existing = slas.find((s) => s.name === preset.sla!.name);
        sla = existing
          ? existing.id
          : (await endpoints.createSla({ ...preset.sla, description: preset.blurb })).id;
      }
      const body = {
        name,
        description: description || null,
        members: chosen.map((uplink) => ({
          uplink,
          // The server rejects a weight other than 1 under failover, because a
          // number that does nothing is worse than no number.
          weight: strategy === "load_balance" ? (weights[uplink] ?? 1) : 1,
        })),
        strategy,
        sla_profile_id: sla,
      };
      return group
        ? endpoints.updateSdwanGroup(group.id, body)
        : endpoints.createSdwanGroup(body);
    },
    onSuccess: () => {
      toast.ok(group ? "Group updated." : "Group created.");
      onDone();
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  const tooFewToBalance = strategy === "load_balance" && chosen.length < 2;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (chosen.length === 0 || tooFewToBalance) return;
    save.mutate();
  }

  return (
    <div className="card">
      <h2>{group ? `Edit ${group.name}` : "New SD-WAN group"}</h2>
      {save.isError && <div className="error">{(save.error as Error).message}</div>}

      {!group && (
        <>
          <p className="muted" style={{ marginTop: 0 }}>
            Start from what the traffic is. Every field stays editable afterwards.
          </p>
          <div className="preset-grid">
            {POLICY_PRESETS.map((option) => (
              <button
                key={option.id}
                type="button"
                className={`preset${preset?.id === option.id ? " selected" : ""}`}
                onClick={() => applyPreset(preset?.id === option.id ? null : option)}
                aria-pressed={preset?.id === option.id}
              >
                <span className="preset-title">{option.title}</span>
                <span className="preset-blurb">{option.blurb}</span>
              </button>
            ))}
          </div>
          {preset && (
            <div className="preset-why muted">
              <strong>{preset.title}.</strong> {preset.reasoning}
              {preset.sla && (
                <>
                  {" "}Uses {preset.sla.loss_percent}% loss / {preset.sla.latency_ms} ms
                  {preset.sla.jitter_ms !== null && ` / ${preset.sla.jitter_ms} ms jitter`},
                  probed every {preset.sla.probe_interval_seconds} s
                  {slas.some((s) => s.name === preset.sla!.name)
                    ? "."
                    : ` — the profile "${preset.sla.name}" will be created when you save.`}
                </>
              )}
            </div>
          )}
        </>
      )}

      <form onSubmit={submit}>
        <div className="row">
          <label>
            Name
            <input required value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Description
            <input
              placeholder="optional"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
        </div>

        <label>
          How traffic uses them
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value as GroupStrategy)}
          >
            <option value="failover">
              One at a time — the first healthy uplink in the list
            </option>
            <option value="load_balance">
              All at once — connections spread across them by weight
            </option>
          </select>
        </label>

        <label>
          {strategy === "failover"
            ? "Uplinks, in the order traffic should prefer them"
            : "Uplinks to spread across, and each one's share"}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 6 }}>
            {uplinks.length === 0 && (
              <span className="muted">
                No uplinks known yet. Add a device and confirm its uplinks first.
              </span>
            )}
            {uplinks.map((uplink) => (
              <button
                type="button"
                key={uplink}
                className={chosen.includes(uplink) ? "primary" : ""}
                onClick={() => toggle(uplink)}
              >
                {/* Under failover the number is a position, under balancing it
                    is a share. Showing a position next to a weight field would
                    read as though both applied. */}
                {chosen.includes(uplink) && strategy === "failover"
                  ? `${chosen.indexOf(uplink) + 1}. ${uplink}`
                  : uplink}
              </button>
            ))}
          </div>
        </label>

        {strategy === "load_balance" && chosen.length > 0 && (
          <div className="row">
            {chosen.map((uplink) => (
              <label key={uplink}>
                {uplink} share
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={weights[uplink] ?? 1}
                  onChange={(e) =>
                    setWeights((prev) => ({
                      ...prev,
                      [uplink]: Math.max(1, Number(e.target.value) || 1),
                    }))
                  }
                />
              </label>
            ))}
          </div>
        )}

        {tooFewToBalance && (
          <div className="warn">
            Spreading traffic needs at least two uplinks. With one, choose "one
            at a time".
          </div>
        )}

        <label>
          Health standard
          <select value={slaId} onChange={(e) => setSlaId(e.target.value)}>
            <option value="">default (20% loss / 300 ms, probed every 10 s)</option>
            {slas.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} — {s.loss_percent}% loss / {s.latency_ms} ms
              </option>
            ))}
          </select>
        </label>

        {strategy === "failover" ? (
          <p className="muted">
            Traffic uses the first uplink in this list that is present at the
            device and meeting the standard above. When one degrades past it,
            traffic moves to the next; when it recovers, traffic moves back.
          </p>
        ) : (
          <p className="muted">
            Each new connection is assigned an uplink, in proportion to the
            shares above — a share of 3 next to a share of 1 sends three
            connections one way for every one the other. An uplink that fails
            or breaches the standard above stops receiving new connections and
            its existing ones move to the others.{" "}
            <strong>
              This spreads connections, not packets: one download still uses one
              uplink and will not go faster.
            </strong>{" "}
            That is true of RouterOS and of every other router that balances
            this way.
          </p>
        )}

        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div className="no-grow">
            <button
              className="primary"
              type="submit"
              data-busy={save.isPending}
              disabled={save.isPending || chosen.length === 0 || tooFewToBalance}
            >
              {save.isPending ? "Saving…" : group ? "Save" : "Create"}
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
