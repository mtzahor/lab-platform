import {
  ArrowLeft,
  Download,
  ExternalLink,
  FileArchive,
  Pause,
  Play,
  RefreshCw,
  Search,
  StopCircle,
  TerminalSquare,
  WifiOff,
} from "lucide-react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  apiBase,
  apiFetch,
  booleanValue,
  errorMessage,
  nested,
  records,
  stringValue,
  type ApiRecord,
} from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { useLive } from "../app/LiveProvider";
import { useToast } from "../app/ToastProvider";
import {
  Button,
  ConfirmDialog,
  DetailGrid,
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  SearchField,
  StatusBadge,
} from "../components/ui";
import { useApiDetail, useApiList, useApiMutation } from "../hooks/useApi";
import {
  formatBytes,
  formatDate,
  formatDuration,
  formatRelative,
  shortId,
  titleCase,
} from "../lib/format";
import { isActiveStatus } from "../lib/status";
import { boundedLogLines, DEFAULT_LOG_BUFFER_LIMIT } from "../lib/logs";

const MAX_LOG_LINES = DEFAULT_LOG_BUFFER_LIMIT;
const LOG_PAGE_SIZE = 300;
const SERIAL_API_PAGE_SIZE = 2_000;
const MAX_SERIAL_QUERY_PAGES = Math.ceil(MAX_LOG_LINES / SERIAL_API_PAGE_SIZE);

