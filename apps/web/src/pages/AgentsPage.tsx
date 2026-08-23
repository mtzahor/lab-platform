import { useQuery } from "@tanstack/react-query";
import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { Bot, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { generatedApi, labels, records, stringValue, type ApiRecord } from "../api/client";
import { useLive } from "../app/LiveProvider";
import {
  Button,
  ChipList,
  DataTable,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  SearchField,
  StatusBadge,
} from "../components/ui";
import { formatRelative } from "../lib/format";

export function AgentsPage() {
  const navigate = useNavigate();
  const live = useLive();
  const query = useQuery({
    queryKey: ["agents"],
    queryFn: generatedApi.listAgents,
    staleTime: 10_000,
    refetchInterval: live.state === "live" ? false : live.pollingIntervalMs,
  });
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [location, setLocation] = useState("");
  const [version, setVersion] = useState("");
  const items = records(query.data);
  const locations = [
    ...new Set(items.map((item) => stringValue(item, "location")).filter(Boolean) as string[]),
  ].sort();
  const versions = [
    ...new Set(items.map((item) => stringValue(item, "version")).filter(Boolean) as string[]),
  ].sort();
  const filtered = items.filter(
    (item) =>
      (!search ||
        `${stringValue(item, "name")} ${stringValue(item, "slug")} ${Object.values(labels(item)).join(" ")}`
          .toLowerCase()
          .includes(search.toLowerCase())) &&
      (!status || stringValue(item, "status") === status) &&
      (!location || stringValue(item, "location") === location) &&
      (!version || stringValue(item, "version") === version),
  );
  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor((row) => stringValue(row, "name") ?? stringValue(row, "slug") ?? "", {
        id: "name",
        header: "Agent",
        cell: ({ row, getValue }) => (
          <div className="primary-cell">
            <Link to={`/agents/${stringValue(row.original, "id")}`}>{getValue()}</Link>
            <code>{stringValue(row.original, "slug")}</code>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "status") ?? "UNKNOWN", {
        id: "status",
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      column.accessor((row) => stringValue(row, "version") ?? "", {
        id: "version",
        header: "Version",
        cell: ({ getValue, row }) => (
          <div className="stacked-cell">
            <code>{getValue()}</code>
            <small>Protocol {stringValue(row.original, "protocol_version") ?? "—"}</small>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "location") ?? "", {
        id: "location",
        header: "Location",
        cell: ({ getValue }) => getValue() || "—",
      }),
      column.display({
        id: "labels",
        header: "Labels",
        cell: ({ row }) => (
          <ChipList
            values={Object.entries(labels(row.original)).map(([key, value]) => `${key}=${value}`)}
            limit={2}
          />
        ),
      }),
      column.accessor((row) => Number(stringValue(row, "bench_count") ?? 0), {
        id: "benches",
        header: "Benches",
      }),
      column.accessor((row) => stringValue(row, "last_seen_at") ?? "", {
        id: "heartbeat",
        header: "Heartbeat",
        cell: ({ getValue }) => formatRelative(getValue()),
      }),
      column.accessor((row) => stringValue(row, "last_connected_at") ?? "", {
        id: "connected",
        header: "Last connected",
        cell: ({ getValue }) => formatRelative(getValue()),
      }),
    ];
  }, []);
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Infrastructure"
        title="Agents"
        description="Connectivity, compatibility and load across remote lab controllers."
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
          placeholder="Search Agent name, slug or label…"
        />
        <label className="compact-select">
          <span className="sr-only">Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All states</option>
            {[
              "ONLINE",
              "OFFLINE",
              "DEGRADED",
              "DRAINING",
              "DRAINED",
              "INCOMPATIBLE",
              "REVOKED",
            ].map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="compact-select">
          <span className="sr-only">Location</span>
          <select value={location} onChange={(event) => setLocation(event.target.value)}>
            <option value="">All locations</option>
            {locations.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="compact-select">
          <span className="sr-only">Version</span>
          <select value={version} onChange={(event) => setVersion(event.target.value)}>
            <option value="">All versions</option>
            {versions.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading Agents" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No Agents are connected"
          description="Create an enrollment token to register the first Agent."
          icon={Bot}
        />
      ) : filtered.length === 0 ? (
        <EmptyState title="No Agents match" description="Adjust the current filters." icon={Bot} />
      ) : (
        <DataTable
          data={filtered}
          columns={columns}
          rowLabel={(row) => `Open ${stringValue(row, "name")}`}
          onRowClick={(row) => navigate(`/agents/${stringValue(row, "id")}`)}
        />
      )}
    </div>
  );
}
