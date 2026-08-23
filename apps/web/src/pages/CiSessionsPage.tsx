import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { GitBranch, PlayCircle, RefreshCw } from "lucide-react";
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
import { formatDuration, formatRelative, shortId, titleCase } from "../lib/format";

export function CiSessionsPage() {
  const navigate = useNavigate();
  const query = useApiList("ci-sessions", "/api/v1/ci/sessions?limit=1000", true, () =>
    generatedApi.listCiSessions({ limit: 1000 }),
  );
  const [search, setSearch] = useState("");
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const [after, setAfter] = useState("");
  const items = records(query.data);
  const providers = [
    ...new Set(items.map((item) => stringValue(item, "provider")).filter(Boolean) as string[]),
  ];
  const filtered = items.filter((item) => {
    const created = stringValue(item, "created_at");
    return (
      (!search ||
        `${stringValue(item, "repository")} ${stringValue(item, "ref")} ${stringValue(item, "commit_sha")} ${stringValue(item, "bench_id")} ${stringValue(item, "actor")}`
          .toLowerCase()
          .includes(search.toLowerCase())) &&
      (!provider || stringValue(item, "provider") === provider) &&
      (!status || stringValue(item, "status") === status) &&
      (!after || !created || new Date(created) >= new Date(after))
    );
  });
  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor((row) => stringValue(row, "provider") ?? "", {
        id: "provider",
        header: "Provider",
        cell: ({ getValue }) => <span className="provider-badge">{titleCase(getValue())}</span>,
      }),
      column.accessor((row) => stringValue(row, "repository") ?? "", {
        id: "repository",
        header: "Repository / ref",
        cell: ({ row, getValue }) => (
          <div className="primary-cell">
            <Link to={`/ci-sessions/${stringValue(row.original, "id")}`}>
              {getValue() || "Unspecified repository"}
            </Link>
            <span>
              <GitBranch size={12} /> {stringValue(row.original, "ref") ?? "—"}
            </span>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "commit_sha") ?? "", {
        id: "commit",
        header: "Commit",
        cell: ({ getValue }) => <code>{getValue() ? getValue().slice(0, 8) : "—"}</code>,
      }),
      column.accessor(
        (row) => stringValue(row, "actor") ?? stringValue(row, "requested_by") ?? "",
        { id: "actor", header: "Actor", cell: ({ getValue }) => getValue() || "—" },
      ),
      column.accessor(
        (row) =>
          stringValue(row, "bench_id") ?? stringValue(nested(row, "binding"), "bench_id") ?? "",
        {
          id: "bench",
          header: "Bench",
          cell: ({ getValue }) =>
            getValue() ? (
              <Link to={`/benches/${encodeURIComponent(getValue())}`}>{getValue()}</Link>
            ) : (
              <span className="muted">Allocating</span>
            ),
        },
      ),
      column.accessor(
        (row) =>
          stringValue(nested(row, "binding"), "workflow_name") ??
          stringValue(row, "workflow_name") ??
          "",
        { id: "workflow", header: "Workflow", cell: ({ getValue }) => getValue() || "—" },
      ),
      column.accessor((row) => stringValue(row, "status") ?? "UNKNOWN", {
        id: "status",
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      column.accessor((row) => stringValue(row, "cleanup_status") ?? "NOT_STARTED", {
        id: "cleanup",
        header: "Cleanup",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      column.display({
        id: "duration",
        header: "Duration",
        cell: ({ row }) =>
          formatDuration(
            stringValue(row.original, "started_at") ?? stringValue(row.original, "created_at"),
            stringValue(row.original, "completed_at"),
          ),
      }),
      column.accessor((row) => stringValue(row, "created_at") ?? "", {
        id: "created",
        header: "Created",
        cell: ({ getValue }) => formatRelative(getValue()),
      }),
    ];
  }, []);
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Automation"
        title="CI Sessions"
        description="External pipeline runs, assigned hardware and cleanup outcomes."
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
          placeholder="Search repository, ref, commit, bench or actor…"
        />
        <label className="compact-select">
          <span className="sr-only">Provider</span>
          <select value={provider} onChange={(event) => setProvider(event.target.value)}>
            <option value="">All providers</option>
            {providers.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="compact-select">
          <span className="sr-only">Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All states</option>
            {[
              "created",
              "waiting_for_bench",
              "reserved",
              "running",
              "succeeded",
              "failed",
              "cancel_requested",
              "cancelled",
              "timed_out",
              "cleanup_pending",
              "completed",
            ].map((item) => (
              <option key={item} value={item}>
                {titleCase(item)}
              </option>
            ))}
          </select>
        </label>
        <label className="date-filter">
          <span>After</span>
          <input type="date" value={after} onChange={(event) => setAfter(event.target.value)} />
        </label>
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading CI sessions" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No CI sessions"
          description="Sessions created by GitHub, GitLab or Jenkins will appear here."
          icon={PlayCircle}
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No sessions match"
          description="Adjust the provider, status or date filters."
          icon={PlayCircle}
        />
      ) : (
        <DataTable
          data={filtered}
          columns={columns}
          rowLabel={(row) => `Open CI session ${shortId(stringValue(row, "id"))}`}
          onRowClick={(row) => navigate(`/ci-sessions/${stringValue(row, "id")}`)}
        />
      )}
    </div>
  );
}
