import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "./AuthProvider";
import { LiveProvider, liveQueryRoots } from "./LiveProvider";

vi.mock("./AuthProvider", () => ({ useAuth: vi.fn() }));

describe("LiveProvider", () => {
  beforeEach(() => vi.clearAllMocks());

  it("invalidates every operational root for overview events", () => {
    expect(liveQueryRoots("overview.updated", {})).toEqual(
      expect.arrayContaining([
        "operations",
        "workflow-runs",
        "serial",
        "benches",
        "reservations",
        "agents",
        "ci-sessions",
        "artifacts",
      ]),
    );
  });

  it("does not open EventSource before authentication", () => {
    vi.mocked(useAuth).mockReturnValue({
      authenticated: false,
      sseEnabled: true,
      pollingFallbackSeconds: 5,
    } as ReturnType<typeof useAuth>);
    const eventSource = vi.fn();
    vi.stubGlobal("EventSource", eventSource);
    const client = new QueryClient();

    render(
      <QueryClientProvider client={client}>
        <LiveProvider>content</LiveProvider>
      </QueryClientProvider>,
    );

    expect(eventSource).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
