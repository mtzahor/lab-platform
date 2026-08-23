import createClient from "openapi-fetch";
import type { components, operations, paths } from "./schema";

export type ApiRecord = Record<string, unknown>;

const API_BASE =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

const GENERATED_API_BASE =
  typeof window === "undefined"
    ? API_BASE
    : new URL(API_BASE || "/", window.location.origin).toString().replace(/\/$/, "");

export const SESSION_EXPIRED_EVENT = "lab-platform:session-expired";

function signalSessionExpired() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

function csrfToken(): string | undefined {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
  if (meta) return meta;
  for (const entry of document.cookie.split(";")) {
    const [rawName, ...rawValue] = entry.trim().split("=");
    if (rawName === "lab_csrf" || rawName === "csrf_token") {
      return decodeURIComponent(rawValue.join("="));
    }
  }
  return undefined;
}

let refreshInFlight: Promise<boolean> | undefined;

function requestPath(input: RequestInfo | URL): string {
  const value = input instanceof Request ? input.url : String(input);
  try {
    return new URL(value, window.location.origin).pathname;
  } catch {
    return value;
  }
}

function canRefreshSession(input: RequestInfo | URL): boolean {
  return !["/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/auth/refresh"].includes(
    requestPath(input),
  );
}

async function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    const headers = new Headers({ Accept: "application/json" });
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
    refreshInFlight = fetch(`${API_BASE}/api/v1/auth/refresh`, {
      method: "POST",
      headers,
      credentials: "include",
    })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        refreshInFlight = undefined;
      });
  }
  return refreshInFlight;
}

async function sessionFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const replay = input instanceof Request ? input.clone() : input;
  const response = await fetch(input, init);
  if (response.status !== 401 || !canRefreshSession(input)) {
    return response;
  }
  if (!(await refreshSession())) {
    signalSessionExpired();
    return response;
  }
  const method = replay instanceof Request ? replay.method : (init?.method ?? "GET");
  let retry: Response;
  if (/^(GET|HEAD|OPTIONS)$/i.test(method)) {
    retry = await fetch(replay, init);
    if (retry.status === 401) signalSessionExpired();
    return retry;
  }
  const token = csrfToken();
  if (replay instanceof Request) {
    const headers = new Headers(replay.headers);
    if (token) headers.set("X-CSRF-Token", token);
    else headers.delete("X-CSRF-Token");
    retry = await fetch(new Request(replay, { headers }));
  } else {
    const headers = new Headers(init?.headers);
    if (token) headers.set("X-CSRF-Token", token);
    else headers.delete("X-CSRF-Token");
    retry = await fetch(replay, { ...init, headers });
  }
  if (retry.status === 401) signalSessionExpired();
  return retry;
}

export const client = createClient<paths>({
  baseUrl: GENERATED_API_BASE,
  credentials: "include",
  fetch: sessionFetch,
});

client.use({
  async onRequest({ request }) {
    if (!/^(GET|HEAD|OPTIONS)$/i.test(request.method)) {
      const token = csrfToken();
      if (token) request.headers.set("X-CSRF-Token", token);
    }
    request.headers.set("Accept", "application/json");
    return request;
  },
});

const ERROR_MESSAGES: Record<string, string> = {
  AGENT_OFFLINE: "The Agent controlling this resource is offline. No operation was started.",
  BENCH_ALREADY_RESERVED: "This bench is currently reserved by another operator.",
  BENCH_NOT_FOUND: "This bench is no longer available.",
  PERMISSION_DENIED: "You do not have permission to perform this action.",
  RESERVATION_LEASE_EXPIRED: "This reservation has expired. Refresh before trying again.",
  RESERVATION_LEASE_VERSION_MISMATCH:
    "This reservation changed elsewhere. The page has been refreshed.",
  SESSION_EXPIRED: "Your session expired. Sign in again to continue.",
  AUTHENTICATION_REQUIRED: "Sign in to continue.",
};

export class ApiError extends Error {
  status: number;
  code?: string;
  requestId?: string;
  details?: unknown;

