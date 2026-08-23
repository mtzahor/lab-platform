import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "./AuthProvider";
import { LiveProvider } from "./LiveProvider";
import { NotificationProvider, useNotifications } from "./NotificationProvider";
import { liveNotificationFromEvent } from "./liveNotifications";

vi.mock("./AuthProvider", () => ({ useAuth: vi.fn() }));

class NamedEventSource {
  static latest: NamedEventSource | undefined;

  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  readonly listeners = new Map<string, Set<EventListener>>();
  readonly close = vi.fn();

  constructor() {
    NamedEventSource.latest = this;
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string, payload: unknown, lastEventId: string) {
    const event = new MessageEvent<string>(type, {
      data: JSON.stringify(payload),
      lastEventId,
    });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function NotificationProbe() {
  const { notifications } = useNotifications();
  return (
    <div>
      <output data-testid="notification-total">{notifications.length}</output>
      {notifications.map((notification) => (
        <div key={notification.id}>
          <strong>{notification.title}</strong>
          <span>{notification.message}</span>
          <span>{notification.href}</span>
        </div>
      ))}
    </div>
  );
}

function resourceEnvelope(
  notify: boolean,
  summary: string,
  status = "FAILED",
): Record<string, unknown> {
  return {
    id: "server-envelope-id",
    type: "workflow.updated",
    timestamp: "2026-08-23T09:15:00Z",
    data: {
      resource_type: "WORKFLOW",
      notify,
      summary,
      status,
      data: {
        run_id: "workflow/run-17",
        password: "never-render-this",
        access_token: "also-never-render-this",
      },
    },
  };
}

describe("live notification integration", () => {
  beforeEach(() => {
    NamedEventSource.latest = undefined;
    vi.mocked(useAuth).mockReturnValue({
      authenticated: true,
      sseEnabled: true,
      pollingFallbackSeconds: 5,
    } as ReturnType<typeof useAuth>);
    vi.stubGlobal("EventSource", NamedEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("formats only explicitly notifiable safe summaries", () => {
    expect(
      liveNotificationFromEvent(
        "workflow.updated",
        resourceEnvelope(true, "A workflow failed."),
        "event-17",
      ),
    ).toEqual({
      dedupeKey: "event-17",
      title: "Workflow Failed",
      message: "A workflow failed.",
      tone: "error",
      href: "/workflow-runs/workflow%2Frun-17",
      createdAt: "2026-08-23T09:15:00Z",
    });
    expect(
      liveNotificationFromEvent(
        "workflow.updated",
        resourceEnvelope(false, "Routine progress changed."),
        "event-18",
      ),
    ).toBeUndefined();
    expect(
      liveNotificationFromEvent(
        "workflow.updated",
        resourceEnvelope(true, "token=super-secret"),
        "event-19",
      ),
    ).toBeUndefined();
  });

  it("deduplicates named frames by lastEventId and ignores snapshot and progress noise", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <NotificationProvider>
          <LiveProvider>
            <NotificationProbe />
          </LiveProvider>
        </NotificationProvider>
      </QueryClientProvider>,
    );
    const source = NamedEventSource.latest;
    expect(source).toBeDefined();

    act(() => {
      source?.emit("workflow.updated", resourceEnvelope(true, "A workflow failed."), "event-17");
      source?.emit(
        "workflow.updated",
        resourceEnvelope(true, "Duplicate must not replace the first event."),
        "event-17",
      );
      source?.emit(
        "operation.updated",
        {
          type: "operation.updated",
          data: { resource_type: "OPERATION", notify: false, summary: "Progress changed." },
        },
        "event-18",
      );
      source?.emit(
        "overview.updated",
        {
          type: "overview.updated",
          data: { notify: true, summary: "Snapshot noise." },
        },
        "event-19",
      );
      source?.emit("heartbeat", {}, "event-20");
    });

    expect(screen.getByTestId("notification-total")).toHaveTextContent("1");
    expect(screen.getByText("Workflow Failed")).toBeVisible();
    expect(screen.getByText("A workflow failed.")).toBeVisible();
    expect(screen.queryByText(/Duplicate must not/)).not.toBeInTheDocument();
    expect(screen.queryByText(/never-render-this|also-never-render-this/)).not.toBeInTheDocument();
    expect(screen.queryByText("Snapshot noise.")).not.toBeInTheDocument();
  });
});
