import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { BellRing, Bot, CheckCircle2, Clock3, Gauge, RefreshCw, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  asRecord,
  booleanValue,
  errorMessage,
  generatedApi,
  nested,
  numberValue,
  records,
  stringValue,
  type ApiRecord,
} from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { useToast } from "../app/ToastProvider";
import {
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  MetricCard,
  PageHeader,
  Panel,
  StatusBadge,
} from "../components/ui";
import { agentUpgradeAssessment } from "../features/agents/upgrade";
import { useApiDetail, useApiList, useApiMutation } from "../hooks/useApi";
import { formatRelative, shortId, titleCase } from "../lib/format";
import type { Tone } from "../lib/status";

const PERCENT = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumFractionDigits: 1,
});

const WINDOW_OPTIONS = [
  { hours: 24, label: "Last 24 hours" },
  { hours: 24 * 7, label: "Last 7 days" },
  { hours: 24 * 30, label: "Last 30 days" },
  { hours: 24 * 90, label: "Last 90 days" },
];

type FleetVersion = {
  version: string;
  count: number;
  protocols: Set<string>;
  blocked: number;
  attention: number;
  unknown: number;
};

function optionalNumber(value: unknown, key: string): number | undefined {
  const raw = asRecord(value)?.[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : undefined;
}

function formatPercent(value?: number): string {
  return value === undefined ? "—" : PERCENT.format(value);
}

function formatSeconds(value?: number): string {
  if (value === undefined) return "No samples";
  if (value < 60) return `${Math.round(value)}s`;
  const minutes = Math.round(value / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function alertResourcePath(alert: ApiRecord): string | undefined {
  const resourceId = stringValue(alert, "resource_id");
  if (!resourceId) return undefined;
  switch (stringValue(alert, "resource_type")?.toLowerCase()) {
    case "agent":
      return `/agents/${resourceId}`;
    case "bench":
      return `/benches/${encodeURIComponent(resourceId)}`;
    case "operation":
      return `/operations/${resourceId}`;
    default:
      return undefined;
  }
}

function reliabilityTone(successRate?: number): Tone {
  if (successRate === undefined) return "neutral";
  if (successRate >= 0.95) return "positive";
  if (successRate >= 0.8) return "warning";
  return "negative";
}

function severityRank(alert: ApiRecord): number {
  return { CRITICAL: 0, WARNING: 1, INFO: 2 }[stringValue(alert, "severity") ?? ""] ?? 3;
}

function agentWorkBlocked(agent: ApiRecord): boolean {
  const assessment = agentUpgradeAssessment(agent);
  return (
    assessment.workAllowed === false ||
    ["upgrade_required", "unsupported"].includes(assessment.status ?? "") ||
    stringValue(agent, "status") === "INCOMPATIBLE"
  );
}

function fleetVersions(agents: ApiRecord[]): FleetVersion[] {
  const groups = new Map<string, FleetVersion>();
  for (const agent of agents) {
    const version = stringValue(agent, "version") ?? "Unknown";
    const group = groups.get(version) ?? {
      version,
      count: 0,
      protocols: new Set<string>(),
      blocked: 0,
      attention: 0,
      unknown: 0,
    };
    const assessment = agentUpgradeAssessment(agent);
    group.count += 1;
    const protocol = stringValue(agent, "protocol_version");
    if (protocol) group.protocols.add(protocol);
    if (agentWorkBlocked(agent)) {
      group.blocked += 1;
    } else if (assessment.status && assessment.status !== "up_to_date") {
      group.attention += 1;
    } else if (!assessment.status || assessment.workAllowed === undefined) {
      group.unknown += 1;
    }
    groups.set(version, group);
  }
  return [...groups.values()].sort(
    (left, right) =>
      right.blocked - left.blocked ||
      right.attention - left.attention ||
      right.unknown - left.unknown ||
      right.version.localeCompare(left.version, undefined, { numeric: true }),
  );
}

export function AnalyticsPage() {
  const { can } = useAuth();
  const { notify } = useToast();
  const [windowHours, setWindowHours] = useState(24 * 7);
  const canReadAgents = can("agents:read");
  const canManageAlerts = can("benches:manage");
  const analyticsPath = `/api/v1/operational/analytics?window_hours=${windowHours}`;
  const alertsPath = "/api/v1/operational/alerts?status=OPEN";
  const agentsPath = "/api/v1/agents";
  const analyticsQuery = useApiDetail("operational-analytics", analyticsPath, true, () =>
    generatedApi.operationalAnalytics({ window_hours: windowHours }),
  );
  const alertsQuery = useApiList("operational-alerts", alertsPath, true, () =>
    generatedApi.listOperationalAlerts({ status: "OPEN" }),
  );
  const agentsQuery = useApiList("agents", agentsPath, canReadAgents, generatedApi.listAgents);
  const alertMutation = useApiMutation(
    ({ alertId, action }: { alertId: string; action: "acknowledge" | "resolve" }) =>
      action === "acknowledge"
        ? generatedApi.acknowledgeOperationalAlert(alertId)
        : generatedApi.resolveOperationalAlert(alertId),
    [["operational-alerts"]],
  );

  const snapshot = asRecord(analyticsQuery.data);
  const benches = records(snapshot, "benches");
  const queue = nested(snapshot, "queue");
  const reliability = nested(snapshot, "reliability");
  const alerts = useMemo(
    () =>
      records(alertsQuery.data).sort(
        (left, right) =>
          severityRank(left) - severityRank(right) ||
          (stringValue(right, "created_at") ?? "").localeCompare(
            stringValue(left, "created_at") ?? "",
          ),
      ),
    [alertsQuery.data],
  );
  const agents = records(agentsQuery.data);
  const versions = useMemo(() => fleetVersions(agents), [agents]);
  const availableSeconds = benches.reduce(
    (total, bench) =>
      total + (optionalNumber(nested(bench, "utilisation"), "available_seconds") ?? 0),
    0,
  );
  const observationSeconds = benches.reduce(
    (total, bench) =>
      total + (optionalNumber(nested(bench, "utilisation"), "observation_seconds") ?? 0),
    0,
  );
  const utilisedSeconds = benches.reduce(
    (total, bench) =>
      total + (optionalNumber(nested(bench, "utilisation"), "utilised_seconds") ?? 0),
    0,
  );
  const utilisation =
    availableSeconds > 0 ? Math.min(1, utilisedSeconds / availableSeconds) : undefined;
  const availability =
    observationSeconds > 0 ? Math.min(1, availableSeconds / observationSeconds) : undefined;
  const successRate = optionalNumber(reliability, "success_rate");
  const queueDepth = numberValue(queue, "queue_depth");
  const queueP95 = optionalNumber(queue, "p95_wait_seconds");
  const abandoned = numberValue(queue, "abandoned");
  const flakyBenches = benches.filter((bench) =>
    booleanValue(nested(bench, "flaky"), "potentially_flaky"),
  ).length;
  const criticalAlerts = alerts.filter(
    (alert) => stringValue(alert, "severity") === "CRITICAL",
  ).length;
  const blockedAgents = agents.filter(agentWorkBlocked).length;
  const queueTone: Tone =
    queueP95 !== undefined && queueP95 >= 15 * 60
      ? "warning"
      : queueDepth > 0
        ? "info"
        : "positive";
  const alertTone: Tone =
    alertsQuery.isLoading || alertsQuery.error
      ? "neutral"
      : criticalAlerts
        ? "negative"
        : alerts.length
          ? "warning"
          : "positive";

  const benchRows = useMemo(
    () =>
      [...benches].sort((left, right) => {
        const flakyDelta =
          Number(booleanValue(nested(right, "flaky"), "potentially_flaky")) -
          Number(booleanValue(nested(left, "flaky"), "potentially_flaky"));
        if (flakyDelta) return flakyDelta;
        return (
          (optionalNumber(nested(right, "utilisation"), "utilisation_ratio") ?? -1) -
          (optionalNumber(nested(left, "utilisation"), "utilisation_ratio") ?? -1)
        );
      }),
    [benches],
  );

  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor((row) => stringValue(row, "name") ?? stringValue(row, "bench_id") ?? "", {
        id: "bench",
        header: "Bench",
        cell: ({ row, getValue }) => (
          <div className="primary-cell">
            <Link
              to={`/benches/${encodeURIComponent(stringValue(row.original, "bench_id") ?? "")}`}
            >
              {getValue()}
            </Link>
            <code>{stringValue(row.original, "bench_id")}</code>
          </div>
        ),
      }),
      column.accessor(
        (row) => optionalNumber(nested(row, "utilisation"), "utilisation_ratio") ?? -1,
        {
          id: "utilisation",
          header: "Utilisation",
          cell: ({ row, getValue }) => {
            const ratio = getValue() < 0 ? undefined : Number(getValue());
            const label = formatPercent(ratio);
            return ratio === undefined ? (
              <span aria-label="Utilisation unavailable">—</span>
            ) : (
              <div className="analytics-meter">
                <progress
                  value={ratio * 100}
                  max={100}
                  aria-label={`${stringValue(row.original, "name") ?? stringValue(row.original, "bench_id")} utilisation ${label}`}
                />
                <span>{label}</span>
              </div>
            );
          },
        },
      ),
      column.accessor((row) => optionalNumber(row, "availability_ratio") ?? -1, {
        id: "availability",
        header: "Availability",
        cell: ({ getValue }) => formatPercent(getValue() < 0 ? undefined : Number(getValue())),
      }),
      column.accessor((row) => optionalNumber(nested(row, "reliability"), "success_rate") ?? -1, {
        id: "reliability",
        header: "Reliability",
        cell: ({ row, getValue }) => (
          <div className="stacked-cell">
            <strong>{formatPercent(getValue() < 0 ? undefined : Number(getValue()))}</strong>
            <small>
              {numberValue(nested(row.original, "reliability"), "operations")} operations
            </small>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(nested(row, "maintenance"), "status") ?? "UNKNOWN", {
        id: "condition",
        header: "Condition",
        cell: ({ row, getValue }) => {
          const flaky = booleanValue(nested(row.original, "flaky"), "potentially_flaky");
          return (
            <div className="stacked-cell">
              <StatusBadge
                status={flaky ? "DEGRADED" : getValue()}
                label={flaky ? "Potentially flaky" : undefined}
              />
              {stringValue(nested(row.original, "recommendation"), "message") && (
                <small>{stringValue(nested(row.original, "recommendation"), "message")}</small>
              )}
            </div>
          );
        },
      }),
    ];
  }, []);

  async function refresh() {
    await Promise.all([
      analyticsQuery.refetch(),
      alertsQuery.refetch(),
      ...(canReadAgents ? [agentsQuery.refetch()] : []),
    ]);
  }

  async function updateAlert(alert: ApiRecord, action: "acknowledge" | "resolve") {
    const alertId = stringValue(alert, "id");
    if (!alertId) return;
    try {
      await alertMutation.mutateAsync({ alertId, action });
      notify({
        title: action === "acknowledge" ? "Alert acknowledged" : "Alert resolved",
        message: stringValue(alert, "message"),
        tone: "success",
      });
    } catch (error) {
      notify({
        title: action === "acknowledge" ? "Couldn’t acknowledge alert" : "Couldn’t resolve alert",
        message: errorMessage(error),
        tone: "error",
      });
    }
  }

  if (analyticsQuery.isLoading) {
    return (
      <div className="page-stack">
        <PageHeader
          eyebrow="Operations"
          title="Analytics"
          description="Utilisation, queue pressure and reliability across the lab."
        />
        <LoadingState label="Loading operational analytics" />
      </div>
    );
  }

  if (analyticsQuery.error || !snapshot) {
    return (
      <div className="page-stack">
        <PageHeader eyebrow="Operations" title="Analytics" />
        <ErrorState
          error={analyticsQuery.error ?? new Error("Analytics snapshot is unavailable")}
          retry={() => void analyticsQuery.refetch()}
        />
      </div>
    );
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Operations"
        title="Analytics"
        description={
          <>
            <span>Utilisation, queue pressure and reliability across the lab.</span>
            <span className="updated-at">
              Snapshot generated {formatRelative(stringValue(snapshot, "generated_at"))}
            </span>
          </>
        }
        actions={
          <div className="action-row">
            <label className="compact-select">
              <span className="sr-only">Analytics window</span>
              <select
                value={windowHours}
                onChange={(event) => setWindowHours(Number(event.target.value))}
              >
                {WINDOW_OPTIONS.map((option) => (
                  <option key={option.hours} value={option.hours}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <Button
              variant="secondary"
              icon={RefreshCw}
              disabled={
                analyticsQuery.isFetching ||
                alertsQuery.isFetching ||
                (canReadAgents && agentsQuery.isFetching)
              }
              onClick={() => void refresh()}
            >
              Refresh
            </Button>
          </div>
        }
      />

      <div className="metric-grid">
        <MetricCard
          label="Lab utilisation"
          value={formatPercent(utilisation)}
          hint={`Lab availability ${formatPercent(availability)}`}
          icon={Gauge}
          tone="info"
        />
        <MetricCard
          label="Queue depth"
          value={queueDepth}
          hint={`p95 wait ${formatSeconds(queueP95)} · ${abandoned} abandoned`}
          icon={Clock3}
          tone={queueTone}
        />
        <MetricCard
          label="Reliability"
          value={formatPercent(successRate)}
          hint={`${numberValue(reliability, "operations")} operations · ${flakyBenches} flaky ${flakyBenches === 1 ? "bench" : "benches"}`}
          icon={ShieldCheck}
          tone={reliabilityTone(successRate)}
        />
        <MetricCard
          label="Open alerts"
          value={alertsQuery.isLoading || alertsQuery.error ? "—" : alerts.length}
          hint={
            alertsQuery.error
              ? "Alert feed unavailable"
              : `${criticalAlerts} critical · ${alerts.length - criticalAlerts} other`
          }
          icon={BellRing}
          tone={alertTone}
        />
      </div>

      <div className="operational-insights-grid">
        <Panel
          title="Open alerts"
          description="Active conditions that need operator attention"
          className={!canReadAgents ? "analytics-wide" : ""}
        >
          {alertsQuery.isLoading ? (
            <LoadingState label="Loading alerts" />
          ) : alertsQuery.error ? (
            <ErrorState error={alertsQuery.error} retry={() => void alertsQuery.refetch()} />
          ) : alerts.length ? (
            <div className="compact-list alert-list">
              {alerts.map((alert) => {
                const href =
                  stringValue(alert, "resource_type") === "agent" && !canReadAgents
                    ? undefined
                    : alertResourcePath(alert);
                const resourceId = stringValue(alert, "resource_id");
                const alertId = stringValue(alert, "id");
                const message = stringValue(alert, "message") ?? "Operational alert";
                const updating =
                  alertMutation.isPending && alertMutation.variables?.alertId === alertId;
                return (
                  <div className="alert-row" key={alertId} aria-busy={updating || undefined}>
                    <span>
                      <strong>{message}</strong>
                      <small>
                        {titleCase(stringValue(alert, "type"))}
                        {resourceId && " · "}
                        {href ? (
                          <Link to={href}>
                            {stringValue(alert, "resource_type") === "bench"
                              ? resourceId
                              : shortId(resourceId)}
                          </Link>
                        ) : (
                          resourceId
                        )}
                      </small>
                    </span>
                    <StatusBadge status={stringValue(alert, "severity")} />
                    <time dateTime={stringValue(alert, "created_at")}>
                      {formatRelative(stringValue(alert, "created_at"))}
                    </time>
                    {canManageAlerts && alertId && (
                      <span className="alert-actions">
                        <Button
                          variant="ghost"
                          disabled={alertMutation.isPending}
                          aria-label={`Acknowledge alert: ${message}`}
                          onClick={() => void updateAlert(alert, "acknowledge")}
                        >
                          Acknowledge
                        </Button>
                        <Button
                          variant="ghost"
                          disabled={alertMutation.isPending}
                          aria-label={`Resolve alert: ${message}`}
                          onClick={() => void updateAlert(alert, "resolve")}
                        >
                          Resolve
                        </Button>
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <EmptyState
              title="No open alerts"
              description="No active operational conditions need attention."
              icon={CheckCircle2}
            />
          )}
        </Panel>

        {canReadAgents && (
          <Panel
            title="Agent fleet"
            description={
              agentsQuery.isLoading || agentsQuery.error
                ? "Versions and control-plane compatibility"
                : `${agents.length} Agents across ${versions.length} ${versions.length === 1 ? "version" : "versions"}`
            }
            action={
              <Link className="text-link" to="/agents">
                View Agents
              </Link>
            }
          >
            {agentsQuery.isLoading ? (
              <LoadingState label="Loading Agent fleet" />
            ) : agentsQuery.error ? (
              <ErrorState error={agentsQuery.error} retry={() => void agentsQuery.refetch()} />
            ) : versions.length ? (
              <div className="compact-list fleet-version-list">
                {versions.map((group) => (
                  <div key={group.version}>
                    <span>
                      <strong>v{group.version}</strong>
                      <small>
                        {group.count} {group.count === 1 ? "Agent" : "Agents"} · protocol{" "}
                        {[...group.protocols].sort().join(", ") || "unknown"}
                      </small>
                    </span>
                    <StatusBadge
                      status={
                        group.blocked
                          ? "INCOMPATIBLE"
                          : group.attention
                            ? "UPGRADE_AVAILABLE"
                            : group.unknown
                              ? "UNKNOWN"
                              : "COMPATIBLE"
                      }
                      label={
                        group.blocked
                          ? `${group.blocked} work blocked`
                          : group.attention
                            ? `${group.attention} to review`
                            : group.unknown
                              ? `${group.unknown} unknown`
                              : "Compatible"
                      }
                    />
                  </div>
                ))}
                {blockedAgents > 0 && (
                  <p className="panel-note" role="status">
                    {blockedAgents} {blockedAgents === 1 ? "Agent is" : "Agents are"} incompatible
                    with the current control plane and cannot accept work.
                  </p>
                )}
              </div>
            ) : (
              <EmptyState
                title="No Agents registered"
                description="Fleet compatibility will appear after an Agent enrolls."
                icon={Bot}
              />
            )}
          </Panel>
        )}
      </div>

      <Panel
        title="Bench utilisation and reliability"
        description={`Per-bench signals for ${WINDOW_OPTIONS.find((option) => option.hours === windowHours)?.label.toLowerCase() ?? "the selected window"}`}
      >
        {benchRows.length ? (
          <DataTable data={benchRows} columns={columns} pageSize={10} />
        ) : (
          <EmptyState
            title="No bench observations"
            description="Operational metrics will appear after benches become visible."
            icon={Gauge}
          />
        )}
      </Panel>
    </div>
  );
}
