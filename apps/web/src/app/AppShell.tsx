import {
  Activity,
  Archive,
  Bot,
  Boxes,
  ChartNoAxesCombined,
  ChevronDown,
  ChevronRight,
  CircuitBoard,
  ClipboardList,
  Gauge,
  LogOut,
  Menu,
  Network,
  PanelLeftClose,
  PlayCircle,
  ScrollText,
  Settings,
  ShieldCheck,
  Users,
  Workflow,
  X,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { errorMessage, generatedApi, stringValue } from "../api/client";
import { useAuth } from "./AuthProvider";
import { useLive } from "./LiveProvider";
import { NotificationCenter } from "./NotificationCenter";
import { useToast } from "./ToastProvider";

const PRIMARY_NAV = [
  { to: "/", label: "Overview", icon: Gauge, end: true, permissions: [] },
  { to: "/benches", label: "Benches", icon: CircuitBoard, permissions: ["benches:read"] },
  {
    to: "/reservations",
    label: "Reservations",
    icon: ClipboardList,
    permissions: ["benches:reserve"],
  },
  { to: "/workflows", label: "Workflows", icon: Workflow, permissions: ["workflows:read"] },
  { to: "/operations", label: "Operations", icon: Activity, permissions: ["operations:read"] },
  {
    to: "/analytics",
    label: "Analytics",
    icon: ChartNoAxesCombined,
    permissions: ["benches:read", "operations:read"],
  },
  {
    to: "/ci-sessions",
    label: "CI Sessions",
    icon: PlayCircle,
    permissions: ["ci:sessions:read"],
  },
  { to: "/agents", label: "Agents", icon: Bot, permissions: ["agents:read"] },
  { to: "/artifacts", label: "Artifacts", icon: Archive, permissions: ["artifacts:read"] },
  { to: "/audit", label: "Audit", icon: ScrollText, permissions: ["audit:read"] },
];

const ADMIN_NAV = [
  { to: "/admin/users", label: "Users", icon: Users, permission: "users:read" },
  { to: "/admin/teams", label: "Teams", icon: Network, permission: "teams:read" },
  {
    to: "/admin/service-accounts",
    label: "Service accounts",
    icon: Boxes,
    permission: "service_accounts:read",
  },
  { to: "/admin/roles", label: "Roles & access", icon: ShieldCheck, permission: "roles:read" },
  {
    to: "/admin/organisation",
    label: "Organisation",
    icon: Settings,
    permission: "organisation:read",
  },
];

export function AppShell() {
  const { user, organisation, canAny, clearSession } = useAuth();
  const live = useLive();
  const { notify } = useToast();
  const navigate = useNavigate();
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [adminOpen, setAdminOpen] = useState(location.pathname.startsWith("/admin"));
  const [accountOpen, setAccountOpen] = useState(false);
  const visibleAdminNav = ADMIN_NAV.filter((item) => canAny(item.permission));
  const canAdmin = visibleAdminNav.length > 0;
  const visibleNav = PRIMARY_NAV.filter(
    (item) =>
      !item.permissions.length || item.permissions.every((permission) => canAny(permission)),
  );

  async function logout() {
    try {
      await generatedApi.logout();
      clearSession();
      navigate("/login", { replace: true });
    } catch (error) {
      notify({ title: "Couldn’t sign out", message: errorMessage(error), tone: "error" });
    }
  }

  return (
    <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      {sidebarOpen && (
        <button
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside
        className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}
        aria-label="Primary navigation"
      >
        <div className="brand-row">
          <NavLink to="/" className="brand" onClick={() => setSidebarOpen(false)}>
            <span className="brand-mark">
              <CircuitBoard size={20} />
            </span>
            <span>
              <strong>Lab Platform</strong>
              <small>Operations</small>
            </span>
          </NavLink>
          <button
            className="mobile-close icon-button"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>
        <nav>
          <p className="nav-label">Workspace</p>
          {visibleNav.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setSidebarOpen(false)}
              title={collapsed ? label : undefined}
            >
              <Icon size={18} aria-hidden />
              <span>{label}</span>
            </NavLink>
          ))}
          {canAdmin && (
            <>
              <p className="nav-label admin-label">Manage</p>
              <button
                className={`nav-group-button ${location.pathname.startsWith("/admin") ? "active" : ""}`}
                onClick={() => setAdminOpen((open) => !open)}
              >
                <Settings size={18} />
                <span>Administration</span>
                {adminOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              </button>
              {adminOpen && (
                <div className="nav-submenu">
                  {visibleAdminNav.map(({ to, label, icon: Icon }) => (
                    <NavLink key={to} to={to} onClick={() => setSidebarOpen(false)}>
                      <Icon size={16} />
                      <span>{label}</span>
                    </NavLink>
                  ))}
                </div>
              )}
            </>
          )}
        </nav>
        <div className="sidebar-footer">
          <button
            className="collapse-button"
            onClick={() => setCollapsed((value) => !value)}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <PanelLeftClose size={17} />
            <span>Collapse sidebar</span>
          </button>
          <div className="version">
            <span className="pulse-dot" /> v0.9.0-beta
          </div>
        </div>
      </aside>
      <div className="app-body">
        <header className="topbar">
          <button
            className="mobile-menu icon-button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open navigation"
          >
            <Menu size={20} />
          </button>
          <div className={`live-indicator live-${live.state}`} role="status">
            <span />
            {live.state === "live"
              ? "Live"
              : live.state === "connecting"
                ? "Reconnecting"
                : live.state === "stale"
                  ? "Updates stale"
                  : "Polling"}
          </div>
          <div className="topbar-spacer" />
          <NotificationCenter />
          <button
            className="account-button"
            onClick={() => setAccountOpen((open) => !open)}
            aria-expanded={accountOpen}
          >
            <span className="avatar">
              {(stringValue(user, "display_name") ?? stringValue(user, "username") ?? "U")
                .slice(0, 2)
                .toUpperCase()}
            </span>
            <span>
              <strong>{stringValue(user, "display_name") ?? stringValue(user, "username")}</strong>
              <small>
                {stringValue(organisation, "name") ?? stringValue(organisation, "slug")}
              </small>
            </span>
            <ChevronDown size={15} />
          </button>
          {accountOpen && (
            <div className="account-menu">
              <div>
                <strong>{stringValue(user, "display_name") ?? "Signed in"}</strong>
                <span>{stringValue(user, "type")}</span>
              </div>
              <button onClick={logout}>
                <LogOut size={16} /> Sign out
              </button>
            </div>
          )}
        </header>
        <main id="main-content" className="main-content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
