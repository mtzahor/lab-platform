import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  CalendarClock,
  CheckCircle2,
  ChevronRight,
  CircleOff,
  ClipboardList,
  FlaskConical,
  TriangleAlert,
  Workflow,
} from "lucide-react";
import { Link } from "react-router-dom";
import {
  ApiError,
  generatedApi,
  asRecord,
  numberValue,
  records,
  stringValue,
  type ApiRecord,
} from "../api/client";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  MetricCard,
  PageHeader,
  Panel,
  StatusBadge,
} from "../components/ui";
import { useLive } from "../app/LiveProvider";
import { formatRelative, titleCase } from "../lib/format";
import { isActiveStatus } from "../lib/status";

type OverviewData = {
  counts: Record<string, number>;
  activeOperations: ApiRecord[];
  failedWorkflows: ApiRecord[];
  unhealthyBenches: ApiRecord[];
  upcomingReservations: ApiRecord[];
  agentDisconnects: ApiRecord[];
};

function overviewFromPayload(payload: ApiRecord): OverviewData {
  const countsRoot = asRecord(payload.counts) ?? asRecord(payload.summary) ?? payload;
  const section = (snake: string, camel: string) =>
    records({ items: payload[snake] ?? payload[camel] });
  return {
    counts: {
      benches_total: numberValue(
        countsRoot,
        "benches_total",
        numberValue(countsRoot, "total_benches"),
      ),
      benches_available: numberValue(
        countsRoot,
        "benches_available",
        numberValue(countsRoot, "available_benches"),
      ),
      benches_reserved: numberValue(
        countsRoot,
        "benches_reserved",
        numberValue(countsRoot, "reserved_benches"),
      ),
      benches_offline: numberValue(
        countsRoot,
        "benches_offline",
        numberValue(countsRoot, "offline_benches"),
      ),
      agents_online: numberValue(countsRoot, "agents_online"),
      operations_active: numberValue(
        countsRoot,
        "operations_active",
        numberValue(countsRoot, "active_operations"),
      ),
      reservations_queued: numberValue(
        countsRoot,
        "reservations_queued",
        numberValue(countsRoot, "queued_reservations"),
      ),
      workflows_failed_24h: numberValue(
        countsRoot,
        "workflows_failed_24h",
        numberValue(countsRoot, "failed_workflows_24h"),
      ),
    },
    activeOperations: section("active_operations", "activeOperations"),
    failedWorkflows: section("failed_workflow_runs", "failedWorkflows"),
    unhealthyBenches: section("degraded_benches", "unhealthyBenches"),
    upcomingReservations: section("active_reservations", "upcomingReservations"),
    agentDisconnects: section("recent_agent_disconnects", "agentDisconnects"),
  };
}

async function loadOverview(): Promise<OverviewData> {
  try {
    return overviewFromPayload(await generatedApi.overview());
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
  }
  const [benchData, agentData, operationData, reservationData] = await Promise.all([
    generatedApi.listBenches(),
    generatedApi.listAgents(),
    generatedApi.listOperations({ limit: 100 }),
    generatedApi.listReservations({ limit: 100 }),
  ]);
  const benches = records(benchData);
  const agents = records(agentData);
  const operations = records(operationData);
  const reservations = records(reservationData);
  const activeOperations = operations.filter((item) => isActiveStatus(stringValue(item, "status")));
  const failedWorkflows = operations
    .filter(
      (item) =>
        stringValue(item, "operation_type")?.includes("WORKFLOW") &&
        stringValue(item, "status") === "FAILED",
    )
    .slice(0, 5);
  const unhealthyBenches = benches
    .filter((item) =>
      ["DEGRADED", "OFFLINE"].includes(
        stringValue(item, "health") ?? stringValue(item, "status") ?? "",
      ),
    )
    .slice(0, 6);
  const upcomingReservations = reservations
    .filter((item) => {
      const reservation = asRecord(item.reservation) ?? item;
      return ["ACTIVE", "SCHEDULED", "QUEUED"].includes(
        (
          stringValue(reservation, "status") ??
          stringValue(reservation, "state") ??
          ""
        ).toUpperCase(),
      );
    })
    .slice(0, 5);
  return {
    counts: {
      benches_total: benches.length,
      benches_available: benches.filter((item) =>
        ["ONLINE", "AVAILABLE"].includes(stringValue(item, "status") ?? ""),
      ).length,
      benches_reserved: reservations.filter(
        (item) =>
          (
            stringValue(asRecord(item.reservation) ?? item, "status") ??
            stringValue(asRecord(item.reservation) ?? item, "state") ??
            ""
          ).toUpperCase() === "ACTIVE",
      ).length,
      benches_offline: benches.filter((item) => stringValue(item, "status") === "OFFLINE").length,
      agents_online: agents.filter((item) => stringValue(item, "status") === "ONLINE").length,
      operations_active: activeOperations.length,
      reservations_queued: reservations.filter(
        (item) =>
          (
            stringValue(asRecord(item.reservation) ?? item, "status") ??
            stringValue(asRecord(item.reservation) ?? item, "state") ??
            ""
          ).toUpperCase() === "QUEUED",
      ).length,
      workflows_failed_24h: failedWorkflows.length,
    },
    activeOperations: activeOperations.slice(0, 5),
    failedWorkflows,
    unhealthyBenches,
    upcomingReservations,
    agentDisconnects: agents
      .filter((item) => stringValue(item, "status") === "OFFLINE")
      .slice(0, 5),
  };
}

