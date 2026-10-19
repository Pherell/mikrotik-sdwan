import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";

import { ArmedIndicator } from "./components/ArmedIndicator";
import { Shortcuts } from "./components/Shortcuts";
import { TopBar } from "./components/TopBar";
import { endpoints, getToken, setToken } from "./lib/api";
import "./styles.css";

export function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const hasToken = Boolean(getToken());

  const [mobileOpen, setMobileOpen] = useState(false);

  // A menu left open across navigation would hover over the page you just
  // asked for.
  useEffect(() => setMobileOpen(false), [location.pathname]);

  const { data: user, isError } = useQuery({
    queryKey: ["me"],
    queryFn: endpoints.me,
    enabled: hasToken,
  });

  if (!hasToken || isError) return <Navigate to="/login" replace />;

  return (
    <div className="layout">
      {/* First tab stop on the page: the nav is the first thing a keyboard
          user meets, so offer to jump straight past it. */}
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <Shortcuts onEscape={() => setMobileOpen(false)} />
      <TopBar
        user={user}
        mobileOpen={mobileOpen}
        onToggleMobile={() => setMobileOpen((v) => !v)}
        onCloseMobile={() => setMobileOpen(false)}
        onSignOut={() => {
          setToken(null);
          navigate("/login", { replace: true });
        }}
      />
      <div className="content">
        <ArmedIndicator />
        <main id="main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
