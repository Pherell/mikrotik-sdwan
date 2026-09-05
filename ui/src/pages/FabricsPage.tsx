import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { endpoints } from "../lib/api";

export function FabricsPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);

  const fabrics = useQuery({ queryKey: ["fabrics"], queryFn: endpoints.fabrics });
  const transports = useQuery({
    queryKey: ["transports"],
    queryFn: endpoints.transports,
  });

  return (
    <>
      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>Fabrics</h2>
          <div style={{ flex: 0 }}>
            <button className="primary" onClick={() => setAdding(true)}>
              New fabric
            </button>
          </div>
        </div>
      </div>

      {adding && (
        <NewFabricForm
          transports={(transports.data ?? []).map((t) => t.name)}
          onDone={() => {
            setAdding(false);
            queryClient.invalidateQueries({ queryKey: ["fabrics"] });
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="card">
        {fabrics.isLoading && <p className="muted">Loading…</p>}
        {fabrics.isError && <div className="error">{(fabrics.error as Error).message}</div>}
        {fabrics.data?.length === 0 && (
          <p className="muted">
            No fabrics yet. A fabric is one overlay: a transport, a topology, and the
            sites that take part.
          </p>
        )}
        {fabrics.data && fabrics.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Transport</th>
                <th>Topology</th>
                <th>Members</th>
                <th>Links</th>
                <th>Tunnel pool</th>
              </tr>
            </thead>
            <tbody>
              {fabrics.data.map((f) => (
                <tr key={f.id}>
                  <td>
                    <Link to={`/fabrics/${f.id}`}>{f.name}</Link>
                  </td>
                  <td>{f.transport}</td>
                  <td>{f.topology}</td>
                  <td>{f.members.length}</td>
                  <td>{f.link_count}</td>
                  <td className="muted">
                    {f.ip_pool}
                    <div>
                      {f.link_count} of {f.pool_capacity} used
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function NewFabricForm({
  transports,
  onDone,
  onCancel,
}: {
  transports: string[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState({
    name: "",
    transport: "ipsec_gre",
    topology: "hub_spoke",
    ip_pool: "10.255.0.0/16",
    asn: 65000,
  });

  const create = useMutation({
    mutationFn: () => endpoints.createFabric(form),
    onSuccess: onDone,
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <div className="card">
      <h2>New fabric</h2>
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
            Transport
            <select
              value={form.transport}
              onChange={(e) => setForm({ ...form, transport: e.target.value })}
            >
              {(transports.length ? transports : ["ipsec_gre"]).map((t) => (
                <option key={t} value={t}>
                  {t}
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
              required
              value={form.ip_pool}
              onChange={(e) => setForm({ ...form, ip_pool: e.target.value })}
            />
          </label>
          <label>
            AS number
            <input
              type="number"
              value={form.asn}
              onChange={(e) => setForm({ ...form, asn: Number(e.target.value) })}
            />
          </label>
        </div>
        <p className="muted">
          Every tunnel takes a /31 from the pool. Changing it later is refused once
          links exist, because renumbering drops every tunnel on the overlay.
        </p>
        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div style={{ flex: 0 }}>
            <button className="primary" type="submit" disabled={create.isPending}>
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
