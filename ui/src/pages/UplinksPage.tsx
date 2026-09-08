/**
 * Every uplink, across every device.
 *
 * An uplink was only ever visible inside the device that owns it, which makes
 * the question SD-WAN is actually about -- "which internet connections do I
 * have, and what are they like?" -- something you answer by opening devices one
 * at a time.
 *
 * It is also the list that SD-WAN groups select from, so it needs to exist as
 * its own thing before groups can refer to it.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { endpoints, type Site, type Wan } from "../lib/api";

type Row = { wan: Wan; site: Site };

function reachability(wan: Wan): { label: string; tone: string } {
  // The distinction that decides whether a tunnel can be built to this uplink
  // or only from it, which is the single most confusing thing about uplinks.
  if (wan.dial_out_only) {
    return { label: "dials out only", tone: "drifted" };
  }
  return { label: "accepts tunnels", tone: "reachable" };
}

export function UplinksPage() {
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });

  const rows: Row[] = (sites.data ?? []).flatMap((site) =>
    site.wans.map((wan) => ({ wan, site })),
  );

  return (
    <>
      <PageHeader
        title="Uplinks"
        description="One uplink is one internet connection on one device. Tunnels are built per uplink, not per device, so a device with two uplinks joins a tunnel network twice — which is what lets one fail without taking the site down."
      />

      <div className="card">
        {sites.isLoading && <Skeleton rows={4} />}
        {sites.isError && (
          <div className="error">{(sites.error as Error).message}</div>
        )}

        {sites.data && rows.length === 0 && (
          <p className="muted">
            No uplinks yet. Add a device, and the controller works out which of its
            interfaces reach the internet — you confirm them and they appear here.
          </p>
        )}

        {rows.length > 0 && (
          <table className="stack">
            <thead>
              <tr>
                <th>Uplink</th>
                <th>Device</th>
                <th>Interface</th>
                <th>Address</th>
                <th>Provider</th>
                <th>Reachability</th>
                <th>NAT</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ wan, site }) => {
                const reach = reachability(wan);
                return (
                  <tr key={wan.id}>
                    <td data-label="Uplink">
                      <strong>{wan.name}</strong>
                      {Object.keys(wan.tags ?? {}).length > 0 && (
                        <div className="muted">
                          {Object.keys(wan.tags).join(" · ")}
                        </div>
                      )}
                    </td>
                    <td data-label="Device">
                      <Link to={`/devices/${site.id}`}>{site.name}</Link>
                    </td>
                    <td data-label="Interface" className="muted">
                      {wan.interface}
                    </td>
                    <td data-label="Address" className="muted">
                      {wan.public_ip ?? (wan.dynamic ? "dynamic" : "—")}
                    </td>
                    <td data-label="Provider" className="muted">
                      {wan.provider ?? "—"}
                    </td>
                    <td data-label="Reachability">
                      <span className={`badge ${reach.tone}`}>{reach.label}</span>
                    </td>
                    <td data-label="NAT" className="muted">
                      {wan.masquerade ? "on" : "off"}
                    </td>
                    <td data-label="State">
                      {wan.enabled ? (
                        <span className="muted">in use</span>
                      ) : (
                        <span className="badge drifted">disabled</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h2>What the columns mean</h2>
        <dl className="kv">
          <dt>Reachability</dt>
          <dd>
            Whether other devices can build a tunnel <em>to</em> this uplink.
            An uplink with no public address, or behind carrier NAT, can only dial
            out — so two of them can never connect directly and must meet at a hub.
          </dd>
          <dt>NAT</dt>
          <dd>
            Whether traffic leaving here has its source address rewritten. On for
            an internet connection. Off for private transit, where rewriting it
            would break the far end.
          </dd>
          <dt>Tags</dt>
          <dd>
            Labels you steer by. A traffic rule names tags rather than interfaces,
            so one rule works across devices whose uplinks are wired differently.
          </dd>
        </dl>
      </div>
    </>
  );
}
