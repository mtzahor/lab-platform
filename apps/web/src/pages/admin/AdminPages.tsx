import {
  Copy,
  KeyRound,
  Network,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UsersRound,
} from "lucide-react";
import { useState } from "react";
import {
  asRecord,
  errorMessage,
  generatedApi,
  nested,
  records,
  stringList,
  stringValue,
  type ApiRecord,
} from "../../api/client";
import { useToast } from "../../app/ToastProvider";
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
} from "../../components/ui";
import { useApiDetail, useApiList, useApiMutation } from "../../hooks/useApi";
import { copyText, formatDate, formatRelative, shortId, titleCase } from "../../lib/format";

type TeamRole = Parameters<typeof generatedApi.addTeamMember>[1]["role"];
type ServiceAccountStatus = NonNullable<
  Parameters<typeof generatedApi.updateServiceAccount>[1]["status"]
>;
type RoleAssignmentInput = Parameters<typeof generatedApi.createRoleAssignment>[0];
type RoleName = Parameters<typeof generatedApi.getRolePermissions>[0];

export function TeamsPage() {
  const { notify } = useToast();
  const teamsQuery = useApiList("teams", "/api/v1/teams", true, generatedApi.listTeams);
  const usersQuery = useApiList("users", "/api/v1/users", true, generatedApi.listUsers);
  const assignmentsQuery = useApiList(
    "role-assignments",
    "/api/v1/role-assignments",
    true,
    generatedApi.listRoleAssignments,
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [selected, setSelected] = useState<ApiRecord>();
  const [deleteTarget, setDeleteTarget] = useState<ApiRecord>();
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [memberId, setMemberId] = useState("");
  const [memberRole, setMemberRole] = useState<TeamRole>("MEMBER");
  const [editSlug, setEditSlug] = useState("");
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const selectedId = stringValue(selected, "id") ?? "";
  const membersQuery = useApiDetail(
    "team-members",
    selectedId ? "/api/v1/teams/" + selectedId + "/members" : undefined,
    Boolean(selectedId),
    () => generatedApi.listTeamMembers(selectedId),
  );
  const teams = records(teamsQuery.data);
  const users = records(usersQuery.data);
  const memberships = records(membersQuery.data);
  const assignments = records(assignmentsQuery.data).filter(
    (item) => stringValue(item, "subject_id") === selectedId,
  );
  const createMutation = useApiMutation(
    () => generatedApi.createTeam({ slug, name, description: description || null }),
    [["teams"]],
  );
  const memberMutation = useApiMutation(
    () => generatedApi.addTeamMember(selectedId, { user_id: memberId, role: memberRole }),
    [["team-members"], ["user-teams"], ["teams"]],
  );
  const removeMemberMutation = useApiMutation(
    (userId: string) => generatedApi.removeTeamMember(selectedId, userId),
    [["team-members"], ["user-teams"], ["teams"]],
  );
  const updateMutation = useApiMutation(
    () =>
      generatedApi.updateTeam(selectedId, {
        slug: editSlug,
        name: editName,
        description: editDescription || null,
      }),
    [["teams"], ["team-members"]],
  );
  const deleteMutation = useApiMutation(
    (team: ApiRecord) => generatedApi.deleteTeam(stringValue(team, "id") ?? ""),
    [["teams"]],
  );

  async function create() {
    try {
      await createMutation.mutateAsync();
      notify({ title: "Team created", message: name, tone: "success" });
      setSlug("");
      setName("");
      setDescription("");
      setCreateOpen(false);
    } catch (error) {
      notify({ title: "Team wasn’t created", message: errorMessage(error), tone: "error" });
    }
  }
  async function addMember() {
    try {
      await memberMutation.mutateAsync();
      notify({
        title: "Team member added",
        message: "The membership is now active.",
        tone: "success",
      });
      setMemberId("");
    } catch (error) {
      notify({ title: "Member wasn’t added", message: errorMessage(error), tone: "error" });
    }
  }
  async function removeMember(userId: string) {
    try {
      await removeMemberMutation.mutateAsync(userId);
      notify({ title: "Member removed", tone: "success" });
    } catch (error) {
      notify({ title: "Member wasn’t removed", message: errorMessage(error), tone: "error" });
    }
  }
  function beginEdit() {
    setEditSlug(stringValue(selected, "slug") ?? "");
    setEditName(stringValue(selected, "name") ?? "");
    setEditDescription(stringValue(selected, "description") ?? "");
    setEditOpen(true);
  }
  async function updateTeam() {
    try {
      const updated = (await updateMutation.mutateAsync()) as ApiRecord;
      setSelected(updated);
      setEditOpen(false);
      notify({ title: "Team updated", message: editName, tone: "success" });
    } catch (error) {
      notify({ title: "Team wasn’t updated", message: errorMessage(error), tone: "error" });
    }
  }
  async function removeTeam() {
    if (!deleteTarget) return;
    try {
      await deleteMutation.mutateAsync(deleteTarget);
      notify({
        title: "Team deleted",
        message: stringValue(deleteTarget, "name"),
        tone: "success",
      });
      setDeleteTarget(undefined);
      setSelected(undefined);
    } catch (error) {
      notify({ title: "Team wasn’t deleted", message: errorMessage(error), tone: "error" });
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Administration"
        title="Teams"
        description="Group people once, then assign lab access to the team."
        actions={
          <>
            <Button variant="secondary" icon={RefreshCw} onClick={() => void teamsQuery.refetch()}>
              Refresh
            </Button>
            <Button icon={Plus} onClick={() => setCreateOpen(true)}>
              Create team
            </Button>
          </>
        }
      />
      {teamsQuery.isLoading ? (
        <LoadingState label="Loading teams" />
      ) : teamsQuery.error ? (
        <ErrorState error={teamsQuery.error} retry={() => void teamsQuery.refetch()} />
      ) : teams.length === 0 ? (
        <EmptyState
          title="No teams"
          description="Create a team to manage shared access."
          icon={UsersRound}
        />
      ) : (
        <div className="admin-card-grid">
          {teams.map((team) => (
            <button
              className="admin-card"
              key={stringValue(team, "id")}
              onClick={() => setSelected(team)}
            >
              <span className="resource-icon">
                <UsersRound size={18} />
              </span>
              <span>
                <strong>{stringValue(team, "name")}</strong>
                <small>{stringValue(team, "description") ?? `@${stringValue(team, "slug")}`}</small>
              </span>
              <StatusBadge status="ACTIVE" />
            </button>
          ))}
        </div>
      )}
      <Dialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Create team"
        description="Team slugs are stable identifiers used by automation."
      >
        <div className="two-column-form">
          <Field label="Name" required>
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="Slug" hint="Lowercase letters, numbers and hyphens" required>
            <input
              value={slug}
              onChange={(event) =>
                setSlug(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))
              }
            />
          </Field>
        </div>
        <Field label="Description">
          <textarea
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        <div className="dialog-actions">
          <Button variant="ghost" onClick={() => setCreateOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => void create()}
            disabled={!name || !slug || createMutation.isPending}
          >
            Create team
          </Button>
        </div>
      </Dialog>
      <Dialog
        open={Boolean(selected)}
        onClose={() => setSelected(undefined)}
        title={stringValue(selected, "name") ?? "Team"}
        description={`@${stringValue(selected, "slug")}`}
        size="wide"
      >
        <DetailGrid
          items={[
            { label: "Team ID", value: <code>{selectedId}</code> },
            { label: "Created", value: formatDate(stringValue(selected, "created_at")) },
            { label: "Description", value: stringValue(selected, "description") },
          ]}
        />
        <div className="admin-detail-columns">
          <section>
            <h3>Members</h3>
            {memberships.length ? (
              <div className="compact-list">
                {memberships.map((entry) => {
                  const membership = nested(entry, "membership") ?? entry;
                  const includedUser = nested(entry, "user");
                  const userId = stringValue(membership, "user_id") ?? "";
                  const user =
                    includedUser ?? users.find((item) => stringValue(item, "id") === userId);
                  return (
                    <div key={userId}>
                      <span>
                        <strong>{stringValue(user, "display_name") ?? shortId(userId)}</strong>
                        <small>{titleCase(stringValue(membership, "role"))}</small>
                      </span>
                      <Button variant="ghost" onClick={() => void removeMember(userId)}>
                        Remove
                      </Button>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="muted-block">
                No memberships are included in this team response yet. You can still add a member
                below.
              </p>
            )}
            <div className="inline-form">
              <select
                aria-label="User"
                value={memberId}
                onChange={(event) => setMemberId(event.target.value)}
              >
                <option value="">Choose user</option>
                {users.map((user) => (
                  <option key={stringValue(user, "id")} value={stringValue(user, "id")}>
                    {stringValue(user, "display_name")} (@{stringValue(user, "username")})
                  </option>
                ))}
              </select>
              <select
                aria-label="Team role"
                value={memberRole}
                onChange={(event) => setMemberRole(event.target.value as TeamRole)}
              >
                <option>MEMBER</option>
                <option>MANAGER</option>
                <option>VIEWER</option>
              </select>
              <Button
                onClick={() => void addMember()}
                disabled={!memberId || memberMutation.isPending}
              >
                Add
              </Button>
            </div>
          </section>
          <section>
            <h3>Role assignments</h3>
            {assignments.length ? (
              <div className="compact-list">
                {assignments.map((item) => (
                  <div key={stringValue(item, "id")}>
                    <span>
                      <strong>{titleCase(stringValue(item, "role"))}</strong>
                      <small>
                        {titleCase(stringValue(item, "resource_type"))} ·{" "}
                        {stringValue(item, "resource_id")}
                      </small>
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="muted-block">
                No direct assignments. Organisation defaults may still apply.
              </p>
            )}
          </section>
        </div>
        <div className="dialog-actions">
          <Button variant="danger" icon={Trash2} onClick={() => setDeleteTarget(selected)}>
            Delete team
          </Button>
          <Button variant="secondary" onClick={beginEdit}>
            Edit team
          </Button>
          <Button onClick={() => setSelected(undefined)}>Done</Button>
        </div>
      </Dialog>
      <Dialog
        open={editOpen}
        onClose={() => setEditOpen(false)}
        title={`Edit ${stringValue(selected, "name") ?? "team"}`}
        description="Update the team identity used for shared access."
      >
        <div className="two-column-form">
          <Field label="Name" required>
            <input value={editName} onChange={(event) => setEditName(event.target.value)} />
          </Field>
          <Field label="Slug" required>
            <input
              value={editSlug}
              onChange={(event) =>
                setEditSlug(event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))
              }
            />
          </Field>
        </div>
        <Field label="Description">
          <textarea
            rows={3}
            value={editDescription}
            onChange={(event) => setEditDescription(event.target.value)}
          />
        </Field>
        <div className="dialog-actions">
          <Button variant="ghost" onClick={() => setEditOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => void updateTeam()}
            disabled={!editName || !editSlug || updateMutation.isPending}
          >
            Save changes
          </Button>
        </div>
      </Dialog>
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title={`Delete ${stringValue(deleteTarget, "name")}?`}
        message={
          <>
            Delete team <strong>{stringValue(deleteTarget, "name")}</strong>? Its role assignments
            and memberships will no longer grant access.
          </>
        }
        confirmLabel="Delete team"
        onConfirm={() => void removeTeam()}
        onClose={() => setDeleteTarget(undefined)}
        busy={deleteMutation.isPending}
      />
    </div>
  );
}

export function ServiceAccountsPage() {
  const { notify } = useToast();
  const query = useApiList(
    "service-accounts",
    "/api/v1/service-accounts",
    true,
    generatedApi.listServiceAccounts,
  );
  const [selected, setSelected] = useState<ApiRecord>();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [credentialName, setCredentialName] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [issuedSecret, setIssuedSecret] = useState<string>();
  const [revokeTarget, setRevokeTarget] = useState<ApiRecord>();
  const accountId = stringValue(selected, "id") ?? "";
  const credentialsQuery = useApiList(
    "credentials",
    accountId ? "/api/v1/service-accounts/" + accountId + "/credentials" : "",
    Boolean(accountId),
    () => generatedApi.listServiceAccountCredentials(accountId),
  );
  const accounts = records(query.data);
  const credentials = records(credentialsQuery.data);
  const createMutation = useApiMutation(
    () => generatedApi.createServiceAccount({ name, description: description || null }),
    [["service-accounts"]],
  );
  const statusMutation = useApiMutation(
    (status: ServiceAccountStatus) => generatedApi.updateServiceAccount(accountId, { status }),
    [["service-accounts"]],
  );
  const credentialMutation = useApiMutation(
    () =>
      generatedApi.createServiceAccountCredential(accountId, {
        name: credentialName,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        allowed_ip_ranges: [],
      }),
    [["credentials"]],
  );
  const revokeMutation = useApiMutation(
    (credential: ApiRecord) => generatedApi.revokeCredential(stringValue(credential, "id") ?? ""),
    [["credentials"]],
  );
  async function create() {
    try {
      await createMutation.mutateAsync();
      notify({ title: "Service account created", message: name, tone: "success" });
      setName("");
      setDescription("");
      setCreateOpen(false);
    } catch (error) {
      notify({ title: "Account wasn’t created", message: errorMessage(error), tone: "error" });
    }
  }
  async function changeStatus(status: ServiceAccountStatus) {
    try {
      await statusMutation.mutateAsync(status);
      notify({ title: `Service account ${status.toLowerCase()}`, tone: "success" });
    } catch (error) {
      notify({ title: "Status change failed", message: errorMessage(error), tone: "error" });
    }
  }
  async function issueCredential() {
    try {
      const result = await credentialMutation.mutateAsync();
      setIssuedSecret(stringValue(result, "token"));
      setCredentialName("");
      setExpiresAt("");
    } catch (error) {
      notify({ title: "Credential wasn’t created", message: errorMessage(error), tone: "error" });
    }
  }
  async function revokeCredential() {
    if (!revokeTarget) return;
    try {
      await revokeMutation.mutateAsync(revokeTarget);
      notify({ title: "Credential revoked", tone: "success" });
      setRevokeTarget(undefined);
    } catch (error) {
      notify({ title: "Credential wasn’t revoked", message: errorMessage(error), tone: "error" });
    }
  }
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Administration"
        title="Service accounts"
        description="Non-human identities, scoped credentials and last-use visibility."
        actions={
          <>
            <Button variant="secondary" icon={RefreshCw} onClick={() => void query.refetch()}>
              Refresh
            </Button>
            <Button icon={Plus} onClick={() => setCreateOpen(true)}>
              Create account
            </Button>
          </>
        }
      />
      {query.isLoading ? (
        <LoadingState label="Loading service accounts" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : accounts.length === 0 ? (
        <EmptyState
          title="No service accounts"
          description="Create one for CI or controlled automation."
          icon={KeyRound}
        />
      ) : (
        <div className="admin-card-grid">
          {accounts.map((account) => (
            <button
              className="admin-card"
              key={stringValue(account, "id")}
              onClick={() => setSelected(account)}
            >
              <span className="resource-icon">
                <KeyRound size={18} />
              </span>
              <span>
                <strong>{stringValue(account, "name")}</strong>
                <small>
                  {stringValue(account, "description") ?? shortId(stringValue(account, "id"))}
                </small>
              </span>
              <StatusBadge status={stringValue(account, "status")} />
            </button>
          ))}
        </div>
      )}
      <Dialog open={createOpen} onClose={() => setCreateOpen(false)} title="Create service account">
        <Field label="Name" required>
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label="Description">
          <textarea
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        <div className="dialog-actions">
          <Button variant="ghost" onClick={() => setCreateOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => void create()} disabled={!name || createMutation.isPending}>
            Create account
          </Button>
        </div>
      </Dialog>
      <Dialog
        open={Boolean(selected)}
        onClose={() => setSelected(undefined)}
        title={stringValue(selected, "name") ?? "Service account"}
        description="Plaintext credential secrets are displayed once."
        size="wide"
      >
        <DetailGrid
          items={[
            { label: "Status", value: <StatusBadge status={stringValue(selected, "status")} /> },
            { label: "Account ID", value: <code>{accountId}</code> },
            { label: "Last used", value: formatRelative(stringValue(selected, "last_used_at")) },
            { label: "Created", value: formatDate(stringValue(selected, "created_at")) },
          ]}
        />
        <h3>Credentials</h3>
        {credentials.length ? (
          <div className="compact-list">
            {credentials.map((credential) => (
              <div key={stringValue(credential, "id")}>
                <span>
                  <strong>{stringValue(credential, "name")}</strong>
                  <small>
                    Last used {formatRelative(stringValue(credential, "last_used_at"))} · expires{" "}
                    {formatRelative(stringValue(credential, "expires_at"))}
                  </small>
                </span>
                <Button variant="danger" onClick={() => setRevokeTarget(credential)}>
                  Revoke
                </Button>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted-block">No active credentials.</p>
        )}
        <div className="inline-form">
          <input
            aria-label="Credential name"
            placeholder="Credential name"
            value={credentialName}
            onChange={(event) => setCredentialName(event.target.value)}
          />
          <input
            aria-label="Expiry"
            type="datetime-local"
            value={expiresAt}
            onChange={(event) => setExpiresAt(event.target.value)}
          />
          <Button
            icon={Plus}
            onClick={() => void issueCredential()}
            disabled={!credentialName || credentialMutation.isPending}
          >
            Create credential
          </Button>
        </div>
        <div className="dialog-actions">
          {stringValue(selected, "status") === "ACTIVE" ? (
            <Button variant="danger" onClick={() => void changeStatus("DISABLED")}>
              Disable account
            </Button>
          ) : (
            <Button variant="secondary" onClick={() => void changeStatus("ACTIVE")}>
              Enable account
            </Button>
          )}
          <Button onClick={() => setSelected(undefined)}>Done</Button>
        </div>
      </Dialog>
      <Dialog
        open={Boolean(issuedSecret)}
        onClose={() => setIssuedSecret(undefined)}
        title="Copy this credential now"
        description="The secret cannot be retrieved again."
      >
        <div className="secret-display">
          <code>{issuedSecret}</code>
          <Button
            icon={Copy}
            onClick={() => {
              if (issuedSecret)
                void copyText(issuedSecret).then(() =>
                  notify({ title: "Credential copied", tone: "success" }),
                );
            }}
          >
            Copy
          </Button>
        </div>
        <div className="warning-callout">
          <KeyRound size={18} />
          <div>
            <strong>Store it securely</strong>
            <p>Closing this dialog permanently hides the plaintext value.</p>
          </div>
        </div>
        <div className="dialog-actions">
          <Button onClick={() => setIssuedSecret(undefined)}>I stored it securely</Button>
        </div>
      </Dialog>
      <ConfirmDialog
        open={Boolean(revokeTarget)}
        title={`Revoke ${stringValue(revokeTarget, "name")}?`}
        message={
          <>
            Revoke credential <strong>{stringValue(revokeTarget, "name")}</strong>? Automation using
            it will immediately lose access.
          </>
        }
        confirmLabel="Revoke credential"
        onConfirm={() => void revokeCredential()}
        onClose={() => setRevokeTarget(undefined)}
        busy={revokeMutation.isPending}
      />
    </div>
  );
}

export function RolesPage() {
  const { notify } = useToast();
  const rolesQuery = useApiList("roles", "/api/v1/roles", true, generatedApi.listRoles);
  const assignmentsQuery = useApiList("role-assignments", "/api/v1/role-assignments", true, () =>
    generatedApi.listRoleAssignments(),
  );
  const usersQuery = useApiList("users", "/api/v1/users", true, generatedApi.listUsers);
  const teamsQuery = useApiList("teams", "/api/v1/teams", true, generatedApi.listTeams);
  const accountsQuery = useApiList(
    "service-accounts",
    "/api/v1/service-accounts",
    true,
    generatedApi.listServiceAccounts,
  );
  const rawRoles = asRecord(rolesQuery.data)?.items;
  const roles = (
    Array.isArray(rawRoles)
      ? rawRoles.filter((item): item is string => typeof item === "string")
      : []
  ) as RoleName[];
  const assignments = records(assignmentsQuery.data);
  const [selectedRole, setSelectedRole] = useState<RoleName | "">("");
  const permissionsQuery = useApiList(
    "role-permissions",
    selectedRole ? "/api/v1/roles/" + selectedRole + "/permissions" : "",
    Boolean(selectedRole),
    () => generatedApi.getRolePermissions(selectedRole as RoleName),
  );
  const [subjectType, setSubjectType] = useState<RoleAssignmentInput["subject_type"]>("USER");
  const [subjectId, setSubjectId] = useState("");
  const [role, setRole] = useState<RoleAssignmentInput["role"] | "">("");
  const [resourceType, setResourceType] =
    useState<RoleAssignmentInput["resource_type"]>("ORGANISATION");
  const [resourceId, setResourceId] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [revokeTarget, setRevokeTarget] = useState<ApiRecord>();
  const [inspectorPermission, setInspectorPermission] = useState("benches:read");
  const [inspectorSubjectType, setInspectorSubjectType] = useState("USER");
  const [inspectorSubjectId, setInspectorSubjectId] = useState("");
  const [inspectorType, setInspectorType] = useState("ORGANISATION");
  const [inspectorId, setInspectorId] = useState("");
  const [decision, setDecision] = useState<ApiRecord>();
  const subjects =
    subjectType === "TEAM"
      ? records(teamsQuery.data)
      : subjectType === "SERVICE_ACCOUNT"
        ? records(accountsQuery.data)
        : records(usersQuery.data);
  const assignMutation = useApiMutation(
    () =>
      generatedApi.createRoleAssignment({
        subject_type: subjectType,
        subject_id: subjectId,
        role: role as RoleAssignmentInput["role"],
        resource_type: resourceType,
        resource_id: resourceId,
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
      }),
    [["role-assignments"]],
  );
  const revokeMutation = useApiMutation(
    (item: ApiRecord) => generatedApi.deleteRoleAssignment(stringValue(item, "id") ?? ""),
    [["role-assignments"]],
  );
  async function assign() {
    try {
      await assignMutation.mutateAsync();
      notify({
        title: "Role assigned",
        message: `${titleCase(role)} access is active.`,
        tone: "success",
      });
      setSubjectId("");
      setResourceId("");
      setExpiresAt("");
    } catch (error) {
      notify({ title: "Role wasn’t assigned", message: errorMessage(error), tone: "error" });
    }
  }
  async function revoke() {
    if (!revokeTarget) return;
    try {
      await revokeMutation.mutateAsync(revokeTarget);
      notify({ title: "Role assignment revoked", tone: "success" });
      setRevokeTarget(undefined);
    } catch (error) {
      notify({ title: "Assignment wasn’t revoked", message: errorMessage(error), tone: "error" });
    }
  }
  async function inspect() {
    try {
      const result = await generatedApi.effectivePermissions({
        subject_type: inspectorSubjectType as Parameters<
          typeof generatedApi.effectivePermissions
        >[0]["subject_type"],
        subject_id: inspectorSubjectId,
        resource_type: inspectorType as Parameters<
          typeof generatedApi.effectivePermissions
        >[0]["resource_type"],
        resource_id: inspectorId,
        permission: inspectorPermission,
      });
      setDecision(result);
    } catch (error) {
      notify({
        title: "Access couldn’t be evaluated",
        message: errorMessage(error),
        tone: "error",
      });
    }
  }
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Administration"
        title="Roles & access"
        description="Assign built-in roles and inspect the server’s effective authorization decision."
        actions={
          <Button
            variant="secondary"
            icon={RefreshCw}
            onClick={() => void Promise.all([rolesQuery.refetch(), assignmentsQuery.refetch()])}
          >
            Refresh
          </Button>
        }
      />
      <div className="roles-layout">
        <Panel
          title="Built-in roles"
          description="Select a role to inspect its permission vocabulary"
        >
          <div className="role-list">
            {roles.map((item) => (
              <button
                className={selectedRole === item ? "active" : ""}
                key={item}
                onClick={() => setSelectedRole(item)}
              >
                <ShieldCheck size={17} />
                <span>
                  <strong>{titleCase(item)}</strong>
                  <small>
                    {selectedRole === item
                      ? `${stringList(permissionsQuery.data, "permissions").length} permissions`
                      : "Inspect permissions"}
                  </small>
                </span>
              </button>
            ))}
          </div>
          {selectedRole && (
            <div className="permission-cloud">
              <ChipList values={stringList(permissionsQuery.data, "permissions")} limit={100} />
            </div>
          )}
        </Panel>
        <Panel
          title="Assign a role"
          description="The server validates subject, scope, inheritance and expiry"
        >
          <div className="two-column-form">
            <Field label="Subject type">
              <select
                value={subjectType}
                onChange={(event) => {
                  setSubjectType(event.target.value as RoleAssignmentInput["subject_type"]);
                  setSubjectId("");
                }}
              >
                <option>USER</option>
                <option>TEAM</option>
                <option>SERVICE_ACCOUNT</option>
              </select>
            </Field>
            <Field label="Subject" required>
              <select value={subjectId} onChange={(event) => setSubjectId(event.target.value)}>
                <option value="">Choose subject</option>
                {subjects.map((item) => (
                  <option key={stringValue(item, "id")} value={stringValue(item, "id")}>
                    {stringValue(item, "display_name") ??
                      stringValue(item, "name") ??
                      stringValue(item, "username")}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Role" required>
              <select
                value={role}
                onChange={(event) =>
                  setRole(event.target.value as RoleAssignmentInput["role"] | "")
                }
              >
                <option value="">Choose role</option>
                {roles.map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </Field>
            <Field label="Resource type">
              <select
                value={resourceType}
                onChange={(event) =>
                  setResourceType(event.target.value as RoleAssignmentInput["resource_type"])
                }
              >
                {["ORGANISATION", "AGENT", "BENCH", "WORKFLOW"].map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </Field>
            <Field label="Resource ID" required>
              <input
                value={resourceId}
                onChange={(event) => setResourceId(event.target.value)}
                placeholder="Organisation UUID, Agent UUID or bench ID"
              />
            </Field>
            <Field label="Optional expiry">
              <input
                type="datetime-local"
                value={expiresAt}
                onChange={(event) => setExpiresAt(event.target.value)}
              />
            </Field>
          </div>
          <Button
            onClick={() => void assign()}
            disabled={!subjectId || !role || !resourceId || assignMutation.isPending}
          >
            Assign role
          </Button>
        </Panel>
      </div>
      <Panel
        title="Assignments"
        description="Direct assignments; inherited access may come through teams or organisation roles"
      >
        {assignments.length ? (
          <div className="assignment-list">
            {assignments.map((item) => (
              <div key={stringValue(item, "id")}>
                <span className="resource-icon">
                  <ShieldCheck size={16} />
                </span>
                <span>
                  <strong>{titleCase(stringValue(item, "role"))}</strong>
                  <small>
                    {titleCase(stringValue(item, "subject_type"))}{" "}
                    {shortId(stringValue(item, "subject_id"))} →{" "}
                    {titleCase(stringValue(item, "resource_type"))}{" "}
                    {stringValue(item, "resource_id")}
                  </small>
                </span>
                <span>
                  {stringValue(item, "expires_at")
                    ? `Expires ${formatRelative(stringValue(item, "expires_at"))}`
                    : "No expiry"}
                </span>
                <Button variant="danger" onClick={() => setRevokeTarget(item)}>
                  Revoke
                </Button>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState
            title="No direct role assignments"
            description="Organisation roles may still grant access."
            icon={ShieldCheck}
          />
        )}
      </Panel>
      <Panel
        title="Effective access inspector"
        description="Ask the control plane why the current or selected subject can perform an action"
      >
        <div className="inline-form inspector-form">
          <select
            aria-label="Subject type"
            value={inspectorSubjectType}
            onChange={(event) => setInspectorSubjectType(event.target.value)}
          >
            <option>USER</option>
            <option>TEAM</option>
            <option>SERVICE_ACCOUNT</option>
          </select>
          <input
            aria-label="Subject ID"
            value={inspectorSubjectId}
            onChange={(event) => setInspectorSubjectId(event.target.value)}
            placeholder="Subject UUID"
          />
          <select
            aria-label="Permission"
            value={inspectorPermission}
            onChange={(event) => setInspectorPermission(event.target.value)}
          >
            {[
              "benches:read",
              "benches:reserve",
              "benches:flash",
              "workflows:run",
              "agents:manage",
              "audit:read",
            ].map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
          <select
            aria-label="Resource type"
            value={inspectorType}
            onChange={(event) => setInspectorType(event.target.value)}
          >
            {["ORGANISATION", "AGENT", "BENCH", "WORKFLOW"].map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
          <input
            aria-label="Resource ID"
            value={inspectorId}
            onChange={(event) => setInspectorId(event.target.value)}
            placeholder="Resource ID"
          />
          <Button onClick={() => void inspect()} disabled={!inspectorSubjectId || !inspectorId}>
            Inspect
          </Button>
        </div>
        {decision && (
          <div
            className={`decision-card ${decision.allowed ? "decision-allowed" : "decision-denied"}`}
          >
            <StatusBadge
              status={decision.allowed ? "SUCCEEDED" : "DENIED"}
              label={decision.allowed ? "Allowed" : "Denied"}
            />
            <p>
              {decision.allowed
                ? `Access is granted by ${stringList(decision, "roles").map(titleCase).join(", ") || "an effective permission"}.`
                : "No effective role grants this permission for the resource."}
            </p>
            <ChipList values={stringList(decision, "permissions")} limit={12} />
          </div>
        )}
      </Panel>
      <ConfirmDialog
        open={Boolean(revokeTarget)}
        title={`Revoke ${titleCase(stringValue(revokeTarget, "role"))} assignment?`}
        message={
          <>
            Revoke access to <strong>{stringValue(revokeTarget, "resource_id")}</strong>? The
            subject may retain access through another assignment.
          </>
        }
        confirmLabel="Revoke assignment"
        onConfirm={() => void revoke()}
        onClose={() => setRevokeTarget(undefined)}
        busy={revokeMutation.isPending}
      />
    </div>
  );
}

export function OrganisationPage() {
  const { notify } = useToast();
  const query = useApiDetail(
    "organisation",
    "/api/v1/organisation",
    true,
    generatedApi.getOrganisation,
  );
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const mutation = useApiMutation(
    () => generatedApi.updateOrganisation({ name }),
    [["organisation"], ["auth"]],
  );
  const organisation = query.data;
  async function save() {
    try {
      await mutation.mutateAsync();
      notify({ title: "Organisation updated", message: name, tone: "success" });
      setEditing(false);
    } catch (error) {
      notify({ title: "Organisation wasn’t updated", message: errorMessage(error), tone: "error" });
    }
  }
  if (query.isLoading)
    return (
      <>
        <PageHeader title="Organisation" />
        <LoadingState label="Loading organisation" />
      </>
    );
  if (query.error || !organisation)
    return (
      <>
        <PageHeader title="Organisation" />
        <ErrorState
          error={query.error ?? new Error("Organisation unavailable")}
          retry={() => void query.refetch()}
        />
      </>
    );
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Administration"
        title="Organisation"
        description="Workspace identity and stable tenant metadata."
        actions={
          <Button
            variant="secondary"
            onClick={() => {
              setName(stringValue(organisation, "name") ?? "");
              setEditing(true);
            }}
          >
            Edit name
          </Button>
        }
      />
      <div className="detail-layout">
        <div className="detail-main">
          <Panel title="Workspace">
            <DetailGrid
              items={[
                { label: "Name", value: stringValue(organisation, "name") },
                { label: "Slug", value: <code>{stringValue(organisation, "slug")}</code> },
                { label: "Organisation ID", value: <code>{stringValue(organisation, "id")}</code> },
                {
                  label: "Status",
                  value: <StatusBadge status={stringValue(organisation, "status") ?? "ACTIVE"} />,
                },
                { label: "Created", value: formatDate(stringValue(organisation, "created_at")) },
                {
                  label: "Updated",
                  value: formatRelative(stringValue(organisation, "updated_at")),
                },
              ]}
            />
          </Panel>
        </div>
        <aside className="detail-side">
          <Panel title="Security boundary">
            <div className="info-callout">
              <Network size={18} />
              <span>
                The dashboard never crosses organisation boundaries. Every API result remains
                server-filtered.
              </span>
            </div>
          </Panel>
        </aside>
      </div>
      <Dialog
        open={editing}
        onClose={() => setEditing(false)}
        title="Rename organisation"
        description="The stable slug and organisation ID do not change."
      >
        <Field label="Organisation name" required>
          <input value={name} onChange={(event) => setName(event.target.value)} autoFocus />
        </Field>
        <div className="dialog-actions">
          <Button variant="ghost" onClick={() => setEditing(false)}>
            Cancel
          </Button>
          <Button onClick={() => void save()} disabled={!name || mutation.isPending}>
            Save name
          </Button>
        </div>
      </Dialog>
    </div>
  );
}
