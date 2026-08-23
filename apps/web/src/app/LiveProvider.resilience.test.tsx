import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "./AuthProvider";
import { LiveProvider, useLive } from "./LiveProvider";

vi.mock("./AuthProvider", () => ({ useAuth: vi.fn() }));

class ControlledEventSource {
  static latest: ControlledEventSource | undefined;

  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  readonly listeners = new Map<string, Set<EventListener>>();
  readonly close = vi.fn();

  constructor() {
    ControlledEventSource.latest = this;
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string, event: Event) {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function LiveStateProbe() {
  const live = useLive();
  return <output data-testid="live-state">{live.state}</output>;
}

describe("LiveProvider silence watchdog", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    ControlledEventSource.latest = undefined;
    vi.mocked(useAuth).mockReturnValue({
      authenticated: true,
      sseEnabled: true,
      pollingFallbackSeconds: 5,
    } as ReturnType<typeof useAuth>);
    vi.stubGlobal("EventSource", ControlledEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("marks an opened stream stale when no first event arrives and recovers on heartbeat", () => {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <LiveProvider>
          <LiveStateProbe />
        </LiveProvider>
      </QueryClientProvider>,
    );
    const source = ControlledEventSource.latest;
    expect(source).toBeDefined();

    act(() => source?.onopen?.(new Event("open")));
    expect(screen.getByTestId("live-state")).toHaveTextContent("live");

    act(() => vi.advanceTimersByTime(15_000));
    expect(screen.getByTestId("live-state")).toHaveTextContent("stale");

    act(() => source?.emit("heartbeat", new MessageEvent("heartbeat")));
    expect(screen.getByTestId("live-state")).toHaveTextContent("live");
  });
});
