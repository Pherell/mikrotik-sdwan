/**
 * The VPN section: one filtered view of the fabric engine per protocol.
 *
 * A fabric is exactly one transport, so a "WireGuard networks" page is the
 * fabric list with a server-side transport filter — the model is not forked
 * per protocol. The protocol is chosen by the menu, never re-asked inside
 * the form; that is the whole point of splitting the menu.
 */

import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import {
  endpoints,
  type Fabric,
  type TransportInfo,
  type TransportOption,
} from "../lib/api";

type Protocol = {
  key: string;
  label: string;
  transports: string[];
  title: string;
  blurb: string;
  createLabel: string;
  note?: string;
  plaintext?: boolean;
};

export const PROTOCOLS = {
  wireguard: {
    key: "wireguard",
    label: "WireGuard",
    transports: ["wireguard"],
    title: "WireGuard networks",
    blurb:
      "Only WireGuard fabrics live here. The protocol is fixed by the menu — there is no transport picker inside the form.",
    createLabel: "New WireGuard network",
    note: "WireGuard learns a roaming peer's address from its handshake, so a NAT'd branch only needs one reachable side. On links where neither side is behind NAT, a silently dead path is detected by the BGP hold time (30s) — there is no WireGuard keepalive in that case.",
  },
  ipsec: {
    key: "ipsec",
    label: "IPsec",
    transports: ["ipsec_gre"],
    title: "IPsec networks",
    blurb:
      "IPsec with a GRE overlay (the ipsec_gre transport). Every crypto parameter is set once here and rendered to both ends, so the two sides agree by construction.",
    createLabel: "New IPsec network",
  },
  gre: {
    key: "gre",
    label: "GRE / IPIP",
    transports: ["gre", "ipip"],
    title: "GRE / IPIP networks",
    blurb:
      "Plain encapsulation with no encryption of its own. Use it inside a trusted path, or where another layer already encrypts.",
    createLabel: "New GRE / IPIP network",
    plaintext: true,
  },
  l2: {
    key: "l2",
    label: "L2 — VXLAN / EoIP",
    transports: ["vxlan", "eoip"],
    title: "L2 networks — VXLAN / EoIP",
    blurb:
      "Layer-2 segments stretched across sites. Both ride on an IPsec/GRE underlay link of the same fabric pair, so the segment inherits that link's encryption.",
    createLabel: "New L2 network",
  },
} satisfies Record<string, Protocol>;

const TOPOLOGY_LABEL: Record<string, string> = {
  hub_spoke: "Hub & spoke",
  hub_spoke_dynamic: "Hub-spoke dynamic",
  full_mesh: "Full mesh",
};

export function TopologyCell({ topology }: { topology: string }) {
  return (
    <>
      {TOPOLOGY_LABEL[topology] ?? topology}
      {topology === "hub_spoke_dynamic" && (
        <>
          {" "}
          <span className="badge drifted">dynamic mesh not active yet</span>
        </>
      )}
    </>
  );
}

