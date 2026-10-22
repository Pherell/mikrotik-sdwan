/**
 * The top navigation bar, Sophos Central style.
 *
 * A full-width Sophos-blue bar: stacked wordmark at left, section menus
 * across the top (dropdowns where a section holds several pages, mirroring
 * the old sidebar's groups), and the signed-in user plus sign-out at right.
 * On narrow screens the menus collapse behind a hamburger that opens a
 * stacked panel.
 */

import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

import type { CurrentUser } from "../lib/api";

type Child = { to: string; label: string; adminOnly?: boolean };
type Entry = { label: string; to?: string; end?: boolean; children?: Child[] };

const NAV: Entry[] = [
  { label: "Overview", to: "/", end: true },
  {
    label: "SD-WAN",
    children: [{ to: "/uplinks", label: "Uplinks" }],
  },
  {
    label: "Traffic",
    children: [
      { to: "/traffic/rules", label: "Rules" },
      { to: "/traffic/groups", label: "SD-WAN groups" },
      { to: "/traffic/sla-profiles", label: "SLA profiles" },
      { to: "/traffic/compliance", label: "SLA compliance" },
    ],
  },
  {
    label: "VPN",
    children: [
      { to: "/vpn", label: "All tunnels" },
      { to: "/vpn/wireguard", label: "WireGuard" },
      { to: "/vpn/ipsec", label: "IPsec" },
      { to: "/vpn/gre", label: "GRE / IPIP" },
      { to: "/vpn/l2", label: "L2 — VXLAN / EoIP" },
    ],
  },
  {
    label: "Devices",
    children: [
      { to: "/devices", label: "Devices" },
      { to: "/diagnostics", label: "Diagnostics" },
    ],
  },
  {
    label: "System",
    children: [
      { to: "/jobs", label: "Jobs" },
      { to: "/logs", label: "Logs" },
      // Admin-only server-side; hidden here for the same reason the old
      // sidebar hid it -- a menu item that can only ever 403 is worse
      // than no menu item.
      { to: "/api-access", label: "API access", adminOnly: true },
      { to: "/settings", label: "Settings" },
      { to: "/users", label: "Users", adminOnly: true },
    ],
  },
  { label: "Guide", to: "/guide" },
];

function visibleChildren(entry: Entry, user?: CurrentUser): Child[] {
  return (entry.children ?? []).filter(
    (c) => !c.adminOnly || user?.role === "admin",
  );
}

function MenuIcon() {
  return (
    <svg
      width="22"
      height="22"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

export function TopBar({
  user,
  mobileOpen,
  onToggleMobile,
  onCloseMobile,
  onSignOut,
}: {
  user?: CurrentUser;
  mobileOpen: boolean;
  onToggleMobile: () => void;
  onCloseMobile: () => void;
  onSignOut: () => void;
}) {
  const location = useLocation();
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const barRef = useRef<HTMLElement>(null);

  // A menu left open across navigation would hover over the page you just
  // asked for.
  useEffect(() => setOpenMenu(null), [location.pathname]);

  // Clicking anywhere outside the bar closes an open dropdown.
  useEffect(() => {
    if (!openMenu) return;
    const onDown = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) {
        setOpenMenu(null);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [openMenu]);

  const isChildActive = (children: Child[]) =>
    children.some(
      (c) =>
        location.pathname === c.to ||
        location.pathname.startsWith(`${c.to}/`),
    );

  return (
    <>
      <header className="topbar" ref={barRef}>
        <button
          className="topbar-toggle"
          onClick={onToggleMobile}
          aria-label={mobileOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={mobileOpen}
        >
          <MenuIcon />
        </button>

        <Link to="/" className="topbar-brand" onClick={onCloseMobile}>
          <span className="topbar-wordmark" aria-label="SD-WAN Controller">
            <span className="topbar-wordmark-main">SD-WAN</span>
            <span className="topbar-wordmark-sub">CONTROLLER</span>
          </span>
        </Link>

        <nav className="topbar-nav" aria-label="Main">
          {NAV.map((entry) => {
            const children = visibleChildren(entry, user);
            if (entry.to) {
              return (
                <NavLink
                  key={entry.label}
                  to={entry.to}
                  end={entry.end}
                  className={({ isActive }) =>
                    `topbar-item${isActive ? " active" : ""}`
                  }
                >
                  {entry.label}
                </NavLink>
              );
            }
            if (children.length === 0) return null;
            const open = openMenu === entry.label;
            const active = isChildActive(children);
            return (
              <div className="topbar-drop" key={entry.label}>
                <button
                  className={`topbar-item${open || active ? " active" : ""}`}
                  aria-expanded={open}
                  aria-haspopup="true"
                  onClick={() => setOpenMenu(open ? null : entry.label)}
                >
                  {entry.label}
                  <span className="topbar-caret" aria-hidden="true">
                    ▼
                  </span>
                </button>
                {open && (
                  <div className="topbar-menu" role="menu">
                    {children.map((c) => (
                      <NavLink
                        key={c.to}
                        to={c.to}
                        role="menuitem"
                        className={({ isActive }) =>
                          `topbar-menu-item${isActive ? " active" : ""}`
                        }
                        onClick={() => setOpenMenu(null)}
                      >
                        {c.label}
                      </NavLink>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </nav>

        <div className="topbar-right">
          {user && (
            <div className="topbar-user">
              <div className="topbar-user-email" title={user.email}>
                {user.email}
              </div>
              <div className="topbar-user-role">{user.role}</div>
            </div>
          )}
          <button className="topbar-signout" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>

      {mobileOpen && (
        <nav className="topbar-mobile" aria-label="Main">
          {NAV.map((entry) => {
            const children = visibleChildren(entry, user);
            if (entry.to) {
              return (
                <div className="topbar-mobile-group" key={entry.label}>
                  <NavLink
                    to={entry.to}
                    end={entry.end}
                    className={({ isActive }) =>
                      `topbar-mobile-item${isActive ? " active" : ""}`
                    }
                    onClick={onCloseMobile}
                  >
                    {entry.label}
                  </NavLink>
                </div>
              );
            }
            if (children.length === 0) return null;
            return (
              <div className="topbar-mobile-group" key={entry.label}>
                <div className="topbar-mobile-heading">{entry.label}</div>
                {children.map((c) => (
                  <NavLink
                    key={c.to}
                    to={c.to}
                    className={({ isActive }) =>
                      `topbar-mobile-item${isActive ? " active" : ""}`
                    }
                    onClick={onCloseMobile}
                  >
                    {c.label}
                  </NavLink>
                ))}
              </div>
            );
          })}
        </nav>
      )}
    </>
  );
}
