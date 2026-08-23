import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { CircuitBoard, Filter, LayoutGrid, List, RefreshCw, SlidersHorizontal } from "lucide-react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  generatedApi,
  labels,
  nested,
  records,
  stringList,
  stringValue,
  type ApiRecord,
} from "../api/client";
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
import { formatRelative, titleCase } from "../lib/format";

function searchableBench(bench: ApiRecord): string {
  const agent = nested(bench, "agent");
  return [
    stringValue(bench, "name"),
    stringValue(bench, "id"),
    stringValue(bench, "agent_slug"),
    stringValue(bench, "target_type"),
    stringValue(agent, "name"),
    stringValue(bench, "location"),
    ...Object.values(labels(bench)),
  ]
    .join(" ")
    .toLowerCase();
}

function reservationStatus(bench: ApiRecord): string {
  const availability = stringValue(bench, "availability");
  if (availability) return availability.toUpperCase();
  const reservation = nested(bench, "current_reservation");
  if (reservation)
    return (
      stringValue(reservation, "state") ??
      stringValue(reservation, "status") ??
      "RESERVED"
    ).toUpperCase();
  const status = (stringValue(bench, "status") ?? "UNKNOWN").toUpperCase();
  if (status === "ONLINE") return "AVAILABLE";
  if (status === "DRAINING") return "DRAINING";
  return status;
}

