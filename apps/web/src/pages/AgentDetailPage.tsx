import {
  Activity,
  ArrowLeft,
  Bot,
  Check,
  Clipboard,
  FileWarning,
  KeyRound,
  PauseCircle,
  RefreshCw,
  RotateCcw,
  ShieldX,
  Unplug,
} from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  generatedApi,
  booleanValue,
  errorMessage,
  labels,
  nested,
  records,
  stringValue,
  type ApiRecord,
} from "../api/client";
import { useAuth } from "../app/AuthProvider";
import { useToast } from "../app/ToastProvider";
import {
  Button,
  ChipList,
  ConfirmDialog,
  DetailGrid,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  Panel,
  StatusBadge,
} from "../components/ui";
import { useApiDetail, useApiList, useApiMutation } from "../hooks/useApi";
import { copyText, formatDate, formatRelative, titleCase } from "../lib/format";

type AgentAction =
  "actions/refresh-inventory" | "credentials/rotate" | "drain" | "revoke" | "undrain";

type ConfirmAction = "revoke" | "undrain" | null;

export function AgentDetailPage() {
  const { agentId = "" } = useParams();
  const auth = useAuth();
  const { notify } = useToast();
  const [drainOpen, setDrainOpen] = useState(false);
  const [cancelQueued, setCancelQueued] = useState(false);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);
  const [rotationOpen, setRotationOpen] = useState(false);
  const [currentCredential, setCurrentCredential] = useState("");
  const [credential, setCredential] = useState<string>();
  const [copied, setCopied] = useState(false);
  const query = useApiDetail("agents", "/api/v1/agents/" + agentId, true, () =>
    generatedApi.getAgent(agentId),
  );
  const timelineQuery = useApiList(
    "agent-timeline",
    "/api/v1/agents/" + agentId + "/timeline?limit=200",
    true,
    () => generatedApi.listAgentTimeline(agentId, { limit: 200 }),
  );
  const operationsQuery = useApiList(
    "operations",
    "/api/v1/operations?agent_id=" + agentId + "&limit=100",
    true,
    () => generatedApi.listOperations({ agent_id: agentId, limit: 100 }),
  );
  const agent = query.data;
  const permissions = nested(agent, "permissions");
  const canAdmin =
    permissions && typeof permissions.admin === "boolean"
      ? booleanValue(permissions, "admin")
      : auth.can("agents:manage");
  const mutation = useApiMutation(
    ({ action, body }: { action: AgentAction; body?: ApiRecord }) => {
      switch (action) {
        case "actions/refresh-inventory":
          return generatedApi.refreshAgentInventory(agentId);
        case "credentials/rotate":
          return generatedApi.rotateAgentCredential(agentId, {
            current_credential: stringValue(body, "current_credential") ?? "",
          });
        case "drain":
          return generatedApi.drainAgent(agentId, {
            cancel_queued_work: booleanValue(body, "cancel_queued_work"),
          });
        case "revoke":
          return generatedApi.revokeAgent(agentId);
        case "undrain":
          return generatedApi.undrainAgent(agentId);
      }
    },
    [["agents"], ["benches"], ["overview"], ["agent-timeline"]],
  );
  async function act(action: AgentAction, body?: ApiRecord) {
    try {
      const result = (await mutation.mutateAsync({ action, body })) as ApiRecord;
      if (action === "credentials/rotate")
        setCredential(
          stringValue(result, "credential") ??
            stringValue(result, "token") ??
            stringValue(result, "secret"),
        );
      if (action === "credentials/rotate") {
        setRotationOpen(false);
        setCurrentCredential("");
      }
      notify({
        title: `${titleCase(action.split("/").at(-1))} requested`,
        message: `Agent ${stringValue(agent, "name") ?? agentId} accepted the administrative request.`,
        tone: "success",
      });
      setDrainOpen(false);
      setConfirmAction(null);
    } catch (error) {
      notify({ title: "Agent action failed", message: errorMessage(error), tone: "error" });
    }
  }
  if (query.isLoading)
    return (
      <>
        <PageHeader title="Agent" />
        <LoadingState label="Loading Agent" />
      </>
    );
  if (query.error || !agent)
    return (
      <>
        <PageHeader title="Agent unavailable" />
        <ErrorState
          error={query.error ?? new Error("Agent not found")}
          retry={() => void query.refetch()}
        />
      </>
    );
  const benches = Array.isArray(agent.benches)
    ? agent.benches.filter((item): item is ApiRecord => Boolean(item && typeof item === "object"))
    : [];
  const operations = records(operationsQuery.data);
  const timeline = records(timelineQuery.data);
  const workload = nested(agent, "workload");
  const status = stringValue(agent, "status") ?? "UNKNOWN";
  return (
    <div className="page-stack">
      <Link className="back-link" to="/agents">
        <ArrowLeft size={15} /> Agents
      </Link>
      <PageHeader
        eyebrow={stringValue(agent, "slug")}
        title={stringValue(agent, "name") ?? agentId}
        description={
          <span className="headline-status">
            <StatusBadge status={status} />
            <span>{stringValue(agent, "location") ?? "Location unknown"}</span>
            <span>Heartbeat {formatRelative(stringValue(agent, "last_seen_at"))}</span>
          </span>
        }
        actions={
          canAdmin ? (
            <div className="action-row">
              <Button
                variant="secondary"
                icon={RefreshCw}
                onClick={() => void act("actions/refresh-inventory")}
              >
                Refresh inventory
              </Button>
              {["DRAINING", "DRAINED"].includes(status) ? (
                <Button icon={RotateCcw} onClick={() => setConfirmAction("undrain")}>
                  Undrain
                </Button>
              ) : (
                <Button variant="secondary" icon={PauseCircle} onClick={() => setDrainOpen(true)}>
                  Drain Agent
                </Button>
              )}
            </div>
          ) : undefined
        }
      />
      {status === "OFFLINE" && (
        <div className="warning-callout">
          <Unplug size={20} />
          <div>
            <strong>Agent is offline</strong>
            <p>
              Owned benches show their last known state. New operations cannot start until the
              connection returns.
            </p>
          </div>
        </div>
      )}
      <div className="detail-layout">
        <div className="detail-main">
          <Panel title="Owned benches" description="Inventory currently assigned to this Agent">
            {benches.length ? (
              <div className="agent-bench-grid">
                {benches.map((bench) => (
                  <Link
                    key={stringValue(bench, "id")}
                    to={`/benches/${encodeURIComponent(stringValue(bench, "id") ?? "")}`}
                  >
                    <span className="resource-icon">
                      <Bot size={16} />
                    </span>
                    <span>
                      <strong>{stringValue(bench, "name")}</strong>
                      <small>{stringValue(bench, "id")}</small>
                    </span>
                    <StatusBadge
                      status={stringValue(bench, "health") ?? stringValue(bench, "status")}
                    />
                  </Link>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No owned benches"
                description="Refresh inventory after configuring the Agent backend."
                icon={Bot}
              />
            )}
          </Panel>
          <Panel title="Current load" description="Commands and operations assigned to this Agent">
            <div className="load-metrics">
              <div>
                <span>Active reservations</span>
                <strong>{stringValue(workload, "active_reservations") ?? "0"}</strong>
              </div>
              <div>
                <span>Active operations</span>
                <strong>
                  {
                    operations.filter((item) =>
                      ["CREATED", "DISPATCHED", "RUNNING", "UNKNOWN"].includes(
                        stringValue(item, "status") ?? "",
                      ),
                    ).length
                  }
                </strong>
              </div>
              <div>
                <span>Queued operations</span>
                <strong>{stringValue(workload, "queued_operations") ?? "0"}</strong>
              </div>
              <div>
                <span>Reported benches</span>
                <strong>{stringValue(workload, "bench_count") ?? String(benches.length)}</strong>
              </div>
            </div>
            {operations.length ? (
              <div className="compact-list">
                {operations.slice(0, 6).map((item) => (
                  <Link key={stringValue(item, "id")} to={`/operations/${stringValue(item, "id")}`}>
                    <span>
                      <strong>{titleCase(stringValue(item, "operation_type"))}</strong>
                      <small>{stringValue(item, "bench_id")}</small>
                    </span>
                    <StatusBadge status={stringValue(item, "status")} />
                  </Link>
                ))}
              </div>
            ) : null}
          </Panel>
          <Panel
            title="Connection timeline"
            description="Heartbeats, disconnects, reconciliation and failures"
          >
            {timeline.length ? (
              <ol className="timeline">
                {timeline.map((item) => (
                  <li key={stringValue(item, "id")}>
                    <span className="timeline-marker" />
                    <div>
                      <span>{titleCase(stringValue(item, "event_type"))}</span>
                      <strong>
                        {stringValue(item, "message") ?? titleCase(stringValue(item, "event_type"))}
                      </strong>
                      <small>
                        {formatDate(
                          stringValue(item, "created_at") ?? stringValue(item, "timestamp"),
                        )}
                      </small>
                    </div>
                    <StatusBadge status={stringValue(item, "severity") ?? "INFO"} />
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState
                title="No timeline events"
                description="Connection and inventory changes will appear here."
                icon={Activity}
              />
            )}
          </Panel>
        </div>
        <aside className="detail-side">
          <Panel title="Identity">
            <DetailGrid
              items={[
                { label: "Agent ID", value: <code>{agentId}</code> },
                { label: "Version", value: <code>{stringValue(agent, "version")}</code> },
                { label: "Protocol", value: <code>{stringValue(agent, "protocol_version")}</code> },
                {
                  label: "Compatibility",
                  value: (
                    <StatusBadge
                      status={status === "INCOMPATIBLE" ? "INCOMPATIBLE" : "HEALTHY"}
                      label={status === "INCOMPATIBLE" ? "Incompatible" : "Compatible"}
                    />
                  ),
                },
                {
                  label: "Last connected",
                  value: formatDate(stringValue(agent, "last_connected_at")),
                },
                { label: "Registered", value: formatDate(stringValue(agent, "registered_at")) },
              ]}
            />
            <div className="metadata-row single">
              <div>
                <h3>Labels</h3>
                <ChipList
                  values={Object.entries(labels(agent)).map(([key, value]) => `${key}=${value}`)}
                  limit={20}
                />
              </div>
            </div>
          </Panel>
          <Panel title="Recent failures">
            {records(agent, "recent_errors").length ? (
              <div className="compact-list">
                {records(agent, "recent_errors").map((item) => (
                  <div key={stringValue(item, "id")}>
                    <span>
                      <strong>{stringValue(item, "error_code")}</strong>
                      <small>{stringValue(item, "message")}</small>
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No recent failures"
                description="No Agent failures were reported."
                icon={FileWarning}
              />
            )}
          </Panel>
          {canAdmin && (
            <Panel title="Administration" description="Sensitive actions are audited">
              <div className="operation-buttons">
                <Button variant="secondary" icon={KeyRound} onClick={() => setRotationOpen(true)}>
                  Rotate credential
                </Button>
                <Button variant="danger" icon={ShieldX} onClick={() => setConfirmAction("revoke")}>
                  Revoke Agent
                </Button>
              </div>
            </Panel>
          )}
        </aside>
      </div>
      <Dialog
        open={drainOpen}
        onClose={() => setDrainOpen(false)}
        title={`Drain Agent ${stringValue(agent, "name")}?`}
        description="No new reservations will be assigned while the Agent drains."
      >
        <p>Active operations are allowed to reach a safe boundary unless explicitly cancelled.</p>
        <label className="check-row">
          <input
            type="checkbox"
            checked={cancelQueued}
            onChange={(event) => setCancelQueued(event.target.checked)}
          />
          <span>
            <strong>Cancel queued work</strong>
            <small>Pending commands that have not started will be cancelled.</small>
          </span>
        </label>
        <div className="dialog-actions">
          <Button variant="ghost" onClick={() => setDrainOpen(false)}>
            Keep online
          </Button>
          <Button
            variant="danger"
            icon={PauseCircle}
            onClick={() => void act("drain", { cancel_queued_work: cancelQueued })}
            disabled={mutation.isPending}
          >
            Drain Agent
          </Button>
        </div>
      </Dialog>
      <ConfirmDialog
        open={confirmAction === "undrain"}
        title={`Undrain ${stringValue(agent, "name")}?`}
        message="New reservations and operations may be assigned to this Agent again."
        confirmLabel="Undrain Agent"
        tone="primary"
        onConfirm={() => void act("undrain")}
        onClose={() => setConfirmAction(null)}
        busy={mutation.isPending}
      />
      <ConfirmDialog
        open={confirmAction === "revoke"}
        title={`Revoke Agent ${stringValue(agent, "name")}?`}
        message={
          <>
            Revoke <strong>{stringValue(agent, "slug")}</strong>? Its credential will stop working
            and owned benches will become unavailable.
          </>
        }
        confirmLabel="Revoke Agent"
        onConfirm={() => void act("revoke")}
        onClose={() => setConfirmAction(null)}
        busy={mutation.isPending}
      />
      <Dialog
        open={rotationOpen}
        title={`Rotate credential for ${stringValue(agent, "name")}?`}
        description="Prove possession of the current credential before the control plane issues a replacement."
        onClose={() => {
          setRotationOpen(false);
          setCurrentCredential("");
        }}
      >
        <Field label="Current Agent credential" required>
          <input
            type="password"
            autoComplete="off"
            value={currentCredential}
            onChange={(event) => setCurrentCredential(event.target.value)}
          />
        </Field>
        <div className="dialog-actions">
          <Button
            variant="ghost"
            onClick={() => {
              setRotationOpen(false);
              setCurrentCredential("");
            }}
          >
            Cancel
          </Button>
          <Button
            onClick={() =>
              void act("credentials/rotate", { current_credential: currentCredential })
            }
            disabled={!currentCredential || mutation.isPending}
          >
            Rotate credential
          </Button>
        </div>
      </Dialog>
      <Dialog
        open={Boolean(credential)}
        onClose={() => setCredential(undefined)}
        title="New Agent credential"
        description="This secret is displayed once. Store it securely before closing."
      >
        <div className="secret-display">
          <code>{credential}</code>
          <Button
            variant="secondary"
            icon={copied ? Check : Clipboard}
            onClick={() => {
              if (credential) void copyText(credential).then(() => setCopied(true));
            }}
          >
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
        <div className="warning-callout">
          <KeyRound size={18} />
          <div>
            <strong>You won’t see this again</strong>
            <p>Closing this dialog permanently removes the plaintext secret from the dashboard.</p>
          </div>
        </div>
        <div className="dialog-actions">
          <Button onClick={() => setCredential(undefined)}>I stored the credential</Button>
        </div>
      </Dialog>
    </div>
  );
}
