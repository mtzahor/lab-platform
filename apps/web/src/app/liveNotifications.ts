import { asRecord, stringValue, type ApiRecord } from "../api/client";
import { titleCase } from "../lib/format";
import {
  safeNotificationText,
  type NotificationDraft,
  type NotificationTone,
} from "./NotificationProvider";

export const LIVE_RESOURCE_EVENT_NAMES = [
  "operation.updated",
  "workflow.updated",
  "serial.updated",
  "agent.updated",
  "bench.updated",
  "reservation.updated",
  "ci_session.updated",
] as const;

type LiveResource = (typeof LIVE_RESOURCE_EVENT_NAMES)[number];

const RESOURCE_LABELS: Record<LiveResource, string> = {
  "operation.updated": "Operation",
  "workflow.updated": "Workflow",
  "serial.updated": "Serial log",
  "agent.updated": "Agent",
  "bench.updated": "Bench",
  "reservation.updated": "Reservation",
  "ci_session.updated": "CI session",
};

function liveResource(eventType: string, payload: ApiRecord): LiveResource | undefined {
  const payloadType = stringValue(payload, "type");
  const candidate = (payloadType ?? eventType).toLowerCase();
  return LIVE_RESOURCE_EVENT_NAMES.find((name) => name === candidate);
}

function safeStatus(value: unknown): string | undefined {
  const status = safeNotificationText(value, 40);
  return status && /^[a-z][a-z0-9 _-]{0,39}$/i.test(status) ? status : undefined;
}

function firstIdentifier(records: ApiRecord[], keys: string[]): string | undefined {
  for (const record of records) {
    for (const key of keys) {
      const value = stringValue(record, key);
      if (value) return value;
    }
  }
  return undefined;
}

function notificationHref(resource: LiveResource, contract: ApiRecord): string | undefined {
  const details = asRecord(contract.data) ?? contract;
  const records = [details, contract];
  if (resource === "operation.updated" || resource === "serial.updated") {
    const id = firstIdentifier(records, ["operation_id"]);
    return id ? `/operations/${encodeURIComponent(id)}` : "/operations";
  }
  if (resource === "workflow.updated") {
    const id = firstIdentifier(records, ["workflow_run_id", "run_id", "operation_id"]);
    return id ? `/workflow-runs/${encodeURIComponent(id)}` : "/workflows";
  }
  if (resource === "agent.updated") {
    const id = firstIdentifier(records, ["agent_id"]);
    return id ? `/agents/${encodeURIComponent(id)}` : "/agents";
  }
  if (resource === "bench.updated") {
    const id = firstIdentifier(records, ["bench_id"]);
    return id ? `/benches/${encodeURIComponent(id)}` : "/benches";
  }
  if (resource === "ci_session.updated") {
    const id = firstIdentifier(records, ["ci_session_id", "session_id"]);
    return id ? `/ci-sessions/${encodeURIComponent(id)}` : "/ci-sessions";
  }
  return "/reservations";
}

function notificationTone(status?: string): NotificationTone {
  const normalized = status?.toUpperCase() ?? "";
  if (/FAILED|ERROR|DENIED|CLEANUP_FAILED/.test(normalized)) return "error";
  if (/WARNING|CANCELLED|DISCONNECTED|OFFLINE|EXPIRING|DEGRADED|UNKNOWN/.test(normalized))
    return "warning";
  if (
    /SUCCEEDED|COMPLETED|COMPLETE|ACTIVE|ONLINE|CONNECTED|CREATED|PROMOTED|READY/.test(normalized)
  )
    return "success";
  return "info";
}

export function liveNotificationFromEvent(
  eventType: string,
  payload: unknown,
  lastEventId: string,
): NotificationDraft | undefined {
  const envelope = asRecord(payload);
  if (!envelope) return undefined;
  const resource = liveResource(eventType, envelope);
  if (!resource) return undefined;
  const contract = asRecord(envelope.data) ?? envelope;
  if (contract.notify !== true) return undefined;
  const summary = safeNotificationText(contract.summary, 240);
  if (!summary) return undefined;
  const status = safeStatus(contract.status);
  const title = status
    ? `${RESOURCE_LABELS[resource]} ${titleCase(status)}`
    : `${RESOURCE_LABELS[resource]} update`;
  return {
    dedupeKey: safeNotificationText(lastEventId, 200),
    title,
    message: summary,
    tone: notificationTone(status),
    href: notificationHref(resource, contract),
    createdAt: stringValue(envelope, "timestamp") ?? stringValue(contract, "timestamp"),
  };
}