export function BenchesPage() {
  const navigate = useNavigate();
  const live = useLive();
  const [params, setParams] = useSearchParams();
  const [filtersOpen, setFiltersOpen] = useState(true);
  const [view, setView] = useState<"table" | "cards">("table");
  const query = useQuery({
    queryKey: ["benches"],
    queryFn: generatedApi.listBenches,
    staleTime: 10_000,
    refetchInterval: live.state === "live" ? false : live.pollingIntervalMs,
  });
  const benches = records(query.data);
  const search = params.get("q") ?? "";
  const status = params.get("status") ?? "";
  const health = params.get("health") ?? (params.get("attention") ? "attention" : "");
  const kind = params.get("kind") ?? "";
  const targetType = params.get("target_type") ?? "";
  const capability = params.get("capability") ?? "";
  const agent = params.get("agent") ?? "";
  const location = params.get("location") ?? "";
  function updateFilter(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key === "health") next.delete("attention");
    setParams(next, { replace: true });
  }
  const options = useMemo(
    () => ({
      capabilities: [
        ...new Set(benches.flatMap((bench) => stringList(bench, "capabilities"))),
      ].sort(),
      agents: [
        ...new Set(
          benches.map((bench) => stringValue(bench, "agent_slug")).filter(Boolean) as string[],
        ),
      ].sort(),
      locations: [
        ...new Set(
          benches
            .map(
              (bench) =>
                stringValue(bench, "location") ?? stringValue(nested(bench, "agent"), "location"),
            )
            .filter(Boolean) as string[],
        ),
      ].sort(),
      targetTypes: [
        ...new Set(
          benches.map((bench) => stringValue(bench, "target_type")).filter(Boolean) as string[],
        ),
      ].sort(),
    }),
    [benches],
  );
  const filtered = useMemo(
    () =>
      benches.filter((bench) => {
        if (search && !searchableBench(bench).includes(search.toLowerCase())) return false;
        if (status && reservationStatus(bench) !== status) return false;
        const benchHealth = (stringValue(bench, "health") ?? "UNKNOWN").toUpperCase();
        const benchStatus = (stringValue(bench, "status") ?? "UNKNOWN").toUpperCase();
        if (
          health === "attention" &&
          !["DEGRADED", "OFFLINE"].includes(benchHealth) &&
          benchStatus !== "OFFLINE"
        )
          return false;
        if (health && health !== "attention" && benchHealth !== health) return false;
        if (kind && (stringValue(bench, "kind") ?? "").toUpperCase() !== kind) return false;
        if (targetType && stringValue(bench, "target_type") !== targetType) return false;
        if (capability && !stringList(bench, "capabilities").includes(capability)) return false;
        if (agent && stringValue(bench, "agent_slug") !== agent) return false;
        if (
          location &&
          (stringValue(bench, "location") ?? stringValue(nested(bench, "agent"), "location")) !==
            location
        )
          return false;
        return true;
      }),
    [agent, benches, capability, health, kind, location, search, status, targetType],
  );
  const columnHelper = createColumnHelper<ApiRecord>();
  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(
    () => [
      columnHelper.accessor((row) => stringValue(row, "name") ?? "", {
        id: "name",
        header: "Bench",
        size: 230,
        cell: ({ row }) => (
          <div className="primary-cell">
            <Link to={`/benches/${encodeURIComponent(stringValue(row.original, "id") ?? "")}`}>
              {stringValue(row.original, "name")}
            </Link>
            <code>{stringValue(row.original, "id")}</code>
          </div>
        ),
      }),
      columnHelper.accessor((row) => reservationStatus(row), {
        id: "reservation",
        header: "Reservation",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      columnHelper.accessor((row) => (stringValue(row, "health") ?? "UNKNOWN").toUpperCase(), {
        id: "health",
        header: "Health",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      columnHelper.accessor(
        (row) => stringValue(row, "agent_slug") ?? stringValue(nested(row, "agent"), "name") ?? "",
        { id: "agent", header: "Agent", cell: ({ getValue }) => getValue() || "—" },
      ),
      columnHelper.accessor(
        (row) =>
          stringValue(row, "location") ?? stringValue(nested(row, "agent"), "location") ?? "",
        { id: "location", header: "Location", cell: ({ getValue }) => getValue() || "—" },
      ),
      columnHelper.accessor((row) => stringValue(row, "backend_id") ?? "", {
        id: "backend",
        header: "Backend",
        cell: ({ row, getValue }) => (
          <div className="stacked-cell">
            <span>{getValue() || "—"}</span>
            <small>{titleCase(stringValue(row.original, "kind"))}</small>
          </div>
        ),
      }),
      columnHelper.display({
        id: "capabilities",
        header: "Capabilities",
        cell: ({ row }) => <ChipList values={stringList(row.original, "capabilities")} limit={2} />,
      }),
      columnHelper.accessor(
        (row) => stringValue(nested(row, "active_operation"), "operation_type") ?? "",
        {
          id: "operation",
          header: "Active operation",
          cell: ({ row, getValue }) =>
            getValue() ? (
              <div className="stacked-cell">
                <span>{titleCase(getValue())}</span>
                <small>
                  {stringValue(nested(row.original, "active_operation"), "progress") ?? "0"}%
                </small>
              </div>
            ) : (
              <span className="muted">Idle</span>
            ),
        },
      ),
      columnHelper.accessor((row) => stringValue(row, "firmware_version") ?? "", {
        id: "firmware",
        header: "Firmware",
        cell: ({ getValue }) => (getValue() ? <code>{getValue()}</code> : "—"),
      }),
      columnHelper.accessor((row) => stringValue(row, "last_seen_at") ?? "", {
        id: "lastSeen",
        header: "Last seen",
        cell: ({ getValue }) => <span title={getValue()}>{formatRelative(getValue())}</span>,
      }),
    ],
    [columnHelper],
  );
  const activeFilterCount = [status, health, kind, targetType, capability, agent, location].filter(
    Boolean,
  ).length;
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Inventory"
        title="Benches"
        description="Find an available target and understand exactly why another is unavailable."
        actions={
          <Button
            variant="secondary"
            icon={RefreshCw}
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            {query.isFetching ? "Refreshing…" : "Refresh"}
          </Button>
        }
      />
      <div className="toolbar">
        <SearchField
          value={search}
          onChange={(event) => updateFilter("q", event.target.value)}
          placeholder="Search name, bench ID, Agent or label…"
        />
        <Button
          variant="secondary"
          icon={SlidersHorizontal}
          onClick={() => setFiltersOpen((open) => !open)}
        >
          Filters
          {activeFilterCount > 0 && <span className="button-count">{activeFilterCount}</span>}
        </Button>
        <div className="view-toggle" aria-label="View">
          <button
            className={view === "table" ? "active" : ""}
            onClick={() => setView("table")}
            aria-label="Table view"
          >
            <List size={17} />
          </button>
          <button
            className={view === "cards" ? "active" : ""}
            onClick={() => setView("cards")}
            aria-label="Card view"
          >
            <LayoutGrid size={17} />
          </button>
        </div>
      </div>
      {filtersOpen && (
        <div className="filter-bar">
          <label>
            <span>Status</span>
            <select value={status} onChange={(event) => updateFilter("status", event.target.value)}>
              <option value="">All</option>
              {["AVAILABLE", "RESERVED", "BUSY", "DRAINING", "OFFLINE", "UNKNOWN"].map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Health</span>
            <select value={health} onChange={(event) => updateFilter("health", event.target.value)}>
              <option value="">All</option>
              <option value="attention">Needs attention</option>
              {["HEALTHY", "DEGRADED", "OFFLINE", "UNKNOWN"].map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Kind</span>
            <select value={kind} onChange={(event) => updateFilter("kind", event.target.value)}>
              <option value="">Simulated + physical</option>
              <option>SIMULATED</option>
              <option>PHYSICAL</option>
            </select>
          </label>
          <label>
            <span>Target type</span>
            <select
              value={targetType}
              onChange={(event) => updateFilter("target_type", event.target.value)}
            >
              <option value="">Any target</option>
              {options.targetTypes.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Capability</span>
            <select
              value={capability}
              onChange={(event) => updateFilter("capability", event.target.value)}
            >
              <option value="">Any capability</option>
              {options.capabilities.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Agent</span>
            <select value={agent} onChange={(event) => updateFilter("agent", event.target.value)}>
              <option value="">Any Agent</option>
              {options.agents.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Location</span>
            <select
              value={location}
              onChange={(event) => updateFilter("location", event.target.value)}
            >
              <option value="">Any location</option>
              {options.locations.map((item) => (
                <option key={item}>{item}</option>
              ))}
            </select>
          </label>
          {activeFilterCount > 0 && (
            <button
              className="clear-filters"
              onClick={() => setParams(search ? { q: search } : {})}
            >
              Clear filters
            </button>
          )}
        </div>
      )}
      <div className="result-summary">
        <span>
          <strong>{filtered.length.toLocaleString()}</strong> of {benches.length.toLocaleString()}{" "}
          benches
        </span>
        {query.dataUpdatedAt > 0 && (
          <span>Last updated {formatRelative(new Date(query.dataUpdatedAt).toISOString())}</span>
        )}
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading bench inventory" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : benches.length === 0 ? (
        <EmptyState
          title="No benches are registered"
          description="Enroll an Agent or start SimLab to add benches."
          icon={CircuitBoard}
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No benches match these filters"
          description="Clear a filter or broaden your search."
          icon={Filter}
        />
      ) : view === "table" ? (
        <DataTable
          data={filtered}
          columns={columns}
          rowLabel={(row) => `Open ${stringValue(row, "name")}`}
          onRowClick={(row) =>
            navigate(`/benches/${encodeURIComponent(stringValue(row, "id") ?? "")}`)
          }
        />
      ) : (
        <div className="bench-card-grid">
          {filtered.map((bench) => (
            <Link
              className="bench-card"
              key={stringValue(bench, "id")}
              to={`/benches/${encodeURIComponent(stringValue(bench, "id") ?? "")}`}
            >
              <div className="bench-card-top">
                <span className="resource-icon">
                  <CircuitBoard size={18} />
                </span>
                <StatusBadge status={stringValue(bench, "health")} />
              </div>
              <h2>{stringValue(bench, "name")}</h2>
              <code>{stringValue(bench, "id")}</code>
              <div className="bench-card-status">
                <div>
                  <span>Reservation</span>
                  <StatusBadge status={reservationStatus(bench)} />
                </div>
                <div>
                  <span>Agent</span>
                  <strong>{stringValue(bench, "agent_slug")}</strong>
                </div>
              </div>
              <ChipList values={stringList(bench, "capabilities")} limit={3} />
              <small>Seen {formatRelative(stringValue(bench, "last_seen_at"))}</small>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
