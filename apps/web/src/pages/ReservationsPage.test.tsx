import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReservationsPage } from "./ReservationsPage";

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

describe("ReservationsPage ownership controls", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("disables a non-owner action and routes an administrator through revoke", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST") return jsonResponse({ state: "REVOKED" });
      if (path.includes("/api/v1/reservations?")) {
        return jsonResponse({
          items: [
            {
              reservation: {
                id: "reservation-denied",
                bench_id: "simlab/denied",
                owner: "Other Owner",
                status: "ACTIVE",
                created_at: "2026-08-23T11:00:00Z",
                ends_at: "2026-08-23T13:00:00Z",
              },
              lease: { lease_version: 2, valid_until: "2026-08-23T12:30:00Z" },
              state: "ACTIVE",
              permissions: {
                owned_by_caller: false,
                release: false,
                extend: false,
                cancel: false,
                administrator: false,
              },
            },
            {
              reservation: {
                id: "reservation-admin",
                bench_id: "simlab/admin",
                owner: "Another Owner",
                status: "ACTIVE",
                created_at: "2026-08-23T11:00:00Z",
                ends_at: "2026-08-23T13:00:00Z",
              },
              lease: { lease_version: 4, valid_until: "2026-08-23T12:30:00Z" },
              state: "ACTIVE",
              permissions: {
                owned_by_caller: false,
                release: true,
                extend: false,
                cancel: false,
                administrator: true,
              },
            },
          ],
        });
      }
      return jsonResponse({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <ReservationsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("button", { name: "Release" })).toBeDisabled();
    const revoke = screen.getByRole("button", { name: "Revoke" });
    expect(revoke).toBeEnabled();
    await userEvent.click(revoke);
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke reservation" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/v1/reservations/reservation-admin/revoke",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });
});
