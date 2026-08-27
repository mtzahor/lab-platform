import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastProvider } from "../app/ToastProvider";
import { AnalyticsPage } from "./AnalyticsPage";

const authState = vi.hoisted(() => ({ canReadAgents: true, canManageAlerts: true }));

vi.mock("../app/AuthProvider", () => ({
  useAuth: () => ({
    can: (permission: string) => {
      if (permission === "agents:read") return authState.canReadAgents;
      if (permission === "benches:manage") return authState.canManageAlerts;
      return true;
    },
  }),
}));

vi.mock("../app/LiveProvider", () => ({
  useLive: () => ({ state: "live", pollingIntervalMs: 60_000 }),
}));

const analytics = {
  generated_at: "2026-08-26T09:00:00Z",
  window: {
    started_at: "2026-08-19T09:00:00Z",
    ended_at: "2026-08-26T09:00:00Z",
  },
  semantics: {
    bench_utilisation: "reserved or actively operating time divided by available observation time",
    availability: "Agent-connected, non-maintenance time divided by the observation window",
    queue_wait: "time from queue creation until promotion; abandonment is separate",
    reliability: "successful operations divided by succeeded plus failed operations",
  },
  queue: {
    promoted_samples: 14,
    average_wait_seconds: 300,
    median_wait_seconds: 240,
    p95_wait_seconds: 1_200,
    abandoned: 2,
    abandonment_rate: 0.125,
    queue_depth: 3,
  },
  reliability: {
    operations: 20,
    succeeded: 18,
    failed: 2,
    infrastructure_failures: 1,
    success_rate: 0.9,
    failure_counts: { FLASH_ERROR: 1, USER_ERROR: 1 },
  },
  benches: [
    {
      bench_id: "simlab/alpha",
      name: "Bench Alpha",
      target_type: "esp32",
      availability_ratio: 0.8,
      utilisation: {
        observation_seconds: 1_000,
        available_seconds: 800,
        unavailable_seconds: 200,
        utilised_seconds: 600,
        utilisation_ratio: 0.75,
      },
      reservation_utilisation_ratio: 0.5,
      reliability: {
        operations: 10,
        succeeded: 8,
        failed: 2,
        infrastructure_failures: 1,
        success_rate: 0.8,
        failure_counts: { FLASH_ERROR: 1, USER_ERROR: 1 },
      },
      flaky: {
        bench_id: "simlab/alpha",
        potentially_flaky: true,
        sample_size: 10,
        infrastructure_failures: 3,
        infrastructure_failure_rate: 0.3,
        distinct_contexts: 2,
        primary_failure: "FLASH_ERROR",
        reasons: ["Repeated flash failures"],
      },
      maintenance: {
        bench_id: "simlab/alpha",
        status: "HEALTHY",
        updated_at: "2026-08-26T09:00:00Z",
        reason: null,
        manually_set: false,
      },
      recommendation: {
        code: "CHECK_FLASH_PATH",
        message: "Inspect the probe and flash wiring.",
        failure_category: "FLASH_ERROR",
        heuristic: true,
      },
    },
    {
      bench_id: "simlab/beta",
      name: "Bench Beta",
      target_type: "nrf52",
      availability_ratio: 0.2,
      utilisation: {
        observation_seconds: 1_000,
        available_seconds: 200,
        unavailable_seconds: 800,
        utilised_seconds: 100,
        utilisation_ratio: 0.5,
      },
      reservation_utilisation_ratio: 0.4,
      reliability: {
        operations: 10,
        succeeded: 10,
        failed: 0,
        infrastructure_failures: 0,
        success_rate: 1,
        failure_counts: {},
      },
      flaky: {
        bench_id: "simlab/beta",
        potentially_flaky: false,
        sample_size: 10,
        infrastructure_failures: 0,
        infrastructure_failure_rate: 0,
        distinct_contexts: 0,
        primary_failure: null,
        reasons: [],
      },
      maintenance: {
        bench_id: "simlab/beta",
        status: "HEALTHY",
        updated_at: "2026-08-26T09:00:00Z",
        reason: null,
        manually_set: false,
      },
      recommendation: null,
    },
  ],
  workflows: [],
};

