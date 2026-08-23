import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { CalendarClock, CalendarPlus, Clock3, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  apiFetch,
  asRecord,
  errorMessage,
  idempotencyKey,
  records,
  stringValue,
  type ApiRecord,
} from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { useToast } from "../app/ToastProvider";
import {
  Button,
  ConfirmDialog,
  DataTable,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  SearchField,
  StatusBadge,
} from "../components/ui";
import {
  reservationActionAllowed,
  reservationActionExplanation,
  reservationAdministrator,
  reservationOwnedByCaller,
} from "../features/reservations/permissions";
import { useApiList, useApiMutation } from "../hooks/useApi";
import { formatDate, formatDuration, formatRelative } from "../lib/format";

function reservation(item: ApiRecord): ApiRecord {
  return asRecord(item.reservation) ?? item;
}
function lease(item: ApiRecord): ApiRecord | undefined {
  return asRecord(item.lease) ?? asRecord(reservation(item).lease);
}
function reservationState(item: ApiRecord): string {
  return (
    stringValue(reservation(item), "status") ??
    stringValue(reservation(item), "state") ??
    stringValue(lease(item), "state") ??
    "UNKNOWN"
  ).toUpperCase();
}

export function ReservationsPage() {
  const auth = useAuth();
  const { notify } = useToast();
  const query = useApiList("reservations", "/api/v1/reservations?limit=1000");
  const benchesQuery = useApiList("benches", "/api/v1/benches");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [target, setTarget] = useState<ApiRecord>();
  const [selected, setSelected] = useState<ApiRecord>();
  const items = records(query.data);
  const filtered = useMemo(
    () =>
      items.filter((item) => {
        const value = reservation(item);
        const haystack =
          `${stringValue(value, "bench_id")} ${stringValue(value, "owner")} ${stringValue(value, "id")}`.toLowerCase();
        return (
          (!search || haystack.includes(search.toLowerCase())) &&
          (!status || reservationState(item) === status)
        );
      }),
    [items, search, status],
  );
  const mutation = useApiMutation(
    async (item: ApiRecord) => {
      const value = reservation(item);
      const currentLease = lease(item);
      const action = reservationState(item) === "SCHEDULED" ? "cancel" : "release";
      if (!reservationActionAllowed(item, action)) {
        throw new Error(reservationActionExplanation(action));
      }
      const administrator = reservationAdministrator(item) && !reservationOwnedByCaller(item);
      const path = administrator ? "revoke" : "release";
      const body =
        reservationState(item) === "SCHEDULED"
          ? { idempotency_key: idempotencyKey(`web-${path}`) }
          : {
              expected_lease_version: Number(stringValue(currentLease, "lease_version") ?? 1),
              idempotency_key: idempotencyKey(`web-${path}`),
            };
      return apiFetch(`/api/v1/reservations/${stringValue(value, "id")}/${path}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
    },
    [["reservations"], ["benches"], ["overview"]],
  );
  const columns = useMemo<ColumnDef<ApiRecord, any>[]>(() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor((row) => stringValue(reservation(row), "bench_id") ?? "", {
        id: "bench",
        header: "Bench",
        cell: ({ getValue }) => (
          <Link to={`/benches/${encodeURIComponent(getValue())}`}>{getValue()}</Link>
        ),
      }),
      column.accessor((row) => stringValue(reservation(row), "owner") ?? "", {
        id: "owner",
        header: "Owner",
      }),
      column.accessor((row) => reservationState(row), {
        id: "status",
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      column.accessor(
        (row) =>
          stringValue(reservation(row), "starts_at") ??
          stringValue(reservation(row), "created_at") ??
          "",
        {
          id: "start",
          header: "Started",
          cell: ({ getValue }) => (
            <span title={formatDate(getValue())}>{formatRelative(getValue())}</span>
          ),
        },
      ),
      column.accessor(
        (row) =>
          stringValue(reservation(row), "ends_at") ?? stringValue(lease(row), "valid_until") ?? "",
        {
          id: "end",
          header: "Ends",
          cell: ({ getValue, row }) => (
            <div className="stacked-cell">
              <strong>{getValue() ? formatRelative(getValue()) : "Server managed"}</strong>
              <small>
                {formatDuration(
                  stringValue(reservation(row.original), "starts_at") ??
                    stringValue(reservation(row.original), "created_at"),
                  getValue(),
                )}
              </small>
            </div>
          ),
        },
      ),
      column.accessor((row) => Number(stringValue(lease(row), "lease_version") ?? 0), {
        id: "lease",
        header: "Lease",
        cell: ({ getValue }) => (getValue() ? `v${getValue()}` : "—"),
      }),
      column.accessor((row) => stringValue(reservation(row), "source") ?? "", {
        id: "source",
        header: "Source",
        cell: ({ getValue }) => getValue() || "API",
      }),
      column.display({
        id: "actions",
        header: "",
        cell: ({ row }) => {
          const rowState = reservationState(row.original);
          if (!["ACTIVE", "RESERVED", "SCHEDULED"].includes(rowState)) return null;
          const action = rowState === "SCHEDULED" ? "cancel" : "release";
          const permitted = reservationActionAllowed(row.original, action);
          const administrator =
            reservationAdministrator(row.original) && !reservationOwnedByCaller(row.original);
          return (
            <Button
              variant="ghost"
              disabled={!permitted}
              title={permitted ? undefined : reservationActionExplanation(action)}
              onClick={(event) => {
                event.stopPropagation();
                setSelected(row.original);
              }}
            >
              {rowState === "SCHEDULED" ? "Cancel" : administrator ? "Revoke" : "Release"}
            </Button>
          );
        },
      }),
    ];
  }, []);
  async function releaseSelected() {
    if (!selected) return;
    const scheduled = reservationState(selected) === "SCHEDULED";
    const administrator = reservationAdministrator(selected) && !reservationOwnedByCaller(selected);
    const action = scheduled ? "cancel" : "release";
    if (!reservationActionAllowed(selected, action)) return;
    try {
      await mutation.mutateAsync(selected);
      notify({
        title: scheduled
          ? "Scheduled reservation cancelled"
          : administrator
            ? "Reservation revoked"
            : "Reservation released",
        message: scheduled
          ? `${stringValue(reservation(selected), "bench_id")} no longer has that upcoming slot.`
          : `${stringValue(reservation(selected), "bench_id")} is returning to the pool.`,
        tone: "success",
      });
      setSelected(undefined);
    } catch (error) {
      notify({ title: "Release failed", message: errorMessage(error), tone: "error" });
    }
  }
  const selectedScheduled = reservationState(selected ?? {}) === "SCHEDULED";
  const selectedAdministrator =
    reservationAdministrator(selected) && !reservationOwnedByCaller(selected);
  const selectedVerb = selectedScheduled ? "Cancel" : selectedAdministrator ? "Revoke" : "Release";
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Scheduling"
        title="Reservations"
        description="Active leases, upcoming demand and the server-reported queue."
        actions={
          <>
            <Button variant="secondary" icon={RefreshCw} onClick={() => void query.refetch()}>
              Refresh
            </Button>
            <Button
              icon={CalendarPlus}
              onClick={() => setCreateOpen(true)}
              disabled={!auth.can("benches:reserve")}
            >
              New reservation
            </Button>
          </>
        }
      />
      <div className="toolbar">
        <SearchField
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search bench, owner or reservation ID…"
        />
        <label className="compact-select">
          <span className="sr-only">Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All states</option>
            {["ACTIVE", "SCHEDULED", "QUEUED", "RELEASED", "EXPIRED", "REVOKED"].map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading reservations" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No reservations yet"
          description="Reserve an available bench to begin work."
          icon={CalendarClock}
          action={
            <Button onClick={() => setCreateOpen(true)} disabled={!auth.can("benches:reserve")}>
              Reserve a bench
            </Button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No reservations match"
          description="Change the search or state filter."
          icon={Clock3}
        />
      ) : (
        <>
          <DataTable data={filtered} columns={columns} />
          <p className="permission-note">
            Reservation actions are available to the owner. Bench administrators can revoke an
            active reservation or cancel an upcoming slot.
          </p>
        </>
      )}
      <Dialog
        open={createOpen}
        onClose={() => {
          setCreateOpen(false);
          setTarget(undefined);
        }}
        title="Choose a bench"
        description="Only inventory reported as available is shown."
      >
        <Field label="Available bench">
          <select
            value={stringValue(target, "id") ?? ""}
            onChange={(event) =>
              setTarget(
                records(benchesQuery.data).find(
                  (bench) => stringValue(bench, "id") === event.target.value,
                ),
              )
            }
          >
            <option value="">Select a bench</option>
            {records(benchesQuery.data)
              .filter(
                (bench) =>
                  (
                    stringValue(bench, "availability") ??
                    stringValue(bench, "status") ??
                    ""
                  ).toUpperCase() === "AVAILABLE",
              )
              .map((bench) => (
                <option key={stringValue(bench, "id")} value={stringValue(bench, "id")}>
                  {stringValue(bench, "name")} · {stringValue(bench, "id")}
                </option>
              ))}
          </select>
        </Field>
        <div className="dialog-actions">
          <Button variant="ghost" onClick={() => setCreateOpen(false)}>
            Cancel
          </Button>
          {target ? (
            <Link
              className="button button-primary"
              to={`/benches/${encodeURIComponent(stringValue(target, "id") ?? "")}`}
            >
              Continue on bench
            </Link>
          ) : (
            <Button disabled>Continue</Button>
          )}
        </div>
      </Dialog>
      <ConfirmDialog
        open={Boolean(selected)}
        title={`${selectedVerb} ${stringValue(reservation(selected ?? {}), "bench_id")}?`}
        message={
          selectedScheduled ? (
            <>
              Cancel the upcoming slot owned by{" "}
              <strong>{stringValue(reservation(selected ?? {}), "owner")}</strong>? It will never
              activate.
            </>
          ) : (
            <>
              {selectedAdministrator ? "Revoke" : "Release"} the reservation owned by{" "}
              <strong>{stringValue(reservation(selected ?? {}), "owner")}</strong>? Active work may
              be interrupted.
            </>
          )
        }
        confirmLabel={
          selectedScheduled
            ? "Cancel reservation"
            : selectedAdministrator
              ? "Revoke reservation"
              : "Release reservation"
        }
        onConfirm={() => void releaseSelected()}
        onClose={() => setSelected(undefined)}
        busy={mutation.isPending}
      />
    </div>
  );
}
