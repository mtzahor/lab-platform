import { zodResolver } from "@hookform/resolvers/zod";
import { CheckCircle2, FileUp, ShieldAlert, UploadCloud } from "lucide-react";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import {
  ApiError,
  apiFetch,
  errorMessage,
  idempotencyKey,
  stringValue,
  type ApiRecord,
} from "../../api/client";
import { useToast } from "../../app/ToastProvider";
import { useAuth } from "../../app/AuthProvider";
import { Button, Dialog, Field, StatusBadge } from "../../components/ui";
import { formatBytes } from "../../lib/format";
import { useApiMutation } from "../../hooks/useApi";

const reservationSchema = z.object({
  durationMinutes: z.coerce
    .number()
    .int()
    .min(5, "Use at least 5 minutes")
    .max(1_440, "Maximum reservation is 24 hours"),
  description: z.string().max(500).optional(),
});
type ReservationFields = z.output<typeof reservationSchema>;
type ReservationInput = z.input<typeof reservationSchema>;

export function ReservationDialog({
  bench,
  open,
  onClose,
}: {
  bench: ApiRecord;
  open: boolean;
  onClose: () => void;
}) {
  const { notify } = useToast();
  const [mode, setMode] = useState<"now" | "future">("now");
  const [startsAt, setStartsAt] = useState("");
  const [queueIfBusy, setQueueIfBusy] = useState(false);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<ReservationInput, unknown, ReservationFields>({
    resolver: zodResolver(reservationSchema),
    defaultValues: { durationMinutes: 60 },
  });
  const benchId = stringValue(bench, "id") ?? "";
  const mutation = useApiMutation(
    (values: ReservationFields) =>
      apiFetch("/api/v1/reservations", {
        method: "POST",
        body: JSON.stringify({
          bench_id: benchId,
          idempotency_key: idempotencyKey("web-reserve"),
          reservation_duration_seconds: values.durationMinutes * 60,
          starts_at: mode === "future" ? new Date(startsAt).toISOString() : null,
          description: values.description || null,
        }),
      }),
    [["benches"], ["reservations"], ["overview"]],
  );
  async function submit(values: ReservationFields) {
    const startTime = new Date(startsAt).getTime();
    if (
      mode === "future" &&
      (!startsAt || !Number.isFinite(startTime) || startTime <= Date.now())
    ) {
      notify({
        title: "Choose a future start time",
        message: "Scheduled reservations must start in the future.",
        tone: "error",
      });
      return;
    }
    try {
      await mutation.mutateAsync(values);
      notify({
        title: mode === "future" ? "Reservation scheduled" : "Bench reserved",
        message:
          mode === "future"
            ? `${benchId} is scheduled for ${new Date(startsAt).toLocaleString()}.`
            : `${benchId} is ready for your work.`,
        tone: "success",
        href: `/benches/${encodeURIComponent(benchId)}`,
      });
      reset();
      setStartsAt("");
      setMode("now");
      onClose();
    } catch (error) {
      if (queueIfBusy && error instanceof ApiError && error.code === "BENCH_ALREADY_RESERVED") {
        try {
          const entry = await apiFetch<ApiRecord>(
            `/api/v1/benches/${encodeURIComponent(benchId)}/queue`,
            {
              method: "POST",
              body: JSON.stringify({
                duration_seconds: values.durationMinutes * 60,
                description: values.description || null,
                idempotency_key: idempotencyKey("web-queue"),
              }),
            },
          );
          notify({
            title: "Added to reservation queue",
            message: `You are in position ${stringValue(entry, "position") ?? "—"} for ${benchId}.`,
            tone: "success",
            href: `/benches/${encodeURIComponent(benchId)}`,
          });
          reset();
          onClose();
          return;
        } catch (queueError) {
          notify({
            title: "Queue request failed",
            message: errorMessage(queueError),
            tone: "error",
          });
          return;
        }
      }
      notify({ title: "Reservation failed", message: errorMessage(error), tone: "error" });
    }
  }
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Reserve ${stringValue(bench, "name") ?? benchId}`}
      description="The control plane remains authoritative for availability and expiry."
    >
      <div className="segment-control" aria-label="Reservation timing">
        <button
          type="button"
          className={mode === "now" ? "active" : ""}
          onClick={() => setMode("now")}
        >
          Start now
        </button>
        <button
          type="button"
          className={mode === "future" ? "active" : ""}
          onClick={() => setMode("future")}
        >
          Schedule
        </button>
      </div>
      <form onSubmit={handleSubmit(submit)}>
        <div className="target-summary">
          <span>
            <strong>{benchId}</strong>
            <small>
              {stringValue(bench, "target_type") ?? "Any target"} ·{" "}
              {stringValue(bench, "kind") ?? "Unknown kind"}
            </small>
          </span>
          <StatusBadge status={stringValue(bench, "health") ?? stringValue(bench, "status")} />
        </div>
        <Field label="Duration" error={errors.durationMinutes?.message} required>
          <div className="input-suffix">
            <input type="number" min={5} max={1440} {...register("durationMinutes")} />
            <span>minutes</span>
          </div>
        </Field>
        {mode === "future" && (
          <Field label="Start time" hint="Shown in your browser’s local timezone" required>
            <input
              type="datetime-local"
              value={startsAt}
              min={new Date(Date.now() + 60_000).toISOString().slice(0, 16)}
              onChange={(event) => setStartsAt(event.target.value)}
            />
          </Field>
        )}
        <Field
          label="Description"
          error={errors.description?.message}
          hint="Visible to other lab users"
        >
          <textarea rows={3} placeholder="What are you testing?" {...register("description")} />
        </Field>
        {mode === "now" && (
          <label className="check-row">
            <input
              type="checkbox"
              checked={queueIfBusy}
              onChange={(event) => setQueueIfBusy(event.target.checked)}
            />
            <span>
              <strong>Join queue if busy</strong>
              <small>
                If the immediate reservation conflicts, add me to this bench’s FIFO queue.
              </small>
            </span>
          </label>
        )}
        <div className="dialog-actions">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending
              ? mode === "future"
                ? "Scheduling…"
                : "Reserving…"
              : mode === "future"
                ? "Schedule reservation"
                : "Reserve bench"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

const flashSchema = z.object({
  version: z.string().max(200).optional(),
  confirmation: z.string().optional(),
});
type FlashFields = z.infer<typeof flashSchema>;

async function fileSha256(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function FlashDialog({
  bench,
  open,
  onClose,
}: {
  bench: ApiRecord;
  open: boolean;
  onClose: () => void;
}) {
  const { notify } = useToast();
  const { maximumFirmwareSizeMb } = useAuth();
  const [file, setFile] = useState<File>();
  const [checksum, setChecksum] = useState<string>();
  const [computeError, setComputeError] = useState<string>();
  const [confirmed, setConfirmed] = useState(false);
  const benchId = stringValue(bench, "id") ?? "";
  const physical = (stringValue(bench, "kind") ?? "").toUpperCase() === "PHYSICAL";
  const {
    register,
    watch,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FlashFields>({ resolver: zodResolver(flashSchema) });
  const typed = watch("confirmation");
  useEffect(() => {
    if (!file) {
      setChecksum(undefined);
      return;
    }
    setComputeError(undefined);
    void fileSha256(file)
      .then(setChecksum)
      .catch(() => setComputeError("Checksum could not be calculated in this browser."));
  }, [file]);
  const mutation = useApiMutation(
    async (values: FlashFields) => {
      if (!file) throw new Error("Select a firmware file.");
      if (file.size > maximumFirmwareSizeMb * 1024 * 1024)
        throw new Error(`Firmware exceeds the ${maximumFirmwareSizeMb} MB upload limit.`);
      const form = new FormData();
      form.set("firmware", file);
      if (values.version) form.set("version", values.version);
      return apiFetch(`/api/v1/benches/${encodeURIComponent(benchId)}/actions/flash`, {
        method: "POST",
        body: form,
      });
    },
    [["benches"], ["operations"], ["overview"]],
  );
  async function submit(values: FlashFields) {
    if (!file) {
      notify({
        title: "Choose firmware",
        message: "Select the firmware file to flash.",
        tone: "error",
      });
      return;
    }
    if (!confirmed || (physical && typed !== benchId)) return;
    try {
      const result = (await mutation.mutateAsync(values)) as ApiRecord;
      notify({
        title: "Flash started",
        message: `Firmware is being flashed to ${benchId}.`,
        tone: "success",
        href: stringValue(result, "operation_id")
          ? `/operations/${stringValue(result, "operation_id")}`
          : undefined,
      });
      reset();
      setFile(undefined);
      setConfirmed(false);
      onClose();
    } catch (error) {
      notify({ title: "Flash wasn’t started", message: errorMessage(error), tone: "error" });
    }
  }
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Flash firmware to ${stringValue(bench, "name") ?? benchId}`}
      description="Firmware commands are selected by the Agent configuration; arbitrary commands are never accepted."
      size="wide"
    >
      <form onSubmit={handleSubmit(submit)}>
        {physical && (
          <div className="warning-callout">
            <ShieldAlert size={20} />
            <div>
              <strong>Physical hardware</strong>
              <p>
                Flashing can leave this target temporarily unavailable. Confirm the exact bench
                before continuing.
              </p>
            </div>
          </div>
        )}
        <div className="flash-grid">
          <div className="upload-zone">
            <input
              id="firmware-file"
              type="file"
              onChange={(event) => setFile(event.target.files?.[0])}
              accept=".bin,.hex,.elf,.uf2,application/octet-stream"
            />
            <label htmlFor="firmware-file">
              <UploadCloud size={24} />
              <strong>{file ? file.name : "Choose firmware file"}</strong>
              <span>
                {file
                  ? formatBytes(file.size)
                  : `BIN, HEX, ELF or UF2 · up to ${maximumFirmwareSizeMb} MB`}
              </span>
            </label>
          </div>
          <div className="upload-facts">
            <div>
              <span>Bench</span>
              <strong>{benchId}</strong>
            </div>
            <div>
              <span>Target</span>
              <strong>{stringValue(bench, "target_type") ?? "Unknown"}</strong>
            </div>
            <div>
              <span>Flash mode</span>
              <strong>Agent configured</strong>
            </div>
            <div>
              <span>SHA-256</span>
              <code>
                {checksum
                  ? `${checksum.slice(0, 16)}…${checksum.slice(-8)}`
                  : (computeError ?? (file ? "Calculating…" : "Select a file"))}
              </code>
            </div>
          </div>
        </div>
        <Field
          label="Firmware version"
          error={errors.version?.message}
          hint="Optional label recorded with the operation"
        >
          <input placeholder="e.g. 2.4.1-rc3" {...register("version")} />
        </Field>
        {physical && (
          <Field
            label={`Type ${benchId} to confirm`}
            error={typed && typed !== benchId ? "Bench ID must match exactly" : undefined}
            required
          >
            <input autoComplete="off" {...register("confirmation")} />
          </Field>
        )}
        <label className="check-row">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          <span>
            <strong>I checked the target and firmware</strong>
            <small>
              Flash {file?.name ?? "the selected file"} to {benchId}.
            </small>
          </span>
        </label>
        <div className="dialog-actions">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            disabled={!file || !confirmed || (physical && typed !== benchId) || mutation.isPending}
            icon={FileUp}
          >
            {mutation.isPending ? "Uploading…" : "Flash firmware"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

export function SerialReadDialog({
  bench,
  open,
  onClose,
}: {
  bench: ApiRecord;
  open: boolean;
  onClose: () => void;
}) {
  const { notify } = useToast();
  const [seconds, setSeconds] = useState(10);
  const [maxLines, setMaxLines] = useState(500);
  const benchId = stringValue(bench, "id") ?? "";
  const mutation = useApiMutation(
    () =>
      apiFetch(`/api/v1/benches/${encodeURIComponent(benchId)}/actions/read-serial`, {
        method: "POST",
        body: JSON.stringify({ timeout_seconds: seconds, max_lines: maxLines }),
      }),
    [["operations"]],
  );
  async function start() {
    try {
      const result = (await mutation.mutateAsync()) as ApiRecord;
      notify({
        title: "Serial capture started",
        message: `Capturing up to ${maxLines} lines from ${benchId}.`,
        tone: "success",
        href: stringValue(result, "operation_id")
          ? `/operations/${stringValue(result, "operation_id")}`
          : undefined,
      });
      onClose();
    } catch (error) {
      notify({ title: "Serial capture failed", message: errorMessage(error), tone: "error" });
    }
  }
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Read serial from ${stringValue(bench, "name") ?? benchId}`}
      description="This is a bounded read-only capture; interactive shell input is not available."
    >
      <div className="two-column-form">
        <Field label="Capture time">
          <div className="input-suffix">
            <input
              type="number"
              min={1}
              max={3600}
              value={seconds}
              onChange={(event) => setSeconds(event.target.valueAsNumber)}
            />
            <span>seconds</span>
          </div>
        </Field>
        <Field label="Maximum lines">
          <input
            type="number"
            min={1}
            max={100000}
            value={maxLines}
            onChange={(event) => setMaxLines(event.target.valueAsNumber)}
          />
        </Field>
      </div>
      <div className="info-callout">
        <CheckCircle2 size={18} />
        <span>The viewer caps its live buffer. Full output remains available as an artifact.</span>
      </div>
      <div className="dialog-actions">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button onClick={start} disabled={mutation.isPending}>
          {mutation.isPending ? "Starting…" : "Start capture"}
        </Button>
      </div>
    </Dialog>
  );
}
