import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { AddSiteWizard } from "../components/AddSiteWizard";
import { endpoints, type Site } from "../lib/api";

export function SitesPage() {
  const [adding, setAdding] = useState(false);
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });

  return (
    <>
      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>Sites</h2>
          <div style={{ flex: 0 }}>
            <button className="primary" onClick={() => setAdding(true)}>
              Add site
            </button>
          </div>
        </div>
      </div>

      {adding && <AddSiteWizard onClose={() => setAdding(false)} />}

      <div className="card">
        {sites.isLoading && <p className="muted">Loading…</p>}
        {sites.isError && <div className="error">{(sites.error as Error).message}</div>}
        {sites.data?.length === 0 && (
          <p className="muted">
            No sites yet. Add one to probe a RouterOS device and discover its uplinks.
          </p>
        )}
        {sites.data && sites.data.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Role</th>
                <th>Management</th>
                <th>RouterOS</th>
                <th>Uplinks</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {sites.data.map((site) => (
                <SiteRow key={site.id} site={site} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function SiteRow({ site }: { site: Site }) {
  return (
    <tr>
      <td>
        <Link to={`/sites/${site.id}`}>{site.name}</Link>
        {site.region && <div className="muted">{site.region}</div>}
      </td>
      <td>{site.role}</td>
      <td className="muted">{site.mgmt_host}</td>
      <td>
        {site.ros_version ?? <span className="muted">unknown</span>}
        {site.board_name && <div className="muted">{site.board_name}</div>}
      </td>
      <td>
        {site.wans.length === 0 ? (
          <span className="muted">none</span>
        ) : (
          site.wans.map((w) => (
            <div key={w.id}>
              {w.name}
              {w.dial_out_only && <span className="muted"> · dial-out only</span>}
            </div>
          ))
        )}
      </td>
      <td>
        <span className={`badge ${site.status}`}>{site.status}</span>
      </td>
    </tr>
  );
}