  constructor(
    message: string,
    status: number,
    code?: string,
    requestId?: string,
    details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let payload: unknown;
  try {
    payload = await response.clone().json();
  } catch {
    payload = undefined;
  }
  const root = asRecord(payload) ?? {};
  const detail = asRecord(root.error) ?? asRecord(root.detail) ?? root;
  const code = stringValue(detail, "code") ?? stringValue(root, "code");
  const apiMessage = stringValue(detail, "message") ?? stringValue(detail, "detail");
  const requestId =
    response.headers.get("X-Request-ID") ??
    stringValue(detail, "request_id") ??
    stringValue(root, "request_id");
  const fallback =
    response.status === 403
      ? ERROR_MESSAGES.PERMISSION_DENIED
      : `Request failed (${response.status}).`;
  return new ApiError(
    (code && ERROR_MESSAGES[code]) || apiMessage || fallback,
    response.status,
    code,
    requestId,
    detail.details ?? payload,
  );
}

function clientError(error: unknown, response: Response): ApiError {
  const root = asRecord(error);
  const detail = asRecord(root?.error) ?? asRecord(root?.detail) ?? root;
  const code = stringValue(detail, "code");
  const requestId = response.headers.get("X-Request-ID") ?? stringValue(detail, "request_id");
  return new ApiError(
    (code && ERROR_MESSAGES[code]) ||
      stringValue(detail, "message") ||
      `Request failed (${response.status}).`,
    response.status,
    code,
    requestId,
    detail?.details ?? error,
  );
}

type GeneratedResult = {
  data?: unknown;
  error?: unknown;
  response: Response;
};

type BenchListQuery = NonNullable<
  operations["list_benches_api_v1_benches_get"]["parameters"]["query"]
>;
type AgentTimelineQuery = NonNullable<
  operations["agent_timeline_api_v1_agents__agent_id__timeline_get"]["parameters"]["query"]
>;
type ArtifactListQuery = NonNullable<
  operations["list_artifacts_api_v1_artifacts_get"]["parameters"]["query"]
>;
export type AuditListQuery = NonNullable<
  operations["list_audit_events_api_v1_audit_events_get"]["parameters"]["query"]
>;
type CiSessionListQuery = NonNullable<
  operations["list_ci_sessions_api_v1_ci_sessions_get"]["parameters"]["query"]
>;
type OperationListQuery = NonNullable<
  operations["list_operations_api_v1_operations_get"]["parameters"]["query"]
>;
type ReservationListQuery = NonNullable<
  operations["list_reservations_api_v1_reservations_get"]["parameters"]["query"]
>;
type WorkflowRunListQuery = NonNullable<
  operations["list_workflow_runs_api_v1_workflow_runs_get"]["parameters"]["query"]
>;
type RoleAssignmentListQuery = NonNullable<
  operations["list_role_assignments_api_v1_role_assignments_get"]["parameters"]["query"]
>;
type EffectivePermissionsQuery = NonNullable<
  operations["effective_permissions_api_v1_permissions_effective_get"]["parameters"]["query"]
>;

function generatedRecord(result: GeneratedResult): ApiRecord {
  if (!result.response.ok) throw clientError(result.error, result.response);
  return asRecord(result.data) ?? {};
}

function generatedValue(result: GeneratedResult): unknown {
  if (!result.response.ok) throw clientError(result.error, result.response);
  return result.data;
}

export const generatedApi = {
  async authenticationConfig() {
    return generatedRecord(await client.GET("/api/v1/auth/config"));
  },
  async currentIdentity() {
    return generatedRecord(await client.GET("/api/v1/auth/me"));
  },
  async login(body: components["schemas"]["LoginRequest"]) {
    return generatedRecord(await client.POST("/api/v1/auth/login", { body }));
  },
  async logout() {
    return generatedValue(await client.POST("/api/v1/auth/logout"));
  },
  async overview() {
    return generatedRecord(await client.GET("/api/v1/overview"));
  },
  async listBenches() {
    return generatedRecord(await client.GET("/api/v1/benches"));
  },
  async searchBenches(query: BenchListQuery) {
    return generatedRecord(
      await client.GET("/api/v1/benches", {
        params: { query },
      }),
    );
  },
  async listAgents() {
    return generatedRecord(await client.GET("/api/v1/agents"));
  },
  async getAgent(agentId: string) {
    return generatedRecord(
      await client.GET("/api/v1/agents/{agent_id}", {
        params: { path: { agent_id: agentId } },
      }),
    );
  },
  async listAgentTimeline(agentId: string, query: AgentTimelineQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/agents/{agent_id}/timeline", {
        params: { path: { agent_id: agentId }, query },
      }),
    );
  },
  async refreshAgentInventory(agentId: string) {
    return generatedRecord(
      await client.POST("/api/v1/agents/{agent_id}/actions/refresh-inventory", {
        params: { path: { agent_id: agentId } },
      }),
    );
  },
  async drainAgent(agentId: string, body: components["schemas"]["DrainRequest"]) {
    return generatedRecord(
      await client.POST("/api/v1/agents/{agent_id}/drain", {
        params: { path: { agent_id: agentId } },
        body,
      }),
    );
  },
  async undrainAgent(agentId: string) {
    return generatedValue(
      await client.POST("/api/v1/agents/{agent_id}/undrain", {
        params: { path: { agent_id: agentId } },
      }),
    );
  },
  async revokeAgent(agentId: string) {
    return generatedValue(
      await client.POST("/api/v1/agents/{agent_id}/revoke", {
        params: { path: { agent_id: agentId } },
      }),
    );
  },
  async rotateAgentCredential(
    agentId: string,
    body: components["schemas"]["AgentCredentialRotationRequest"],
  ) {
    return generatedRecord(
      await client.POST("/api/v1/agents/{agent_id}/credentials/rotate", {
        params: { path: { agent_id: agentId } },
        body,
      }),
    );
  },
  async listOperations(query: OperationListQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/operations", {
        params: { query },
      }),
    );
  },
  async getOperation(operationId: string) {
    return generatedRecord(
      await client.GET("/api/v1/operations/{operation_id}", {
        params: { path: { operation_id: operationId } },
      }),
    );
  },
  async listReservations(query: ReservationListQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/reservations", {
        params: { query },
      }),
    );
  },
  async listWorkflows() {
    return generatedRecord(await client.GET("/api/v1/workflows"));
  },
  async getWorkflow(workflowName: string, version?: number) {
    return generatedRecord(
      await client.GET("/api/v1/workflows/{workflow_name}", {
        params: {
          path: { workflow_name: workflowName },
          query: { version },
        },
      }),
    );
  },
  async runWorkflow(workflowName: string, body: components["schemas"]["WorkflowRunRequest"]) {
    return generatedRecord(
      await client.POST("/api/v1/workflows/{workflow_name}/runs", {
        params: { path: { workflow_name: workflowName } },
        body,
      }),
    );
  },
  async listWorkflowRuns(query: WorkflowRunListQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/workflow-runs", {
        params: { query },
      }),
    );
  },
  async getWorkflowRun(operationId: string) {
    return generatedRecord(
      await client.GET("/api/v1/workflow-runs/{operation_id}", {
        params: { path: { operation_id: operationId } },
      }),
    );
  },
  async getWorkflowResults(operationId: string) {
    return generatedRecord(
      await client.GET("/api/v1/workflow-runs/{operation_id}/results", {
        params: { path: { operation_id: operationId } },
      }),
    );
  },
  async cancelWorkflowRun(
    operationId: string,
    body: components["schemas"]["OperationCancelRequest"],
  ) {
    return generatedRecord(
      await client.POST("/api/v1/workflow-runs/{operation_id}/cancel", {
        params: { path: { operation_id: operationId } },
        body,
      }),
    );
  },
  async listCiSessions(query: CiSessionListQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/ci/sessions", {
        params: { query },
      }),
    );
  },
  async getCiSession(sessionId: string) {
    return generatedRecord(
      await client.GET("/api/v1/ci/sessions/{session_id}", {
        params: { path: { session_id: sessionId } },
      }),
    );
  },
  async listCiSessionArtifacts(sessionId: string) {
    return generatedRecord(
      await client.GET("/api/v1/ci/sessions/{session_id}/artifacts", {
        params: { path: { session_id: sessionId } },
      }),
    );
  },
  async cancelCiSession(sessionId: string) {
    return generatedRecord(
      await client.POST("/api/v1/ci/sessions/{session_id}/cancel", {
        params: { path: { session_id: sessionId } },
      }),
    );
  },
  async listArtifacts(query: ArtifactListQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/artifacts", {
        params: { query },
      }),
    );
  },
  async deleteArtifact(artifactId: string) {
    return generatedValue(
      await client.DELETE("/api/v1/artifacts/{artifact_id}", {
        params: { path: { artifact_id: artifactId } },
      }),
    );
  },
  async getArtifactContent(artifactId: string) {
    return generatedValue(
      await client.GET("/api/v1/artifacts/{artifact_id}/content", {
        params: { path: { artifact_id: artifactId } },
        parseAs: "blob",
      }),
    );
  },
  async listAuditEvents(query: AuditListQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/audit-events", {
        params: { query },
      }),
    );
  },
  async listUsers() {
    return generatedRecord(await client.GET("/api/v1/users"));
  },
  async createUser(body: components["schemas"]["UserCreateRequest"]) {
    return generatedRecord(await client.POST("/api/v1/users", { body }));
  },
  async enableUser(userId: string) {
    return generatedRecord(
      await client.POST("/api/v1/users/{user_id}/enable", {
        params: { path: { user_id: userId } },
      }),
    );
  },
  async disableUser(userId: string) {
    return generatedRecord(
      await client.POST("/api/v1/users/{user_id}/disable", {
        params: { path: { user_id: userId } },
      }),
    );
  },
  async resetUserPassword(userId: string, body: components["schemas"]["PasswordResetRequest"]) {
    return generatedValue(
      await client.POST("/api/v1/users/{user_id}/reset-password", {
        params: { path: { user_id: userId } },
        body,
      }),
    );
  },
  async listUserTeams(userId: string) {
    return generatedRecord(
      await client.GET("/api/v1/users/{user_id}/teams", {
        params: { path: { user_id: userId } },
      }),
    );
  },
  async listUserSessions(userId: string) {
    return generatedRecord(
      await client.GET("/api/v1/users/{user_id}/sessions", {
        params: { path: { user_id: userId } },
      }),
    );
  },
  async listTeams() {
    return generatedRecord(await client.GET("/api/v1/teams"));
  },
  async createTeam(body: components["schemas"]["TeamCreateRequest"]) {
    return generatedRecord(await client.POST("/api/v1/teams", { body }));
  },
  async updateTeam(teamId: string, body: components["schemas"]["TeamUpdateRequest"]) {
    return generatedRecord(
      await client.PATCH("/api/v1/teams/{team_id}", {
        params: { path: { team_id: teamId } },
        body,
      }),
    );
  },
  async deleteTeam(teamId: string) {
    return generatedValue(
      await client.DELETE("/api/v1/teams/{team_id}", {
        params: { path: { team_id: teamId } },
      }),
    );
  },
  async listTeamMembers(teamId: string) {
    return generatedRecord(
      await client.GET("/api/v1/teams/{team_id}/members", {
        params: { path: { team_id: teamId } },
      }),
    );
  },
  async addTeamMember(teamId: string, body: components["schemas"]["TeamMemberCreateRequest"]) {
    return generatedRecord(
      await client.POST("/api/v1/teams/{team_id}/members", {
        params: { path: { team_id: teamId } },
        body,
      }),
    );
  },
  async removeTeamMember(teamId: string, userId: string) {
    return generatedValue(
      await client.DELETE("/api/v1/teams/{team_id}/members/{user_id}", {
        params: { path: { team_id: teamId, user_id: userId } },
      }),
    );
  },
  async listServiceAccounts() {
    return generatedRecord(await client.GET("/api/v1/service-accounts"));
  },
  async createServiceAccount(body: components["schemas"]["ServiceAccountCreateRequest"]) {
    return generatedRecord(await client.POST("/api/v1/service-accounts", { body }));
  },
  async updateServiceAccount(
    accountId: string,
    body: components["schemas"]["ServiceAccountUpdateRequest"],
  ) {
    return generatedRecord(
      await client.PATCH("/api/v1/service-accounts/{account_id}", {
        params: { path: { account_id: accountId } },
        body,
      }),
    );
  },
  async listServiceAccountCredentials(accountId: string) {
    return generatedRecord(
      await client.GET("/api/v1/service-accounts/{account_id}/credentials", {
        params: { path: { account_id: accountId } },
      }),
    );
  },
  async createServiceAccountCredential(
    accountId: string,
    body: components["schemas"]["CredentialCreateRequest"],
  ) {
    return generatedRecord(
      await client.POST("/api/v1/service-accounts/{account_id}/credentials", {
        params: { path: { account_id: accountId } },
        body,
      }),
    );
  },
  async revokeCredential(credentialId: string) {
    return generatedValue(
      await client.DELETE("/api/v1/credentials/{credential_id}", {
        params: { path: { credential_id: credentialId } },
      }),
    );
  },
  async listRoles() {
    return generatedRecord(await client.GET("/api/v1/roles"));
  },
  async getRolePermissions(role: components["schemas"]["RoleName"]) {
    return generatedRecord(
      await client.GET("/api/v1/roles/{role}/permissions", {
        params: { path: { role } },
      }),
    );
  },
  async listRoleAssignments(query: RoleAssignmentListQuery = {}) {
    return generatedRecord(
      await client.GET("/api/v1/role-assignments", {
        params: { query },
      }),
    );
  },
  async createRoleAssignment(body: components["schemas"]["RoleAssignmentCreateRequest"]) {
    return generatedRecord(await client.POST("/api/v1/role-assignments", { body }));
  },
  async deleteRoleAssignment(assignmentId: string) {
    return generatedValue(
      await client.DELETE("/api/v1/role-assignments/{assignment_id}", {
        params: { path: { assignment_id: assignmentId } },
      }),
    );
  },
  async effectivePermissions(query: EffectivePermissionsQuery) {
    return generatedRecord(
      await client.GET("/api/v1/permissions/effective", {
        params: { query },
      }),
    );
  },
  async getOrganisation() {
    return generatedRecord(await client.GET("/api/v1/organisation"));
  },
  async updateOrganisation(body: components["schemas"]["OrganisationUpdateRequest"]) {
    return generatedRecord(await client.PATCH("/api/v1/organisation", { body }));
  },
};

