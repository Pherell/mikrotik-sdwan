/**
 * The navigation rail.
 *
 * The old top bar listed seven destinations as equal peers in a row, which said
 * nothing about the order they are used in -- and the order is the product:
 * sites, then a fabric over them, then policies on top. Grouping is the cheapest
 * way to say that without a paragraph of text.
 *
 * Icons are inline SVG rather than a library. Nine 18px glyphs are not worth a
 * dependency, and the collapsed rail needs them to mean anything.
 */

import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import type { CurrentUser } from "../lib/api";

const COLLAPSED_KEY = "sdwan.sidebar.collapsed";

export function readCollapsed(): boolean {
  // Browsers set to block site data throw on access rather than returning null,
  // so this cannot be a bare read.
  try {
    return localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

export function writeCollapsed(value: boolean): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, value ? "1" : "0");
  } catch {
    /* a remembered rail width is not worth failing over */
  }
}

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="nav-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const icons = {
  overview: (
    <Icon>
      <rect x="3" y="3" width="7" height="9" />
      <rect x="14" y="3" width="7" height="5" />
      <rect x="14" y="12" width="7" height="9" />
      <rect x="3" y="16" width="7" height="5" />
    </Icon>
  ),
  sites: (
    <Icon>
      <rect x="2" y="14" width="20" height="7" rx="2" />
      <path d="M6 17.5h.01M10 17.5h.01" />
      <path d="M12 14V9" />
      <circle cx="12" cy="6" r="3" />
    </Icon>
  ),
  fabrics: (
    <Icon>
      <circle cx="12" cy="5" r="2.5" />
      <circle cx="5" cy="18" r="2.5" />
      <circle cx="19" cy="18" r="2.5" />
      <path d="M10.2 6.9 6.8 15.6M13.8 6.9l3.4 8.7M7.5 18h9" />
    </Icon>
  ),
  policies: (
    <Icon>
      <path d="M3 6h18M3 12h11M3 18h7" />
      <circle cx="18.5" cy="12" r="2.5" />
    </Icon>
  ),
  jobs: (
    <Icon>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </Icon>
  ),
  settings: (
    <Icon>
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" />
      <circle cx="12" cy="12" r="3.2" />
    </Icon>
  ),
  users: (
    <Icon>
      <circle cx="9" cy="8" r="3.2" />
      <path d="M3 20c0-3.3 2.7-5.5 6-5.5s6 2.2 6 5.5" />
      <path d="M16 11.2A3.2 3.2 0 0 0 16 5M18 19.5c0-2.2-.8-3.9-2-4.9" />
    </Icon>
  ),
  uplinks: (
    <Icon>
      <path d="M4 16h6a2 2 0 0 0 2-2V8" />
      <path d="M16 5l4 3-4 3" />
      <path d="M12 8h8" />
      <rect x="2" y="13" width="5" height="6" rx="1" />
    </Icon>
  ),
  logs: (
    <Icon>
      <path d="M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z" />
      <path d="M14 4v5h5M8 13h8M8 17h5" />
    </Icon>
  ),
  diagnostics: (
    <Icon>
      <path d="M3 12h3l2.5-6 3 12 2.5-6H21" />
    </Icon>
  ),
  groups: (
    <Icon>
      <path d="M3 6h4M3 12h4M3 18h4" />
      <path d="M7 6c6 0 6 6 10 6M7 12h10M7 18c6 0 6-6 10-6" />
      <circle cx="19" cy="12" r="2.2" />
    </Icon>
  ),
  collapse: (
    <Icon>
      <path d="M15 6l-6 6 6 6" />
    </Icon>
  ),
  expand: (
    <Icon>
      <path d="M9 6l6 6-6 6" />
    </Icon>
  ),
  menu: (
    <Icon>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </Icon>
  ),
  signout: (
    <Icon>
      <path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" />
      <path d="M10 8l-4 4 4 4M6 12h10" />
    </Icon>
  ),
};

