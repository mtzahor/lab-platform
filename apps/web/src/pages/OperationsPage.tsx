import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { Activity, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { generatedApi, nested, records, stringValue, type ApiRecord } from "../api/client";
import {
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  SearchField,
  StatusBadge,
} from "../components/ui";
import { useApiList } from "../hooks/useApi";
import { formatDuration, formatRelative, titleCase } from "../lib/format";

export function OperationsPage() {
  const navigate = useNavigate();
  const query = useApiList("operations", "/api/v1/operations?limit=1000", true, () =>
    generatedApi.listOperations({ limit: 1000 }),
  );
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [type, setType] = useState("");
  const items = records(query.data);
  const types = [
    ...new Set(
      items.map((item) => stringValue(item, "operation_type")).filter(Boolean) as string[],
    ),
  ].sort();
  const filtered = items.filter(
    (item) =>
      (!search ||
        `${stringValue(item, "id")} ${stringValue(item, "bench_id")} ${stringValue(item, "operation_type")}`
          .toLowerCase()
          .includes(search.toLowerCase())) &&
      (!status || stringValue(item, "status") === status) &&
      (!type || stringValue(item, "operation_type") === type),
  );
  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor((row) => stringValue(row, "operation_type") ?? "", {
        id: "type",
        header: "Operation",
        cell: ({ row, getValue }) => (
          <div className="primary-cell">
            <Link to={`/operations/${stringValue(row.original, "id")}`}>
              {titleCase(getValue())}
            </Link>
            <code>{stringValue(row.original, "id")}</code>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "bench_id") ?? "", {
        id: "bench",
        header: "Bench",
        cell: ({ getValue }) => (
          <Link to={`/benches/${encodeURIComponent(getValue())}`}>{getValue()}</Link>
        ),
      }),
      column.accessor((row) => stringValue(row, "status") ?? "UNKNOWN", {
        id: "status",
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      column.accessor((row) => Number(stringValue(row, "progress") ?? 0), {
        id: "progress",
        header: "Progress",
        cell: ({ getValue }) => (
          <div className="table-progress">
            <progress value={getValue()} max={100} aria-label={`${getValue()}% complete`} />
            <small>{getValue()}%</small>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "agent_id") ?? "", {
        id: "agent",
        header: "Agent",
        cell: ({ getValue }) => <code>{getValue() ? `${getValue().slice(0, 8)}…` : "—"}</code>,
      }),
      column.accessor(
        (row) =>
          stringValue(nested(row, "actor"), "display_name") ?? stringValue(row, "owner") ?? "",
        { id: "actor", header: "Initiated by", cell: ({ getValue }) => getValue() || "—" },
      ),
      column.accessor(
        (row) => stringValue(row, "started_at") ?? stringValue(row, "created_at") ?? "",
        { id: "started", header: "Started", cell: ({ getValue }) => formatRelative(getValue()) },
      ),
      column.display({
        id: "duration",
        header: "Duration",
        cell: ({ row }) =>
          formatDuration(
            stringValue(row.original, "started_at") ?? stringValue(row.original, "created_at"),
            stringValue(row.original, "completed_at"),
          ),
      }),
    ];
  }, []);
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Execution"
        title="Operations"
        description="Remote commands, progress and reconciliation across every Agent."
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
          placeholder="Search operation, bench or ID…"
        />
        <label className="compact-select">
          <span className="sr-only">Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All states</option>
            {[
              "CREATED",
              "DISPATCHED",
              "ACCEPTED",
              "RUNNING",
              "UNKNOWN",
              "RECONCILING",
              "SUCCEEDED",
              "FAILED",
              "CANCELLED",
            ].map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="compact-select">
          <span className="sr-only">Operation type</span>
          <select value={type} onChange={(event) => setType(event.target.value)}>
            <option value="">All types</option>
            {types.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading operations" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No operations yet"
          description="Flash, reset, serial and workflow operations will appear here."
          icon={Activity}
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No operations match"
          description="Adjust the current filters."
          icon={Activity}
        />
      ) : (
        <DataTable
          data={filtered}
          columns={columns}
          rowLabel={(row) => `Open ${titleCase(stringValue(row, "operation_type"))}`}
          onRowClick={(row) => navigate(`/operations/${stringValue(row, "id")}`)}
        />
      )}
    </div>
  );
}