export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = init.method ?? "GET";
  if (!/^(GET|HEAD|OPTIONS)$/i.test(method)) {
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }
  if (!(init.body instanceof FormData) && init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");
  const response = await sessionFetch(`${API_BASE}${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
  });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("json")) return (await response.blob()) as T;
  return (await response.json()) as T;
}

export function jsonBody(value: unknown): Pick<RequestInit, "body" | "headers"> {
  return { body: JSON.stringify(value), headers: { "Content-Type": "application/json" } };
}

export function asRecord(value: unknown): ApiRecord | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as ApiRecord)
    : undefined;
}

export function records(value: unknown, key = "items"): ApiRecord[] {
  const root = asRecord(value);
  const items = root?.[key];
  return Array.isArray(items)
    ? items.filter((item): item is ApiRecord => asRecord(item) !== undefined)
    : [];
}

export function stringValue(value: unknown, key: string, fallback?: string): string | undefined {
  const raw = asRecord(value)?.[key];
  if (raw === null || raw === undefined || raw === "") return fallback;
  return typeof raw === "string" || typeof raw === "number" ? String(raw) : fallback;
}

export function numberValue(value: unknown, key: string, fallback = 0): number {
  const raw = asRecord(value)?.[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : fallback;
}

export function booleanValue(value: unknown, key: string, fallback = false): boolean {
  const raw = asRecord(value)?.[key];
  return typeof raw === "boolean" ? raw : fallback;
}

export function nested(value: unknown, key: string): ApiRecord | undefined {
  return asRecord(asRecord(value)?.[key]);
}

export function stringList(value: unknown, key: string): string[] {
  const raw = asRecord(value)?.[key];
  return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === "string") : [];
}

export function labels(value: unknown): Record<string, string> {
  const raw = asRecord(asRecord(value)?.labels);
  if (!raw) return {};
  return Object.fromEntries(
    Object.entries(raw)
      .filter((entry): entry is [string, string | number | boolean] =>
        ["string", "number", "boolean"].includes(typeof entry[1]),
      )
      .map(([key, item]) => [key, String(item)]),
  );
}

export function idempotencyKey(prefix: string): string {
  return `${prefix}:${crypto.randomUUID()}`;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}

export function apiErrorDetails(error: unknown): { code?: string; requestId?: string } {
  return error instanceof ApiError ? { code: error.code, requestId: error.requestId } : {};
}

export const apiBase = API_BASE;
