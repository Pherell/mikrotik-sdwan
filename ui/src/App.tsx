import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";

import { ArmedIndicator } from "./components/ArmedIndicator";
import { MobileBar, Sidebar, readCollapsed, writeCollapsed } from "./components/Sidebar";
import { endpoints, getToken, setToken } from "./lib/api";
import "./styles.css";

export function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const hasToken = Boolean(getToken());

  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);

  // A drawer that survived navigation would cover the page you just asked for.
  useEffect(() => setMobileOpen(false), [location.pathname]);

  const { data: user, isError } = useQuery({
    queryKey: ["me"],
    queryFn: endpoints.me,
    enabled: hasToken,
  });

  if (!hasToken || isError) return <Navigate to="/login" replace />;

  const toggleCollapsed = () =>
    setCollapsed((previous) => {
      writeCollapsed(!previous);
      return !previous;
    });

  return (
    <div className="layout" data-collapsed={collapsed}>
      <Sidebar
        user={user}
        collapsed={collapsed}
        onToggleCollapsed={toggleCollapsed}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        onSignOut={() => {
          setToken(null);
          navigate("/login", { replace: true });
        }}
      />
      <div className="content">
        <MobileBar onOpen={() => setMobileOpen(true)} />
        <ArmedIndicator />
        <main>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
