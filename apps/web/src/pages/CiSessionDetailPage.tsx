import {
  ArrowLeft,
  CheckCircle2,
  Download,
  FileArchive,
  GitBranch,
  GitCommit,
  HeartPulse,
  StopCircle,
  Workflow,
} from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiBase, errorMessage, generatedApi, nested, records, stringValue } from "../api/client";
import { useAuth } from "../app/AuthProvider";
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

export function CiSessionDetailPage() {
  const { sessionId = "" } = useParams();
  const auth = useAuth();
  const { notify } = useToast();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const query = useApiDetail("ci-sessions", "/api/v1/ci/sessions/" + sessionId, true, () =>
    generatedApi.getCiSession(sessionId),
  );
  const artifactQuery = useApiList(
    "ci-artifacts",
    "/api/v1/ci/sessions/" + sessionId + "/artifacts",
    true,
    () => generatedApi.listCiSessionArtifacts(sessionId),
  );
  const session = query.data;
  const binding =
    nested(session, "workflow") ??
    nested(session, "binding") ??
    nested(session, "distributed_workflow");
  const cleanup = nested(session, "cleanup_result");
  const active = isActiveStatus(stringValue(session, "status"));
  const cancelMutation = useApiMutation(
    () => generatedApi.cancelCiSession(sessionId),
    [["ci-sessions"], ["overview"]],
  );
  async function cancel() {
    try {
      await cancelMutation.mutateAsync();
      notify({
        title: "CI cancellation requested",
        message: "Workflow and reservation cleanup will follow.",
        tone: "success",
      });
      setConfirmCancel(false);
    } catch (error) {
      notify({ title: "Cancellation failed", message: errorMessage(error), tone: "error" });
    }
  }
  if (query.isLoading)
    return (
      <>
        <PageHeader title="CI session" />
        <LoadingState label="Loading CI session" />
      </>
    );
  if (query.error || !session)
    return (
      <>
        <PageHeader title="CI session unavailable" />
        <ErrorState
          error={query.error ?? new Error("Session not found")}
          retry={() => void query.refetch()}
        />
      </>
    );
  const artifacts = records(artifactQuery.data);
  return (
    <div className="page-stack">
      <Link className="back-link" to="/ci-sessions">
        <ArrowLeft size={15} /> CI sessions
      </Link>
      <PageHeader
        eyebrow={`${titleCase(stringValue(session, "provider"))} · ${shortId(sessionId)}`}
        title={stringValue(session, "repository") ?? "CI session"}
        description={
          <span className="headline-status">
            <StatusBadge status={stringValue(session, "status")} />
            <span>
              <GitBranch size={14} /> {stringValue(session, "ref") ?? "No ref"}
            </span>
            <code>{stringValue(session, "commit_sha")?.slice(0, 12)}</code>
          </span>
        }
        actions={
          active && auth.can("ci:sessions:cancel") ? (
            <Button variant="danger" icon={StopCircle} onClick={() => setConfirmCancel(true)}>
              Cancel session
            </Button>
          ) : undefined
        }
      />
      <div className="detail-layout">
        <div className="detail-main">
          <Panel
            title="Workflow progress"
            description="Assigned hardware and distributed execution"
          >
            {binding ? (
              <div className="activity-card">
                <div className="activity-card-head">
                  <span className="resource-icon">
                    <Workflow size={18} />
                  </span>
                  <div>
                    <p className="eyebrow">{stringValue(binding, "workflow_name") ?? "Workflow"}</p>
                    <strong>Version {stringValue(binding, "workflow_version") ?? "—"}</strong>
                  </div>
                  <StatusBadge
                    status={stringValue(binding, "status") ?? stringValue(session, "status")}
                  />
                </div>
                <div className="progress-row">
                  <progress
                    value={Number(stringValue(binding, "progress") ?? 0)}
                    max={100}
                    aria-label={`${stringValue(binding, "progress") ?? "0"}% complete`}
                  />
                  <strong>{stringValue(binding, "progress") ?? "0"}%</strong>
                </div>
                <div className="card-actions">
                  {stringValue(binding, "operation_id") && (
                    <Link
                      className="button button-secondary"
                      to={`/workflow-runs/${stringValue(binding, "operation_id")}`}
                    >
                      Open workflow run
                    </Link>
                  )}
                  {stringValue(session, "bench_id") && (
                    <Link
                      className="button button-ghost"
                      to={`/benches/${encodeURIComponent(stringValue(session, "bench_id") ?? "")}`}
                    >
                      Open bench
                    </Link>
                  )}
                </div>
              </div>
            ) : (
              <EmptyState
                title="Workflow not assigned"
                description="The session is waiting for allocation or has no distributed workflow."
                icon={Workflow}
              />
            )}
          </Panel>
          <Panel title="Heartbeats" description="CI runner liveness reported to the control plane">
            <div className="heartbeat-card">
              <span className="resource-icon">
                <HeartPulse size={18} />
              </span>
              <div>
                <strong>
                  Last heartbeat {formatRelative(stringValue(session, "heartbeat_at"))}
                </strong>
                <small>Timeout at {formatDate(stringValue(session, "timeout_at"))}</small>
              </div>
              <StatusBadge
                status={stringValue(session, "heartbeat_at") ? "ONLINE" : "UNKNOWN"}
                label={stringValue(session, "heartbeat_at") ? "Receiving" : "Not reported"}
              />
            </div>
          </Panel>
          <Panel title="Artifacts" description="Pipeline output and hardware test results">
            {artifactQuery.isLoading ? (
              <LoadingState label="Loading artifacts" />
            ) : artifacts.length ? (
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
                      <Download size={15} /> Download
                    </a>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No artifacts"
                description="Results will appear after the workflow produces them."
                icon={FileArchive}
              />
            )}
          </Panel>
        </div>
        <aside className="detail-side">
          <Panel title="Provider metadata">
            <DetailGrid
              items={[
                { label: "Provider", value: titleCase(stringValue(session, "provider")) },
                { label: "External run", value: stringValue(session, "external_run_id") },
                {
                  label: "Actor",
                  value: stringValue(session, "actor") ?? stringValue(session, "requested_by"),
                },
                { label: "Repository", value: stringValue(session, "repository") },
                { label: "Ref", value: stringValue(session, "ref") },
                {
                  label: "Commit",
                  value: (
                    <span className="inline-code">
                      <GitCommit size={14} />
                      <code>{stringValue(session, "commit_sha")}</code>
                    </span>
                  ),
                },
              ]}
            />
          </Panel>
          <Panel title="Assignment">
            <DetailGrid
              items={[
                { label: "Bench", value: stringValue(session, "bench_id") },
                { label: "Reservation", value: shortId(stringValue(session, "reservation_id")) },
                { label: "Agent", value: shortId(stringValue(binding, "agent_id")) },
                { label: "Started", value: formatDate(stringValue(session, "started_at")) },
                {
                  label: "Duration",
                  value: formatDuration(
                    stringValue(session, "started_at") ?? stringValue(session, "created_at"),
                    stringValue(session, "completed_at"),
                  ),
                },
              ]}
            />
          </Panel>
          <Panel title="Cleanup">
            <div className="cleanup-card">
              <StatusBadge status={stringValue(session, "cleanup_status")} />
              <p>
                {stringValue(session, "failure_reason") ??
                  stringValue(session, "error_message") ??
                  (stringValue(session, "cleanup_status") === "FAILED"
                    ? "One or more cleanup actions failed."
                    : "Cleanup status is reported by the control plane.")}
              </p>
              {cleanup && (
                <ul>
                  {Object.entries(cleanup)
                    .filter(([, value]) => typeof value === "boolean")
                    .map(([key, value]) => (
                      <li key={key}>
                        <CheckCircle2 size={14} /> {titleCase(key)}: {value ? "Done" : "Pending"}
                      </li>
                    ))}
                </ul>
              )}
            </div>
          </Panel>
        </aside>
      </div>
      <ConfirmDialog
        open={confirmCancel}
        title={`Cancel CI session ${shortId(sessionId)}?`}
        message={
          <>
            Cancel the {titleCase(stringValue(session, "provider"))} session for{" "}
            <strong>{stringValue(session, "repository") ?? sessionId}</strong>? Hardware cleanup
            will run where supported.
          </>
        }
        confirmLabel="Cancel CI session"
        onConfirm={() => void cancel()}
        onClose={() => setConfirmCancel(false)}
        busy={cancelMutation.isPending}
      />
    </div>
  );
}
