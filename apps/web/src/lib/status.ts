import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  CircleX,
  Clock3,
  LoaderCircle,
  WifiOff,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type Tone = "positive" | "warning" | "negative" | "info" | "neutral";

const POSITIVE = new Set([
  "AVAILABLE",
  "ONLINE",
  "HEALTHY",
  "ACTIVE",
  "SUCCEEDED",
  "PASSED",
  "COMPLETE",
  "COMPLETED",
  "CLEAN",
  "COMPATIBLE",
  "UP_TO_DATE",
]);
const WARNING = new Set([
  "DEGRADED",
  "DRAINING",
  "DRAINED",
  "QUEUED",
  "PENDING",
  "EXPIRING",
  "UNKNOWN",
  "RECONCILING",
  "UPGRADE_AVAILABLE",
  "UPGRADE_RECOMMENDED",
]);
const NEGATIVE = new Set([
  "OFFLINE",
  "FAILED",
  "ERROR",
  "DENIED",
  "REVOKED",
  "INCOMPATIBLE",
  "UPGRADE_REQUIRED",
  "UNSUPPORTED",
  "CANCELLED",
  "EXPIRED",
]);
const INFO = new Set([
  "RESERVED",
  "BUSY",
  "RUNNING",
  "STARTED",
  "DISPATCHED",
  "ACKNOWLEDGED",
  "CREATED",
]);

export function statusTone(status?: string): Tone {
  const normalized = (status ?? "UNKNOWN").toUpperCase();
  if (POSITIVE.has(normalized)) return "positive";
  if (WARNING.has(normalized)) return "warning";
  if (NEGATIVE.has(normalized)) return "negative";
  if (INFO.has(normalized)) return "info";
  return "neutral";
}

export function statusIcon(status?: string): LucideIcon {
  const tone = statusTone(status);
  if (tone === "positive") return CheckCircle2;
  if (tone === "negative") return (status ?? "").toUpperCase() === "OFFLINE" ? WifiOff : CircleX;
  if (tone === "warning") return AlertTriangle;
  if (tone === "info") return (status ?? "").toUpperCase() === "RUNNING" ? LoaderCircle : Clock3;
  return CircleDashed;
}

export function isActiveStatus(status?: string): boolean {
  return [
    "CREATED",
    "ALLOCATING",
    "WAITING_FOR_BENCH",
    "RESERVED",
    "QUEUED",
    "DISPATCHED",
    "ACCEPTED",
    "ACKNOWLEDGED",
    "RUNNING",
    "RECONCILING",
    "CANCELLING",
    "CANCEL_REQUESTED",
    "CLEANUP_PENDING",
    "UNKNOWN",
  ].includes((status ?? "").toUpperCase());
}
