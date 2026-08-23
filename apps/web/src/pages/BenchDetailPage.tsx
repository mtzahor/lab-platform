import {
  Activity,
  ArrowLeft,
  CalendarPlus,
  ChevronRight,
  Clock3,
  FileArchive,
  FileUp,
  Gauge,
  History,
  PlugZap,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  TerminalSquare,
  Workflow,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  apiFetch,
  apiBase,
  asRecord,
  booleanValue,
  errorMessage,
  idempotencyKey,
  labels,
  nested,
  records,
  stringList,
  stringValue,
  type ApiRecord,
} from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { useToast } from "../app/ToastProvider";
import {
  Button,
  ChipList,
  ConfirmDialog,
  DetailGrid,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  StatusBadge,
} from "../components/ui";
import { FlashDialog, ReservationDialog, SerialReadDialog } from "../features/benches/BenchDialogs";
import { ReservationCountdown } from "../features/reservations/ReservationCountdown";
import {
  reservationActionAllowed,
  reservationActionExplanation,
  reservationAdministrator,
  reservationOwnedByCaller,
} from "../features/reservations/permissions";
import { useApiDetail, useApiList, useApiMutation } from "../hooks/useApi";
import {
  formatBytes,
  formatDate,
  formatDuration,
  formatRelative,
  shortId,
  titleCase,
} from "../lib/format";

type DialogName = "reserve" | "flash" | "serial" | "reset" | "release" | "cancel" | null;

