import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiRecord } from "../api/client";
import { AgentsPage } from "./AgentsPage";
import { OperationsPage } from "./OperationsPage";
import { AuditPage } from "./admin/AuditPage";

function testClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: Number.POSITIVE_INFINITY,
      },
    },
  });
}

function renderPage(element: ReactNode, client = testClient()) {
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter>{element}</MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function auditRecord(index: number): ApiRecord {
  const serial = String(index).padStart(5, "0");
  return {
    id: `audit-${serial}`,
    created_at: "2026-08-23T09:15:00Z",
    actor_display_name: `Actor ${serial}`,
    action: "workflow.completed",
    resource_type: "workflow_run",
    resource_id: `workflow-run-${serial}`,
    outcome: index % 17 === 0 ? "DENIED" : "SUCCEEDED",
    source: "api",
    request_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    metadata: {},
  };
}

describe("Phase 7 frontend scale acceptance", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("keeps 100 Agents paginated, searchable, and keyboard understandable", async () => {
    const agents = Array.from({ length: 100 }, (_, index) => {
      const serial = String(index).padStart(3, "0");
      return {
        id: `agent-${serial}`,
        name: `Agent ${serial}`,
        slug: `agent-${serial}`,
        status: index % 2 === 0 ? "ONLINE" : "OFFLINE",
        version: index % 3 === 0 ? "0.8.0" : "0.8.1",
        protocol_version: "7",
        location: index % 2 === 0 ? "north-lab" : "south-lab",
        labels: { rack: String(index % 10) },
        bench_count: 10,
        last_seen_at: "2026-08-23T09:15:00Z",
        last_connected_at: "2026-08-23T09:00:00Z",
      };
    });
    const client = testClient();
    client.setQueryData(["agents"], { items: agents });
    const fetchMock = vi.fn(() => Promise.reject(new Error("Unexpected Agent request")));
    vi.stubGlobal("fetch", fetchMock);

    renderPage(<AgentsPage />, client);

    expect(screen.getByRole("heading", { level: 1, name: "Agents" })).toBeVisible();
    expect(screen.getByRole("table")).toBeVisible();
    expect(screen.getAllByRole("row")).toHaveLength(26);
    expect(screen.getByText("Page 1 of 4 · 100 results")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Status" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Location" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Version" })).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("row", { name: "Open Agent 025" })).toBeVisible();

    const search = screen.getByRole("searchbox", { name: "Search" });
    await userEvent.type(search, "agent-099");
    await waitFor(() => expect(screen.getByRole("row", { name: "Open Agent 099" })).toBeVisible());
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("renders 10,000 audit records with a fixed 50-row viewport and usable pagination", () => {
    const client = testClient();
    const pages = Array.from({ length: 100 }, (_, pageIndex) => ({
      items: Array.from({ length: 100 }, (_, itemIndex) =>
        auditRecord(pageIndex * 100 + itemIndex),
      ),
      next_cursor: pageIndex < 99 ? `cursor-${pageIndex + 1}` : null,
      has_more: pageIndex < 99,
    }));
    client.setQueryData(["audit", "limit=100"], {
      pages,
      pageParams: ["", ...Array.from({ length: 99 }, (_, index) => `cursor-${index + 1}`)],
    });
    const fetchMock = vi.fn(() => Promise.reject(new Error("Unexpected audit request")));
    vi.stubGlobal("fetch", fetchMock);

    renderPage(<AuditPage />, client);

    expect(screen.getByRole("heading", { level: 1, name: "Audit log" })).toBeVisible();
    expect(screen.getByText("Page 1 of 200 · 10,000 results")).toBeVisible();
    expect(screen.getAllByRole("row")).toHaveLength(51);
    expect(screen.getByText("Actor 00000")).toBeVisible();
    expect(screen.queryByText("Actor 00050")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Actor 00050")).toBeVisible();
    expect(screen.queryByText("Actor 00000")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps 1,000 workflow operations paginated and filters without request fan-out", async () => {
    const runs = Array.from({ length: 1_000 }, (_, index) => {
      const serial = String(index).padStart(4, "0");
      return {
        id: `workflow-run-${serial}`,
        operation_type: "WORKFLOW",
        bench_id: `simlab/bench-${String(index % 100).padStart(3, "0")}`,
        status: index % 10 === 0 ? "RUNNING" : "SUCCEEDED",
        progress: index % 10 === 0 ? 50 : 100,
        agent_id: `agent-${index % 100}`,
        actor: { display_name: `Operator ${index % 20}` },
        created_at: "2026-08-23T09:15:00Z",
        completed_at: index % 10 === 0 ? null : "2026-08-23T09:16:00Z",
      };
    });
    const client = testClient();
    client.setQueryData(["operations", "/api/v1/operations?limit=1000"], { items: runs });
    const fetchMock = vi.fn(() => Promise.reject(new Error("Unexpected operation request")));
    vi.stubGlobal("fetch", fetchMock);

    renderPage(<OperationsPage />, client);

    expect(screen.getByText("Page 1 of 40 · 1,000 results")).toBeVisible();
    expect(screen.getAllByRole("row")).toHaveLength(26);
    expect(screen.getAllByRole("progressbar", { name: "50% complete" }).length).toBeGreaterThan(0);

    await userEvent.type(screen.getByRole("searchbox", { name: "Search" }), "workflow-run-0999");
    await waitFor(() => expect(screen.getByText("workflow-run-0999")).toBeVisible());
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("requests audit history one cursor page at a time only after explicit user action", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
      const cursor = url.searchParams.get("cursor");
      return cursor
        ? jsonResponse({ items: [auditRecord(100)], next_cursor: null, has_more: false })
        : jsonResponse({
            items: Array.from({ length: 100 }, (_, index) => auditRecord(index)),
            next_cursor: "cursor-1",
            has_more: true,
          });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPage(<AuditPage />);

    await screen.findByText("Page 1 of 2 · 100 results");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Load older events" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: "Load older events" })).not.toBeInTheDocument();
  });
});
