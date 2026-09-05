import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints, getToken } from "../lib/api";

export function SettingsPage() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });
  const isAdmin = me.data?.role === "admin";

  return (
    <>
      <ExportPanel />
      {isAdmin && <ImportPanel />}
      <AppGroupsPanel />
    </>
  );
}

/**
 * The export is a plain GET, but it needs an Authorization header, so it cannot
 * be a bare link. Fetch it and hand the browser a blob instead.
 */
function ExportPanel() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function download() {
    setBusy(true);
    setError(null);
    try {
      const resp = await fetch(endpoints.exportUrl(), {
        headers: { Authorization: `Bearer ${getToken() ?? ""}` },
      });
      if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "sdwan-intent.yaml";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h2>Export intent</h2>
      <p className="muted">
        Everything you authored — sites, uplinks, fabrics, policies, SLA profiles —
        as YAML you can commit. <strong>Credentials and link keys are excluded on
        purpose</strong>: they are environment-specific and do not belong in a file
        people put in git. Links are excluded too, because they are derived from
        topology and members; re-expand after an import to rebuild them.
      </p>
      {error && <div className="error">{error}</div>}
      <button className="primary" onClick={download} disabled={busy}>
        {busy ? "Exporting…" : "Download sdwan-intent.yaml"}
      </button>
    </div>
  );
}

function ImportPanel() {
  const [yaml, setYaml] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  const run = useMutation({
    mutationFn: (dryRun: boolean) => endpoints.importIntent(yaml, dryRun),
    onSuccess: setResult,
  });

  return (
    <div className="card">
      <h2>Import intent</h2>
      <p className="muted">
        Creates what is missing and updates what exists, matching on name.{" "}
        <strong>Nothing is ever deleted</strong> — a partial document is the normal
        case, and treating omissions as deletions would make sharing one fabric
        destructive.
      </p>

      <label>
        Paste a document, or load a file
        <input
          type="file"
          accept=".yaml,.yml"
          style={{ marginBottom: 8 }}
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (file) setYaml(await file.text());
          }}
        />
        <textarea
          rows={12}
          value={yaml}
          placeholder="version: 1&#10;sites: …"
          onChange={(e) => setYaml(e.target.value)}
          style={{
            width: "100%",
            fontFamily: "ui-monospace, Consolas, monospace",
            fontSize: 12,
          }}
        />
      </label>

      {run.isError && <div className="error">{(run.error as Error).message}</div>}

      <div className="row" style={{ justifyContent: "flex-start" }}>
        <div style={{ flex: 0 }}>
          <button onClick={() => run.mutate(true)} disabled={!yaml || run.isPending}>
            Dry run
          </button>
        </div>
        <div style={{ flex: 0 }}>
          <button
            className="primary"
            disabled={!yaml || run.isPending}
            onClick={() => {
              if (confirm("Apply this document to the controller?")) run.mutate(false);
            }}
          >
            Import for real
          </button>
        </div>
      </div>

      {result && (
        <>
          <p className="muted" style={{ marginTop: 12 }}>
            {result.dry_run ? "Dry run — nothing was written." : "Imported."}
          </p>
          <pre className="diff">{JSON.stringify(result, null, 2)}</pre>
        </>
      )}
    </div>
  );
}

function AppGroupsPanel() {
  const groups = useQuery({ queryKey: ["app-groups"], queryFn: endpoints.appGroups });
  const [adding, setAdding] = useState(false);

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>Application groups</h2>
        <div style={{ flex: 0 }}>
          <button onClick={() => setAdding(!adding)}>
            {adding ? "Cancel" : "New group"}
          </button>
        </div>
      </div>
      <p className="muted">
        A named set of prefixes and ports a policy can match.{" "}
        <strong>This is prefix matching, not DPI</strong> — RouterOS has no usable
        application classifier, so traffic that moves to a new range is missed until
        the list is updated.
      </p>

      {adding && <AppGroupForm onDone={() => setAdding(false)} />}

      {groups.data?.length === 0 && !adding && (
        <p className="muted">None defined.</p>
      )}
      {groups.data && groups.data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefixes</th>
              <th>Ports</th>
              <th>Protocol</th>
              <th>DSCP</th>
            </tr>
          </thead>
          <tbody>
            {groups.data.map((g) => (
              <tr key={g.id}>
                <td>{g.name}</td>
                <td className="muted">{g.prefixes.length}</td>
                <td className="muted">{g.ports.join(", ") || "—"}</td>
                <td>{g.protocol ?? "any"}</td>
                <td>{g.dscp ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function AppGroupForm({ onDone }: { onDone: () => void }) {
  const [form, setForm] = useState({
    name: "",
    prefixes: "",
    ports: "",
    protocol: "",
    dscp: "",
  });

  const create = useMutation({
    mutationFn: () =>
      endpoints.createAppGroup({
        name: form.name,
        prefixes: split(form.prefixes),
        ports: split(form.ports).map(Number).filter((n) => !Number.isNaN(n)),
        protocol: form.protocol || null,
        dscp: form.dscp ? Number(form.dscp) : null,
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
          Protocol
          <select
            value={form.protocol}
            onChange={(e) => setForm({ ...form, protocol: e.target.value })}
          >
            <option value="">any</option>
            <option value="tcp">tcp</option>
            <option value="udp">udp</option>
          </select>
        </label>
        <label>
          DSCP
          <input
            type="number"
            min={0}
            max={63}
            value={form.dscp}
            onChange={(e) => setForm({ ...form, dscp: e.target.value })}
          />
        </label>
      </div>
      <div className="row">
        <label>
          Prefixes
          <input
            placeholder="52.112.0.0/14, 52.122.0.0/15"
            value={form.prefixes}
            onChange={(e) => setForm({ ...form, prefixes: e.target.value })}
          />
        </label>
        <label>
          Ports
          <input
            placeholder="3478, 3479"
            value={form.ports}
            onChange={(e) => setForm({ ...form, ports: e.target.value })}
          />
        </label>
      </div>
      <button className="primary" type="submit" disabled={create.isPending}>
        {create.isPending ? "Creating…" : "Create"}
      </button>
    </form>
  );
}

function split(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}
