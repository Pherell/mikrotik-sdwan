/**
 * API client.
 *
 * The token lives in memory plus sessionStorage rather than localStorage: a
 * controller session that outlives the browser tab is a liability, and
 * sessionStorage is not shared across tabs opened from untrusted links.
 */

const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";
const TOKEN_KEY = "sdwan.token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

let token: string | null = sessionStorage.getItem(TOKEN_KEY);

export function setToken(value: string | null): void {
  token = value;
  if (value) sessionStorage.setItem(TOKEN_KEY, value);
  else sessionStorage.removeItem(TOKEN_KEY);
}

export function getToken(): string | null {
  return token;
}

async function request<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, ...rest } = init;
  const headers = new Headers(rest.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (json !== undefined) headers.set("Content-Type", "application/json");

  const resp = await fetch(`${BASE}${path}`, {
    ...rest,
    headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });

  if (resp.status === 401) {
    // The token is gone or expired; drop it so the router sends us to login.
    setToken(null);
    throw new ApiError(401, "Your session expired. Sign in again.");
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, await readError(resp));
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

async function readError(resp: Response): Promise<string> {
  // Read once, as text. An unhandled server exception comes back as Starlette's
  // plain-text "Internal Server Error", not JSON, so parsing first and falling
  // back to the status meant the UI rendered the single word "500" with no clue
  // what had happened. Text first, then try to make sense of it.
  const raw = await resp.text().catch(() => "");

  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    // Not JSON. The body itself is the most informative thing available.
    return raw.trim() || `${resp.status} ${resp.statusText}`.trim();
  }

  const detail = (body as { detail?: unknown })?.detail;
  if (typeof detail === "string") return detail;
  // FastAPI validation errors arrive as a list of per-field objects.
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: string[]; msg?: string }) => {
        const field = d.loc?.slice(1).join(".") ?? "";
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  return raw.trim() || `${resp.status} ${resp.statusText}`.trim();
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, json?: unknown) => request<T>(path, { method: "POST", json }),
  patch: <T,>(path: string, json: unknown) => request<T>(path, { method: "PATCH", json }),
  del: (path: string) => request<void>(path, { method: "DELETE" }),
};

// -- types mirroring app/schemas -------------------------------------------

export type SiteRole = "hub" | "spoke";
export type SiteStatus =
  | "unprovisioned"
  | "reachable"
  | "unreachable"
  | "drifted"
  | "error";

export interface Wan {
  id: string;
  site_id: string;
  name: string;
  interface: string;
  public_ip: string | null;
  dynamic: boolean;
  nat_behind: boolean;
  gateway: string | null;
  provider: string | null;
  bandwidth_mbps: number | null;
  cost: number;
  enabled: boolean;
  masquerade: boolean;
  dial_out_only: boolean;
  tags: Record<string, string>;
}

export interface Site {
  id: string;
  name: string;
  description: string | null;
  region: string | null;
  role: SiteRole;
  mgmt_host: string;
  mgmt_port: number | null;
  device_kind: string;
  username: string;
  status: SiteStatus;
  ros_version: string | null;
  board_name: string | null;
  architecture: string | null;
  identity: string | null;
  last_seen_at: string | null;
  last_error: string | null;
  has_credentials: boolean;
  // The device identity recorded on first contact. A later connection
  // presenting anything else is refused, so this is what an operator compares
  // by eye against the router before deciding a mismatch is boring.
  tls_fingerprint: string | null;
  has_ssh_host_key: boolean;
  loopback_ip: string | null;
  local_prefixes: string[];
  drift_action: string;
  wans: Wan[];
}

export interface ProbeResult {
  reachable: boolean;
  error: string | null;
  version: string | null;
  board_name: string | null;
  architecture: string | null;
  identity: string | null;
  ros_major: number | null;
  has_wireguard: boolean;
  has_container: boolean;
  has_netwatch_thresholds: boolean;
  packages: string[];
  suggested_wans: Array<Omit<Wan, "id" | "site_id" | "dial_out_only">>;
  lan_interfaces: Array<{ interface: string; reason: string }>;
}

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string | null;
  role: "admin" | "operator" | "viewer";
}

