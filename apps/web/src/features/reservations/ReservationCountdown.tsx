import { AlertTriangle, Clock3 } from "lucide-react";
import { useEffect, useState } from "react";
import { formatDate } from "../../lib/format";

export const RESERVATION_EXPIRY_WARNING_MS = 10 * 60_000;

export function reservationCountdownText(endsAt: string, now = Date.now()): string | undefined {
  const end = new Date(endsAt).getTime();
  if (!Number.isFinite(end)) return undefined;
  const remainingSeconds = Math.ceil((end - now) / 1_000);
  if (remainingSeconds <= 0) return "Expiry awaiting server confirmation";
  const hours = Math.floor(remainingSeconds / 3_600);
  const minutes = Math.floor((remainingSeconds % 3_600) / 60);
  const seconds = remainingSeconds % 60;
  if (hours > 0) return `Ends in ${hours}h ${minutes}m`;
  if (minutes > 0) return `Ends in ${minutes}m ${seconds}s`;
  return `Ends in ${seconds}s`;
}

export function ReservationCountdown({ endsAt }: { endsAt?: string }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!endsAt) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [endsAt]);

  if (!endsAt) return null;
  const end = new Date(endsAt).getTime();
  const label = reservationCountdownText(endsAt, now);
  if (!label) return null;
  const remaining = end - now;
  const warning = remaining <= RESERVATION_EXPIRY_WARNING_MS;
  const Icon = warning ? AlertTriangle : Clock3;

  return (
    <div
      className={`reservation-expiry ${warning ? "reservation-expiry-warning" : ""}`}
      role="timer"
      title={`Server-reported expiry: ${formatDate(endsAt)}`}
    >
      <Icon size={16} />
      <span>
        <strong>{label}</strong>
        {warning && remaining > 0 && <small>Save work or extend the lease soon.</small>}
        {remaining <= 0 && <small>Waiting for authoritative server state.</small>}
      </span>
    </div>
  );
}
