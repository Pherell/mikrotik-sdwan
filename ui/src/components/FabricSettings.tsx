import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { endpoints, type Fabric } from "../lib/api";

/**
 * Edit a fabric, including the transport switch.
 *
 * This is the "one dropdown" the design promised and the UI never had. Changing
 * it re-keys every link server-side, because WireGuard cannot use an IPsec PSK.
 */
export function FabricSettings({ fabric, onDone }: { fabric: Fabric; onDone: () => void }) {
  const queryClient = useQueryClient();
  const transports = useQuery({ queryKey: ["transports"], queryFn: endpoints.transports });

  const [form, setForm] = useState({
    name: fabric.name,
    description: fabric.description ?? "",
    transport: fabric.transport,
    topology: fabric.topology,
    ip_pool: fabric.ip_pool,
    asn: fabric.asn.toString(),
    mtu: fabric.mtu.toString(),
    enabled: fabric.enabled,
  });

  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: form.name,
        description: form.description || null,
        topology: form.topology,
        asn: Number(form.asn),
        mtu: Number(form.mtu),
        enabled: form.enabled,
      };
      if (form.transport !== fabric.transport) body.transport = form.transport;
      // Only send the pool when it changed; the API refuses a change once links
      // exist, and sending an identical value would trip that check for nothing.
      if (form.ip_pool !== fabric.ip_pool) body.ip_pool = form.ip_pool;
      return endpoints.updateFabric(fabric.id, body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["fabric", fabric.id] });
      queryClient.invalidateQueries({ queryKey: ["fabrics"] });
      queryClient.invalidateQueries({ queryKey: ["fabric-links", fabric.id] });
      onDone();
    },
  });

  const switching = form.transport !== fabric.transport;
  const poolLocked = fabric.link_count > 0;

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <div className="card">
      <h2>Fabric settings</h2>
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
            Transport
            <select
              value={form.transport}
              onChange={(e) => setForm({ ...form, transport: e.target.value })}
            >
              {(transports.data ?? []).map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                  {t.supported_ros.length === 1 ? ` (RouterOS ${t.supported_ros[0]} only)` : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            Topology
            <select
              value={form.topology}
              onChange={(e) => setForm({ ...form, topology: e.target.value })}
            >
              <option value="hub_spoke">hub and spoke</option>
              <option value="hub_spoke_dynamic">hub and spoke + dynamic mesh</option>
              <option value="full_mesh">full mesh</option>
            </select>
          </label>
        </div>

        <div className="row">
          <label>
            Tunnel pool
            <input
              value={form.ip_pool}
              disabled={poolLocked}
              onChange={(e) => setForm({ ...form, ip_pool: e.target.value })}
            />
          </label>
          <label>
            AS number
            <input
              type="number"
              value={form.asn}
              onChange={(e) => setForm({ ...form, asn: e.target.value })}
            />
          </label>
          <label>
            Tunnel MTU
            <input
              type="number"
              value={form.mtu}
              onChange={(e) => setForm({ ...form, mtu: e.target.value })}
            />
          </label>
        </div>

        {poolLocked && (
          <p className="muted">
            The tunnel pool is locked while {fabric.link_count} link(s) are addressed
            out of it. Renumbering would drop every tunnel on this fabric.
          </p>
        )}

        {switching && (
          <p className="warn">
            Switching from <strong>{fabric.transport}</strong> to{" "}
            <strong>{form.transport}</strong> re-keys every link — the old key
            material cannot carry over. Nothing changes on the devices until you
            apply each member site, and the old stack is swept off then.
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
      </form>
    </div>
  );
}