export function OverviewPage() {
  const live = useLive();
  const query = useQuery({
    queryKey: ["overview"],
    queryFn: loadOverview,
    staleTime: 10_000,
    refetchInterval: live.state === "live" ? false : live.pollingIntervalMs,
  });
  if (query.isLoading)
    return (
      <>
        <PageHeader
          eyebrow="Operations"
          title="Overview"
          description="Current state across your distributed lab."
        />
        <LoadingState label="Loading operational status" />
      </>
    );
  if (query.error)
    return (
      <>
        <PageHeader eyebrow="Operations" title="Overview" />
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      </>
    );
  const data = query.data!;
  const counts = data.counts;
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Operations"
        title="Overview"
        description={
          <>
            <span>Current state across your distributed lab.</span>
            {query.dataUpdatedAt > 0 && (
              <span className="updated-at">
                Updated {formatRelative(new Date(query.dataUpdatedAt).toISOString())}
              </span>
            )}
          </>
        }
      />
      <div className="metric-grid">
        <MetricCard label="Total benches" value={counts.benches_total} icon={FlaskConical} />
        <MetricCard
          label="Available"
          value={counts.benches_available}
          icon={CheckCircle2}
          tone="positive"
        />
        <MetricCard
          label="Reserved"
          value={counts.benches_reserved}
          icon={ClipboardList}
          tone="info"
        />
        <MetricCard
          label="Offline"
          value={counts.benches_offline}
          icon={CircleOff}
          tone={counts.benches_offline ? "negative" : "neutral"}
        />
        <MetricCard label="Agents online" value={counts.agents_online} icon={Bot} tone="positive" />
        <MetricCard
          label="Active operations"
          value={counts.operations_active}
          icon={Activity}
          tone="info"
        />
        <MetricCard
          label="Queued reservations"
          value={counts.reservations_queued}
          icon={CalendarClock}
          tone={counts.reservations_queued ? "warning" : "neutral"}
        />
        <MetricCard
          label="Failed workflows · 24h"
          value={counts.workflows_failed_24h}
          icon={Workflow}
          tone={counts.workflows_failed_24h ? "negative" : "neutral"}
        />
      </div>
      <div className="overview-grid">
        <Panel
          title="Active operations"
          description="Work currently running across the lab"
          action={
            <Link className="text-link" to="/operations">
              View all <ChevronRight size={14} />
            </Link>
          }
          className="overview-wide"
        >
          {data.activeOperations.length ? (
            <div className="resource-list">
              {data.activeOperations.map((item) => (
                <Link key={stringValue(item, "id")} to={`/operations/${stringValue(item, "id")}`}>
                  <span className="resource-icon">
                    <Activity size={16} />
                  </span>
                  <span>
                    <strong>{titleCase(stringValue(item, "operation_type"))}</strong>
                    <small>{stringValue(item, "bench_id")}</small>
                  </span>
                  <span className="resource-progress">
                    <progress
                      value={numberValue(item, "progress")}
                      max={100}
                      aria-label={`${numberValue(item, "progress")}% complete`}
                    />
                    <small>{numberValue(item, "progress")}%</small>
                  </span>
                  <StatusBadge status={stringValue(item, "status")} />
                  <ChevronRight size={15} />
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState
              title="No active operations"
              description="The lab is idle. New work will appear here."
              icon={Activity}
            />
          )}
        </Panel>
        <Panel
          title="Attention needed"
          description="Degraded and offline benches"
          action={
            <Link className="text-link" to="/benches?attention=true">
              Inspect <ChevronRight size={14} />
            </Link>
          }
        >
          {data.unhealthyBenches.length ? (
            <div className="compact-list">
              {data.unhealthyBenches.map((item) => (
                <Link
                  key={stringValue(item, "id")}
                  to={`/benches/${encodeURIComponent(stringValue(item, "id") ?? "")}`}
                >
                  <span>
                    <strong>{stringValue(item, "name")}</strong>
                    <small>{stringValue(item, "id")}</small>
                  </span>
                  <StatusBadge
                    status={stringValue(item, "health") ?? stringValue(item, "status")}
                  />
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState
              title="All benches healthy"
              description="No degraded or offline benches need attention."
              icon={CheckCircle2}
            />
          )}
        </Panel>
        <Panel title="Recently failed workflows" description="Failures from the last 24 hours">
          {data.failedWorkflows.length ? (
            <div className="compact-list">
              {data.failedWorkflows.map((item) => (
                <Link
                  key={stringValue(item, "id")}
                  to={`/workflow-runs/${stringValue(item, "id")}`}
                >
                  <span>
                    <strong>
                      {stringValue(item, "workflow_name") ??
                        titleCase(stringValue(item, "operation_type"))}
                    </strong>
                    <small>
                      {stringValue(item, "bench_id")} ·{" "}
                      {formatRelative(stringValue(item, "completed_at"))}
                    </small>
                  </span>
                  <StatusBadge status="FAILED" />
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState
              title="No recent failures"
              description="Workflow failures from the last 24 hours will appear here."
              icon={CheckCircle2}
            />
          )}
        </Panel>
        <Panel title="Upcoming reservations" description="Scheduled and queued demand">
          {data.upcomingReservations.length ? (
            <div className="compact-list">
              {data.upcomingReservations.map((item) => {
                const reservation = asRecord(item.reservation) ?? item;
                return (
                  <Link key={stringValue(reservation, "id")} to="/reservations">
                    <span>
                      <strong>{stringValue(reservation, "bench_id")}</strong>
                      <small>
                        {stringValue(reservation, "owner")} ·{" "}
                        {formatRelative(
                          stringValue(reservation, "starts_at") ??
                            stringValue(reservation, "created_at"),
                        )}
                      </small>
                    </span>
                    <StatusBadge
                      status={
                        stringValue(reservation, "status") ?? stringValue(reservation, "state")
                      }
                    />
                  </Link>
                );
              })}
            </div>
          ) : (
            <EmptyState
              title="No upcoming reservations"
              description="The schedule and queue are currently clear."
              icon={CalendarClock}
            />
          )}
        </Panel>
        <Panel
          title="Recent Agent disconnects"
          description="Connectivity changes that may affect benches"
        >
          {data.agentDisconnects.length ? (
            <div className="compact-list">
              {data.agentDisconnects.map((item) => (
                <Link key={stringValue(item, "id")} to={`/agents/${stringValue(item, "id")}`}>
                  <span>
                    <strong>{stringValue(item, "name") ?? stringValue(item, "slug")}</strong>
                    <small>
                      {stringValue(item, "location") ?? "Location unknown"} ·{" "}
                      {formatRelative(
                        stringValue(item, "disconnected_at") ?? stringValue(item, "last_seen_at"),
                      )}
                    </small>
                  </span>
                  <StatusBadge status={stringValue(item, "status")} />
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState
              title="Connections stable"
              description="No recent Agent disconnects were reported."
              icon={Bot}
            />
          )}
        </Panel>
      </div>
      {(counts.benches_offline > 0 || counts.workflows_failed_24h > 0) && (
        <div className="attention-strip">
          <TriangleAlert size={18} />
          <span>
            <strong>Lab attention:</strong> {counts.benches_offline} offline bench
            {counts.benches_offline === 1 ? "" : "es"} and {counts.workflows_failed_24h} failed
            workflow{counts.workflows_failed_24h === 1 ? "" : "s"} in the last day.
          </span>
        </div>
      )}
    </div>
  );
}
