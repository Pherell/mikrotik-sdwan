import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";

import { App } from "./App";
import { ToastProvider } from "./components/Toaster";
import { ApiError } from "./lib/api";
import { DashboardPage } from "./pages/DashboardPage";
import { ApiPage } from "./pages/ApiPage";
import { DiagnosticsPage } from "./pages/DiagnosticsPage";
import { GuidePage } from "./pages/GuidePage";
import { LogsPage } from "./pages/LogsPage";
import { FabricDetailPage } from "./pages/FabricDetailPage";
import { AllTunnelsPage, PROTOCOLS, ProtocolNetworksPage } from "./pages/VpnPages";
import { JobsPage } from "./pages/JobsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { SettingsPage } from "./pages/SettingsPage";
import { UsersPage } from "./pages/UsersPage";
import { LoginPage } from "./pages/LoginPage";
import { SiteDetailPage } from "./pages/SiteDetailPage";
import { SdwanGroupsPage } from "./pages/SdwanGroupsPage";
import { SitesPage } from "./pages/SitesPage";
import { UplinksPage } from "./pages/UplinksPage";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      // Retrying a 401 or a 403 only delays showing the user what went wrong.
      retry: (count, error) =>
        !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

/** Old /sites/:id links keep working after the rename. */
function LegacySiteRedirect() {
  const { siteId } = useParams();
  return <Navigate to={`/devices/${siteId}`} replace />;
}

function LegacyFabricRedirect() {
  const { fabricId } = useParams();
  return <Navigate to={`/vpn/${fabricId}`} replace />;
}

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <ToastProvider>
        <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<App />}>
            <Route path="/" element={<DashboardPage />} />

            {/* SD-WAN: choosing between uplinks. */}
            <Route path="/uplinks" element={<UplinksPage />} />
            <Route path="/sdwan-groups" element={<SdwanGroupsPage />} />
            <Route path="/traffic-rules" element={<PoliciesPage />} />

            {/* VPN: the overlay those uplinks carry, split per protocol.
                One fabric engine underneath; the menu is the filter. */}
            <Route path="/vpn" element={<AllTunnelsPage />} />
            <Route
              path="/vpn/wireguard"
              element={<ProtocolNetworksPage protocol={PROTOCOLS.wireguard} />}
            />
            <Route
              path="/vpn/ipsec"
              element={<ProtocolNetworksPage protocol={PROTOCOLS.ipsec} />}
            />
            <Route
              path="/vpn/gre"
              element={<ProtocolNetworksPage protocol={PROTOCOLS.gre} />}
            />
            <Route
              path="/vpn/l2"
              element={<ProtocolNetworksPage protocol={PROTOCOLS.l2} />}
            />
            <Route path="/vpn/:fabricId" element={<FabricDetailPage />} />

            {/* Devices. */}
            <Route path="/devices" element={<SitesPage />} />
            <Route path="/devices/:siteId" element={<SiteDetailPage />} />
            <Route path="/diagnostics" element={<DiagnosticsPage />} />

            {/* The old paths, so a bookmark or a pasted link still lands.
                Renaming the words should not cost anyone a 404. */}
            <Route path="/sites" element={<Navigate to="/devices" replace />} />
            <Route path="/sites/:siteId" element={<LegacySiteRedirect />} />
            <Route path="/tunnel-networks" element={<Navigate to="/vpn" replace />} />
            <Route path="/tunnel-networks/:fabricId" element={<LegacyFabricRedirect />} />
            <Route path="/fabrics" element={<Navigate to="/vpn" replace />} />
            <Route path="/fabrics/:fabricId" element={<LegacyFabricRedirect />} />
            <Route path="/policies" element={<Navigate to="/traffic-rules" replace />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/api-access" element={<ApiPage />} />
            <Route path="/guide" element={<GuidePage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
