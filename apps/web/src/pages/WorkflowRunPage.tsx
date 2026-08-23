import {
  ArrowLeft,
  CheckCircle2,
  CircleDashed,
  CircleX,
  Download,
  FileArchive,
  ListChecks,
  StopCircle,
  TerminalSquare,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  generatedApi,
  apiBase,
  asRecord,
  errorMessage,
  nested,
  records,
  stringValue,
  type ApiRecord,
} from "../api/client";
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
import { formatBytes, formatDate, formatDuration, shortId, titleCase } from "../lib/format";
import { isActiveStatus } from "../lib/status";

export function WorkflowRunPage() {
  const { runId = "" } = useParams();
  const auth = useAuth();
  const { notify } = useToast();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const runQuery = useApiDetail("workflow-runs", "/api/v1/workflow-runs/" + runId, true, () =>
    generatedApi.getWorkflowRun(runId),
  );
  const operationQuery = useApiDetail("operations", "/api/v1/operations/" + runId, true, () =>
    generatedApi.getOperation(runId),
  );
  const resultsQuery = useApiDetail(
    "workflow-results",
    "/api/v1/workflow-runs/" + runId + "/results",
    !isActiveStatus(stringValue(runQuery.data, "status")),
    () => generatedApi.getWorkflowResults(runId),
  );
  const artifactQuery = useApiList(
    "artifacts",
    "/api/v1/artifacts?limit=500",
    auth.can("artifacts:read"),
    () => generatedApi.listArtifacts({ limit: 500 }),
  );
  const run = runQuery.data;
  const operation = operationQuery.data;
  const resultRoot = resultsQuery.data;
  const steps = useMemo(() => {
    const completed = records(resultRoot, "steps");
    if (completed.length) return completed;
    return Array.isArray(run?.steps)
      ? run.steps.filter((item): item is ApiRecord => Boolean(asRecord(item)))
      : [];
  }, [resultRoot, run]);
  const assertions = records(resultRoot, "results");
  const artifacts = records(artifactQuery.data).filter(
    (item) =>
      stringValue(item, "owner_id") === runId ||
      stringValue(nested(item, "metadata"), "operation_id") === runId,
  );
  const status = stringValue(run, "status") ?? stringValue(operation, "status") ?? "UNKNOWN";
  const active = isActiveStatus(status);
  const cancelMutation = useApiMutation(
    () => generatedApi.cancelWorkflowRun(runId, { reason: "Cancelled from web dashboard" }),
    [["workflow-runs"], ["operations"], ["overview"]],
  );
  async function cancel() {
    try {
      await cancelMutation.mutateAsync();
      notify({
        title: "Workflow cancellation requested",
        message: "The Agent will stop at a safe boundary.",
        tone: "success",
      });
      setConfirmCancel(false);
    } catch (error) {
      notify({ title: "Cancellation failed", message: errorMessage(error), tone: "error" });
    }
  }
  const logLines = useMemo(
    () =>
      steps
        .flatMap((step) => {
          const output = asRecord(step.output);
          return output
            ? [
                `[${String(Number(stringValue(step, "step_index") ?? 0) + 1).padStart(2, "0")}] ${JSON.stringify(output)}`,
              ]
            : [];
        })
        .slice(-1000),
    [steps],
  );
  if (runQuery.isLoading)
    return (
      <>
        <PageHeader title="Workflow run" />
        <LoadingState label="Loading workflow progress" />
      </>
    );
  if (runQuery.error || !run)
    return (
      <>
        <PageHeader title="Workflow run unavailable" />
        <ErrorState
          error={runQuery.error ?? new Error("Run not found")}
          retry={() => void runQuery.refetch()}
        />
      </>
    );
  return (
    <div className="page-stack">
      <Link className="back-link" to="/workflows">
        <ArrowLeft size={15} /> Workflows
      </Link>
      <PageHeader
        eyebrow={`Run ${shortId(runId)}`}
        title={
          stringValue(run, "workflow_name") ?? titleCase(stringValue(operation, "operation_type"))
        }
        description={
          <span className="headline-status">
            <StatusBadge status={status} />
            <span>
              Bench{" "}
              <Link
                to={`/benches/${encodeURIComponent(stringValue(run, "bench_id") ?? stringValue(operation, "bench_id") ?? "")}`}
              >
                {stringValue(run, "bench_id") ?? stringValue(operation, "bench_id")}
              </Link>
            </span>
          </span>
        }
        actions={
          <>
            {auth.can("operations:cancel") && active && (
              <Button variant="danger" icon={StopCircle} onClick={() => setConfirmCancel(true)}>
                Cancel run
              </Button>
            )}
            <a
              className="button button-secondary"
              href={`${apiBase}/api/v1/workflow-runs/${runId}/results/junit`}
              download
            >
              <Download size={16} /> JUnit
            </a>
          </>
        }
      />
      <div className="run-progress-hero">
        <div>
          <span>Overall progress</span>
          <strong>
            {stringValue(run, "progress") ?? stringValue(operation, "progress") ?? "0"}%
          </strong>
        </div>
        <progress
          value={Number(stringValue(run, "progress") ?? stringValue(operation, "progress") ?? 0)}
          max={100}
          aria-label={`${stringValue(run, "progress") ?? stringValue(operation, "progress") ?? "0"}% complete`}
        />
        <p>
          {stringValue(run, "message") ??
            stringValue(operation, "message") ??
            (status.toUpperCase() === "UNKNOWN"
              ? "Operation status unknown. Awaiting Agent reconciliation."
              : "Waiting for the next update…")}
        </p>
      </div>
      <div className="detail-layout">
        <div className="detail-main">
          <Panel title="Steps" description="Server-reported workflow execution order">
            {steps.length ? (
              <ol className="step-list">
                {steps.map((step, index) => {
                  const stepStatus =
                    stringValue(step, "status") ??
                    (index === Number(stringValue(run, "current_step")) ? "RUNNING" : "PENDING");
                  const Icon =
                    stepStatus === "PASSED" || stepStatus === "SUCCEEDED"
                      ? CheckCircle2
                      : stepStatus === "FAILED"
                        ? CircleX
                        : CircleDashed;
                  return (
                    <li
                      key={stringValue(step, "id") ?? index}
                      className={`step-${stepStatus.toLowerCase()}`}
                    >
                      <span className="step-number">{index + 1}</span>
                      <Icon size={18} />
                      <div>
                        <strong>
                          {stringValue(step, "name") || titleCase(stringValue(step, "action"))}
                        </strong>
                        <small>
                          {formatDuration(
                            stringValue(step, "started_at"),
                            stringValue(step, "completed_at"),
                          )}
                          {stringValue(step, "error_message")
                            ? ` · ${stringValue(step, "error_message")}`
                            : ""}
                        </small>
                      </div>
                      <StatusBadge status={stepStatus} />
                    </li>
                  );
                })}
              </ol>
            ) : (
              <EmptyState
                title="Waiting for step data"
                description="The Agent has not reported workflow steps yet."
                icon={ListChecks}
              />
            )}
          </Panel>
          <Panel title="Assertions" description="Test results derived by the control plane">
            {assertions.length ? (
              <div className="assertion-list">
                {assertions.map((item) => (
                  <div key={stringValue(item, "name")}>
                    <StatusBadge status={stringValue(item, "status")} />
                    <span>
                      <strong>{stringValue(item, "name")}</strong>
                      <small>
                        {stringValue(item, "message") ??
                          `${stringValue(item, "duration_ms") ?? 0} ms`}
                      </small>
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No assertions reported"
                description="Assertions appear when result processing completes."
                icon={CheckCircle2}
              />
            )}
          </Panel>
          <Panel
            title="Run log"
            description={`Bounded to the newest ${Math.min(logLines.length, 1000)} structured output lines`}
          >
            {logLines.length ? (
              <pre className="log-viewer compact-log">{logLines.join("\n")}</pre>
            ) : (
              <EmptyState
                title="No structured output yet"
                description="Step output will be rendered as untrusted text."
                icon={TerminalSquare}
              />
            )}
          </Panel>
        </div>
        <aside className="detail-side">
          <Panel title="Run details">
            <DetailGrid
              items={[
                {
                  label: "Actor",
                  value:
                    stringValue(run, "owner") ??
                    stringValue(nested(operation, "actor"), "display_name"),
                },
                {
                  label: "Reservation",
                  value: shortId(
                    stringValue(run, "reservation_id") ?? stringValue(operation, "reservation_id"),
                  ),
                },
                { label: "Agent", value: shortId(stringValue(operation, "agent_id")) },
                {
                  label: "Started",
                  value: formatDate(
                    stringValue(run, "started_at") ?? stringValue(operation, "started_at"),
                  ),
                },
                {
                  label: "Elapsed",
                  value: formatDuration(
                    stringValue(run, "started_at") ?? stringValue(operation, "started_at"),
                    stringValue(run, "completed_at") ?? stringValue(operation, "completed_at"),
                  ),
                },
                {
                  label: "Error code",
                  value: stringValue(run, "error_code") ?? stringValue(operation, "error_code"),
                },
              ]}
            />
          </Panel>
          <Panel title="Artifacts">
            {artifacts.length ? (
              <div className="compact-list">
                {artifacts.map((item) => (
                  <a
                    key={stringValue(item, "id")}
                    href={`${apiBase}/api/v1/artifacts/${stringValue(item, "id")}/content`}
                    download
                  >
                    <span>
                      <strong>{stringValue(item, "name")}</strong>
                      <small>
                        {titleCase(stringValue(item, "artifact_type"))} ·{" "}
                        {formatBytes(Number(stringValue(item, "size_bytes")))}
                      </small>
                    </span>
                    <Download size={15} />
                  </a>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No artifacts yet"
                description="Logs and test reports appear as they are finalized."
                icon={FileArchive}
              />
            )}
          </Panel>
          <Panel title="Connection">
            <div className="confidence-list">
              <div>
                <span>Updates</span>
                <StatusBadge
                  status={active ? "RUNNING" : "COMPLETE"}
                  label={active ? "Live / polling" : "Final"}
                />
              </div>
              <div>
                <span>Last Agent update</span>
                <strong>{formatDate(stringValue(operation, "last_agent_update_at"))}</strong>
              </div>
              {status.toUpperCase() === "UNKNOWN" && (
                <p className="permission-note">
                  Last known state is preserved while the control plane reconciles with the Agent.
                </p>
              )}
            </div>
          </Panel>
        </aside>
      </div>
      <ConfirmDialog
        open={confirmCancel}
        title={`Cancel ${stringValue(run, "workflow_name") ?? "workflow run"}?`}
        message={
          <>
            Cancel run <strong>{shortId(runId)}</strong> on{" "}
            {stringValue(run, "bench_id") ?? stringValue(operation, "bench_id")}? Cleanup will run
            where supported.
          </>
        }
        confirmLabel="Cancel workflow"
        onConfirm={() => void cancel()}
        onClose={() => setConfirmCancel(false)}
        busy={cancelMutation.isPending}
      />
    </div>
  );
}
