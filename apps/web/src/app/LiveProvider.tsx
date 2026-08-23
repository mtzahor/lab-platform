import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type PropsWithChildren,
} from "react";
import { apiBase, asRecord, stringValue } from "../api/client";
import { useAuth } from "./AuthProvider";
import { LIVE_RESOURCE_EVENT_NAMES, liveNotificationFromEvent } from "./liveNotifications";
import { useNotifications } from "./NotificationProvider";

export type LiveState = "connecting" | "live" | "polling" | "stale";
type LiveContextValue = { state: LiveState; lastEventAt?: string; pollingIntervalMs: number };
const LiveContext = createContext<LiveContextValue>({
  state: "connecting",
  pollingIntervalMs: 5_000,
});

const EVENT_QUERY_KEYS: Record<string, string[]> = {
  agent: ["agents", "overview"],
  bench: ["benches", "overview"],
  reservation: ["reservations", "benches", "overview"],
  operation: ["operations", "workflow-runs", "overview"],
  workflow: ["workflows", "workflow-runs", "overview"],
  ci_session: ["ci-sessions", "overview"],
  artifact: ["artifacts"],
  audit: ["audit"],
};

const OVERVIEW_QUERY_KEYS = [
  "overview",
  "operations",
  "workflow-runs",
  "workflow-results",
  "serial",
  "benches",
  "bench-timeline",
  "reservations",
  "reservation-queue",
  "agents",
  "agent-timeline",
  "ci-sessions",
  "artifacts",
];

export function liveQueryRoots(eventType: string, payload: unknown): string[] {
  const record = asRecord(payload);
  const type = (
    stringValue(record, "type") ??
    stringValue(record, "resource_type") ??
    eventType
  ).toLowerCase();
  if (type.includes("overview")) return OVERVIEW_QUERY_KEYS;
  const matched = Object.entries(EVENT_QUERY_KEYS).find(([prefix]) => type.includes(prefix));
  return matched?.[1] ?? OVERVIEW_QUERY_KEYS;
}

export function LiveProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const auth = useAuth();
  const { pushNotification, clearNotifications } = useNotifications();
  const [state, setState] = useState<LiveState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<string>();
  const failures = useRef(0);
  const lastEventRef = useRef<string | undefined>(undefined);
  const seenEventIds = useRef(new Set<string>());

  useEffect(() => {
    if (!auth.authenticated || !auth.sseEnabled || typeof EventSource === "undefined") {
      setState(auth.authenticated ? "polling" : "connecting");
      if (!auth.authenticated) clearNotifications();
      failures.current = 0;
      lastEventRef.current = undefined;
      setLastEventAt(undefined);
      return;
    }
    lastEventRef.current = undefined;
    setLastEventAt(undefined);
    setState("connecting");
    failures.current = 0;
    const source = new EventSource(`${apiBase}/api/v1/events`, { withCredentials: true });
    const silenceDeadlineMs = Math.max(15_000, auth.pollingFallbackSeconds * 3_000);
    let firstEventTimer: number | undefined;
    let eventReceivedSinceOpen = false;
    const clearFirstEventTimer = () => {
      if (firstEventTimer !== undefined) {
        window.clearTimeout(firstEventTimer);
        firstEventTimer = undefined;
      }
    };
    const staleTimer = window.setInterval(
      () => {
        if (
          lastEventRef.current &&
          Date.now() - new Date(lastEventRef.current).getTime() > silenceDeadlineMs
        )
          setState("stale");
      },
      Math.max(5_000, auth.pollingFallbackSeconds * 1_000),
    );
    const markEventReceived = () => {
      eventReceivedSinceOpen = true;
      clearFirstEventTimer();
      failures.current = 0;
      setState("live");
      const receivedAt = new Date().toISOString();
      lastEventRef.current = receivedAt;
      setLastEventAt(receivedAt);
    };
    source.onopen = () => {
      clearFirstEventTimer();
      eventReceivedSinceOpen = false;
      setState("live");
      firstEventTimer = window.setTimeout(() => {
        if (!eventReceivedSinceOpen) setState("stale");
      }, silenceDeadlineMs);
    };
    source.onerror = () => {
      clearFirstEventTimer();
      failures.current += 1;
      setState(failures.current >= 2 ? "polling" : "connecting");
    };
    const handleEvent = (event: MessageEvent<string>) => {
      markEventReceived();
      if (event.lastEventId) {
        if (seenEventIds.current.has(event.lastEventId)) return;
        seenEventIds.current.add(event.lastEventId);
        if (seenEventIds.current.size > 250) {
          const oldest = seenEventIds.current.values().next().value as string | undefined;
          if (oldest) seenEventIds.current.delete(oldest);
        }
      }
      try {
        const payload = asRecord(JSON.parse(event.data));
        for (const key of liveQueryRoots(event.type, payload))
          void queryClient.invalidateQueries({ queryKey: [key] });
        const notification = liveNotificationFromEvent(event.type, payload, event.lastEventId);
        if (notification) pushNotification(notification);
      } catch {
        for (const key of OVERVIEW_QUERY_KEYS)
          void queryClient.invalidateQueries({ queryKey: [key] });
      }
    };
    const handleHeartbeat = () => {
      markEventReceived();
    };
    source.onmessage = handleEvent;
    source.addEventListener("overview.snapshot", handleEvent as EventListener);
    source.addEventListener("overview.updated", handleEvent as EventListener);
    source.addEventListener("heartbeat", handleHeartbeat as EventListener);
    for (const eventName of LIVE_RESOURCE_EVENT_NAMES)
      source.addEventListener(eventName, handleEvent as EventListener);
    return () => {
      clearFirstEventTimer();
      window.clearInterval(staleTimer);
      source.removeEventListener("overview.snapshot", handleEvent as EventListener);
      source.removeEventListener("overview.updated", handleEvent as EventListener);
      source.removeEventListener("heartbeat", handleHeartbeat as EventListener);
      for (const eventName of LIVE_RESOURCE_EVENT_NAMES)
        source.removeEventListener(eventName, handleEvent as EventListener);
      source.close();
    };
  }, [
    auth.authenticated,
    auth.pollingFallbackSeconds,
    auth.sseEnabled,
    clearNotifications,
    pushNotification,
    queryClient,
  ]);

  return (
    <LiveContext.Provider
      value={{ state, lastEventAt, pollingIntervalMs: auth.pollingFallbackSeconds * 1_000 }}
    >
      {children}
    </LiveContext.Provider>
  );
}

export function useLive() {
  return useContext(LiveContext);
}
