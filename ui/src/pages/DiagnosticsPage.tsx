/**
 * The page that answers "why is this not working" without an SSH session.
 *
 * Two panels, because there are two different questions. Tunnel health is the
 * one nobody can answer by hand quickly: the device will tell you that an
 * IPsec peer is established and that a BGP session exists, but not which
 * tunnel either of those belongs to. The controller named both ends, so it
 * can say.
 *
 * Ping and traceroute are the ordinary tools, with one addition that matters:
 * you can send them out of a chosen uplink. That is the only way to tell "the
 * internet is down" from "this one uplink is down", which is the distinction
 * the whole product turns on.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import {
  endpoints,
  type PingResult,
  type TracerouteResult,
  type TunnelHealth,
} from "../lib/api";

function ms(value: number | null): string {
  if (value === null) return "—";
  return value < 10 ? `${value.toFixed(2)} ms` : `${value.toFixed(1)} ms`;
}

/**
 * Three states, not two. `null` means nobody could answer the question --
 * a GRE tunnel has no IPsec SA to be up or down, and an unreachable device
 * has no opinion about any of its tunnels. Drawing that as a red dot turns
 * one unreachable router into what looks like a total outage.
 */
function Tri({
  value,
  up,
  down,
  unknown,
}: {
  value: boolean | null;
  up: string;
  down: string;
  unknown: string;
}) {
  if (value === null) return <span className="badge">{unknown}</span>;
  return (
    <span className={`badge ${value ? "reachable" : "unreachable"}`}>
      {value ? up : down}
    </span>
  );
}

export function DiagnosticsPage() {
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });
  const [siteId, setSiteId] = useState("");

  // Default to the first device rather than making an empty select the first
  // thing on the page.
  const chosen = siteId || sites.data?.[0]?.id || "";

  return (
    <>
      <PageHeader
        title="Diagnostics"
        description="Run a test from a device, and see what each tunnel it has an end of is actually doing. Everything here reads or probes — nothing changes configuration."
      />

      <div className="card">
        <label>
          Run from
          <select
            value={chosen}
            onChange={(e) => setSiteId(e.target.value)}
            disabled={sites.isLoading || !sites.data?.length}
          >
            {sites.data?.length === 0 && <option value="">No devices yet</option>}
            {sites.data?.map((site) => (
              <option key={site.id} value={site.id}>
                {site.name} — {site.mgmt_host}
              </option>
            ))}
          </select>
          <span className="muted field-note">
            Tests run on the device, not from the controller, so they measure
            the path the device's traffic actually takes.
          </span>
        </label>
      </div>

      {chosen && (
        <>
          <TunnelPanel siteId={chosen} />
          <ReachabilityPanel siteId={chosen} />
        </>
      )}
    </>
  );
}

// -- tunnels ----------------------------------------------------------------

