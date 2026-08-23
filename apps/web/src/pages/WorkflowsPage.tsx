import { useQuery } from "@tanstack/react-query";
import {
  ChevronRight,
  Code2,
  FileArchive,
  Play,
  RefreshCw,
  Search,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  apiBase,
  asRecord,
  errorMessage,
  generatedApi,
  idempotencyKey,
  labels,
  nested,
  records,
  stringList,
  stringValue,
  type ApiRecord,
} from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { useLive } from "../app/LiveProvider";
import { useToast } from "../app/ToastProvider";
import {
  Button,
  ChipList,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  SearchField,
  StatusBadge,
} from "../components/ui";
import { useApiList, useApiMutation } from "../hooks/useApi";
import { formatRelative, shortId, titleCase } from "../lib/format";

function workflowRequirements(workflow: ApiRecord): ApiRecord {
  return nested(workflow, "requirements") ?? {};
}

export function WorkflowsPage() {
  const auth = useAuth();
  const live = useLive();
  const [params] = useSearchParams();
  const [search, setSearch] = useState("");
  const [definition, setDefinition] = useState<ApiRecord>();
  const [runTarget, setRunTarget] = useState<ApiRecord>();
  const query = useQuery({
    queryKey: ["workflows"],
    queryFn: generatedApi.listWorkflows,
    staleTime: 30_000,
    refetchInterval: live.state === "live" ? false : live.pollingIntervalMs,
  });
  const workflows = records(query.data);
  const filtered = workflows.filter((workflow) =>
    `${stringValue(workflow, "name")} ${stringValue(workflow, "description")}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Automation"
        title="Workflows"
        description="Approved, versioned procedures that run through the control plane."
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
          placeholder="Search workflows…"
        />
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading workflows" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : workflows.length === 0 ? (
        <EmptyState
          title="No workflows are available"
          description="Add a workflow definition to the configured workflow directory."
          icon={WorkflowIcon}
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No matching workflows"
          description="Try a broader search."
          icon={Search}
        />
      ) : (
        <div className="workflow-grid">
          {filtered.map((workflow) => {
            const requirements = workflowRequirements(workflow);
            const name = stringValue(workflow, "name") ?? "workflow";
            return (
              <article
                className="workflow-card"
                key={`${name}:${stringValue(workflow, "version")}`}
              >
                <header>
                  <span className="resource-icon">
                    <WorkflowIcon size={18} />
                  </span>
                  <div>
                    <h2>{name}</h2>
                    <span>Version {stringValue(workflow, "version")}</span>
                  </div>
                  <StatusBadge status={stringValue(workflow, "visibility") ?? "AVAILABLE"} />
                </header>
                <p>
                  {stringValue(workflow, "description") ??
                    "No description was provided for this workflow."}
                </p>
                <div className="workflow-requirements">
                  <span>Requires</span>
                  <ChipList
                    values={[
                      ...stringList(requirements, "capabilities"),
                      ...Object.entries(labels(requirements)).map(
                        ([key, value]) => `${key}=${value}`,
                      ),
                    ]}
                    limit={5}
                  />
                </div>
                <div className="workflow-stats">
                  <div>
                    <span>Recent runs</span>
                    <strong>
                      {stringValue(workflow, "recent_success_count") ?? "—"} /{" "}
                      {stringValue(workflow, "recent_run_count") ?? "—"}
                    </strong>
                  </div>
                  <div>
                    <span>Last run</span>
                    <strong>{formatRelative(stringValue(workflow, "last_run_at"))}</strong>
                  </div>
                  <div>
                    <span>Steps</span>
                    <strong>{Array.isArray(workflow.steps) ? workflow.steps.length : "—"}</strong>
                  </div>
                </div>
                <footer>
                  <Button variant="ghost" icon={Code2} onClick={() => setDefinition(workflow)}>
                    View definition
                  </Button>
                  {auth.can("workflows:run") && (
                    <Button icon={Play} onClick={() => setRunTarget(workflow)}>
                      Run workflow <ChevronRight size={15} />
                    </Button>
                  )}
                </footer>
              </article>
            );
          })}
        </div>
      )}
      <Dialog
        open={Boolean(definition)}
        onClose={() => setDefinition(undefined)}
        title={`${stringValue(definition, "name")} · v${stringValue(definition, "version")}`}
        description="Read-only workflow definition from the control plane."
        size="wide"
      >
        <pre className="definition-viewer">{JSON.stringify(definition, null, 2)}</pre>
        <div className="dialog-actions">
          <a
            className="button button-secondary"
            href={`${apiBase}/api/v1/workflows/${encodeURIComponent(stringValue(definition, "name") ?? "")}?version=${stringValue(definition, "version")}`}
            download
          >
            <FileArchive size={16} /> Download JSON
          </a>
          <Button onClick={() => setDefinition(undefined)}>Done</Button>
        </div>
      </Dialog>
      {runTarget && (
        <RunWorkflowDialog
          workflow={runTarget}
          initialBench={params.get("bench") ?? undefined}
          open
          onClose={() => setRunTarget(undefined)}
        />
      )}
    </div>
  );
}

function RunWorkflowDialog({
  workflow,
  initialBench,
  open,
  onClose,
}: {
  workflow: ApiRecord;
  initialBench?: string;
  open: boolean;
  onClose: () => void;
}) {
  const { notify } = useToast();
  const navigate = useNavigate();
  const benchesQuery = useApiList("benches", "/api/v1/benches", true, generatedApi.listBenches);
  const artifactsQuery = useApiList("artifacts", "/api/v1/artifacts?limit=500", true, () =>
    generatedApi.listArtifacts({ limit: 500 }),
  );
  const [selection, setSelection] = useState(initialBench ? "specific" : "automatic");
  const [benchId, setBenchId] = useState(initialBench ?? "");
  const [reuseReservation, setReuseReservation] = useState(true);
  const [releaseAfter, setReleaseAfter] = useState(false);
  const [inputValues, setInputValues] = useState<Record<string, unknown>>({});
  const [validation, setValidation] = useState<Record<string, string>>({});
  const inputDefinitions = asRecord(workflow.inputs) ?? {};
  const requirements = workflowRequirements(workflow);
  const compatibleBenches = records(benchesQuery.data).filter((bench) => {
    const capabilities = stringList(bench, "capabilities");
    if (!stringList(requirements, "capabilities").every((item) => capabilities.includes(item)))
      return false;
    const requiredLabels = labels(requirements);
    const benchLabels = labels(bench);
    return Object.entries(requiredLabels).every(([key, value]) => benchLabels[key] === value);
  });
  const selectedBench = compatibleBenches.find((bench) => stringValue(bench, "id") === benchId);
  const existingReservation = nested(selectedBench, "current_reservation");
  const mutation = useApiMutation(async () => {
    const errors: Record<string, string> = {};
    const prepared: Record<string, unknown> = {};
    for (const [name, rawDefinition] of Object.entries(inputDefinitions)) {
      const definition = asRecord(rawDefinition) ?? {};
      const type = stringValue(definition, "type") ?? "string";
      const raw = inputValues[name] ?? definition.default;
      if (definition.required === true && (raw === undefined || raw === null || raw === "")) {
        errors[name] = "This input is required";
        continue;
      }
      if (raw === undefined || raw === "") continue;
      if (type === "integer") {
        const number = Number(raw);
        if (!Number.isInteger(number)) errors[name] = "Enter a whole number";
        else prepared[name] = number;
      } else if (type === "boolean") prepared[name] = Boolean(raw);
      else if (type === "artifact") {
        const artifact = records(artifactsQuery.data).find(
          (item) => stringValue(item, "id") === raw,
        );
        if (!artifact) errors[name] = "Choose an artifact";
        else prepared[name] = { artifact_id: stringValue(artifact, "id") };
      } else prepared[name] = String(raw);
    }
    if (selection === "specific" && !benchId) errors.bench = "Choose a compatible bench";
    setValidation(errors);
    if (Object.keys(errors).length) throw new Error("Check the highlighted workflow inputs.");
    const reservationId =
      selection === "specific" && reuseReservation
        ? stringValue(existingReservation, "id")
        : undefined;
    return generatedApi.runWorkflow(stringValue(workflow, "name") ?? "", {
      idempotency_key: idempotencyKey("web-workflow"),
      version: Number(stringValue(workflow, "version")),
      bench_id: selection === "specific" ? benchId : null,
      inputs: prepared,
      reservation_id: reservationId,
      release_reservation_after: reservationId ? releaseAfter : undefined,
      reservation_duration_seconds: reservationId ? undefined : 3600,
      lease_ttl_seconds: reservationId ? undefined : 300,
      command_timeout_seconds: 3600,
    });
  }, [["operations"], ["workflow-runs"], ["reservations"], ["overview"]]);
  async function run() {
    try {
      const result = (await mutation.mutateAsync()) as ApiRecord;
      const operation = nested(result, "operation");
      const operationId = stringValue(operation, "id") ?? stringValue(result, "operation_id");
      notify({
        title: "Workflow started",
        message: `${stringValue(workflow, "name")} is running.`,
        tone: "success",
        href: operationId ? `/workflow-runs/${operationId}` : undefined,
      });
      onClose();
      if (operationId) navigate(`/workflow-runs/${operationId}`);
    } catch (error) {
      if (!Object.keys(validation).length)
        notify({ title: "Workflow wasn’t started", message: errorMessage(error), tone: "error" });
    }
  }
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Run ${stringValue(workflow, "name")}`}
      description={`Version ${stringValue(workflow, "version")} · server validation remains authoritative.`}
      size="wide"
    >
      <div className="run-workflow-layout">
        <div>
          <h3>Bench selection</h3>
          <div className="radio-cards">
            <label className={selection === "automatic" ? "selected" : ""}>
              <input
                type="radio"
                checked={selection === "automatic"}
                onChange={() => setSelection("automatic")}
              />
              <span>
                <strong>Automatic selection</strong>
                <small>The scheduler chooses a compatible available bench.</small>
              </span>
            </label>
            <label className={selection === "specific" ? "selected" : ""}>
              <input
                type="radio"
                checked={selection === "specific"}
                onChange={() => setSelection("specific")}
              />
              <span>
                <strong>Specific bench</strong>
                <small>Pin the run to one compatible target.</small>
              </span>
            </label>
          </div>
          {selection === "specific" && (
            <Field label="Compatible bench" error={validation.bench}>
              <select value={benchId} onChange={(event) => setBenchId(event.target.value)}>
                <option value="">Select bench</option>
                {compatibleBenches.map((bench) => (
                  <option key={stringValue(bench, "id")} value={stringValue(bench, "id")}>
                    {stringValue(bench, "name")} · {stringValue(bench, "id")}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {selection === "specific" && existingReservation && (
            <div className="reservation-behavior">
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={reuseReservation}
                  onChange={(event) => setReuseReservation(event.target.checked)}
                />
                <span>
                  <strong>Use active reservation</strong>
                  <small>
                    Reuse reservation {shortId(stringValue(existingReservation, "id"))} instead of
                    creating another.
                  </small>
                </span>
              </label>
              {reuseReservation && (
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={releaseAfter}
                    onChange={(event) => setReleaseAfter(event.target.checked)}
                  />
                  <span>
                    <strong>Release after completion</strong>
                    <small>Leave off to keep your existing reservation for more work.</small>
                  </span>
                </label>
              )}
            </div>
          )}
          <div className="requirements-box">
            <span>Required capabilities</span>
            <ChipList values={stringList(requirements, "capabilities")} limit={10} />
            <small>{compatibleBenches.length} compatible benches currently visible</small>
          </div>
        </div>
        <div>
          <h3>Workflow inputs</h3>
          {Object.keys(inputDefinitions).length === 0 ? (
            <EmptyState
              title="No inputs required"
              description="This workflow is ready to run as defined."
              icon={WorkflowIcon}
            />
          ) : (
            <div className="dynamic-inputs">
              {Object.entries(inputDefinitions).map(([name, raw]) => {
                const definition = asRecord(raw) ?? {};
                const type = stringValue(definition, "type") ?? "string";
                const required = definition.required === true;
                if (type === "boolean")
                  return (
                    <label className="check-row" key={name}>
                      <input
                        type="checkbox"
                        checked={Boolean(inputValues[name] ?? definition.default)}
                        onChange={(event) =>
                          setInputValues((current) => ({
                            ...current,
                            [name]: event.target.checked,
                          }))
                        }
                      />
                      <span>
                        <strong>{titleCase(name)}</strong>
                        <small>Boolean input</small>
                      </span>
                    </label>
                  );
                if (type === "artifact")
                  return (
                    <Field
                      key={name}
                      label={titleCase(name)}
                      error={validation[name]}
                      required={required}
                      hint="Select an existing control-plane artifact"
                    >
                      <select
                        value={String(inputValues[name] ?? "")}
                        onChange={(event) =>
                          setInputValues((current) => ({ ...current, [name]: event.target.value }))
                        }
                      >
                        <option value="">Choose artifact</option>
                        {records(artifactsQuery.data).map((artifact) => (
                          <option
                            key={stringValue(artifact, "id")}
                            value={stringValue(artifact, "id")}
                          >
                            {stringValue(artifact, "name")}
                          </option>
                        ))}
                      </select>
                    </Field>
                  );
                return (
                  <Field
                    key={name}
                    label={titleCase(name)}
                    error={validation[name]}
                    required={required}
                  >
                    <input
                      type={type === "integer" ? "number" : "text"}
                      value={String(inputValues[name] ?? definition.default ?? "")}
                      onChange={(event) =>
                        setInputValues((current) => ({ ...current, [name]: event.target.value }))
                      }
                    />
                  </Field>
                );
              })}
            </div>
          )}
          <div className="info-callout">
            <WorkflowIcon size={18} />
            <span>
              {selection === "specific" && existingReservation && reuseReservation
                ? "The existing reservation follows your release choice."
                : "The control plane creates and cleans up a managed reservation for this run."}
            </span>
          </div>
        </div>
      </div>
      <div className="dialog-actions">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button icon={Play} onClick={() => void run()} disabled={mutation.isPending}>
          {mutation.isPending ? "Starting…" : "Run workflow"}
        </Button>
      </div>
    </Dialog>
  );
}
