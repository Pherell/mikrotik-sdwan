import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import {
  endpoints,
  type TransportInfo,
  type TransportOption,
} from "../lib/api";
import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";

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
      <PageHeader
        title="Tunnel networks"
        description="An encrypted network joining your devices over whatever internet they have. Choose how they connect and which devices take part; the controller works out every tunnel."
      >
        <button className="primary" onClick={() => setAdding(true)}>
          New tunnel network
        </button>
      </PageHeader>

      {adding && (
        <NewFabricForm
          transports={transports.data ?? []}
          onDone={() => {
            setAdding(false);
            queryClient.invalidateQueries({ queryKey: ["fabrics"] });
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="card">
        {fabrics.isLoading && <Skeleton rows={3} />}
        {fabrics.isError && <div className="error">{(fabrics.error as Error).message}</div>}
        {fabrics.data?.length === 0 && (
          <p className="muted">
            No tunnel networks yet. One joins your devices to each other over
            whatever internet they have. You choose which devices take part and how
            they connect — IPsec, WireGuard, plain GRE — and the controller works out
            every tunnel, allocates the addresses and generates the keys.
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
                    <Link to={`/tunnel-networks/${f.id}`}>{f.name}</Link>
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
  transports: TransportInfo[];
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
  const [advanced, setAdvanced] = useState(false);
  // Only what has been changed away from the default is sent. Sending the
  // defaults back would freeze them into the fabric, so a later change to what
  // this build considers sensible would not reach fabrics created today.
  const [params, setParams] = useState<Record<string, string>>({});

  const chosen = transports.find((t) => t.name === form.transport);
  const options = chosen?.options ?? [];

  const create = useMutation({
    mutationFn: () => endpoints.createFabric({ ...form, transport_params: params }),
    onSuccess: onDone,
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <div className="card">
      <h2>New tunnel network</h2>
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
              onChange={(e) => {
                // Overrides belong to the transport that defines them. Keeping
                // them across a switch means sending a setting the new
                // transport has never heard of, which is now correctly
                // refused -- so clear them rather than produce that error.
                setParams({});
                setForm({ ...form, transport: e.target.value });
              }}
            >
              {(transports.length ? transports.map((t) => t.name) : ["ipsec_gre"]).map(
                (t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ),
              )}
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
          <div className="no-grow">
            <button type="button" onClick={() => setAdvanced(!advanced)}>
              {advanced ? "Hide" : "Show"} advanced settings
            </button>
          </div>
        </div>

        {advanced && (
          <AdvancedParams
            options={options}
            values={params}
            onChange={(key, value) =>
              setParams((prev) => {
                const next = { ...prev };
                // Removing the key rather than storing the default, so the
                // fabric keeps following this build's chosen default.
                if (value === "") delete next[key];
                else next[key] = value;
                return next;
              })
            }
          />
        )}
        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div className="no-grow">
            <button className="primary" type="submit" disabled={create.isPending}>
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

/**
 * The settings that must match on both ends.
 *
 * These are negotiated parameters: one side offering aes-256-gcm and the other
 * aes-128-cbc is not a weaker tunnel, it is no tunnel. The controller renders
 * both ends from one fabric, so it is consistent by construction — but only
 * for ends it renders, which is why the warning says what it says.
 *
 * Behind a button because the defaults are chosen and most people should never
 * open this. Visible because "my security standard says ecp384" is a real
 * requirement and the alternative is editing the database.
 */
function AdvancedParams({
  options,
  values,
  onChange,
}: {
  options: TransportOption[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  if (options.length === 0) {
    return (
      <p className="muted">
        This transport has nothing to negotiate. GRE and IPIP carry no
        encryption of their own, and WireGuard's is not selectable by design —
        that is the point of it.
      </p>
    );
  }

  return (
    <>
      <div className="warn">
        Every setting here must match at both ends of every tunnel. The
        controller configures both ends from this one place, so they agree by
        construction — but a device configured by hand, or a peer that is not
        managed here, will simply fail to establish, and IKE does not report a
        mismatch as an error.
      </div>

      {options.map((option) => (
        <label key={option.key}>
          {option.label}
          {option.kind === "choice" ? (
            <select
              value={values[option.key] ?? ""}
              onChange={(e) => onChange(option.key, e.target.value)}
            >
              {/* The empty value means "leave it to the controller", which is
                  different from picking the value that happens to be the
                  default today. */}
              <option value="">default ({String(option.default)})</option>
              {option.choices.map((choice) => (
                <option key={choice} value={choice}>
                  {choice}
                </option>
              ))}
            </select>
          ) : (
            <input
              type={option.kind === "int" ? "number" : "text"}
              min={option.minimum ?? undefined}
              max={option.maximum ?? undefined}
              placeholder={`default (${String(option.default)})`}
              value={values[option.key] ?? ""}
              onChange={(e) => onChange(option.key, e.target.value)}
            />
          )}
          <span className="muted field-note">{option.why}</span>
        </label>
      ))}
    </>
  );
}