export function OperationDetailPage() {
  const { operationId = "" } = useParams();
  const auth = useAuth();
  const live = useLive();
  const { notify } = useToast();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [paused, setPaused] = useState(false);
  const [follow, setFollow] = useState(true);
  const [search, setSearch] = useState("");
  const [pattern, setPattern] = useState("");
  const [page, setPage] = useState(0);
  const frozenLines = useRef<string[] | undefined>(undefined);
  const logEnd = useRef<HTMLDivElement>(null);
  const query = useApiDetail("operations", `/api/v1/operations/${operationId}`);
  const operation = query.data;
  const serialQuery = useInfiniteQuery({
    queryKey: ["serial", operationId],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      apiFetch<ApiRecord>(
        `/api/v1/operations/${operationId}/serial?cursor=${pageParam}&limit=${SERIAL_API_PAGE_SIZE}`,
      ),
    getNextPageParam: (lastPage) =>
      booleanValue(lastPage, "has_more")
        ? Number(stringValue(lastPage, "next_cursor") ?? 0)
        : undefined,
    enabled: Boolean(operationId),
    maxPages: MAX_SERIAL_QUERY_PAGES,
    staleTime: 2_000,
    refetchInterval: live.state === "live" ? 30_000 : live.pollingIntervalMs,
  });
  const artifactQuery = useApiList(
    "artifacts",
    "/api/v1/artifacts?limit=500",
    auth.can("artifacts:read"),
  );
  const status = stringValue(operation, "status") ?? "UNKNOWN";
  const active = isActiveStatus(status);
  const { data: serialData, fetchNextPage, hasNextPage, isFetchingNextPage } = serialQuery;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage) {
      void fetchNextPage();
    }
  }, [fetchNextPage, hasNextPage, isFetchingNextPage, serialData?.pages.length]);
  const serialPages = serialData?.pages ?? [];
  const latestSerialPage = serialPages.at(-1);
  const serialArtifact = nested(latestSerialPage, "artifact");
  const serialArtifactReady = booleanValue(serialArtifact, "ready");
  const serialArtifactUrl = stringValue(serialArtifact, "download_url");
  const serialRecords = serialPages.flatMap((serialPage) => records(serialPage, "lines"));
  const allLines = boundedLogLines(
    serialRecords.length
      ? serialRecords.map(
          (line) =>
            `${stringValue(line, "timestamp") ? `[${stringValue(line, "timestamp")}] ` : ""}${stringValue(line, "text") ?? ""}`,
        )
      : (stringValue(latestSerialPage, "text") ?? "").split(/\r?\n/),
  );
  if (paused && !frozenLines.current) frozenLines.current = allLines;
  if (!paused) frozenLines.current = undefined;
  const lines = paused ? (frozenLines.current ?? allLines) : allLines;
  const filteredLines = useMemo(() => {
    let values = lines;
    if (search) values = values.filter((line) => line.toLowerCase().includes(search.toLowerCase()));
    if (pattern) {
      try {
        const regex = new RegExp(pattern, "i");
        values = values.filter((line) => regex.test(line));
      } catch {
        /* Invalid patterns simply match nothing until corrected. */ return [];
      }
    }
    return values;
  }, [lines, pattern, search]);
  const visibleLines = filteredLines.slice(
    Math.max(0, filteredLines.length - (page + 1) * LOG_PAGE_SIZE),
    filteredLines.length - page * LOG_PAGE_SIZE || undefined,
  );
  useEffect(() => {
    if (follow && !paused && page === 0) logEnd.current?.scrollIntoView({ block: "nearest" });
  }, [follow, page, paused, visibleLines.length]);
  const artifacts = records(artifactQuery.data).filter(
    (item) =>
      stringValue(item, "owner_id") === operationId ||
      stringValue(nested(item, "metadata"), "operation_id") === operationId ||
      stringValue(item, "command_id") === stringValue(operation, "remote_command_id"),
  );
  const cancelMutation = useApiMutation(
    () =>
      apiFetch(`/api/v1/operations/${operationId}/cancel`, {
        method: "POST",
        body: JSON.stringify({ reason: "Cancelled from web dashboard" }),
      }),
    [["operations"], ["overview"]],
  );
  const reconcileMutation = useApiMutation(
    () =>
      apiFetch(`/api/v1/operations/${operationId}/reconcile`, {
        method: "POST",
        body: JSON.stringify({}),
      }),
    [["operations"]],
  );
  async function cancel() {
    try {
      await cancelMutation.mutateAsync();
      notify({
        title: "Cancellation requested",
        message: "The Agent will stop at a safe boundary.",
        tone: "success",
      });
      setConfirmCancel(false);
    } catch (error) {
      notify({ title: "Cancellation failed", message: errorMessage(error), tone: "error" });
    }
  }
  async function reconcile() {
    try {
      await reconcileMutation.mutateAsync();
      notify({
        title: "Reconciliation requested",
        message: "The control plane is asking the Agent for its current state.",
        tone: "success",
      });
    } catch (error) {
      notify({ title: "Reconciliation failed", message: errorMessage(error), tone: "error" });
    }
  }
  if (query.isLoading)
    return (
      <>
        <PageHeader title="Operation" />
        <LoadingState label="Loading operation" />
      </>
    );
  if (query.error || !operation)
    return (
      <>
        <PageHeader title="Operation unavailable" />
        <ErrorState
          error={query.error ?? new Error("Operation not found")}
          retry={() => void query.refetch()}
        />
      </>
    );
  return (
    <div className="page-stack">
      <Link className="back-link" to="/operations">
        <ArrowLeft size={15} /> Operations
      </Link>
      <PageHeader
        eyebrow={`Operation ${shortId(operationId)}`}
        title={titleCase(stringValue(operation, "operation_type"))}
        description={
          <span className="headline-status">
            <StatusBadge status={status} />
            <span>
              on{" "}
              <Link to={`/benches/${encodeURIComponent(stringValue(operation, "bench_id") ?? "")}`}>
                {stringValue(operation, "bench_id")}
              </Link>
            </span>
            <span>
              {formatDuration(
                stringValue(operation, "started_at") ?? stringValue(operation, "created_at"),
                stringValue(operation, "completed_at"),
              )}
            </span>
          </span>
        }
        actions={
          <>
            {status === "UNKNOWN" && auth.can("agents:manage") && (
              <Button
                variant="secondary"
                icon={RefreshCw}
                onClick={() => void reconcile()}
                disabled={reconcileMutation.isPending}
              >
                Reconcile
              </Button>
            )}
            {active && auth.can("operations:cancel") && (
              <Button variant="danger" icon={StopCircle} onClick={() => setConfirmCancel(true)}>
                Cancel operation
              </Button>
            )}
          </>
        }
      />
      {status === "UNKNOWN" && (
        <div className="warning-callout">
          <WifiOff size={20} />
          <div>
            <strong>Operation status unknown</strong>
            <p>
              Awaiting Agent reconciliation. The last known state is preserved and has not been
              marked failed.
            </p>
          </div>
        </div>
      )}
      <div className="operation-progress">
        <progress
          value={Number(stringValue(operation, "progress") ?? 0)}
          max={100}
          aria-label={`${stringValue(operation, "progress") ?? "0"}% complete`}
        />
        <div>
          <strong>{stringValue(operation, "progress") ?? "0"}%</strong>
          <span>{stringValue(operation, "message") ?? "Waiting for the next Agent update."}</span>
        </div>
      </div>
      <div className="detail-layout">
        <div className="detail-main">
          <Panel
            title="Serial log"
            description="Bounded, read-only output rendered as untrusted text"
            action={
              <div className="inline-actions">
                <StatusBadge
                  status={
                    live.state === "live"
                      ? "ONLINE"
                      : live.state === "polling"
                        ? "DEGRADED"
                        : "UNKNOWN"
                  }
                  label={
                    live.state === "live"
                      ? "Streaming"
                      : live.state === "polling"
                        ? "Polling"
                        : "Reconnecting"
                  }
                />
                <Button
                  variant="ghost"
                  icon={paused ? Play : Pause}
                  onClick={() => setPaused((value) => !value)}
                >
                  {paused ? "Resume" : "Pause"}
                </Button>
                {serialArtifactReady && serialArtifactUrl ? (
                  <a
                    className="button button-ghost"
                    href={`${apiBase}${serialArtifactUrl}`}
                    download={stringValue(serialArtifact, "name") ?? `${operationId}-serial.log`}
                  >
                    <Download size={16} /> Download full log
                  </a>
                ) : (
                  <Button
                    variant="ghost"
                    icon={Download}
                    disabled
                    title="The authoritative serial artifact is not ready yet."
                  >
                    Full log pending
                  </Button>
                )}
              </div>
            }
          >
            <div className="log-toolbar">
              <SearchField
                value={search}
                onChange={(event) => {
                  setSearch(event.target.value);
                  setPage(0);
                }}
                placeholder="Search log text…"
              />
              <label className="pattern-field">
                <Search size={15} />
                <span className="sr-only">Filter by regular expression</span>
                <input
                  value={pattern}
                  onChange={(event) => {
                    setPattern(event.target.value);
                    setPage(0);
                  }}
                  placeholder="Filter pattern (regex)"
                />
              </label>
              <label className="check-inline">
                <input
                  type="checkbox"
                  checked={follow}
                  onChange={(event) => setFollow(event.target.checked)}
                />{" "}
                Follow newest
              </label>
            </div>
            {serialQuery.isLoading ? (
              <LoadingState label="Loading serial output" />
            ) : serialQuery.error && !lines.length ? (
              <EmptyState
                title="No serial output"
                description="This operation has no synchronized serial artifact yet."
                icon={TerminalSquare}
              />
            ) : lines.length ? (
              <>
                <div className="log-meta">
                  <span>
                    Showing {visibleLines.length.toLocaleString()} of{" "}
                    {filteredLines.length.toLocaleString()} matched lines
                  </span>
                  {booleanValue(latestSerialPage, "tail_truncated") && (
                    <span>Earlier live output is available in the full artifact</span>
                  )}
                  {booleanValue(latestSerialPage, "artifact_truncated") && (
                    <span>Artifact reached its configured size limit</span>
                  )}
                  {allLines.length >= MAX_LOG_LINES && (
                    <span>Live buffer capped at {MAX_LOG_LINES.toLocaleString()} lines</span>
                  )}
                </div>
                <pre className="log-viewer" aria-label="Serial output">
                  {visibleLines.map((line, index) => (
                    <span key={`${page}:${index}`}>
                      <i>
                        {String(
                          Math.max(
                            1,
                            filteredLines.length -
                              page * LOG_PAGE_SIZE -
                              visibleLines.length +
                              index +
                              1,
                          ),
                        ).padStart(5, "0")}
                      </i>
                      <code>{line.replaceAll("\uFFFD", "�")}</code>
                    </span>
                  ))}
                  <div ref={logEnd} />
                </pre>
                {filteredLines.length > LOG_PAGE_SIZE && (
                  <div className="log-pagination">
                    <Button
                      variant="ghost"
                      onClick={() => setPage((value) => value + 1)}
                      disabled={(page + 1) * LOG_PAGE_SIZE >= filteredLines.length}
                    >
                      Older lines
                    </Button>
                    <span>Chunk {page + 1}</span>
                    <Button
                      variant="ghost"
                      onClick={() => setPage((value) => Math.max(0, value - 1))}
                      disabled={page === 0}
                    >
                      Newer lines
                    </Button>
                  </div>
                )}
              </>
            ) : (
              <EmptyState
                title="Waiting for output"
                description="Recent serial lines will appear here as the Agent reports them."
                icon={TerminalSquare}
              />
            )}
          </Panel>
          <Panel
            title="Related artifacts"
            description="Logs and output accessed only through control-plane endpoints"
          >
            {artifacts.length ? (
              <div className="artifact-list">
                {artifacts.map((item) => (
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
                title="No related artifacts"
                description="Finalized output will appear here."
                icon={FileArchive}
              />
            )}
          </Panel>
        </div>
        <aside className="detail-side">
          <Panel title="Operation details">
            <DetailGrid
              items={[
                {
                  label: "Bench",
                  value: (
                    <Link
                      to={`/benches/${encodeURIComponent(stringValue(operation, "bench_id") ?? "")}`}
                    >
                      {stringValue(operation, "bench_id")}
                    </Link>
                  ),
                },
                { label: "Agent", value: shortId(stringValue(operation, "agent_id")) },
                {
                  label: "Actor",
                  value:
                    stringValue(nested(operation, "actor"), "display_name") ??
                    stringValue(operation, "owner"),
                },
                { label: "Reservation", value: shortId(stringValue(operation, "reservation_id")) },
                { label: "Created", value: formatDate(stringValue(operation, "created_at")) },
                { label: "Started", value: formatDate(stringValue(operation, "started_at")) },
                { label: "Completed", value: formatDate(stringValue(operation, "completed_at")) },
              ]}
            />
          </Panel>
          {(stringValue(operation, "error_code") || stringValue(operation, "error_message")) && (
            <Panel title="Failure">
              <div className="error-detail">
                <code>{stringValue(operation, "error_code")}</code>
                <p>{stringValue(operation, "error_message")}</p>
              </div>
            </Panel>
          )}
          <Panel title="Trace">
            <div className="trace-links">
              <Link to={`/audit?operation_id=${operationId}`}>
                Open audit history <ExternalLink size={14} />
              </Link>
              <span>
                Command <code>{shortId(stringValue(operation, "remote_command_id"))}</code>
              </span>
              <span>
                Last Agent update{" "}
                <strong>{formatRelative(stringValue(operation, "last_agent_update_at"))}</strong>
              </span>
            </div>
          </Panel>
        </aside>
      </div>
      <ConfirmDialog
        open={confirmCancel}
        title={`Cancel ${titleCase(stringValue(operation, "operation_type"))}?`}
        message={
          <>
            Cancel operation <strong>{shortId(operationId)}</strong> on{" "}
            {stringValue(operation, "bench_id")}? The Agent will attempt cleanup.
          </>
        }
        confirmLabel="Cancel operation"
        onConfirm={() => void cancel()}
        onClose={() => setConfirmCancel(false)}
        busy={cancelMutation.isPending}
      />
    </div>
  );
}
