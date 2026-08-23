const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

export function titleCase(value?: string): string {
  if (!value) return "Unknown";
  return value
    .toLowerCase()
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : DATE_TIME.format(date);
}

export function formatRelative(value?: string): string {
  if (!value) return "Never";
  const then = new Date(value).getTime();
  if (!Number.isFinite(then)) return "Unknown";
  const delta = then - Date.now();
  const seconds = Math.round(delta / 1_000);
  if (Math.abs(seconds) < 60) return RELATIVE.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return RELATIVE.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return RELATIVE.format(hours, "hour");
  return RELATIVE.format(Math.round(hours / 24), "day");
}

export function formatDuration(start?: string, end?: string): string {
  if (!start) return "—";
  const duration = Math.max(
    0,
    (end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime(),
  );
  if (!Number.isFinite(duration)) return "—";
  const seconds = Math.round(duration / 1_000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatBytes(value?: number): string {
  if (value === undefined || !Number.isFinite(value)) return "—";
  if (value < 1_000) return `${value} B`;
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1)} KB`;
  if (value < 1_000_000_000) return `${(value / 1_000_000).toFixed(1)} MB`;
  return `${(value / 1_000_000_000).toFixed(1)} GB`;
}

export function isStale(value?: string, thresholdMs = 30_000): boolean {
  if (!value) return true;
  return Date.now() - new Date(value).getTime() > thresholdMs;
}

export function shortId(value?: string): string {
  if (!value) return "—";
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

export function copyText(value: string): Promise<void> {
  return navigator.clipboard.writeText(value);
}
