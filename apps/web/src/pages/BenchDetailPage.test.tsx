import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BenchDetailPage } from "./BenchDetailPage";

vi.mock("../app/AuthProvider", () => ({
  useAuth: () => ({ can: () => true }),
}));
vi.mock("../app/ToastProvider", () => ({
  useToast: () => ({ notify: vi.fn() }),
}));

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("BenchDetailPage reservation ownership controls", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses reservation flags even when the caller has a global reserver permission", async () => {
    const endsAt = new Date(Date.now() + 5 * 60_000).toISOString();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (
          path.includes("/api/v1/benches/simlab%2Fbench-01") &&
          !path.endsWith("/queue") &&
          !path.includes("/timeline")
        ) {
          return jsonResponse({
            id: "simlab/bench-01",
            name: "Ownership bench",
            kind: "SIMULATED",
            status: "ONLINE",
            health: "HEALTHY",
            availability: "RESERVED",
            capabilities: [],
            labels: {},
            updated_at: new Date().toISOString(),
            last_seen_at: new Date().toISOString(),
            permissions: { reserve: true, operate: false, serial: false, flash: false },
            current_reservation: {
              id: "reservation-other-owner",
              bench_id: "simlab/bench-01",
              owner: "Other Owner",
              state: "ACTIVE",
              starts_at: new Date(Date.now() - 30 * 60_000).toISOString(),
              ends_at: endsAt,
              lease_valid_until: endsAt,
              lease_version: 3,
              permissions: {
                owned_by_caller: false,
                release: false,
                extend: false,
                cancel: false,
                administrator: false,
              },
            },
          });
        }
        return jsonResponse({ items: [] });
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/benches/simlab%2Fbench-01"]}>
          <Routes>
            <Route path="/benches/:benchId" element={<BenchDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Ownership bench" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Extend 30m" })).toBeDisabled();
    for (const release of screen.getAllByRole("button", { name: "Release" })) {
      expect(release).toBeDisabled();
    }
    expect(
      screen.getByText("Reservation changes are limited to its owner or a bench administrator."),
    ).toBeVisible();
    expect(screen.getByText(/Ends in/)).toBeVisible();
    expect(screen.getByText("Save work or extend the lease soon.")).toBeVisible();
  });
});
