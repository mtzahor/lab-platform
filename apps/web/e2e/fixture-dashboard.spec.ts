import { expect, test, type Page, type Route } from "@playwright/test";

const bench = {
  id: "simulation-cluster/virtual-esp32-01",
  name: "Virtual ESP32 01",
  agent_id: "00000000-0000-4000-8000-000000000010",
  agent_slug: "simulation-cluster",
  backend_id: "simlab",
  kind: "SIMULATED",
  target_type: "esp32",
  status: "ONLINE",
  availability: "AVAILABLE",
  health: "HEALTHY",
  capabilities: ["probe", "reset", "serial", "firmware"],
  labels: { board: "esp32", purpose: "hardware-ci" },
  firmware_version: "0.8.0-demo",
  last_seen_at: "2026-08-20T09:00:00Z",
  updated_at: "2026-08-20T09:00:00Z",
  agent: {
    id: "00000000-0000-4000-8000-000000000010",
    name: "Simulation Cluster",
    status: "ONLINE",
    location: "Jerusalem",
  },
  permissions: {
    read: true,
    reserve: false,
    operate: false,
    reset: false,
    serial: false,
    flash: false,
  },
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installApiFixture(page: Page) {
  let authenticated = false;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = decodeURIComponent(new URL(request.url()).pathname);
    if (path === "/api/v1/auth/config") {
      return json(route, {
        local_enabled: true,
        oidc_enabled: false,
        browser_session_enabled: true,
        web: { live_updates: { sse_enabled: false, polling_fallback_seconds: 30 } },
      });
    }
    if (path === "/api/v1/auth/login" && request.method() === "POST") {
      authenticated = true;
      return json(route, { authenticated: true });
    }
    if (path === "/api/v1/auth/refresh") {
      return json(route, { detail: { code: "AUTHENTICATION_REQUIRED" } }, 401);
    }
    if (path === "/api/v1/auth/me") {
      if (!authenticated) return json(route, { detail: { code: "AUTHENTICATION_REQUIRED" } }, 401);
      return json(route, {
        principal: { id: "viewer-1", display_name: "Bob Viewer", username: "bob", type: "USER" },
        organisation: { id: "org-1", name: "SimLab Demo", slug: "simlab-demo" },
        permissions: ["organisation:read", "benches:read", "operations:read"],
        roles: ["VIEWER"],
        membership_role: "VIEWER",
      });
    }
    if (path === "/api/v1/overview") {
      return json(route, {
        generated_at: "2026-08-20T09:00:00Z",
        counts: {
          benches_total: 1,
          benches_available: 1,
          benches_reserved: 0,
          benches_offline: 0,
          agents_online: 1,
          active_operations: 0,
          queued_reservations: 0,
          failed_workflows_24h: 0,
        },
        active_operations: [],
        failed_workflow_runs: [],
        degraded_benches: [],
        active_reservations: [],
        recent_agent_disconnects: [],
        active_ci_sessions: [],
      });
    }
    if (path === "/api/v1/benches") return json(route, { items: [bench], total: 1 });
    if (path === `/api/v1/benches/${bench.id}`) return json(route, bench);
    if (path.endsWith("/timeline")) return json(route, { items: [], total: 0 });
    if (["/api/v1/operations", "/api/v1/artifacts", "/api/v1/reservations"].includes(path))
      return json(route, { items: [], total: 0 });
    return json(route, { items: [], total: 0 });
  });
}

test("local login opens a permission-aware, keyboard-accessible bench view", async ({ page }) => {
  await installApiFixture(page);
  await page.goto("/login?returnTo=%2Fbenches");
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  await page.getByLabel("Organisation").fill("simlab-demo");
  await page.getByLabel("Username").fill("bob");
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: /Sign in/ }).click();

  await expect(page).toHaveURL(/\/benches$/);
  await expect(page.getByRole("heading", { name: "Benches" })).toBeVisible();
  await expect(page.getByText("Virtual ESP32 01")).toBeVisible();
  await page.getByRole("button", { name: "Administration" }).click();
  await expect(page.getByRole("link", { name: "Organisation" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Users" })).toHaveCount(0);

  await page.locator("tbody tr").first().focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Virtual ESP32 01" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reserve", exact: true })).toBeDisabled();
  await expect(page.getByText("Your role has read-only access to this bench.")).toBeVisible();
});
