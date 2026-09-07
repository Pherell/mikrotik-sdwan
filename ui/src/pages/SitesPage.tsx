import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { AddSiteWizard } from "../components/AddSiteWizard";
import { BulkSiteBar } from "../components/BulkSiteBar";
import { endpoints, type Site } from "../lib/api";
import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";

export function SitesPage() {
  const [adding, setAdding] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const sites = useQuery({ queryKey: ["sites"], queryFn: endpoints.sites });

  const all = sites.data ?? [];
  // A selection that survives a refetch would let you act on a site that is
  // no longer there.
  const live = selected.filter((id) => all.some((s) => s.id === id));
  const allSelected = all.length > 0 && live.length === all.length;

  function toggle(id: string) {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }

  return (
    <>
      <PageHeader
        title="Sites"
        description="A site is a location and the RouterOS device that serves it, with its uplinks. Everything else is built on top of these."
      >
        <button className="primary" onClick={() => setAdding(true)}>
          Add site
        </button>
      </PageHeader>

      {adding && <AddSiteWizard onClose={() => setAdding(false)} />}

      {live.length > 0 && (
        <BulkSiteBar selected={live} sites={all} onClear={() => setSelected([])} />
      )}

      <div className="card">
        {sites.isLoading && <Skeleton rows={4} />}
        {sites.isError && <div className="error">{(sites.error as Error).message}</div>}
        {sites.data?.length === 0 && (
          <p className="muted">
            No sites yet. A site is one location and the RouterOS device that serves
            it. Add one and the controller connects, reads its version and interfaces,
            and works out which of them are uplinks — everything else is built on top
            of these.
          </p>
        )}
        {sites.data && sites.data.length > 0 && (
          <table className="stack">
            <thead>
              <tr>
                <th className="tick">
                  <input
                    type="checkbox"
                    aria-label="Select every site"
                    checked={allSelected}
                    onChange={() =>
                      setSelected(allSelected ? [] : all.map((s) => s.id))
                    }
                  />
                </th>
                <th>Name</th>
                <th>Role</th>
                <th>Management</th>
                <th>RouterOS</th>
                <th>Uplinks</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {all.map((site) => (
                <SiteRow
                  key={site.id}
                  site={site}
                  selected={live.includes(site.id)}
                  onToggle={() => toggle(site.id)}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function SiteRow({
  site,
  selected,
  onToggle,
}: {
  site: Site;
  selected: boolean;
  onToggle: () => void;
}) {
  return (
    <tr className={selected ? "selected" : undefined}>
      <td className="tick">
        <input
          type="checkbox"
          aria-label={`Select ${site.name}`}
          checked={selected}
          onChange={onToggle}
        />
      </td>
      <td data-label="Name">
        <Link to={`/sites/${site.id}`}>{site.name}</Link>
        {site.region && <div className="muted">{site.region}</div>}
      </td>
      <td data-label="Role">{site.role}</td>
      <td className="muted" data-label="Management">{site.mgmt_host}</td>
      <td data-label="RouterOS">
        {site.ros_version ?? <span className="muted">unknown</span>}
        {site.board_name && <div className="muted">{site.board_name}</div>}
      </td>
      <td data-label="Uplinks">
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
      <td data-label="Status">
        <span className={`badge ${site.status}`}>{site.status}</span>
      </td>
    </tr>
  );
}
