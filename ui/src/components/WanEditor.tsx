import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { endpoints, type Site, type Wan } from "../lib/api";

/**
 * Add, edit and remove uplinks.
 *
 * Tags are the vocabulary steering policies use, so they are edited here as
 * plain comma-separated names rather than hidden behind a key/value grid — a
 * policy can only prefer a tag that some uplink actually carries.
 */
export function WanEditor({ site }: { site: Site }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const queryClient = useQueryClient();

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["site", site.id] });
    queryClient.invalidateQueries({ queryKey: ["sites"] });
  };

  const remove = useMutation({
    mutationFn: (wanId: string) => endpoints.deleteWan(site.id, wanId),
    onSuccess: refresh,
  });

  return (
    <div className="card">
      <div className="row" style={{ alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>Uplinks</h2>
        <div style={{ flex: 0 }}>
          <button onClick={() => setAdding(true)}>Add uplink</button>
        </div>
      </div>

      {remove.isError && (
        <div className="error" style={{ marginTop: 12 }}>
          {(remove.error as Error).message}
        </div>
      )}

      {adding && (
        <WanForm
          siteId={site.id}
          onDone={() => {
            setAdding(false);
            refresh();
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      {site.wans.length === 0 ? (
        <p className="muted">
          No uplinks recorded. Probe the device to discover them, or add one by hand.
        </p>
      ) : (
        <table style={{ marginTop: 12 }}>
          <thead>
            <tr>
              <th>Name</th>
              <th>Interface</th>
              <th>Public IP</th>
              <th>Tags</th>
              <th>Cost</th>
              <th>Reachability</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {site.wans.map((wan) =>
              editing === wan.id ? (
                <tr key={wan.id}>
                  <td colSpan={7}>
                    <WanForm
                      siteId={site.id}
                      wan={wan}
                      onDone={() => {
                        setEditing(null);
                        refresh();
                      }}
                      onCancel={() => setEditing(null)}
                    />
                  </td>
                </tr>
              ) : (
                <tr key={wan.id}>
                  <td>
                    {wan.name}
                    {!wan.enabled && <span className="muted"> · disabled</span>}
                  </td>
                  <td>{wan.interface}</td>
                  <td className="muted">{wan.public_ip ?? "none"}</td>
                  <td className="muted">
                    {Object.keys(wan.tags ?? {}).join(", ") || "—"}
                  </td>
                  <td>{wan.cost}</td>
                  <td>
                    {wan.dial_out_only ? (
                      <span className="badge drifted">dial-out only</span>
                    ) : (
                      <span className="badge reachable">accepts tunnels</span>
                    )}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <button onClick={() => setEditing(wan.id)}>Edit</button>{" "}
                    <button
                      onClick={() => {
                        if (
                          confirm(
                            `Remove uplink ${wan.name}? Any links using it will be ` +
                              `dropped on the next fabric expand.`,
                          )
                        )
                          remove.mutate(wan.id);
                      }}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ),
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}

function WanForm({
  siteId,
  wan,
  onDone,
  onCancel,
}: {
  siteId: string;
  wan?: Wan;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    name: wan?.name ?? "",
    interface: wan?.interface ?? "",
    public_ip: wan?.public_ip ?? "",
    nat_behind: wan?.nat_behind ?? false,
    dynamic: wan?.dynamic ?? false,
    gateway: wan?.gateway ?? "",
    provider: wan?.provider ?? "",
    bandwidth_mbps: wan?.bandwidth_mbps?.toString() ?? "",
    cost: wan?.cost?.toString() ?? "1",
    enabled: wan?.enabled ?? true,
    tags: Object.keys(wan?.tags ?? {}).join(", "),
  });

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: form.name,
        interface: form.interface,
        public_ip: form.public_ip || null,
        nat_behind: form.nat_behind,
        dynamic: form.dynamic,
        gateway: form.gateway || null,
        provider: form.provider || null,
        bandwidth_mbps: form.bandwidth_mbps ? Number(form.bandwidth_mbps) : null,
        cost: Number(form.cost),
        enabled: form.enabled,
        // Stored as a map so a tag can carry a value later; the UI only needs
        // the names, so everything gets "yes".
        tags: Object.fromEntries(
          form.tags
            .split(",")
            .map((t) => t.trim())
            .filter(Boolean)
            .map((t) => [t, "yes"]),
        ),
      };
      return wan
        ? endpoints.updateWan(siteId, wan.id, body)
        : endpoints.addWan(siteId, body);
    },
    onSuccess: onDone,
  });

  const dialOutOnly = !form.public_ip || form.nat_behind;

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <form onSubmit={submit} style={{ padding: "12px 0" }}>
      {save.isError && <div className="error">{(save.error as Error).message}</div>}
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
          Interface
          <input
            required
            placeholder="ether1"
            value={form.interface}
            onChange={(e) => setForm({ ...form, interface: e.target.value })}
          />
        </label>
        <label>
          Public IP
          <input
            placeholder="blank if behind NAT"
            value={form.public_ip}
            onChange={(e) => setForm({ ...form, public_ip: e.target.value })}
          />
        </label>
      </div>

      <div className="row">
        <label>
          Tags
          <input
            placeholder="mpls, lte"
            value={form.tags}
            onChange={(e) => setForm({ ...form, tags: e.target.value })}
          />
        </label>
        <label>
          Cost
          <input
            type="number"
            step="0.1"
            value={form.cost}
            onChange={(e) => setForm({ ...form, cost: e.target.value })}
          />
        </label>
        <label>
          Bandwidth (Mbps)
          <input
            type="number"
            value={form.bandwidth_mbps}
            onChange={(e) => setForm({ ...form, bandwidth_mbps: e.target.value })}
          />
        </label>
        <label>
          Provider
          <input
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
          />
        </label>
      </div>

      <div className="row" style={{ justifyContent: "flex-start", gap: 20 }}>
        <label style={{ flex: 0, whiteSpace: "nowrap" }}>
          <input
            type="checkbox"
            style={{ width: "auto", marginRight: 6 }}
            checked={form.nat_behind}
            onChange={(e) => setForm({ ...form, nat_behind: e.target.checked })}
          />
          Behind NAT
        </label>
        <label style={{ flex: 0, whiteSpace: "nowrap" }}>
          <input
            type="checkbox"
            style={{ width: "auto", marginRight: 6 }}
            checked={form.dynamic}
            onChange={(e) => setForm({ ...form, dynamic: e.target.checked })}
          />
          Dynamic address
        </label>
        <label style={{ flex: 0, whiteSpace: "nowrap" }}>
          <input
            type="checkbox"
            style={{ width: "auto", marginRight: 6 }}
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          />
          Enabled
        </label>
      </div>

      {dialOutOnly && (
        <p className="warn">
          With no public address, or behind NAT, this uplink can only dial out. It
          will never accept a tunnel, and two such uplinks can never link to each
          other — they must go through a hub.
        </p>
      )}

      <div className="row" style={{ justifyContent: "flex-start" }}>
        <div style={{ flex: 0 }}>
          <button className="primary" type="submit" disabled={save.isPending}>
            {save.isPending ? "Saving…" : wan ? "Save" : "Add"}
          </button>
        </div>
        <div style={{ flex: 0 }}>
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </form>
  );
}
