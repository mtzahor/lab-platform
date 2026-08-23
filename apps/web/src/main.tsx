import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { ApiError } from "./api/client";
import { AppShell } from "./app/AppShell";
import { AuthProvider } from "./app/AuthProvider";
import { LiveProvider } from "./app/LiveProvider";
import { NotificationProvider } from "./app/NotificationProvider";
import { ToastProvider } from "./app/ToastProvider";
import { AgentDetailPage } from "./pages/AgentDetailPage";
import { AgentsPage } from "./pages/AgentsPage";
import { ArtifactsPage } from "./pages/ArtifactsPage";
import { BenchDetailPage } from "./pages/BenchDetailPage";
import { BenchesPage } from "./pages/BenchesPage";
import { CiSessionDetailPage } from "./pages/CiSessionDetailPage";
import { CiSessionsPage } from "./pages/CiSessionsPage";
import { LoginPage } from "./pages/LoginPage";
import { OperationDetailPage } from "./pages/OperationDetailPage";
import { OperationsPage } from "./pages/OperationsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { ReservationsPage } from "./pages/ReservationsPage";
import { NotFoundPage, PermissionRoute, ProtectedRoute } from "./pages/SystemPages";
import { WorkflowRunPage } from "./pages/WorkflowRunPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";
import { AuditPage } from "./pages/admin/AuditPage";
import {
  OrganisationPage,
  RolesPage,
  ServiceAccountsPage,
  TeamsPage,
} from "./pages/admin/AdminPages";
import { UsersPage } from "./pages/admin/UsersPage";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (attempt, error) => !(error instanceof ApiError && error.status < 500) && attempt < 2,
      refetchOnWindowFocus: true,
    },
  },
});

function WithPermission({
  permission,
  children,
}: {
  permission: string;
  children: React.ReactNode;
}) {
  return <PermissionRoute permission={permission}>{children}</PermissionRoute>;
}

function Application() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<ProtectedRoute />}>
          <Route element={<AppShell />}>
            <Route index element={<OverviewPage />} />
            <Route path="benches" element={<BenchesPage />} />
            <Route path="benches/:benchId" element={<BenchDetailPage />} />
            <Route path="reservations" element={<ReservationsPage />} />
            <Route path="workflows" element={<WorkflowsPage />} />
            <Route path="workflow-runs/:runId" element={<WorkflowRunPage />} />
            <Route path="operations" element={<OperationsPage />} />
            <Route path="operations/:operationId" element={<OperationDetailPage />} />
            <Route path="ci-sessions" element={<CiSessionsPage />} />
            <Route path="ci-sessions/:sessionId" element={<CiSessionDetailPage />} />
            <Route path="agents" element={<AgentsPage />} />
            <Route path="agents/:agentId" element={<AgentDetailPage />} />
            <Route path="artifacts" element={<ArtifactsPage />} />
            <Route
              path="audit"
              element={
                <WithPermission permission="audit:read">
                  <AuditPage />
                </WithPermission>
              }
            />
            <Route
              path="admin/users"
              element={
                <WithPermission permission="users:read">
                  <UsersPage />
                </WithPermission>
              }
            />
            <Route
              path="admin/teams"
              element={
                <WithPermission permission="teams:read">
                  <TeamsPage />
                </WithPermission>
              }
            />
            <Route
              path="admin/service-accounts"
              element={
                <WithPermission permission="service_accounts:read">
                  <ServiceAccountsPage />
                </WithPermission>
              }
            />
            <Route
              path="admin/roles"
              element={
                <WithPermission permission="roles:read">
                  <RolesPage />
                </WithPermission>
              }
            />
            <Route
              path="admin/organisation"
              element={
                <WithPermission permission="organisation:read">
                  <OrganisationPage />
                </WithPermission>
              }
            />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <AuthProvider>
          <NotificationProvider>
            <LiveProvider>
              <Application />
            </LiveProvider>
          </NotificationProvider>
        </AuthProvider>
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
