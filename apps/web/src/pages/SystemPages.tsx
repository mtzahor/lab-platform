import { AlertTriangle, ArrowLeft, LockKeyhole } from "lucide-react";
import { Link, Navigate, Outlet, useLocation } from "react-router-dom";
import { ApiError } from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { Button, LoadingState } from "../components/ui";

export function ProtectedRoute() {
  const auth = useAuth();
  const location = useLocation();
  if (auth.loading)
    return (
      <div className="app-loading">
        <LoadingState label="Opening your workspace" />
      </div>
    );
  if (!auth.authenticated)
    return (
      <Navigate
        to={`/login?returnTo=${encodeURIComponent(location.pathname + location.search)}`}
        replace
      />
    );
  return <Outlet />;
}

export function PermissionRoute({
  permission,
  children,
}: {
  permission: string;
  children: React.ReactNode;
}) {
  const auth = useAuth();
  return auth.can(permission) ? children : <ForbiddenPage />;
}

export function ForbiddenPage() {
  return (
    <div className="system-page">
      <span>
        <LockKeyhole size={26} />
      </span>
      <p className="eyebrow">Permission required</p>
      <h1>This area isn’t available to your role</h1>
      <p>The server remains authoritative. Ask an organisation administrator if you need access.</p>
      <Link className="button button-secondary" to="/">
        <ArrowLeft size={16} /> Return to overview
      </Link>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <div className="system-page">
      <span>
        <AlertTriangle size={26} />
      </span>
      <p className="eyebrow">404</p>
      <h1>We couldn’t find that page</h1>
      <p>The resource may have moved, been removed, or isn’t visible to your role.</p>
      <Button variant="secondary" onClick={() => history.back()} icon={ArrowLeft}>
        Go back
      </Button>
    </div>
  );
}

export function RouteErrorPage({ error }: { error?: unknown }) {
  const status = error instanceof ApiError ? error.status : undefined;
  if (status === 403) return <ForbiddenPage />;
  return (
    <div className="system-page">
      <span>
        <AlertTriangle size={26} />
      </span>
      <p className="eyebrow">Unexpected error</p>
      <h1>This view couldn’t be opened</h1>
      <p>{error instanceof Error ? error.message : "Refresh the page and try again."}</p>
      <Button variant="secondary" onClick={() => location.reload()}>
        Reload page
      </Button>
    </div>
  );
}