export interface PlanSection {
  path: string;
  order: number;
  lines: string[];
}

export interface Plan {
  counts: { add: number; set: number; remove: number };
  empty: boolean;
  unreadable: Record<string, string>;
  sections: PlanSection[];
  text: string;
}

export type JobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "rolled_back"
  | "cancelled";

export interface Job {
  id: string;
  kind: string;
  state: JobState;
  site_id: string | null;
  plan: Plan | null;
  diff: { text: string } | null;
  result: Record<string, unknown> | null;
  log: string | null;
  error: string | null;
  backup_name: string | null;
  rollback_token: string | null;
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface FabricMember {
  id: string;
  site_id: string;
  site_name: string;
  role_override: SiteRole | null;
  loopback_ip: string | null;
  enabled: boolean;
}

export interface Fabric {
  id: string;
  name: string;
  description: string | null;
  transport: string;
  transport_params: Record<string, unknown>;
  topology: string;
  ip_pool: string;
  loopback_pool: string;
  asn: number;
  mtu: number;
  enabled: boolean;
  members: FabricMember[];
  link_count: number;
  pool_capacity: number;
}

export interface FabricLink {
  id: string;
  fabric_id: string;
  slug: string;
  a_wan_id: string;
  b_wan_id: string;
  a_tunnel_ip: string;
  b_tunnel_ip: string;
  subnet: string;
  initiator: string;
  dynamic: boolean;
  enabled: boolean;
  state: string;
  has_secrets: boolean;
}

/**
 * One thing a transport lets you change.
 *
 * The list travels with the transport rather than being written out again
 * here: a second copy of a list of ciphers goes stale the first time one is
 * added, and a stale list silently hides a setting.
 */
export interface TransportOption {
  key: string;
  label: string;
  why: string;
  kind: "choice" | "int" | "duration" | "text";
  choices: string[];
  minimum: number | null;
  maximum: number | null;
  default: string | number | null;
}

export interface TransportInfo {
  name: string;
  supported_ros: number[];
  requires_reachable_responder: boolean;
  supports_dynamic_mesh: boolean;
  // Empty for transports with nothing to negotiate: GRE and IPIP have no
  // ciphers to agree on, and WireGuard's are not selectable by design.
  options: TransportOption[];
}

export interface Expansion {
  created: number;
  kept: number;
  removed: number;
  skipped: number;
  problems: Array<{ a: string; b: string; reason: string }>;
  affected_site_ids: string[];
}

export interface SlaProfile {
  id: string;
  name: string;
  description: string | null;
  loss_percent: number;
  latency_ms: number;
  jitter_ms: number | null;
  probe_interval_seconds: number;
  probe_count: number;
  recovery_seconds: number;
  detection_seconds: number;
}

export interface AppGroup {
  id: string;
  name: string;
  description: string | null;
  prefixes: string[];
  ports: number[];
  protocol: string | null;
  dscp: number | null;
  builtin: boolean;
}

export interface GroupMember {
  uplink: string;
  // Only meaningful under load_balance, where it is a share of the
  // *connections*, not of the bandwidth.
  weight: number;
}

export type GroupStrategy = "failover" | "load_balance";

/**
 * A named set of uplinks, in preference order, with a health standard.
 * The half of a steering decision worth naming once and reusing.
 */
export interface SdwanGroup {
  id: string;
  name: string;
  description: string | null;
  members: GroupMember[];
  strategy: GroupStrategy;
  sla_profile_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Policy {
  id: string;
  name: string;
  description: string | null;
  priority: number;
  enabled: boolean;
  fabric_id: string | null;
  site_ids: string[];
  src_prefixes: string[];
  dst_prefixes: string[];
  app_group_id: string | null;
  protocol: string | null;
  dst_ports: string | null;
  dscp: number | null;
  sdwan_group_id: string | null;
  fallback: string;
}

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  role: "admin" | "operator" | "viewer";
  is_active: boolean;
  created_at: string;
}

export interface ArmedRollback {
  name: string;
  interval: string | null;
  next_run: string | null;
  on_event: string | null;
}

/** Menus the controller will read straight off a device. Mirrors READABLE_PATHS
 *  in app/api/v1/sites.py -- anything else is refused server-side. */
export const DEVICE_MENUS = [
  "system/resource",
  "system/identity",
  "system/package",
  "system/scheduler",
  "system/routerboard",
  "interface",
  "interface/bridge",
  "interface/gre",
  "interface/wireguard",
  "ip/address",
  "ip/route",
  "ip/dhcp-client",
  "ip/firewall/address-list",
  "ip/firewall/mangle",
  "ip/ipsec/active-peers",
  "ip/ipsec/installed-sa",
  "ip/ipsec/policy",
  "ip/ipsec/peer",
  "ip/ipsec/profile",
  "ip/ipsec/proposal",
  "routing/bgp/session",
  "routing/bgp/connection",
  "routing/bgp/template",
  "routing/bgp/network",
  "routing/table",
  "tool/netwatch",
] as const;

export type PortRole =
  | "wan"
  | "candidate"
  | "lan"
  | "unused"
  | "bridge"
  | "tunnel"
  | "other";

export interface Port {
  name: string;
  type: string;
  running: boolean;
  disabled: boolean;
  comment: string | null;
  mac: string | null;
  mtu: number | null;
  speed: string | null;
  default_name: string | null;
  addresses: string[];
  rx_bytes: number | null;
  tx_bytes: number | null;
  bridge: string | null;
  role: PortRole;
  wan_name: string | null;
  wan_enabled: boolean | null;
  dhcp_client: boolean;
  default_route: boolean;
  managed: boolean;
}

// -- API tokens -------------------------------------------------------------

export interface ApiToken {
  id: string;
  name: string;
  // The public half. Enough to tell two tokens apart and to match a row
  // against a credential somebody is holding.
  prefix: string;
  role: "viewer" | "operator" | "admin";
  owner_id: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

// The one response that carries the usable credential. It is not stored and
// cannot be fetched again.
export interface ApiTokenCreated extends ApiToken {
  token: string;
}

// -- logs -------------------------------------------------------------------

export interface AuditEvent {
  id: string;
  created_at: string;
  actor_id: string | null;
  actor_email: string | null;
  action: string;
  object_type: string | null;
  object_id: string | null;
  detail: Record<string, unknown> | null;
  source_ip: string | null;
}

export interface DeviceLogEntry {
  time: string | null;
  topics: string[];
  message: string;
  severity: "info" | "warning" | "error";
}

export interface ConsoleResponse {
  command: string;
  // What the console actually ran after parsing. Shown back, because
  // "/ip route" silently becoming "/ip/route/print" should be visible rather
  // than surprising.
  resolved: string;
  rows: Record<string, unknown>[];
  // The device answered with a failure. Distinct from a 4xx, which means the
  // controller refused before the device was asked.
  error: string | null;
}

// -- diagnostics ------------------------------------------------------------

export interface PingProbe {
  seq: number | null;
  host: string | null;
  ttl: number | null;
  size: number | null;
  // null means no reply at all, which is not the same as a reply in 0 ms.
  time_ms: number | null;
  status: string | null;
}

export interface PingResult {
  target: string;
  interface: string | null;
  sent: number;
  received: number;
  loss_percent: number;
  min_ms: number | null;
  avg_ms: number | null;
  max_ms: number | null;
  probes: PingProbe[];
}

export interface TraceHop {
  hop: number;
  address: string | null;
  loss_percent: number | null;
  sent: number | null;
  last_ms: number | null;
  avg_ms: number | null;
  best_ms: number | null;
  worst_ms: number | null;
  status: string | null;
}

export interface TracerouteResult {
  target: string;
  hops: TraceHop[];
}

export interface TunnelHealth {
  link_id: string;
  fabric_id: string;
  fabric_name: string;
  slug: string;
  peer_site_id: string | null;
  peer_site_name: string | null;
  enabled: boolean;
  state: string;
  last_error: string | null;
  interface: string | null;
  // Every one of these is null when the answer is genuinely unknown -- a GRE
  // fabric has no IPsec SA, and a device that was never applied has no
  // interface. Drawing null as "down" is how one unreachable router looks
  // like a total outage.
  interface_running: boolean | null;
  ipsec_established: boolean | null;
  ipsec_detail: string | null;
  bgp_established: boolean | null;
  bgp_detail: string | null;
  netwatch_status: string | null;
  netwatch_loss_percent: number | null;
  netwatch_latency_ms: number | null;
  error: string | null;
}

export interface DeviceHealth {
  cpu_load_percent: number | null;
  cpu_count: number | null;
  cpu_frequency_mhz: number | null;
  free_memory_bytes: number | null;
  total_memory_bytes: number | null;
  free_disk_bytes: number | null;
  total_disk_bytes: number | null;
  uptime: string | null;
  version: string | null;
  board_name: string | null;
  architecture: string | null;
}

export const endpoints = {
  login: (email: string, password: string) =>
    api.post<{ access_token: string; expires_in: number }>("/auth/login", {
      email,
      password,
    }),
  me: () => api.get<CurrentUser>("/auth/me"),
  sites: () => api.get<Site[]>("/sites"),
  site: (id: string) => api.get<Site>(`/sites/${id}`),
  createSite: (body: unknown) => api.post<Site>("/sites", body),
  deleteSite: (id: string) => api.del(`/sites/${id}`),
  probe: (id: string) => api.post<ProbeResult>(`/sites/${id}/probe`),
  ports: (id: string) => api.get<Port[]>(`/sites/${id}/ports`),
  health: (id: string) => api.get<DeviceHealth>(`/sites/${id}/health`),
  addWan: (siteId: string, body: unknown) => api.post<Wan>(`/sites/${siteId}/wans`, body),
  plan: (siteId: string) => api.post<Plan>(`/sites/${siteId}/plan`),
  apply: (siteId: string, body: { confirm?: boolean; dry_run?: boolean }) =>
    api.post<Job>(`/sites/${siteId}/apply`, body),
  jobs: (siteId?: string) =>
    api.get<Job[]>(siteId ? `/jobs?site_id=${encodeURIComponent(siteId)}` : "/jobs"),
  job: (id: string) => api.get<Job>(`/jobs/${id}`),
  fabrics: () => api.get<Fabric[]>("/fabrics"),
  fabric: (id: string) => api.get<Fabric>(`/fabrics/${id}`),
  createFabric: (body: unknown) => api.post<Fabric>("/fabrics", body),
  deleteFabric: (id: string) => api.del(`/fabrics/${id}`),
  addMember: (id: string, siteId: string) =>
    api.post<FabricMember>(`/fabrics/${id}/members`, { site_id: siteId }),
  removeMember: (id: string, siteId: string) =>
    api.del(`/fabrics/${id}/members/${siteId}`),
  expand: (id: string) => api.post<Expansion>(`/fabrics/${id}/expand`),
  links: (id: string) => api.get<FabricLink[]>(`/fabrics/${id}/links`),
  transports: () => api.get<TransportInfo[]>("/fabrics/transports"),
  policies: () => api.get<Policy[]>("/policies"),
  createPolicy: (body: unknown) => api.post<Policy>("/policies", body),
  updatePolicy: (id: string, body: unknown) => api.patch<Policy>(`/policies/${id}`, body),
  deletePolicy: (id: string) => api.del(`/policies/${id}`),
  slaProfiles: () => api.get<SlaProfile[]>("/sla-profiles"),
  createSla: (body: unknown) => api.post<SlaProfile>("/sla-profiles", body),
  sdwanGroups: () => api.get<SdwanGroup[]>("/sdwan-groups"),
  createSdwanGroup: (body: unknown) => api.post<SdwanGroup>("/sdwan-groups", body),
  updateSdwanGroup: (id: string, body: unknown) =>
    api.patch<SdwanGroup>(`/sdwan-groups/${id}`, body),
  deleteSdwanGroup: (id: string) => api.del(`/sdwan-groups/${id}`),
  deleteSla: (id: string) => api.del(`/sla-profiles/${id}`),
  appGroups: () => api.get<AppGroup[]>("/app-groups"),
  driftCheck: (siteId: string) => api.post<Job>(`/sites/${siteId}/drift`),
  driftSweep: () => api.post<Job[]>("/drift"),
  exportUrl: () => `${BASE}/intent/export`,

  // -- editing (previously API-only) ---------------------------------------
  updateSite: (id: string, body: unknown) => api.patch<Site>(`/sites/${id}`, body),
  // Forget the recorded device identity so the next connection learns it
  // again. Its own call rather than a generic patch, because "clear both
  // pins" is the whole operation and spelling it out at each call site is how
  // one of them ends up clearing only the TLS half.
  forgetDeviceIdentity: (id: string) =>
    api.patch<Site>(`/sites/${id}`, { tls_fingerprint: null, ssh_host_key: null }),
  updateWan: (siteId: string, wanId: string, body: unknown) =>
    api.patch<Wan>(`/sites/${siteId}/wans/${wanId}`, body),
  deleteWan: (siteId: string, wanId: string) =>
    api.del(`/sites/${siteId}/wans/${wanId}`),
  updateFabric: (id: string, body: unknown) => api.patch<Fabric>(`/fabrics/${id}`, body),
  createAppGroup: (body: unknown) => api.post<AppGroup>("/app-groups", body),

  // -- users ---------------------------------------------------------------
  users: () => api.get<User[]>("/users"),
  createUser: (body: unknown) => api.post<User>("/users", body),
  updateUser: (id: string, body: unknown) => api.patch<User>(`/users/${id}`, body),

  // -- API tokens ----------------------------------------------------------
  apiTokens: () => api.get<ApiToken[]>("/api-tokens"),
  createApiToken: (body: {
    name: string;
    role: string;
    expires_in_days: number | null;
  }) => api.post<ApiTokenCreated>("/api-tokens", body),
  renameApiToken: (id: string, name: string) =>
    api.patch<ApiToken>(`/api-tokens/${id}`, { name }),
  revokeApiToken: (id: string) => api.del(`/api-tokens/${id}`),

  // -- logs ----------------------------------------------------------------
  audit: (params: {
    action?: string;
    object_id?: string;
    actor_email?: string;
    limit?: number;
  }) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    const suffix = query.toString();
    return api.get<AuditEvent[]>(`/audit${suffix ? `?${suffix}` : ""}`);
  },
  auditActions: () => api.get<string[]>("/audit/actions"),
  console: (siteId: string, command: string) =>
    api.post<ConsoleResponse>(`/sites/${siteId}/console`, { command }),
  deviceLog: (siteId: string, params: { topic?: string; contains?: string }) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    const suffix = query.toString();
    return api.get<DeviceLogEntry[]>(
      `/sites/${siteId}/log${suffix ? `?${suffix}` : ""}`,
    );
  },