function NetworksTable({
  fabrics,
  showTransport,
}: {
  fabrics: Fabric[];
  showTransport: boolean;
}) {
  return (
    <table>
      <thead>
        <tr>
          <th>Network</th>
          {showTransport && <th>Protocol</th>}
          <th>Topology</th>
          <th>Sites</th>
          <th>Tunnels</th>
          <th>Tunnel pool</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {fabrics.map((f) => (
          <tr key={f.id}>
            <td>
              <Link to={`/vpn/${f.id}`}>{f.name}</Link>
            </td>
            {showTransport && <td>{f.transport}</td>}
            <td>
              <TopologyCell topology={f.topology} />
            </td>
            <td>{f.members.length}</td>
            <td>{f.link_count}</td>
            <td className="muted">
              {f.ip_pool}
              <div>
                {f.link_count} of {f.pool_capacity} used
              </div>
            </td>
            <td>
              <Link to={`/vpn/${f.id}`}>Open</Link>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ProtocolNetworksPage({ protocol }: { protocol: Protocol }) {
  const navigate = useNavigate();
  const [adding, setAdding] = useState(false);
  const [filter, setFilter] = useState("");

  const transports = useQuery({
    queryKey: ["transports"],
    queryFn: endpoints.transports,
  });
  const results = useQueries({
    queries: protocol.transports.map((t) => ({
      queryKey: ["fabrics", t],
      queryFn: () => endpoints.fabrics(t),
    })),
  });

  const isLoading = results.some((r) => r.isLoading);
  const error = results.find((r) => r.isError)?.error as Error | undefined;
  const all = results
    .flatMap((r) => r.data ?? [])
    .sort((a, b) => a.name.localeCompare(b.name));
  const visible = all.filter((f) =>
    f.name.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  return (
    <>
      <PageHeader title={protocol.title} description={protocol.blurb}>
        <button className="primary" onClick={() => setAdding(true)}>
          {protocol.createLabel}
        </button>
      </PageHeader>

      {protocol.plaintext && (
        <div className="warn">
          <strong>Not encrypted.</strong> GRE and IPIP have no encryption of
          their own — everything crosses the internet in clear text. That is
          the right choice inside a trusted path, and the wrong one across
          the open internet.
        </div>
      )}
      {protocol.note && <div className="card muted">{protocol.note}</div>}

      {adding && (
        <FabricWizard
          transports={transports.data ?? []}
          choices={protocol.transports}
          onCreated={(id) => navigate(`/vpn/${id}`)}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="card">
        <div className="row" style={{ justifyContent: "flex-start" }}>
          <label style={{ flex: "0 0 280px", margin: 0 }}>
            <input
              placeholder="Filter by network name…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </label>
          <span className="muted">
            {visible.length} network{visible.length === 1 ? "" : "s"}
          </span>
        </div>
        {isLoading && <Skeleton rows={3} />}
        {error && <div className="error">{error.message}</div>}
        {!isLoading && !error && all.length === 0 && (
          <p className="muted">
            No {protocol.label} networks yet. Create one and the controller
            works out every tunnel, allocates the addresses and generates the
            keys.
          </p>
        )}
        {!isLoading && visible.length > 0 && (
          <NetworksTable fabrics={visible} showTransport={protocol.transports.length > 1} />
        )}
      </div>
    </>
  );
}

export function AllTunnelsPage() {
  const navigate = useNavigate();
  const [adding, setAdding] = useState(false);

  const fabrics = useQuery({ queryKey: ["fabrics"], queryFn: () => endpoints.fabrics() });
  const links = useQuery({ queryKey: ["links"], queryFn: endpoints.allLinks });
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });
  const transports = useQuery({
    queryKey: ["transports"],
    queryFn: endpoints.transports,
  });

  const fabricName = new Map((fabrics.data ?? []).map((f) => [f.id, f.name]));
  const fabricTransport = new Map(
    (fabrics.data ?? []).map((f) => [f.id, f.transport]),
  );
  const wanSite = new Map<string, string>();
  for (const site of sites.data ?? []) {
    for (const wan of site.wans) wanSite.set(wan.id, site.name);
  }
  const pending = (links.data ?? []).filter((l) => l.state === "pending").length;

  return (
    <>
      <PageHeader
        title="All tunnels"
        description="Every VPN network and every tunnel across all protocols, in one place. Protocol-specific setup lives in the per-protocol pages."
      >
        <button className="primary" onClick={() => setAdding(true)}>
          New network
        </button>
      </PageHeader>

      {adding && (
        <FabricWizard
          transports={transports.data ?? []}
          choices={(transports.data ?? []).map((t) => t.name)}
          onCreated={(id) => navigate(`/vpn/${id}`)}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="row">
        <div className="card" style={{ textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: 600 }}>{fabrics.data?.length ?? "–"}</div>
          <div className="muted">networks</div>
        </div>
        <div className="card" style={{ textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: 600 }}>{links.data?.length ?? "–"}</div>
          <div className="muted">tunnels</div>
        </div>
        <div className="card" style={{ textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: 600 }}>
            {new Set((fabrics.data ?? []).map((f) => f.transport)).size}
          </div>
          <div className="muted">protocols in use</div>
        </div>
        <div className="card" style={{ textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: 600 }}>{pending}</div>
          <div className="muted">tunnels pending apply</div>
        </div>
      </div>

      <div className="card">
        <h2>Networks</h2>
        {fabrics.isLoading && <Skeleton rows={3} />}
        {fabrics.isError && (
          <div className="error">{(fabrics.error as Error).message}</div>
        )}
        {fabrics.data && fabrics.data.length === 0 && (
          <p className="muted">No VPN networks yet.</p>
        )}
        {fabrics.data && fabrics.data.length > 0 && (
          <NetworksTable fabrics={fabrics.data} showTransport />
        )}
      </div>

      <div className="card">
        <h2>Tunnels</h2>
        {links.isLoading && <Skeleton rows={3} />}
        {links.isError && <div className="error">{(links.error as Error).message}</div>}
        {links.data && links.data.length === 0 && (
          <p className="muted">No tunnels yet — expand a network to build them.</p>
        )}
        {links.data && links.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Tunnel</th>
                <th>Network</th>
                <th>Protocol</th>
                <th>Between</th>
                <th>Subnet</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {links.data.map((l) => (
                <tr key={l.id}>
                  <td>{l.slug}</td>
                  <td>
                    <Link to={`/vpn/${l.fabric_id}`}>
                      {fabricName.get(l.fabric_id) ?? "?"}
                    </Link>
                  </td>
                  <td>{fabricTransport.get(l.fabric_id) ?? "?"}</td>
                  <td>
                    {wanSite.get(l.a_wan_id) ?? "?"} ↔ {wanSite.get(l.b_wan_id) ?? "?"}
                  </td>
                  <td className="muted">{l.subnet}</td>
                  <td>
                    <span
                      className={`badge ${l.state === "pending" ? "drifted" : "reachable"}`}
                    >
                      {l.state}
                    </span>
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


/** Values that fall below the floor this controller should quietly allow an
 * operator to pick. The backend still accepts them — compatibility with a
 * peer you do not control is a real case — so the UI warns in place instead
 * of blocking. */
const WEAK_VALUES = new Set([
  "3des",
  "des",
  "md5",
  "sha1",
  "modp768",
  "modp1024",
  "none",
]);

const STEPS = ["Identity", "Members", "Protocol settings", "Review"];

function FabricWizard({
  transports,
  choices,
  onCreated,
  onCancel,
}: {
  transports: TransportInfo[];
  choices: string[];
  onCreated: (id: string) => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState({
    name: "",
    transport: choices[0] ?? "ipsec_gre",
    topology: "hub_spoke",
    ip_pool: "10.255.0.0/16",
    asn: 65000,
  });
  // Only what has been changed away from the default is sent. Sending the
  // defaults back would freeze them into the fabric, so a later change to
  // what this build considers sensible would not reach fabrics created today.
  const [params, setParams] = useState<Record<string, string>>({});
  const [memberIds, setMemberIds] = useState<string[]>([]);

  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });
  const chosen = transports.find((t) => t.name === form.transport);
  const options = chosen?.options ?? [];

  const create = useMutation({
    mutationFn: () =>
      endpoints.createFabric({
        ...form,
        member_site_ids: memberIds,
        transport_params: params,
      }),
    onSuccess: (fabric) => {
      queryClient.invalidateQueries({ queryKey: ["fabrics"] });
      onCreated(fabric.id);
    },
  });

  const locked = choices.length === 1;

  return (
    <div className="card">
      <h2>New network</h2>
      <div className="tabs">
        {STEPS.map((label, i) => (
          <button
            key={label}
            type="button"
            className={`tab${step === i ? " active" : ""}`}
            onClick={() => i < step && setStep(i)}
          >
            {i + 1}. {label}
          </button>
        ))}
      </div>
      {create.isError && <div className="error">{(create.error as Error).message}</div>}

      {step === 0 && (
        <>
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
              {locked ? (
                <input value={form.transport} disabled readOnly />
              ) : (
                <select
                  value={form.transport}
                  onChange={(e) => {
                    // Overrides belong to the transport that defines them.
                    setParams({});
                    setForm({ ...form, transport: e.target.value });
                  }}
                >
                  {choices.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              )}
            </label>
            <label>
              Topology
              <select
                value={form.topology}
                onChange={(e) => setForm({ ...form, topology: e.target.value })}
              >
                <option value="hub_spoke">hub and spoke</option>
                <option value="hub_spoke_dynamic">
                  hub and spoke + dynamic mesh
                </option>
                <option value="full_mesh">full mesh</option>
              </select>
            </label>
          </div>
          {form.topology === "hub_spoke_dynamic" && (
            <div className="warn">
              Dynamic spoke-to-spoke mesh is not active yet — today this
              behaves as plain hub and spoke. The label says so rather than
              letting you assume the tunnels exist.
            </div>
          )}
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
            Every tunnel takes a /31 from the pool. Changing it later is
            refused once links exist, because renumbering drops every tunnel
            on the overlay.
          </p>
        </>
      )}

      {step === 1 && (
        <>
          <p className="muted">
            Which devices take part. Members can also be added later, on the
            network's page — leaving this empty creates an empty network.
          </p>
          {sites.isLoading && <Skeleton rows={3} />}
          {(sites.data ?? []).map((site) => (
            <label
              key={site.id}
              style={{ display: "block", marginBottom: 6 }}
            >
              <input
                type="checkbox"
                checked={memberIds.includes(site.id)}
                onChange={(e) =>
                  setMemberIds((prev) =>
                    e.target.checked
                      ? [...prev, site.id]
                      : prev.filter((id) => id !== site.id),
                  )
                }
              />{" "}
              {site.name} <span className="muted">({site.role})</span>
            </label>
          ))}
        </>
      )}

      {step === 2 && (
        <>
          {form.transport === "ipsec_gre" && params["mode"] === "simple" && (
            <div className="warn">
              <strong>Compatibility mode.</strong> This turns off the
              hardening — no PFS, no anti-replay, no strict proposal — leaving
              effectively an IKEv1-era profile. The UI will not stop you, but
              nothing about it is quiet.
            </div>
          )}
          <ParamsFields
            options={options}
            values={params}
            onChange={(key, value) =>
              setParams((prev) => {
                const next = { ...prev };
                if (value === "") delete next[key];
                else next[key] = value;
                return next;
              })
            }
          />
        </>
      )}

      {step === 3 && (
        <dl className="kv">
          <dt>Name</dt>
          <dd>{form.name || "—"}</dd>
          <dt>Protocol</dt>
          <dd>{form.transport}</dd>
          <dt>Topology</dt>
          <dd>{TOPOLOGY_LABEL[form.topology] ?? form.topology}</dd>
          <dt>Tunnel pool</dt>
          <dd>{form.ip_pool}</dd>
          <dt>AS number</dt>
          <dd>{form.asn}</dd>
          <dt>Members</dt>
          <dd>
            {memberIds.length === 0
              ? "none yet — add them on the network's page"
              : (sites.data ?? [])
                  .filter((s) => memberIds.includes(s.id))
                  .map((s) => s.name)
                  .join(", ")}
          </dd>
          <dt>Protocol overrides</dt>
          <dd>
            {Object.keys(params).length === 0
              ? "controller defaults"
              : Object.entries(params)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(", ")}
          </dd>
        </dl>
      )}

      <div className="row" style={{ justifyContent: "flex-start", marginTop: 16 }}>
        <div className="no-grow">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
        {step > 0 && (
          <div className="no-grow">
            <button type="button" onClick={() => setStep(step - 1)}>
              Back
            </button>
          </div>
        )}
        {step < STEPS.length - 1 ? (
          <div className="no-grow">
            <button
              className="primary"
              type="button"
              disabled={step === 0 && !form.name.trim()}
              onClick={() => setStep(step + 1)}
            >
              Next
            </button>
          </div>
        ) : (
          <div className="no-grow">
            <button
              className="primary"
              type="button"
              disabled={create.isPending}
              onClick={() => create.mutate()}
            >
              {create.isPending ? "Creating…" : "Create network"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * The settings that must match on both ends, drawn from the transport's own
 * parameter schema (GET /fabrics/transports) — the backend owns the list, so
 * a new knob appears here without a UI release.
 *
 * Weak choices are warned about in place rather than refused: a peer you do
 * not control may genuinely only speak 3des, and a hard refusal would strand
 * a legitimate migration. The warning makes the choice loud instead.
 */
function ParamsFields({
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

      {options.map((option) => {
        const value = values[option.key] ?? "";
        const weak = WEAK_VALUES.has(value);
        return (
          <label key={option.key}>
            {option.label}
            {option.kind === "choice" ? (
              <select
                value={value}
                onChange={(e) => onChange(option.key, e.target.value)}
              >
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
                value={value}
                onChange={(e) => onChange(option.key, e.target.value)}
              />
            )}
            <span className="muted field-note">{option.why}</span>
            {weak && (
              <span className="field-note" style={{ color: "#b3261e" }}>
                Below the safety floor — acceptable only for a peer you do not
                control that speaks nothing stronger.
              </span>
            )}
          </label>
        );
      })}
    </>
  );
}
