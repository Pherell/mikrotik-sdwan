import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { App } from "./App";
import { ApiError } from "./lib/api";
import { FabricDetailPage } from "./pages/FabricDetailPage";
import { FabricsPage } from "./pages/FabricsPage";
import { JobsPage } from "./pages/JobsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { LoginPage } from "./pages/LoginPage";
import { SiteDetailPage } from "./pages/SiteDetailPage";
import { SitesPage } from "./pages/SitesPage";

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

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<App />}>
            <Route path="/sites" element={<SitesPage />} />
            <Route path="/sites/:siteId" element={<SiteDetailPage />} />
            <Route path="/fabrics" element={<FabricsPage />} />
            <Route path="/fabrics/:fabricId" element={<FabricDetailPage />} />
            <Route path="/policies" element={<PoliciesPage />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="*" element={<Navigate to="/sites" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
