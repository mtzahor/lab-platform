import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { useInfiniteQuery } from "@tanstack/react-query";
import { FileClock, RefreshCw, ShieldAlert } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  asRecord,
  generatedApi,
  records,
  stringValue,
  type ApiRecord,
  type AuditListQuery,
} from "../../api/client";
import {
  Button,
  DataTable,
  DetailGrid,
  Dialog,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  SearchField,
  StatusBadge,
} from "../../components/ui";
import { formatDate, formatRelative, shortId, titleCase } from "../../lib/format";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function AuditPage() {
  const [routeParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState<NonNullable<AuditListQuery["outcome"]> | "">("");
  const [resourceType, setResourceType] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [after, setAfter] = useState("");
  const [before, setBefore] = useState("");
  const [requestId, setRequestId] = useState("");
  const [commandId, setCommandId] = useState("");
  const [selected, setSelected] = useState<ApiRecord>();
  const operationId = routeParams.get("operation_id") ?? "";
  const filters: AuditListQuery = {
    limit: 100,
    actor: search || undefined,
    action: action || undefined,
    outcome: outcome || undefined,
    resource_type: resourceType || undefined,
    resource_id: resourceId || undefined,
    request_id: UUID_PATTERN.test(requestId) ? requestId : undefined,
    command_id: UUID_PATTERN.test(commandId) ? commandId : undefined,
    operation_id: UUID_PATTERN.test(operationId) ? operationId : undefined,
    after: after ? new Date(after).toISOString() : undefined,
    before: before ? new Date(`${before}T23:59:59`).toISOString() : undefined,
  };
  const filterSearch = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null) filterSearch.set(key, String(value));
  }
  const filterKey = filterSearch.toString();
  const query = useInfiniteQuery({
    queryKey: ["audit", filterKey],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      generatedApi.listAuditEvents({
        ...filters,
        cursor: pageParam || undefined,
      }),
    getNextPageParam: (lastPage) =>
      lastPage.has_more === true ? stringValue(lastPage, "next_cursor") : undefined,
  });
  const items = query.data?.pages.flatMap((page) => records(page)) ?? [];
  const actions = [
    ...new Set(items.map((item) => stringValue(item, "action")).filter(Boolean) as string[]),
  ].sort();
  const resourceTypes = [
    ...new Set(items.map((item) => stringValue(item, "resource_type")).filter(Boolean) as string[]),
  ].sort();
  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor(
        (row) => stringValue(row, "created_at") ?? stringValue(row, "timestamp") ?? "",
        {
          id: "timestamp",
          header: "Timestamp",
          cell: ({ getValue }) => (
            <span title={formatDate(getValue())}>{formatRelative(getValue())}</span>
          ),
        },
      ),
      column.accessor(
        (row) =>
          stringValue(row, "actor_display_name") ??
          stringValue(row, "actor_name") ??
          stringValue(asRecord(row.actor), "display_name") ??
          stringValue(asRecord(row.actor), "name") ??
          "",
        { id: "actor", header: "Actor", cell: ({ getValue }) => getValue() || "System" },
      ),
      column.accessor((row) => stringValue(row, "action") ?? "", {
        id: "action",
        header: "Action",
        cell: ({ getValue }) => (
          <button className="link-button" onClick={() => setAction(getValue())}>
            {titleCase(getValue())}
          </button>
        ),
      }),
      column.display({
        id: "resource",
        header: "Resource",
        cell: ({ row }) => (
          <div className="stacked-cell">
            <span>{titleCase(stringValue(row.original, "resource_type"))}</span>
            <code>{shortId(stringValue(row.original, "resource_id"))}</code>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "outcome") ?? "UNKNOWN", {
        id: "outcome",
        header: "Outcome",
        cell: ({ getValue }) => <StatusBadge status={getValue()} label={titleCase(getValue())} />,
      }),
      column.accessor(
        (row) => stringValue(row, "source") ?? stringValue(row, "source_type") ?? "",
        { id: "source", header: "Source", cell: ({ getValue }) => titleCase(getValue()) || "—" },
      ),
      column.accessor((row) => stringValue(row, "request_id") ?? "", {
        id: "requestId",
        header: "Request ID",
        cell: ({ getValue }) => <code title={getValue()}>{shortId(getValue())}</code>,
      }),
    ];
  }, []);
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Governance"
        title="Audit log"
        description="Search authentication, administration and lab-operation decisions without exposing secrets."
        actions={
          <Button variant="secondary" icon={RefreshCw} onClick={() => void query.refetch()}>
            Refresh
          </Button>
        }
      />
      <div className="toolbar">
        <SearchField
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Filter by actor display name…"
        />
        <label className="compact-select">
          <span className="sr-only">Action</span>
          <select value={action} onChange={(event) => setAction(event.target.value)}>
            <option value="">All actions</option>
            {actions.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="compact-select">
          <span className="sr-only">Outcome</span>
          <select
            value={outcome}
            onChange={(event) => setOutcome(event.target.value as typeof outcome)}
          >
            <option value="">All outcomes</option>
            <option>SUCCEEDED</option>
            <option>DENIED</option>
            <option>FAILED</option>
          </select>
        </label>
        <label className="compact-select">
          <span className="sr-only">Resource type</span>
          <select value={resourceType} onChange={(event) => setResourceType(event.target.value)}>
            <option value="">All resources</option>
            {resourceTypes.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="filter-bar audit-filters">
        <label>
          <span>After</span>
          <input type="date" value={after} onChange={(event) => setAfter(event.target.value)} />
        </label>
        <label>
          <span>Before</span>
          <input type="date" value={before} onChange={(event) => setBefore(event.target.value)} />
        </label>
        <label>
          <span>Resource ID</span>
          <input
            value={resourceId}
            onChange={(event) => setResourceId(event.target.value)}
            placeholder="Exact resource ID"
          />
        </label>
        <label>
          <span>Request ID</span>
          <input
            value={requestId}
            onChange={(event) => setRequestId(event.target.value)}
            placeholder="Exact UUID"
          />
        </label>
        <label>
          <span>Command ID</span>
          <input
            value={commandId}
            onChange={(event) => setCommandId(event.target.value)}
            placeholder="Exact UUID"
          />
        </label>
        {operationId && <span className="active-filter">Operation: {shortId(operationId)}</span>}
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading audit events" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No audit events"
          description="Authenticated actions will appear here."
          icon={FileClock}
        />
      ) : (
        <>
          <DataTable
            data={items}
            columns={columns}
            pageSize={50}
            rowLabel={(row) => `Inspect ${stringValue(row, "action")} audit event`}
            onRowClick={setSelected}
          />
          {query.hasNextPage && (
            <div className="pagination-actions">
              <Button
                variant="secondary"
                onClick={() => void query.fetchNextPage()}
                disabled={query.isFetchingNextPage}
              >
                {query.isFetchingNextPage ? "Loading…" : "Load older events"}
              </Button>
            </div>
          )}
        </>
      )}
      <Dialog
        open={Boolean(selected)}
        onClose={() => setSelected(undefined)}
        title={titleCase(stringValue(selected, "action")) || "Audit event"}
        description={`Event ${stringValue(selected, "id") ?? ""}`}
        size="wide"
      >
        {stringValue(selected, "outcome") === "DENIED" && (
          <div className="warning-callout">
            <ShieldAlert size={19} />
            <div>
              <strong>Request denied</strong>
              <p>The authorization policy rejected this action before it was performed.</p>
            </div>
          </div>
        )}
        <DetailGrid
          items={[
            { label: "Outcome", value: <StatusBadge status={stringValue(selected, "outcome")} /> },
            {
              label: "Actor",
              value:
                stringValue(selected, "actor_display_name") ??
                stringValue(selected, "actor_name") ??
                stringValue(asRecord(selected?.actor), "display_name"),
            },
            {
              label: "Resource",
              value: `${titleCase(stringValue(selected, "resource_type"))} · ${stringValue(selected, "resource_id")}`,
            },
            { label: "Source", value: titleCase(stringValue(selected, "source")) },
            {
              label: "Timestamp",
              value: formatDate(
                stringValue(selected, "created_at") ?? stringValue(selected, "timestamp"),
              ),
            },
            { label: "Request ID", value: <code>{stringValue(selected, "request_id")}</code> },
          ]}
        />
        <h3>Controlled metadata</h3>
        <pre className="metadata-viewer">
          {JSON.stringify(asRecord(selected?.metadata) ?? {}, null, 2)}
        </pre>
        <div className="dialog-actions">
          <Button onClick={() => setSelected(undefined)}>Done</Button>
        </div>
      </Dialog>
    </div>
  );
}
