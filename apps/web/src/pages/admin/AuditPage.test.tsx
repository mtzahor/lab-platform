import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuditPage } from "./AuditPage";

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("AuditPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders the canonical actor_display_name in the table and event detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          items: [
            {
              id: "audit-event-1",
              created_at: "2026-08-23T09:15:00Z",
              actor_display_name: "Dana Canonical",
              actor_name: "Legacy Actor",
              action: "reservation.created",
              resource_type: "reservation",
              resource_id: "reservation-12345678",
              outcome: "SUCCEEDED",
              source: "api",
              request_id: "1c7245d3-4f5d-4cee-a259-bf79750db639",
              metadata: {},
            },
          ],
          next_cursor: null,
          has_more: false,
        }),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <AuditPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const eventRow = await screen.findByRole("row", {
      name: "Inspect reservation.created audit event",
    });
    expect(within(eventRow).getByText("Dana Canonical")).toBeVisible();
    expect(within(eventRow).queryByText("Legacy Actor")).not.toBeInTheDocument();

    await userEvent.click(eventRow);

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Dana Canonical")).toBeVisible();
    expect(within(dialog).queryByText("Legacy Actor")).not.toBeInTheDocument();
  });
});
