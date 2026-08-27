import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./app/AppShell";
import { useAuth } from "./app/AuthProvider";
import { useLive } from "./app/LiveProvider";
import { ToastProvider, useToast } from "./app/ToastProvider";
import { LoginPage } from "./pages/LoginPage";

vi.mock("./app/AuthProvider", () => ({ useAuth: vi.fn() }));
vi.mock("./app/LiveProvider", () => ({ useLive: vi.fn() }));

const baseAuth = {
  user: { id: "user-1", username: "alice", display_name: "Alice Operator" },
  organisation: { id: "org-1", slug: "simlab", name: "SimLab" },
  permissions: new Set(["benches:read", "workflows:read", "operations:read", "agents:read"]),
  roles: ["OPERATOR"],
  authenticated: true,
  loading: false,
  error: undefined,
  oidcEnabled: false,
  localEnabled: true,
  sseEnabled: true,
  pollingFallbackSeconds: 5,
  maximumFirmwareSizeMb: 100,
  refresh: vi.fn(),
  clearSession: vi.fn(),
  can: vi.fn(),
  canAny: vi.fn((...required: string[]) =>
    required.some((permission) =>
      ["benches:read", "workflows:read", "operations:read", "agents:read"].includes(permission),
    ),
  ),
} as ReturnType<typeof useAuth>;

function NotificationTrigger() {
  const { notify } = useToast();
  return (
    <button
      onClick={() =>
        notify({
          title: "Reservation created",
          message: "simlab/bench-01 is ready.",
          tone: "success",
        })
      }
    >
      Notify
    </button>
  );
}

describe("core workflow accessibility acceptance", () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReturnValue(baseAuth);
    vi.mocked(useLive).mockReturnValue({
      state: "live",
      pollingIntervalMs: 5_000,
      lastEventAt: "2026-08-23T09:15:00Z",
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("exposes shell landmarks, a skip link, permission-aware navigation, and live status", async () => {
    render(
      <ToastProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<h1>Operational overview</h1>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ToastProvider>,
    );

    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute(
      "href",
      "#main-content",
    );
    const sidebar = screen.getByRole("complementary", { name: "Primary navigation" });
    expect(within(sidebar).getByRole("navigation")).toBeVisible();
    expect(screen.getByRole("link", { name: "Benches" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Workflows" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Analytics" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Audit" })).not.toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
    expect(screen.getByRole("heading", { level: 1, name: "Operational overview" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Live");

    const account = screen.getByRole("button", { name: /Alice Operator.*SimLab/ });
    expect(account).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(account);
    expect(account).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeVisible();
  });

  it("hides Analytics unless the role can read both benches and operations", () => {
    vi.mocked(useAuth).mockReturnValue({
      ...baseAuth,
      permissions: new Set(["benches:read"]),
      canAny: vi.fn((...required: string[]) => required.includes("benches:read")),
    });

    render(
      <ToastProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<h1>Operational overview</h1>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ToastProvider>,
    );

    expect(screen.getByRole("link", { name: "Benches" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "Analytics" })).not.toBeInTheDocument();
  });

  it("gives the local login workflow programmatic labels and announced validation errors", async () => {
    vi.mocked(useAuth).mockReturnValue({
      ...baseAuth,
      user: undefined,
      organisation: undefined,
      permissions: new Set(),
      roles: [],
      authenticated: false,
      canAny: vi.fn(() => false),
    });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <LoginPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("main")).toBeVisible();
    expect(
      screen.getByRole("heading", { level: 1, name: /Your lab, under control/ }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { level: 2, name: "Welcome back" })).toBeVisible();
    expect(screen.getByRole("textbox", { name: /^Organisation/ })).toHaveAttribute(
      "autocomplete",
      "organization",
    );
    expect(screen.getByRole("textbox", { name: /Username/ })).toHaveAttribute(
      "autocomplete",
      "username",
    );
    expect(screen.getByLabelText(/Password/)).toHaveAttribute("autocomplete", "current-password");

    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getByText("Enter your username")).toBeVisible();
    expect(screen.getByText("Enter your password")).toBeVisible();
  });

  it("announces notifications politely and gives dismissal an accessible name", async () => {
    render(
      <ToastProvider>
        <NotificationTrigger />
      </ToastProvider>,
    );

    const region = screen.getByRole("region", { name: "Notifications" });
    expect(region).toHaveAttribute("aria-live", "polite");

    await userEvent.click(screen.getByRole("button", { name: "Notify" }));
    expect(withinRegion(region, "Reservation created")).toBeVisible();
    expect(withinRegion(region, "simlab/bench-01 is ready.")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss notification" }));
    expect(screen.queryByText("Reservation created")).not.toBeInTheDocument();
  });
});

function withinRegion(region: HTMLElement, text: string) {
  const match = Array.from(region.querySelectorAll("*")).find(
    (element) => element.textContent === text,
  );
  if (!(match instanceof HTMLElement)) throw new Error(`Could not find ${text} in notification`);
  return match;
}
