import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, Outlet, useNavigate } from "react-router-dom";

import { endpoints, getToken, setToken } from "./lib/api";
import "./styles.css";

export function App() {
  const navigate = useNavigate();
  const hasToken = Boolean(getToken());

  const { data: user, isError } = useQuery({
    queryKey: ["me"],
    queryFn: endpoints.me,
    enabled: hasToken,
  });

  if (!hasToken || isError) return <Navigate to="/login" replace />;

  return (
    <div className="layout">
      <header className="topbar">
        <h1>
          <Link to="/" style={{ color: "inherit", textDecoration: "none" }}>
            SD-WAN Controller
          </Link>
        </h1>
        <Link to="/" className="navlink">Overview</Link>
        <Link to="/sites" className="navlink">Sites</Link>
        <Link to="/fabrics" className="navlink">Fabrics</Link>
        <Link to="/policies" className="navlink">Policies</Link>
        <Link to="/jobs" className="navlink">Jobs</Link>
        <Link to="/settings" className="navlink">Settings</Link>
        {user?.role === "admin" && (
          <Link to="/users" className="navlink">Users</Link>
        )}
        <span className="spacer" />
        {user && (
          <span className="muted">
            {user.email} · {user.role}
          </span>
        )}
        <button
          onClick={() => {
            setToken(null);
            navigate("/login", { replace: true });
          }}
        >
          Sign out
        </button>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