type Item = { to: string; label: string; icon: ReactNode; end?: boolean };

const GROUPS: { heading: string; items: Item[] }[] = [
  {
    // Choosing between uplinks. Independent of how the tunnels are built,
    // which is why it is its own heading rather than mixed in below.
    heading: "SD-WAN",
    items: [
      { to: "/", label: "Overview", icon: icons.overview, end: true },
      { to: "/uplinks", label: "Uplinks", icon: icons.uplinks },
      { to: "/sdwan-groups", label: "SD-WAN groups", icon: icons.groups },
      { to: "/traffic-rules", label: "Traffic rules", icon: icons.policies },
    ],
  },
  {
    // The overlay those uplinks carry.
    heading: "Tunnels",
    items: [
      { to: "/tunnel-networks", label: "Tunnel networks", icon: icons.fabrics },
    ],
  },
  {
    heading: "Devices",
    items: [
      { to: "/devices", label: "Devices", icon: icons.sites },
      // Sits with devices because every test here runs *from* one.
      { to: "/diagnostics", label: "Diagnostics", icon: icons.diagnostics },
    ],
  },
  {
    heading: "System",
    items: [
      { to: "/jobs", label: "Jobs", icon: icons.jobs },
      { to: "/logs", label: "Logs", icon: icons.logs },
      { to: "/settings", label: "Settings", icon: icons.settings },
      { to: "/users", label: "Users", icon: icons.users },
    ],
  },
];

export function Sidebar({
  user,
  collapsed,
  onToggleCollapsed,
  mobileOpen,
  onCloseMobile,
  onSignOut,
}: {
  user?: CurrentUser;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onSignOut: () => void;
}) {
  return (
    <>
      {mobileOpen && (
        <div className="sidebar-scrim" onClick={onCloseMobile} aria-hidden="true" />
      )}
      <nav
        className="sidebar"
        data-collapsed={collapsed}
        data-open={mobileOpen}
        aria-label="Main"
      >
        <div className="sidebar-brand">
          <NavLink to="/" className="brand-link" onClick={onCloseMobile}>
            <span className="brand-mark" aria-hidden="true">
              SD
            </span>
            <span className="nav-label">SD-WAN Controller</span>
          </NavLink>
        </div>

        <div className="sidebar-scroll">
          {GROUPS.map((group) => {
            const items = group.items.filter(
              (i) => i.to !== "/users" || user?.role === "admin",
            );
            if (items.length === 0) return null;
            return (
              <div className="nav-group" key={group.heading}>
                <div className="nav-heading">{group.heading}</div>
                {items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
                    onClick={onCloseMobile}
                    title={collapsed ? item.label : undefined}
                  >
                    {item.icon}
                    <span className="nav-label">{item.label}</span>
                  </NavLink>
                ))}
              </div>
            );
          })}
        </div>

        <div className="sidebar-foot">
          {user && (
            <div className="sidebar-user">
              <div className="nav-label sidebar-user-email" title={user.email}>
                {user.email}
              </div>
              <div className="nav-label muted sidebar-user-role">{user.role}</div>
            </div>
          )}
          <button
            className="ghost sidebar-signout"
            onClick={onSignOut}
            title={collapsed ? "Sign out" : undefined}
          >
            {icons.signout}
            <span className="nav-label">Sign out</span>
          </button>
          <button
            className="ghost sidebar-collapse"
            onClick={onToggleCollapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand" : "Collapse"}
          >
            {collapsed ? icons.expand : icons.collapse}
            <span className="nav-label">Collapse</span>
          </button>
        </div>
      </nav>
    </>
  );
}

export function MobileBar({ onOpen }: { onOpen: () => void }) {
  return (
    <header className="mobile-bar">
      <button className="ghost" onClick={onOpen} aria-label="Open navigation">
        {icons.menu}
      </button>
      <span className="mobile-bar-title">SD-WAN Controller</span>
    </header>
  );
}
