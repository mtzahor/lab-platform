import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "./AuthProvider";
import { LiveProvider, useLive } from "./LiveProvider";

vi.mock("./AuthProvider", () => ({ useAuth: vi.fn() }));

type EventHandler = EventListenerOrEventListenerObject;

class TestEventSource {
  static instances: TestEventSource[] = [];

  readonly url: string;
  readonly withCredentials: boolean;
  readonly listeners = new Map<string, Set<EventHandler>>();
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  closed = false;

  constructor(url: string | URL, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = init?.withCredentials === true;
    TestEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventHandler) {
    const listeners = this.listeners.get(type) ?? new Set<EventHandler>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventHandler) {
    this.listeners.get(type)?.delete(listener);
  }

  close() {
    this.closed = true;
  }

  open() {
    this.onopen?.(new Event("open"));
  }

  error() {
    this.onerror?.(new Event("error"));
  }

  message(payload: unknown, lastEventId = "") {
    this.onmessage?.(
      new MessageEvent<string>("message", {
        data: JSON.stringify(payload),
        lastEventId,
      }),
    );
  }

  malformed(data: string, lastEventId = "") {
    this.onmessage?.(new MessageEvent<string>("message", { data, lastEventId }));
  }

  named(type: string, payload: unknown, lastEventId = "") {
    const event = new MessageEvent<string>(type, {
      data: JSON.stringify(payload),
      lastEventId,
    });
    for (const listener of this.listeners.get(type) ?? []) {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    }
  }
}

function LiveProbe() {
  const live = useLive();
  return (
    <output aria-label="Live connection" data-last-event-at={live.lastEventAt}>
      {live.state}
    </output>
  );
}

function renderLiveProvider() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <LiveProvider>
        <LiveProbe />
      </LiveProvider>
    </QueryClientProvider>,
  );
  return { ...result, client, source: TestEventSource.instances[0]! };
}

describe("LiveProvider acceptance", () => {
  beforeEach(() => {
    TestEventSource.instances = [];
    vi.mocked(useAuth).mockReturnValue({
      authenticated: true,
      sseEnabled: true,
      pollingFallbackSeconds: 5,
    } as ReturnType<typeof useAuth>);
    vi.stubGlobal("EventSource", TestEventSource);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps the native reconnecting stream open, falls back after repeated errors, and recovers", () => {
    const { source, unmount } = renderLiveProvider();

    expect(TestEventSource.instances).toHaveLength(1);
    expect(source.url).toMatch(/\/api\/v1\/events$/);
    expect(source.withCredentials).toBe(true);
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveTextContent("connecting");

    act(() => source.open());
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveTextContent("live");

    act(() => source.error());
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveTextContent("connecting");
    expect(source.closed).toBe(false);
    expect(TestEventSource.instances).toHaveLength(1);

    act(() => source.error());
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveTextContent("polling");
    expect(source.closed).toBe(false);

    act(() => {
      source.open();
      source.named("heartbeat", {});
    });
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveTextContent("live");
    act(() => source.error());
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveTextContent("connecting");

    unmount();
    expect(source.closed).toBe(true);
  });

  it("invalidates only affected query roots and ignores duplicate event IDs", () => {
    const { client, source } = renderLiveProvider();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    act(() => {
      source.open();
      source.message(
        { type: "operation.progress", operation_id: "operation-17", progress: 42 },
        "event-17",
      );
    });

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["operations"],
      ["workflow-runs"],
      ["overview"],
    ]);
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveAttribute(
      "data-last-event-at",
    );

    act(() => {
      source.message(
        { type: "operation.progress", operation_id: "operation-17", progress: 42 },
        "event-17",
      );
    });
    expect(invalidate).toHaveBeenCalledTimes(3);

    act(() => {
      source.message({ type: "reservation.updated", reservation_id: "reservation-2" }, "event-18");
    });
    expect(invalidate.mock.calls.slice(3).map(([filters]) => filters?.queryKey)).toEqual([
      ["reservations"],
      ["benches"],
      ["overview"],
    ]);
  });

  it("uses one connection for 50 live operations and keeps duplicate tracking bounded", () => {
    const { client, source } = renderLiveProvider();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    act(() => {
      source.open();
      for (let index = 0; index < 50; index += 1) {
        source.message(
          { type: "operation.progress", operation_id: `operation-${index}`, progress: index },
          `event-${index}`,
        );
      }
    });

    expect(TestEventSource.instances).toHaveLength(1);
    expect(invalidate).toHaveBeenCalledTimes(150);

    act(() => {
      source.message(
        { type: "operation.progress", operation_id: "operation-49", progress: 49 },
        "event-49",
      );
    });
    expect(invalidate).toHaveBeenCalledTimes(150);

    act(() => {
      for (let index = 50; index < 260; index += 1) {
        source.message(
          { type: "operation.progress", operation_id: `operation-${index}`, progress: index },
          `event-${index}`,
        );
      }
    });
    expect(invalidate).toHaveBeenCalledTimes(780);

    act(() => {
      source.message(
        { type: "operation.progress", operation_id: "operation-0", progress: 100 },
        "event-0",
      );
      source.message(
        { type: "operation.progress", operation_id: "operation-259", progress: 100 },
        "event-259",
      );
    });
    expect(invalidate).toHaveBeenCalledTimes(783);
  });

  it("invalidates the full safety set when an event payload cannot be decoded", () => {
    const { client, source } = renderLiveProvider();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    act(() => source.malformed("not-json", "event-malformed"));

    const roots = invalidate.mock.calls.map(([filters]) => filters?.queryKey?.[0]);
    expect(roots).toEqual(
      expect.arrayContaining([
        "overview",
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

  it("treats heartbeats as liveness without triggering data refetches", () => {
    const { client, source } = renderLiveProvider();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    act(() => {
      source.error();
      source.named("heartbeat", {});
    });

    expect(screen.getByRole("status", { name: "Live connection" })).toHaveTextContent("live");
    expect(screen.getByRole("status", { name: "Live connection" })).toHaveAttribute(
      "data-last-event-at",
    );
    expect(invalidate).not.toHaveBeenCalled();
  });
});