  // -- diagnostics ---------------------------------------------------------
  ping: (
    siteId: string,
    body: { target: string; count?: number; interface?: string | null },
  ) => api.post<PingResult>(`/sites/${siteId}/diagnostics/ping`, body),
  traceroute: (
    siteId: string,
    body: { target: string; seconds?: number; interface?: string | null },
  ) => api.post<TracerouteResult>(`/sites/${siteId}/diagnostics/traceroute`, body),
  tunnels: (siteId: string) => api.get<TunnelHealth[]>(`/sites/${siteId}/tunnels`),

  // -- device console and rollback recovery --------------------------------
  deviceRead: (siteId: string, menu: string) =>
    api.get<Record<string, unknown>[]>(`/sites/${siteId}/device/${menu}`),
  rollbacks: (siteId: string) => api.get<ArmedRollback[]>(`/sites/${siteId}/rollbacks`),
  clearRollback: (siteId: string, name: string) =>
    api.del(`/sites/${siteId}/rollbacks/${encodeURIComponent(name)}`),

  // -- GitOps --------------------------------------------------------------
  importIntent: (yaml: string, dryRun: boolean) =>
    request<Record<string, unknown>>(`/intent/import?dry_run=${dryRun}`, {
      method: "POST",
      body: yaml,
      headers: { "Content-Type": "application/yaml" },
    }),
};
