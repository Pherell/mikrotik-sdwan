/**
 * Acting on several sites at once, without giving up the review step.
 *
 * "Apply all" already existed and pushed to every site with credentials, one
 * after another, showing you results as they landed. Reviewing a fleet-wide
 * change after it has happened is not reviewing it. This plans first, shows one
 * combined summary, and only then offers to push.
 *
 * Bulk edit is N sequential PATCHes rather than one endpoint. That is a
 * deliberate choice, not a shortcut: PATCH /sites/{id} already exists and is
 * already the thing the single-site form uses, so there is no second code path
 * to keep honest. A real bulk endpoint is worth having when N gets large enough
 * for the round trips to matter, and not before.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints, type Plan, type Site } from "../lib/api";

type PlanOutcome = {
  site: Site;
  plan?: Plan;
  error?: string;
};

const EDITABLE = [
  { field: "role", label: "Role", options: ["hub", "spoke"] },
  { field: "drift_action", label: "On drift", options: ["alert", "auto-remediate"] },
] as const;

export function BulkSiteBar({
  selected,
  sites,
  onClear,
}: {
  selected: string[];
  sites: Site[];
  onClear: () => void;
}) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"none" | "edit">("none");
  const [field, setField] = useState<string>("role");
  const [value, setValue] = useState<string>("hub");
  const [plans, setPlans] = useState<PlanOutcome[] | null>(null);
  const [progress, setProgress] = useState<string | null>(null);

  const chosen = sites.filter((s) => selected.includes(s.id));
  const withCredentials = chosen.filter((s) => s.has_credentials);

  const edit = useMutation({
    mutationFn: async () => {
      for (const site of chosen) {
        setProgress(site.name);
        await endpoints.updateSite(site.id, { [field]: value });
      }
      setProgress(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      setMode("none");
      onClear();
    },
  });

  const plan = useMutation({
    mutationFn: async () => {
      const out: PlanOutcome[] = [];
      setPlans(null);
      for (const site of withCredentials) {
        setProgress(site.name);
        try {
          out.push({ site, plan: await endpoints.plan(site.id) });
        } catch (e) {
          out.push({ site, error: (e as Error).message });
        }
        setPlans([...out]);
      }
      setProgress(null);
    },
  });

  const apply = useMutation({
    mutationFn: async () => {
      const changing = (plans ?? []).filter((p) => p.plan && !p.plan.empty);
      for (const { site } of changing) {
        setProgress(site.name);
        await endpoints.apply(site.id, { confirm: true });
      }
      setProgress(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["sites"] });
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      setPlans(null);
      onClear();
    },
  });

  const busy = edit.isPending || plan.isPending || apply.isPending;
  const changing = (plans ?? []).filter((p) => p.plan && !p.plan.empty);
  const failed = (plans ?? []).filter((p) => p.error);
  const clean = (plans ?? []).filter((p) => p.plan?.empty);

  return (
    <div className="bulk-bar">
      <div className="bulk-row">
        <strong className="no-grow">{selected.length} selected</strong>
        <div className="bulk-actions no-grow">
          <button
            className="sm"
            onClick={() => setMode(mode === "edit" ? "none" : "edit")}
            disabled={busy}
          >
            Edit
          </button>
          <button
            className="sm"
            onClick={() => plan.mutate()}
            data-busy={plan.isPending}
            disabled={busy || withCredentials.length === 0}
          >
            {plan.isPending ? `Planning ${progress ?? ""}…` : "Plan changes"}
          </button>
          <button className="sm ghost" onClick={onClear} disabled={busy}>
            Clear
          </button>
        </div>
      </div>

      {withCredentials.length < chosen.length && (
        <p className="muted bulk-note">
          {chosen.length - withCredentials.length}{" "}
          {chosen.length - withCredentials.length === 1 ? "has" : "have"} no stored
          credentials and will be skipped when planning.
        </p>
      )}

      {mode === "edit" && (
        <div className="bulk-edit">
          <div className="row">
            <label>
              Field
              <select
                value={field}
                onChange={(e) => {
                  setField(e.target.value);
                  const next = EDITABLE.find((f) => f.field === e.target.value);
                  setValue(next?.options[0] ?? "");
                }}
              >
                {EDITABLE.map((f) => (
                  <option key={f.field} value={f.field}>
                    {f.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Set to
              <select value={value} onChange={(e) => setValue(e.target.value)}>
                {EDITABLE.find((f) => f.field === field)?.options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            </label>
            <div className="no-grow" style={{ alignSelf: "flex-end" }}>
              <button
                className="primary"
                onClick={() => edit.mutate()}
                data-busy={edit.isPending}
                disabled={busy}
              >
                {edit.isPending ? `Saving ${progress ?? ""}…` : `Apply to ${chosen.length}`}
              </button>
            </div>
          </div>
          <p className="muted bulk-note">
            Only fields that mean the same thing across sites are offered here.
            Credentials are per device by definition and are never bulk-edited.
          </p>
          {edit.isError && (
            <div className="error">{(edit.error as Error).message}</div>
          )}
        </div>
      )}

      {plans && !plan.isPending && (
        <div className="bulk-plans">
          <p>
            <strong>
              {plans.length} planned · {changing.length} with changes · {clean.length}{" "}
              already correct
              {failed.length > 0 && ` · ${failed.length} failed`}
            </strong>
          </p>
          <ul className="bulk-list">
            {plans.map(({ site, plan: p, error }) => (
              <li key={site.id}>
                <span className="bulk-site">{site.name}</span>{" "}
                {error ? (
                  <span className="bulk-fail">{error}</span>
                ) : p?.empty ? (
                  <span className="muted">no changes</span>
                ) : (
                  <span>
                    +{p?.counts.add} ~{p?.counts.set} −{p?.counts.remove}
                  </span>
                )}
              </li>
            ))}
          </ul>
          {changing.length > 0 && (
            <>
              <div className="warn">
                Applying pushes to {changing.length} device
                {changing.length > 1 ? "s" : ""} in turn. Each takes a backup and arms
                its own rollback first.
              </div>
              <button
                className="primary"
                onClick={() => apply.mutate()}
                data-busy={apply.isPending}
                disabled={busy}
              >
                {apply.isPending
                  ? `Applying ${progress ?? ""}…`
                  : `Apply to ${changing.length}`}
              </button>
            </>
          )}
          {apply.isError && (
            <div className="error">{(apply.error as Error).message}</div>
          )}
        </div>
      )}
    </div>
  );
}