function TunnelPanel({ siteId }: { siteId: string }) {
  const tunnels = useQuery({
    queryKey: ["tunnels", siteId],
    queryFn: () => endpoints.tunnels(siteId),
    retry: false,
  });

  const unreachable = tunnels.data?.find((t) => t.error)?.error;

  return (
    <div className="card">
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 12,
        }}
      >
        <h2>Tunnels on this device</h2>
        <button
          className="sm"
          onClick={() => tunnels.refetch()}
          data-busy={tunnels.isFetching}
          disabled={tunnels.isFetching}
        >
          {tunnels.isFetching ? "Checking…" : "Re-check"}
        </button>
      </div>

      {tunnels.isLoading && <Skeleton rows={3} />}
      {tunnels.isError && (
        <div className="error">{(tunnels.error as Error).message}</div>
      )}

      {unreachable && (
        <div className="warn">
          Could not read this device: {unreachable}. The intent below is what the
          controller has recorded; nothing on this device has been checked.
        </div>
      )}

      {tunnels.data?.length === 0 && (
        <p className="muted">
          This device has no tunnels. Add it to a tunnel network, then expand and
          apply.
        </p>
      )}

      {tunnels.data && tunnels.data.length > 0 && (
        <table className="stack">
          <thead>
            <tr>
              <th>To</th>
              <th>Tunnel network</th>
              <th>Interface</th>
              <th>Encryption</th>
              <th>Routing</th>
              <th>Measured</th>
            </tr>
          </thead>
          <tbody>
            {tunnels.data.map((t) => (
              <TunnelRow key={t.link_id} tunnel={t} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function TunnelRow({ tunnel }: { tunnel: TunnelHealth }) {
  return (
    <>
    <tr>
      <td data-label="To">
        <strong>{tunnel.peer_site_name ?? "unknown device"}</strong>
        {!tunnel.enabled && <div className="muted">disabled</div>}
        {tunnel.last_error && <div className="error">{tunnel.last_error}</div>}
      </td>
      <td data-label="Tunnel network" className="muted">
        {tunnel.fabric_name}
      </td>
      <td data-label="Interface">
        <code>{tunnel.interface ?? "—"}</code>
        <div>
          <Tri
            value={tunnel.interface_running}
            up="up"
            down="down"
            // The distinction that stops a never-applied tunnel reading as a
            // broken one.
            unknown={tunnel.error ? "not checked" : "not on the device yet"}
          />
        </div>
      </td>
      <td data-label="Encryption">
        <Tri
          value={tunnel.ipsec_established}
          up="encrypted"
          down="no security association"
          // Unknown means two different things, and saying the wrong one is
          // worse than saying nothing: the device could not be asked, or this
          // tunnel network does not use IPsec in the first place.
          unknown={tunnel.error ? "not checked" : "not encrypted by design"}
        />
        {tunnel.ipsec_detail && (
          <div className="muted">{tunnel.ipsec_detail}</div>
        )}
      </td>
      <td data-label="Routing">
        <Tri
          value={tunnel.bgp_established}
          up="routes exchanged"
          down="no routes"
          unknown={tunnel.error ? "not checked" : "no tunnel address yet"}
        />
        {tunnel.bgp_detail && <div className="muted">{tunnel.bgp_detail}</div>}
      </td>
      <td data-label="Measured" className="muted">
        {tunnel.netwatch_status ? (
          <>
            {tunnel.netwatch_status}
            {/* A probe that timed out reports an rtt of zero, and printing it
                as "0.00 ms" reads as the fastest path on the page. Only a
                probe that answered has a latency worth showing. */}
            {tunnel.netwatch_status === "up" &&
              tunnel.netwatch_latency_ms !== null && (
                <> · {ms(tunnel.netwatch_latency_ms)}</>
              )}
            {tunnel.netwatch_loss_percent !== null && (
              <> · {tunnel.netwatch_loss_percent}% loss</>
            )}
          </>
        ) : (
          "no health standard attached"
        )}
      </td>
    </tr>
    {tunnel.diagnosis && (
      <tr>
        <td colSpan={6} style={{ paddingTop: 0 }}>
          <div className="warn" style={{ margin: 0 }}>
            <strong>Why it is not up:</strong> {tunnel.diagnosis}
            {tunnel.transit_rules.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary style={{ cursor: "pointer" }}>
                  Rules for the device in the path
                </summary>
                <p className="muted" style={{ margin: "6px 0" }}>
                  That router is not one this controller manages, so run these on
                  it yourself. Both directions, because a firewall in the middle
                  sees both.
                </p>
                <pre className="diff">{tunnel.transit_rules.join("\n")}</pre>
              </details>
            )}
          </div>
        </td>
      </tr>
    )}
    </>
  );
}

// -- ping and traceroute ----------------------------------------------------

function ReachabilityPanel({ siteId }: { siteId: string }) {
  const toast = useToast();
  const [target, setTarget] = useState("8.8.8.8");
  const [uplink, setUplink] = useState("");
  const [ping, setPing] = useState<PingResult | null>(null);
  const [trace, setTrace] = useState<TracerouteResult | null>(null);

  const site = useQuery({
    queryKey: ["site", siteId],
    queryFn: () => endpoints.site(siteId),
  });

  const runPing = useMutation({
    mutationFn: () =>
      endpoints.ping(siteId, { target, count: 5, interface: uplink || null }),
    onSuccess: (result) => {
      setTrace(null);
      setPing(result);
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  const runTrace = useMutation({
    mutationFn: () =>
      endpoints.traceroute(siteId, {
        target,
        seconds: 8,
        interface: uplink || null,
      }),
    onSuccess: (result) => {
      setPing(null);
      setTrace(result);
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  const busy = runPing.isPending || runTrace.isPending;

  function submit(event: FormEvent) {
    event.preventDefault();
    runPing.mutate();
  }

  return (
    <div className="card">
      <h2>Reach a destination</h2>
      <form onSubmit={submit}>
        <div className="row">
          <label>
            Destination
            <input
              required
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder="8.8.8.8"
            />
            <span className="muted field-note">
              A hostname or an IP address.
            </span>
          </label>
          <label>
            Out of
            <select value={uplink} onChange={(e) => setUplink(e.target.value)}>
              <option value="">whichever route the device picks</option>
              {site.data?.wans.map((wan) => (
                <option key={wan.id} value={wan.interface}>
                  {wan.name} ({wan.interface})
                </option>
              ))}
            </select>
            <span className="muted field-note">
              Choosing an uplink is how you tell "the internet is down" from
              "this one uplink is down".
            </span>
          </label>
        </div>

        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div className="no-grow">
            <button className="primary" type="submit" data-busy={busy} disabled={busy}>
              {runPing.isPending ? "Pinging…" : "Ping"}
            </button>
          </div>
          <div className="no-grow">
            <button type="button" onClick={() => runTrace.mutate()} disabled={busy}>
              {runTrace.isPending ? "Tracing…" : "Trace the path"}
            </button>
          </div>
        </div>
      </form>

      {ping && <PingReport result={ping} />}
      {trace && <TraceReport result={trace} />}
    </div>
  );
}

function PingReport({ result }: { result: PingResult }) {
  const lost = result.sent - result.received;
  return (
    <>
      <p>
        <strong>
          {result.received} of {result.sent} replies
        </strong>{" "}
        from {result.target}
        {result.interface && <> out of {result.interface}</>}
        {lost > 0 && <> — {result.loss_percent}% loss</>}
        {result.avg_ms !== null && (
          <>
            {" "}
            · {ms(result.min_ms)} / {ms(result.avg_ms)} / {ms(result.max_ms)}{" "}
            <span className="muted">min / average / max</span>
          </>
        )}
      </p>
      <table className="stack">
        <thead>
          <tr>
            <th>#</th>
            <th>From</th>
            <th>Time</th>
            <th>TTL</th>
          </tr>
        </thead>
        <tbody>
          {result.probes.map((probe, i) => (
            <tr key={i}>
              <td data-label="#">{probe.seq ?? i + 1}</td>
              <td data-label="From">{probe.host ?? "—"}</td>
              <td data-label="Time">
                {/* A probe that never answered has no time, and showing 0 ms
                    there would read as the fastest reply on the page. */}
                {probe.status ? (
                  <span className="badge unreachable">{probe.status}</span>
                ) : (
                  ms(probe.time_ms)
                )}
              </td>
              <td data-label="TTL" className="muted">
                {probe.ttl ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function TraceReport({ result }: { result: TracerouteResult }) {
  return (
    <>
      <p>
        Path to <strong>{result.target}</strong>, {result.hops.length} hops.
      </p>
      <table className="stack">
        <thead>
          <tr>
            <th>Hop</th>
            <th>Address</th>
            <th>Average</th>
            <th>Best / worst</th>
            <th>Loss</th>
          </tr>
        </thead>
        <tbody>
          {result.hops.map((hop) => (
            <tr key={hop.hop}>
              <td data-label="Hop">{hop.hop}</td>
              <td data-label="Address">
                {/* A hop that never answers keeps its place. Dropping it would
                    renumber every hop after it. */}
                {hop.address ?? <span className="muted">no reply</span>}
              </td>
              <td data-label="Average">{ms(hop.avg_ms)}</td>
              <td data-label="Best / worst" className="muted">
                {ms(hop.best_ms)} / {ms(hop.worst_ms)}
              </td>
              <td data-label="Loss">
                {hop.loss_percent === null ? (
                  "—"
                ) : hop.loss_percent > 0 ? (
                  <span className="badge unreachable">{hop.loss_percent}%</span>
                ) : (
                  "0%"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