export function BenchDetailPage() {
  const { benchId = "" } = useParams();
  const decodedId = decodeURIComponent(benchId);
  const { can } = useAuth();
  const { notify } = useToast();
  const navigate = useNavigate();
  const [dialog, setDialog] = useState<DialogName>(null);
  const benchQuery = useApiDetail("benches", `/api/v1/benches/${encodeURIComponent(decodedId)}`);
  const canViewReservations = can("benches:reserve");
  const reservationQuery = useApiList(
    "reservations",
    `/api/v1/reservations?bench_id=${encodeURIComponent(decodedId)}&limit=50`,
    canViewReservations,
  );
  const queueQuery = useApiList(
    "reservation-queue",
    `/api/v1/benches/${encodeURIComponent(decodedId)}/queue`,
    canViewReservations,
  );
  const operationQuery = useApiList(
    "operations",
    `/api/v1/operations?bench_id=${encodeURIComponent(decodedId)}&limit=100`,
  );
  const timelineQuery = useApiList(
    "bench-timeline",
    `/api/v1/benches/${encodeURIComponent(decodedId)}/timeline?limit=100`,
  );
  const artifactQuery = useApiList(
    "artifacts",
    "/api/v1/artifacts?limit=500",
    can("artifacts:read"),
  );
  const bench = benchQuery.data;
  const specific = nested(bench, "permissions");
  const resourcePermissionKey: Record<string, string> = {
    probe: "operate",
    read_serial: "serial",
  };
  const allowed = (action: string, global: string) =>
    specific ? booleanValue(specific, resourcePermissionKey[action] ?? action) : can(global);
  const reservationRecords = records(reservationQuery.data).map((item) =>
    asRecord(item.reservation) ? item : { reservation: item },
  );
  const responseReservation = nested(bench, "current_reservation");
  const activeRecord = reservationRecords.find((item) => {
    const reservation = nested(item, "reservation");
    return (
      ["ACTIVE", "RESERVED"].includes(
        (
          stringValue(reservation, "status") ??
          stringValue(reservation, "state") ??
          stringValue(nested(item, "lease"), "state") ??
          ""
        ).toUpperCase(),
      ) && !stringValue(reservation, "released_at")
    );
  });
  const currentReservation = responseReservation ?? nested(activeRecord, "reservation");
  const currentReservationPermissionSource = responseReservation ?? activeRecord;
  const currentLease = nested(activeRecord, "lease") ?? nested(responseReservation, "lease");
  const currentLeaseVersion =
    stringValue(currentLease, "lease_version") ??
    stringValue(currentReservation, "lease_version") ??
    "1";
  const canReleaseCurrent = reservationActionAllowed(currentReservationPermissionSource, "release");
  const canExtendCurrent = reservationActionAllowed(currentReservationPermissionSource, "extend");
  const currentReservationOwned = reservationOwnedByCaller(currentReservationPermissionSource);
  const releaseAsAdministrator =
    reservationAdministrator(currentReservationPermissionSource) && !currentReservationOwned;
  const reservationEndsAt =
    stringValue(currentReservation, "ends_at") ??
    stringValue(currentReservation, "lease_valid_until") ??
    stringValue(currentLease, "valid_until");
  const operations = records(operationQuery.data);
  const queueEntries = records(queueQuery.data);
  const activeOperation =
    nested(bench, "active_operation") ??
    operations.find((item) =>
      ["CREATED", "DISPATCHED", "ACKNOWLEDGED", "RUNNING", "UNKNOWN", "CANCELLING"].includes(
        stringValue(item, "status") ?? "",
      ),
    );
  const recentArtifacts = records(artifactQuery.data)
    .filter(
      (item) =>
        stringValue(item, "bench_id") === decodedId ||
        stringValue(nested(item, "metadata"), "bench_id") === decodedId,
    )
    .slice(0, 8);
  const physical = stringValue(bench, "kind") === "PHYSICAL";
  const reserved = Boolean(currentReservation);
  const benchHealth = (stringValue(bench, "health") ?? "UNKNOWN").toUpperCase();
  const benchStatus = (stringValue(bench, "status") ?? "UNKNOWN").toUpperCase();
  const releaseMutation = useApiMutation(
    () =>
      apiFetch(
        `/api/v1/reservations/${stringValue(currentReservation, "id")}/${releaseAsAdministrator ? "revoke" : "release"}`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_lease_version: Number(currentLeaseVersion),
            idempotency_key: idempotencyKey("web-release"),
          }),
        },
      ),
    [["reservations"], ["benches"], ["overview"]],
  );
  const renewMutation = useApiMutation(
    () =>
      apiFetch(`/api/v1/reservations/${stringValue(currentReservation, "id")}/renew`, {
        method: "POST",
        body: JSON.stringify({
          expected_lease_version: Number(currentLeaseVersion),
          idempotency_key: idempotencyKey("web-renew"),
          lease_ttl_seconds: 1800,
        }),
      }),
    [["reservations"], ["benches"]],
  );
  const actionMutation = useApiMutation(
    ({ action, body }: { action: string; body?: ApiRecord }) =>
      apiFetch(`/api/v1/benches/${encodeURIComponent(decodedId)}/actions/${action}`, {
        method: "POST",
        body: JSON.stringify(body ?? {}),
      }),
    [["operations"], ["benches"], ["overview"]],
  );
  const cancelMutation = useApiMutation(
    () =>
      apiFetch(`/api/v1/operations/${stringValue(activeOperation, "id")}/cancel`, {
        method: "POST",
        body: JSON.stringify({ reason: "Cancelled from web dashboard" }),
      }),
    [["operations"], ["benches"], ["overview"]],
  );
  const leaveQueueMutation = useApiMutation(
    (entry: ApiRecord) =>
      apiFetch(`/api/v1/queue/${stringValue(entry, "id")}`, { method: "DELETE" }),
    [["reservation-queue"], ["overview"]],
  );
  async function release() {
    try {
      await releaseMutation.mutateAsync();
      notify({
        title: releaseAsAdministrator ? "Reservation revoked" : "Reservation released",
        message: `${decodedId} is available to the lab.`,
        tone: "success",
      });
      setDialog(null);
    } catch (error) {
      notify({ title: "Release failed", message: errorMessage(error), tone: "error" });
    }
  }
  async function renew() {
    try {
      await renewMutation.mutateAsync();
      notify({
        title: "Reservation renewed",
        message: "The control plane extended the reservation lease.",
        tone: "success",
      });
    } catch (error) {
      notify({ title: "Renewal failed", message: errorMessage(error), tone: "error" });
    }
  }
  async function runAction(action: "probe" | "reset") {
    try {
      const result = (await actionMutation.mutateAsync({ action })) as ApiRecord;
      notify({
        title: `${titleCase(action)} started`,
        message: `${decodedId} accepted the request.`,
        tone: "success",
        href: stringValue(result, "operation_id")
          ? `/operations/${stringValue(result, "operation_id")}`
          : undefined,
      });
      setDialog(null);
    } catch (error) {
      notify({
        title: `${titleCase(action)} wasn’t started`,
        message: errorMessage(error),
        tone: "error",
      });
    }
  }
  async function cancel() {
    try {
      await cancelMutation.mutateAsync();
      notify({
        title: "Cancellation requested",
        message: "The operation will stop at a safe boundary.",
        tone: "success",
      });
      setDialog(null);
    } catch (error) {
      notify({ title: "Cancellation failed", message: errorMessage(error), tone: "error" });
    }
  }
  async function leaveQueue(entry: ApiRecord) {
    try {
      await leaveQueueMutation.mutateAsync(entry);
      notify({
        title: "Queue request cancelled",
        message: `You left the queue for ${decodedId}.`,
        tone: "success",
      });
    } catch (error) {
      notify({ title: "Couldn’t leave queue", message: errorMessage(error), tone: "error" });
    }
  }

  const timeline = useMemo(() => {
    const provided = records(timelineQuery.data).length
      ? records(timelineQuery.data)
      : Array.isArray(bench?.timeline)
        ? bench.timeline.filter((item): item is ApiRecord => Boolean(asRecord(item)))
        : [];
    if (provided.length) return provided;
    return [
      ...operations.map((item) => ({
        ...item,
        event_type: "OPERATION",
        timestamp: stringValue(item, "created_at"),
      })),
      ...reservationRecords.map((item) => ({
        ...item,
        event_type: "RESERVATION",
        timestamp: stringValue(nested(item, "reservation"), "created_at"),
      })),
    ]
      .sort((a, b) => String(b.timestamp ?? "").localeCompare(String(a.timestamp ?? "")))
      .slice(0, 12);
  }, [bench?.timeline, operations, reservationRecords, timelineQuery.data]);

  if (benchQuery.isLoading)
    return (
      <>
        <PageHeader title="Bench" />
        <LoadingState label="Loading bench state" />
      </>
    );
  if (benchQuery.error || !bench)
    return (
      <>
        <PageHeader
          title="Bench unavailable"
          actions={
            <Button variant="ghost" icon={ArrowLeft} onClick={() => navigate("/benches")}>
              Back
            </Button>
          }
        />
        <ErrorState
          error={benchQuery.error ?? new Error("Bench not found")}
          retry={() => void benchQuery.refetch()}
        />
      </>
    );
  const agentName = stringValue(bench, "agent_slug") ?? stringValue(nested(bench, "agent"), "name");
  const location =
    stringValue(bench, "location") ?? stringValue(nested(bench, "agent"), "location");
  const labelEntries = Object.entries(labels(bench)).map(([key, value]) => `${key}=${value}`);
  return (
    <div className="page-stack">
      <Link className="back-link" to="/benches">
        <ArrowLeft size={15} /> Bench inventory
      </Link>
      <PageHeader
        eyebrow={decodedId}
        title={stringValue(bench, "name") ?? decodedId}
        description={
          <span className="headline-status">
            <StatusBadge status={stringValue(bench, "health")} />
            <span>Reservation:</span>
            <StatusBadge
              status={
                reserved
                  ? "RESERVED"
                  : (stringValue(bench, "availability") ?? stringValue(bench, "status"))
              }
            />
            <span>Seen {formatRelative(stringValue(bench, "last_seen_at"))}</span>
          </span>
        }
        actions={
          <div className="action-row">
            {!reserved && (
              <Button
                icon={CalendarPlus}
                onClick={() => setDialog("reserve")}
                disabled={!allowed("reserve", "benches:reserve")}
                title={
                  !allowed("reserve", "benches:reserve")
                    ? "You need the Reserver role on this bench."
                    : undefined
                }
              >
                Reserve
              </Button>
            )}
            {reserved && (
              <Button
                variant="secondary"
                onClick={() => setDialog("release")}
                disabled={!canReleaseCurrent}
                title={canReleaseCurrent ? undefined : reservationActionExplanation("release")}
              >
                {releaseAsAdministrator ? "Revoke" : "Release"}
              </Button>
            )}
            <Button
              variant="secondary"
              icon={RefreshCw}
              onClick={() =>
                void Promise.all([
                  benchQuery.refetch(),
                  operationQuery.refetch(),
                  reservationQuery.refetch(),
                ])
              }
            >
              Refresh
            </Button>
          </div>
        }
      />
      {benchStatus === "OFFLINE" && (
        <div className="warning-callout">
          <ShieldAlert size={20} />
          <div>
            <strong>Bench is offline</strong>
            <p>
              Last known state is shown. New operations cannot start until its Agent reconnects.
            </p>
          </div>
        </div>
      )}
      {benchStatus !== "OFFLINE" && ["WARNING", "UNHEALTHY", "DEGRADED"].includes(benchHealth) && (
        <div className="warning-callout">
          <ShieldAlert size={20} />
          <div>
            <strong>Bench health needs attention</strong>
            <p>
              Last known state is shown. Check Agent connectivity and recent activity before
              operating physical hardware.
            </p>
          </div>
        </div>
      )}
      <div className="detail-layout">
        <div className="detail-main">
          <Panel title="Summary" description="Inventory and target identity">
            <DetailGrid
              items={[
                { label: "Global bench ID", value: <code>{decodedId}</code> },
                {
                  label: "Agent",
                  value: agentName ? (
                    <Link to={`/agents/${stringValue(bench, "agent_id")}`}>{agentName}</Link>
                  ) : (
                    "—"
                  ),
                },
                { label: "Backend", value: stringValue(bench, "backend_id") },
                { label: "Target type", value: stringValue(bench, "target_type") },
                { label: "Kind", value: titleCase(stringValue(bench, "kind")) },
                { label: "Location", value: location },
                {
                  label: "Firmware",
                  value: stringValue(bench, "firmware_version") ? (
                    <code>{stringValue(bench, "firmware_version")}</code>
                  ) : (
                    "—"
                  ),
                },
                { label: "Last seen", value: formatDate(stringValue(bench, "last_seen_at")) },
              ]}
            />
            <div className="metadata-row">
              <div>
                <h3>Capabilities</h3>
                <ChipList values={stringList(bench, "capabilities")} limit={20} />
              </div>
              <div>
                <h3>Labels</h3>
                <ChipList values={labelEntries} limit={20} />
              </div>
            </div>
          </Panel>
          <Panel title="Current activity" description="Live work and reconciliation state">
            {activeOperation ? (
              <div className="activity-card">
                <div className="activity-card-head">
                  <span className="resource-icon">
                    <Activity size={18} />
                  </span>
                  <div>
                    <p className="eyebrow">
                      {titleCase(stringValue(activeOperation, "operation_type"))}
                    </p>
                    <Link to={`/operations/${stringValue(activeOperation, "id")}`}>
                      {shortId(stringValue(activeOperation, "id"))}
                    </Link>
                  </div>
                  <StatusBadge status={stringValue(activeOperation, "status")} />
                </div>
                <div className="progress-row">
                  <progress
                    value={Number(stringValue(activeOperation, "progress") ?? 0)}
                    max={100}
                    aria-label={`${stringValue(activeOperation, "progress") ?? "0"}% complete`}
                  />
                  <strong>{stringValue(activeOperation, "progress") ?? "0"}%</strong>
                </div>
                <p>
                  {stringValue(activeOperation, "message") ??
                    (stringValue(activeOperation, "status") === "UNKNOWN"
                      ? "Awaiting Agent reconciliation. Last known status is preserved."
                      : "Waiting for the next Agent update.")}
                </p>
                <div className="activity-meta">
                  <span>
                    <Clock3 size={14} />{" "}
                    {formatDuration(
                      stringValue(activeOperation, "started_at") ??
                        stringValue(activeOperation, "created_at"),
                    )}
                  </span>
                  <span>
                    <PlugZap size={14} /> Agent {agentName}
                  </span>
                </div>
                <div className="card-actions">
                  <Link
                    className="button button-secondary"
                    to={`/operations/${stringValue(activeOperation, "id")}`}
                  >
                    View operation <ChevronRight size={15} />
                  </Link>
                  {allowed("cancel_operation", "operations:cancel") && (
                    <Button variant="danger" onClick={() => setDialog("cancel")}>
                      Cancel operation
                    </Button>
                  )}
                </div>
              </div>
            ) : (
              <EmptyState
                title="Bench is idle"
                description="No operation or workflow is currently running."
                icon={Gauge}
              />
            )}
          </Panel>
          <Panel
            title="Timeline"
            description="Recent reservations, operations, health and firmware changes"
          >
            {timeline.length ? (
              <ol className="timeline">
                {timeline.map((item, index) => {
                  const kind = stringValue(item, "event_type") ?? "EVENT";
                  const resource = nested(item, "reservation") ?? item;
                  return (
                    <li key={stringValue(item, "id") ?? index}>
                      <span className="timeline-marker" />
                      <div>
                        <span>{titleCase(kind)}</span>
                        <strong>
                          {kind === "RESERVATION"
                            ? `${stringValue(resource, "owner")} reserved the bench`
                            : titleCase(
                                stringValue(resource, "operation_type") ??
                                  stringValue(resource, "message") ??
                                  kind,
                              )}
                        </strong>
                        <small>
                          {formatDate(
                            stringValue(item, "timestamp") ?? stringValue(resource, "created_at"),
                          )}
                        </small>
                      </div>
                      <StatusBadge status={stringValue(resource, "status")} />
                    </li>
                  );
                })}
              </ol>
            ) : (
              <EmptyState
                title="No recent activity"
                description="Reservations and operations will build this timeline."
                icon={History}
              />
            )}
          </Panel>
          <Panel
            title="Recent artifacts"
            description="Serial logs, flashing logs, test results and summaries"
            action={
              can("artifacts:read") ? (
                <Link
                  className="text-link"
                  to={`/artifacts?bench=${encodeURIComponent(decodedId)}`}
                >
                  All artifacts <ChevronRight size={14} />
                </Link>
              ) : undefined
            }
          >
            {recentArtifacts.length ? (
              <div className="artifact-list">
                {recentArtifacts.map((item) => (
                  <div key={stringValue(item, "id")}>
                    <span className="resource-icon">
                      <FileArchive size={16} />
                    </span>
                    <span>
                      <strong>{stringValue(item, "name")}</strong>
                      <small>
                        {titleCase(stringValue(item, "artifact_type"))} ·{" "}
                        {formatBytes(Number(stringValue(item, "size_bytes")))}
                      </small>
                    </span>
                    <a
                      className="button button-ghost"
                      href={`${apiBase}/api/v1/artifacts/${stringValue(item, "id")}/content`}
                      download
                    >
                      Download
                    </a>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No artifacts yet"
                description="Operation logs and results will appear here."
                icon={FileArchive}
              />
            )}
          </Panel>
        </div>
        <aside className="detail-side">
          <Panel title="Reservation">
            {currentReservation ? (
              <div className="reservation-card">
                <div>
                  <span>Current owner</span>
                  <strong>{stringValue(currentReservation, "owner")}</strong>
                </div>
                <div>
                  <span>Started</span>
                  <strong>
                    {formatRelative(
                      stringValue(currentReservation, "starts_at") ??
                        stringValue(currentReservation, "created_at"),
                    )}
                  </strong>
                </div>
                <div>
                  <span>Ends</span>
                  <strong>{formatRelative(reservationEndsAt)}</strong>
                </div>
                <ReservationCountdown endsAt={reservationEndsAt} />
                <div>
                  <span>Lease</span>
                  <strong>v{currentLeaseVersion}</strong>
                </div>
                <div className="reservation-actions">
                  <Button
                    variant="secondary"
                    onClick={renew}
                    disabled={!canExtendCurrent || renewMutation.isPending}
                    title={canExtendCurrent ? undefined : reservationActionExplanation("extend")}
                  >
                    {renewMutation.isPending ? "Extending…" : "Extend 30m"}
                  </Button>
                  <Button
                    variant="ghost"
                    onClick={() => setDialog("release")}
                    disabled={!canReleaseCurrent}
                    title={canReleaseCurrent ? undefined : reservationActionExplanation("release")}
                  >
                    {releaseAsAdministrator ? "Revoke" : "Release"}
                  </Button>
                </div>
                {(!canExtendCurrent || !canReleaseCurrent) && (
                  <p className="permission-note">
                    {releaseAsAdministrator
                      ? "Administrator access can revoke this reservation; only its owner can extend it."
                      : "Reservation changes are limited to its owner or a bench administrator."}
                  </p>
                )}
              </div>
            ) : (
              <EmptyState
                title="Available"
                description="No active reservation is reported for this bench."
                icon={CalendarPlus}
                action={
                  <Button
                    onClick={() => setDialog("reserve")}
                    disabled={!allowed("reserve", "benches:reserve")}
                  >
                    Reserve bench
                  </Button>
                }
              />
            )}
            <div className="queue-summary">
              <span>Queue length</span>
              <strong>{queueEntries.length}</strong>
              <small>{queueEntries.length === 1 ? "Request waiting" : "Requests waiting"}</small>
            </div>
            {queueEntries.length > 0 && (
              <div className="queue-list">
                {queueEntries.map((entry) => (
                  <div key={stringValue(entry, "id")}>
                    <span>
                      <strong>Position {stringValue(entry, "position") ?? "—"}</strong>
                      <small>
                        {stringValue(entry, "requester") ?? "Another requester"} ·{" "}
                        {Math.round(
                          Number(stringValue(entry, "requested_duration_seconds") ?? 0) / 60,
                        )}
                        m
                      </small>
                    </span>
                    {booleanValue(entry, "cancellable") && (
                      <Button
                        variant="ghost"
                        onClick={() => void leaveQueue(entry)}
                        disabled={leaveQueueMutation.isPending}
                      >
                        Leave
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Panel>
          <Panel
            title="Operate bench"
            description={
              reserved
                ? "Actions use the current reservation."
                : "Reserve this bench before mutating it."
            }
          >
            <div className="operation-buttons">
              {allowed("probe", "benches:operate") && (
                <Button
                  variant="secondary"
                  icon={Gauge}
                  onClick={() => void runAction("probe")}
                  disabled={actionMutation.isPending}
                >
                  Probe target
                </Button>
              )}
              {allowed("flash", "benches:flash") && (
                <Button
                  variant="secondary"
                  icon={FileUp}
                  onClick={() => setDialog("flash")}
                  disabled={!reserved}
                  title={!reserved ? "An active reservation is required." : undefined}
                >
                  Flash firmware
                </Button>
              )}
              {allowed("reset", "benches:reset") && (
                <Button
                  variant="secondary"
                  icon={RotateCcw}
                  onClick={() => setDialog("reset")}
                  disabled={!reserved}
                  title={!reserved ? "An active reservation is required." : undefined}
                >
                  Reset target
                </Button>
              )}
              {allowed("read_serial", "benches:serial") && (
                <Button
                  variant="secondary"
                  icon={TerminalSquare}
                  onClick={() => setDialog("serial")}
                  disabled={!reserved}
                >
                  Read serial
                </Button>
              )}
              {allowed("run_workflow", "workflows:run") && (
                <Link
                  className={`button button-primary ${!reserved ? "disabled" : ""}`}
                  aria-disabled={!reserved}
                  to={reserved ? `/workflows?bench=${encodeURIComponent(decodedId)}` : "#"}
                >
                  <Workflow size={16} /> Run workflow
                </Link>
              )}
            </div>
            {!allowed("reserve", "benches:reserve") && !allowed("flash", "benches:flash") && (
              <p className="permission-note">Your role has read-only access to this bench.</p>
            )}
          </Panel>
          <Panel title="State confidence">
            <div className="confidence-list">
              <div>
                <span>Agent</span>
                <StatusBadge
                  status={
                    stringValue(nested(bench, "agent"), "status") ??
                    (stringValue(bench, "status") === "OFFLINE" ? "OFFLINE" : "ONLINE")
                  }
                />
              </div>
              <div>
                <span>Inventory</span>
                <StatusBadge
                  status={
                    Date.now() - new Date(stringValue(bench, "updated_at") ?? 0).getTime() > 30_000
                      ? "UNKNOWN"
                      : "HEALTHY"
                  }
                  label={
                    Date.now() - new Date(stringValue(bench, "updated_at") ?? 0).getTime() > 30_000
                      ? "Stale"
                      : "Current"
                  }
                />
              </div>
              <small>
                Last update {formatRelative(stringValue(bench, "updated_at"))}. Unknown state is
                never converted to failure.
              </small>
            </div>
          </Panel>
        </aside>
      </div>
      <ReservationDialog
        bench={bench}
        open={dialog === "reserve"}
        onClose={() => setDialog(null)}
      />
      <FlashDialog bench={bench} open={dialog === "flash"} onClose={() => setDialog(null)} />
      <SerialReadDialog bench={bench} open={dialog === "serial"} onClose={() => setDialog(null)} />
      <ConfirmDialog
        open={dialog === "reset"}
        title={`Reset ${stringValue(bench, "name")}?`}
        message={
          physical ? (
            <>
              Reset physical bench <strong>{decodedId}</strong>? The target will briefly disconnect
              and any in-memory state may be lost.
            </>
          ) : (
            <>
              Reset SimLab bench <strong>{decodedId}</strong>?
            </>
          )
        }
        confirmLabel="Reset target"
        onConfirm={() => void runAction("reset")}
        onClose={() => setDialog(null)}
        busy={actionMutation.isPending}
      />
      <ConfirmDialog
        open={dialog === "release"}
        title={`${releaseAsAdministrator ? "Revoke" : "Release"} ${stringValue(bench, "name")}?`}
        message={
          <>
            {releaseAsAdministrator ? "Revoke" : "Release"} <strong>{decodedId}</strong>? Any
            operation still using this reservation may fail.
          </>
        }
        confirmLabel={releaseAsAdministrator ? "Revoke reservation" : "Release reservation"}
        onConfirm={() => void release()}
        onClose={() => setDialog(null)}
        busy={releaseMutation.isPending}
      />
      <ConfirmDialog
        open={dialog === "cancel"}
        title={`Cancel ${titleCase(stringValue(activeOperation, "operation_type"))}?`}
        message={
          <>
            Cancel operation <strong>{shortId(stringValue(activeOperation, "id"))}</strong> on{" "}
            {decodedId}? Cleanup will run where supported.
          </>
        }
        confirmLabel="Cancel operation"
        onConfirm={() => void cancel()}
        onClose={() => setDialog(null)}
        busy={cancelMutation.isPending}
      />
    </div>
  );
}
