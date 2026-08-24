import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentDetailPage } from "./AgentDetailPage";

vi.mock("../app/AuthProvider", () => ({
  useAuth: () => ({ can: () => false }),
}));
vi.mock("../app/ToastProvider", () => ({
  useToast: () => ({ notify: vi.fn() }),
}));

describe("AgentDetailPage compatibility", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows a blocking upgrade assessment and its support window", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
    });
    client.setQueryData(["agents", "/api/v1/agents/agent-1"], {
      id: "agent-1",
      name: "North Lab Agent",
      slug: "north-lab-agent",
      status: "ONLINE",
      version: "0.7.9",
      protocol_version: "1.0",
      location: "North lab",
      labels: {},
      benches: [],
      recent_errors: [],
      permissions: { admin: false },
      upgrade_status: "upgrade_required",
      upgrade: {
        status: "upgrade_required",
        reason: "Agent must be upgraded before accepting work.",
        target_version: "0.9.0-beta",
        minimum_supported_version: "0.8.0",
        work_allowed: false,
      },
    });
    client.setQueryData(["agent-timeline", "/api/v1/agents/agent-1/timeline?limit=200"], {
      items: [],
    });
    client.setQueryData(["operations", "/api/v1/operations?agent_id=agent-1&limit=100"], {
      items: [],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("Unexpected Agent request"))),
    );

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agents/agent-1"]}>
          <Routes>
            <Route path="/agents/:agentId" element={<AgentDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "North Lab Agent" })).toBeVisible();
    expect(screen.getByText(/Agent must be upgraded before accepting work\./)).toBeVisible();
    expect(screen.getByText("Incompatible")).toBeVisible();
    expect(screen.getAllByText(/0\.9\.0-beta/)).toHaveLength(2);
    expect(screen.getByText("0.8.0")).toBeVisible();
    expect(screen.getAllByText("Upgrade Required")).toHaveLength(2);
  });
});