const alerts = {
  items: [
    {
      id: "alert-critical",
      severity: "CRITICAL",
      type: "DATABASE_ISSUE",
      resource_type: "organisation",
      resource_id: "org-1",
      status: "OPEN",
      message: "Database health check failed.",
      created_at: "2026-08-26T08:58:00Z",
      acknowledged_at: null,
      resolved_at: null,
    },
    {
      id: "alert-flaky",
      severity: "WARNING",
      type: "REPEATED_FAILURES",
      resource_type: "bench",
      resource_id: "simlab/alpha",
      status: "OPEN",
      message: "Bench Alpha has repeated infrastructure failures.",
      created_at: "2026-08-26T08:55:00Z",
      acknowledged_at: null,
      resolved_at: null,
    },
  ],
  total: 2,
};

function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Number.POSITIVE_INFINITY },
    },
  });
}

function renderAnalytics(client = queryClient()) {
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>
          <AnalyticsPage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("AnalyticsPage", () => {
  afterEach(() => {
    cleanup();
    authState.canReadAgents = true;
    authState.canManageAlerts = true;
    vi.unstubAllGlobals();
  });

  it("summarises weighted lab signals, flaky benches, alerts, and fleet compatibility", () => {
    const client = queryClient();
    client.setQueryData(
      ["operational-analytics", "/api/v1/operational/analytics?window_hours=168"],
      analytics,
    );
    client.setQueryData(["operational-alerts", "/api/v1/operational/alerts?status=OPEN"], alerts);
    client.setQueryData(["agents", "/api/v1/agents"], {
      items: [
        {
          id: "agent-1",
          name: "Agent One",
          status: "ONLINE",
          version: "0.9.0",
          protocol_version: "7",
          upgrade: { status: "up_to_date", work_allowed: true },
        },
        {
          id: "agent-2",
          name: "Agent Two",
          status: "INCOMPATIBLE",
          version: "0.7.0",
          protocol_version: "6",
          upgrade: { status: "upgrade_required", work_allowed: false },
        },
      ],
    });
    const fetchMock = vi.fn(() => Promise.reject(new Error("Unexpected request")));
    vi.stubGlobal("fetch", fetchMock);

    renderAnalytics(client);

    expect(screen.getByRole("heading", { level: 1, name: "Analytics" })).toBeVisible();
    expect(screen.getByText("70%")).toBeVisible();
    expect(screen.getByText("Lab availability 50%")).toBeVisible();
    expect(screen.getByText("p95 wait 20m · 2 abandoned")).toBeVisible();
    expect(screen.getByText("90%")).toBeVisible();
    expect(screen.getByText("20 operations · 1 flaky bench")).toBeVisible();
    expect(screen.getByText("Database health check failed.")).toBeVisible();
    expect(screen.getByText("Critical").closest("span")).toHaveClass("tone-negative");
    expect(screen.getByRole("link", { name: "simlab/alpha" })).toHaveAttribute(
      "href",
      "/benches/simlab%2Falpha",
    );
    expect(screen.getByText("1 work blocked")).toBeVisible();
    expect(screen.getByText(/Agent is incompatible with the current control plane/)).toBeVisible();

    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Reliability" })).toBeVisible();
    expect(
      within(table).getByRole("progressbar", { name: "Bench Alpha utilisation 75%" }),
    ).toBeVisible();
    expect(within(table).getByText("Potentially flaky")).toBeVisible();
    expect(within(table).getByText("Inspect the probe and flash wiring.")).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requests the selected window and skips the fleet request without Agent permission", async () => {
    authState.canReadAgents = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input);
      const url = new URL(request.url);
      if (url.pathname === "/api/v1/operational/analytics") return jsonResponse(analytics);
      if (
        url.pathname === "/api/v1/operational/alerts" &&
        url.searchParams.get("status") === "OPEN"
      )
        return jsonResponse({ items: [], total: 0 });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderAnalytics();

    const windowSelect = await screen.findByRole("combobox", { name: "Analytics window" });
    expect(screen.getByRole("heading", { level: 1, name: "Analytics" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Agent fleet" })).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.every(([input]) => {
        const request = input instanceof Request ? input : new Request(input);
        return new URL(request.url).pathname !== "/api/v1/agents";
      }),
    ).toBe(true);

    await userEvent.selectOptions(windowSelect, "24");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) => {
          const request = input instanceof Request ? input : new Request(input);
          const url = new URL(request.url);
          return (
            url.pathname === "/api/v1/operational/analytics" &&
            url.searchParams.get("window_hours") === "24"
          );
        }),
      ).toBe(true),
    );
  });

  it("offers alert actions only to managers and acknowledges through the typed API", async () => {
    authState.canReadAgents = false;
    const client = queryClient();
    client.setQueryData(
      ["operational-analytics", "/api/v1/operational/analytics?window_hours=168"],
      analytics,
    );
    client.setQueryData(["operational-alerts", "/api/v1/operational/alerts?status=OPEN"], alerts);
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(input);
      const url = new URL(request.url);
      if (url.pathname === "/api/v1/operational/alerts/alert-critical/acknowledge") {
        return jsonResponse({ ...alerts.items[0], status: "ACKNOWLEDGED" });
      }
      if (url.pathname === "/api/v1/operational/alerts") {
        return jsonResponse({ items: [alerts.items[1]], total: 1 });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderAnalytics(client);

    await userEvent.click(
      screen.getByRole("button", {
        name: "Acknowledge alert: Database health check failed.",
      }),
    );

    await screen.findByText("Alert acknowledged");
    const [request] = fetchMock.mock.calls[0] as [Request];
    expect(new URL(request.url).pathname).toBe(
      "/api/v1/operational/alerts/alert-critical/acknowledge",
    );
    expect(request.method).toBe("POST");

    cleanup();
    authState.canManageAlerts = false;
    const readOnlyClient = queryClient();
    readOnlyClient.setQueryData(
      ["operational-analytics", "/api/v1/operational/analytics?window_hours=168"],
      analytics,
    );
    readOnlyClient.setQueryData(
      ["operational-alerts", "/api/v1/operational/alerts?status=OPEN"],
      alerts,
    );
    renderAnalytics(readOnlyClient);
    expect(screen.queryByRole("button", { name: /Acknowledge alert:/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Resolve alert:/ })).not.toBeInTheDocument();
  });

  it("keeps unavailable ratios and queue samples distinct from zero", () => {
    authState.canReadAgents = false;
    const client = queryClient();
    client.setQueryData(
      ["operational-analytics", "/api/v1/operational/analytics?window_hours=168"],
      {
        generated_at: "2026-08-26T09:00:00Z",
        queue: {
          queue_depth: 0,
          p95_wait_seconds: null,
          abandoned: 0,
        },
        reliability: {
          operations: 0,
          success_rate: null,
        },
        benches: [
          {
            bench_id: "simlab/offline",
            name: "Offline Bench",
            availability_ratio: 0,
            utilisation: {
              observation_seconds: 1_000,
              available_seconds: 0,
              utilised_seconds: 0,
              utilisation_ratio: null,
            },
            reliability: { operations: 0, success_rate: null },
            flaky: { potentially_flaky: false },
            maintenance: { status: "OFFLINE" },
            recommendation: null,
          },
        ],
        workflows: [],
      },
    );
    client.setQueryData(["operational-alerts", "/api/v1/operational/alerts?status=OPEN"], {
      items: [],
      total: 0,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("Unexpected request"))),
    );

    renderAnalytics(client);

    const utilisationLabel = screen.getByText("Lab utilisation");
    const utilisationCard = utilisationLabel.closest(".metric-card");
    const reliabilityCard = screen
      .getByText("Reliability", { selector: ".metric-card span" })
      .closest(".metric-card");
    expect(utilisationCard).not.toBeNull();
    expect(reliabilityCard).not.toBeNull();
    expect(within(utilisationCard as HTMLElement).getByText("—")).toBeVisible();
    expect(within(reliabilityCard as HTMLElement).getByText("—")).toBeVisible();
    expect(screen.getByText("p95 wait No samples · 0 abandoned")).toBeVisible();
    expect(screen.getByLabelText("Utilisation unavailable")).toBeVisible();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});
