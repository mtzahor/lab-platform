import { zodResolver } from "@hookform/resolvers/zod";
import { createColumnHelper, type ColumnDef } from "@tanstack/react-table";
import { KeyRound, Plus, RefreshCw, UserRound, UserRoundCheck, UserRoundX } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import {
  generatedApi,
  booleanValue,
  errorMessage,
  nested,
  records,
  stringValue,
  type ApiRecord,
} from "../../api/client";
import { useToast } from "../../app/ToastProvider";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DetailGrid,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  PageHeader,
  SearchField,
  StatusBadge,
} from "../../components/ui";
import { useApiDetail, useApiList, useApiMutation } from "../../hooks/useApi";
import { formatDate, formatRelative, titleCase } from "../../lib/format";

const userSchema = z
  .object({
    username: z
      .string()
      .min(1)
      .max(100)
      .regex(/^[A-Za-z0-9_.-]+$/, "Use letters, numbers, dots, hyphens or underscores"),
    display_name: z.string().min(1, "Display name is required").max(200),
    email: z.string().email("Enter a valid email").optional().or(z.literal("")),
    authentication_source: z.enum(["LOCAL", "OIDC"]),
    organisation_role: z.enum(["OWNER", "ADMIN", "MEMBER", "VIEWER"]),
    password: z.string().optional(),
  })
  .superRefine((value, context) => {
    if (value.authentication_source === "LOCAL" && (!value.password || value.password.length < 12))
      context.addIssue({
        code: "custom",
        path: ["password"],
        message: "Local passwords need at least 12 characters",
      });
  });
type UserFields = z.infer<typeof userSchema>;
const passwordSchema = z.object({
  password: z.string().min(12, "Use at least 12 characters").max(4096),
});

export function UsersPage() {
  const { notify } = useToast();
  const query = useApiList("users", "/api/v1/users", true, generatedApi.listUsers);
  const rolesQuery = useApiList("role-assignments", "/api/v1/role-assignments", true, () =>
    generatedApi.listRoleAssignments(),
  );
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [selected, setSelected] = useState<ApiRecord>();
  const [disableTarget, setDisableTarget] = useState<ApiRecord>();
  const [resetTarget, setResetTarget] = useState<ApiRecord>();
  const selectedId = stringValue(selected, "id");
  const userTeamsQuery = useApiDetail(
    "user-teams",
    selectedId ? "/api/v1/users/" + selectedId + "/teams" : undefined,
    Boolean(selectedId),
    () => generatedApi.listUserTeams(selectedId ?? ""),
  );
  const sessionsQuery = useApiDetail(
    "user-sessions",
    selectedId ? "/api/v1/users/" + selectedId + "/sessions" : undefined,
    Boolean(selectedId),
    () => generatedApi.listUserSessions(selectedId ?? ""),
  );
  const items = records(query.data);
  const filtered = items.filter((item) =>
    `${stringValue(item, "username")} ${stringValue(item, "display_name")} ${stringValue(item, "email")}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const createForm = useForm<UserFields>({
    resolver: zodResolver(userSchema),
    defaultValues: { authentication_source: "LOCAL", organisation_role: "MEMBER" },
  });
  const resetForm = useForm<{ password: string }>({ resolver: zodResolver(passwordSchema) });
  const createMutation = useApiMutation(
    (fields: UserFields) =>
      generatedApi.createUser({
        ...fields,
        email: fields.email || null,
        password: fields.authentication_source === "LOCAL" ? fields.password : null,
      }),
    [["users"]],
  );
  const statusMutation = useApiMutation(
    ({ user, enable }: { user: ApiRecord; enable: boolean }) =>
      enable
        ? generatedApi.enableUser(stringValue(user, "id") ?? "")
        : generatedApi.disableUser(stringValue(user, "id") ?? ""),
    [["users"]],
  );
  const passwordMutation = useApiMutation(
    (password: string) =>
      generatedApi.resetUserPassword(stringValue(resetTarget, "id") ?? "", { password }),
    [],
  );
  async function create(fields: UserFields) {
    try {
      await createMutation.mutateAsync(fields);
      notify({
        title: "User created",
        message: `${fields.display_name} can now be assigned access.`,
        tone: "success",
      });
      createForm.reset();
      setCreateOpen(false);
    } catch (error) {
      notify({ title: "User wasn’t created", message: errorMessage(error), tone: "error" });
    }
  }
  async function toggleStatus(user: ApiRecord, enable: boolean) {
    try {
      await statusMutation.mutateAsync({ user, enable });
      notify({
        title: enable ? "User enabled" : "User disabled",
        message: stringValue(user, "display_name"),
        tone: "success",
      });
      setDisableTarget(undefined);
    } catch (error) {
      notify({ title: "Status change failed", message: errorMessage(error), tone: "error" });
    }
  }
  async function resetPassword(fields: { password: string }) {
    try {
      await passwordMutation.mutateAsync(fields.password);
      notify({
        title: "Password reset",
        message: `A new password is active for ${stringValue(resetTarget, "username")}.`,
        tone: "success",
      });
      resetForm.reset();
      setResetTarget(undefined);
    } catch (error) {
      notify({ title: "Password reset failed", message: errorMessage(error), tone: "error" });
    }
  }
  const columns: ColumnDef<ApiRecord, any>[] = (() => {
    const column = createColumnHelper<ApiRecord>();
    return [
      column.accessor((row) => stringValue(row, "display_name") ?? "", {
        id: "name",
        header: "User",
        cell: ({ row, getValue }) => (
          <div className="primary-cell">
            <button className="link-button" onClick={() => setSelected(row.original)}>
              {getValue()}
            </button>
            <span>@{stringValue(row.original, "username")}</span>
          </div>
        ),
      }),
      column.accessor((row) => stringValue(row, "email") ?? "", {
        id: "email",
        header: "Email",
        cell: ({ getValue }) => getValue() || "—",
      }),
      column.accessor((row) => stringValue(row, "status") ?? "UNKNOWN", {
        id: "status",
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
      }),
      column.accessor((row) => stringValue(row, "authentication_source") ?? "", {
        id: "source",
        header: "Authentication",
        cell: ({ getValue }) => titleCase(getValue()),
      }),
      column.accessor((row) => stringValue(row, "organisation_role") ?? "", {
        id: "role",
        header: "Organisation role",
        cell: ({ getValue }) => (getValue() ? titleCase(getValue()) : "Member"),
      }),
      column.accessor((row) => stringValue(row, "last_login_at") ?? "", {
        id: "lastLogin",
        header: "Last login",
        cell: ({ getValue }) => formatRelative(getValue()),
      }),
      column.accessor((row) => stringValue(row, "created_at") ?? "", {
        id: "created",
        header: "Created",
        cell: ({ getValue }) => formatRelative(getValue()),
      }),
      column.display({
        id: "actions",
        header: "",
        cell: ({ row }) => (
          <div className="table-actions">
            {stringValue(row.original, "authentication_source") === "LOCAL" && (
              <Button variant="ghost" icon={KeyRound} onClick={() => setResetTarget(row.original)}>
                Reset password
              </Button>
            )}
            {stringValue(row.original, "status") === "DISABLED" ? (
              <Button
                variant="ghost"
                icon={UserRoundCheck}
                onClick={() => void toggleStatus(row.original, true)}
              >
                Enable
              </Button>
            ) : (
              <Button
                variant="ghost"
                icon={UserRoundX}
                onClick={() => setDisableTarget(row.original)}
              >
                Disable
              </Button>
            )}
          </div>
        ),
      }),
    ];
  })();
  const assignments = records(rolesQuery.data).filter(
    (item) => stringValue(item, "subject_id") === stringValue(selected, "id"),
  );
  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Administration"
        title="Users"
        description="Human identities, authentication source and organisation access."
        actions={
          <>
            <Button variant="secondary" icon={RefreshCw} onClick={() => void query.refetch()}>
              Refresh
            </Button>
            <Button icon={Plus} onClick={() => setCreateOpen(true)}>
              Create user
            </Button>
          </>
        }
      />
      <div className="toolbar">
        <SearchField
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search name, username or email…"
        />
      </div>
      {query.isLoading ? (
        <LoadingState label="Loading users" />
      ) : query.error ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title="No users"
          description="Create the first user for this organisation."
          icon={UserRound}
        />
      ) : filtered.length === 0 ? (
        <EmptyState title="No users match" description="Try a broader search." icon={UserRound} />
      ) : (
        <DataTable data={filtered} columns={columns} />
      )}
      <Dialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Create user"
        description="The server validates identity policy and records this action in the audit log."
        size="wide"
      >
        <form onSubmit={createForm.handleSubmit(create)}>
          <div className="two-column-form">
            <Field label="Username" error={createForm.formState.errors.username?.message} required>
              <input autoComplete="off" {...createForm.register("username")} />
            </Field>
            <Field
              label="Display name"
              error={createForm.formState.errors.display_name?.message}
              required
            >
              <input {...createForm.register("display_name")} />
            </Field>
            <Field label="Email" error={createForm.formState.errors.email?.message}>
              <input type="email" {...createForm.register("email")} />
            </Field>
            <Field label="Authentication source">
              <select {...createForm.register("authentication_source")}>
                <option>LOCAL</option>
                <option>OIDC</option>
              </select>
            </Field>
            <Field label="Organisation role">
              <select {...createForm.register("organisation_role")}>
                <option>MEMBER</option>
                <option>VIEWER</option>
                <option>ADMIN</option>
                <option>OWNER</option>
              </select>
            </Field>
            {createForm.watch("authentication_source") === "LOCAL" && (
              <Field
                label="Initial password"
                error={createForm.formState.errors.password?.message}
                hint="Displayed only to the administrator who sets it"
                required
              >
                <input
                  type="password"
                  autoComplete="new-password"
                  {...createForm.register("password")}
                />
              </Field>
            )}
          </div>
          <div className="dialog-actions">
            <Button type="button" variant="ghost" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending ? "Creating…" : "Create user"}
            </Button>
          </div>
        </form>
      </Dialog>
      <Dialog
        open={Boolean(selected)}
        onClose={() => setSelected(undefined)}
        title={stringValue(selected, "display_name") ?? "User"}
        description={`@${stringValue(selected, "username")}`}
      >
        <DetailGrid
          items={[
            { label: "Status", value: <StatusBadge status={stringValue(selected, "status")} /> },
            { label: "Email", value: stringValue(selected, "email") },
            {
              label: "Authentication",
              value: titleCase(stringValue(selected, "authentication_source")),
            },
            { label: "Last login", value: formatDate(stringValue(selected, "last_login_at")) },
            { label: "Created", value: formatDate(stringValue(selected, "created_at")) },
          ]}
        />
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
            No direct role assignments. Team and organisation roles may still grant access.
          </p>
        )}
        <h3>Team memberships</h3>
        {userTeamsQuery.isLoading ? (
          <LoadingState label="Loading team memberships" />
        ) : records(userTeamsQuery.data).length ? (
          <div className="compact-list">
            {records(userTeamsQuery.data).map((entry) => {
              const team = nested(entry, "team") ?? entry;
              const membership = nested(entry, "membership");
              return (
                <div key={stringValue(team, "id")}>
                  <span>
                    <strong>{stringValue(team, "name")}</strong>
                    <small>
                      @{stringValue(team, "slug")} · {titleCase(stringValue(membership, "role"))}
                    </small>
                  </span>
                </div>
              );
            })}
          </div>
        ) : (
          <p className="muted-block">This user does not belong to a team.</p>
        )}
        <h3>Sessions</h3>
        {sessionsQuery.isLoading ? (
          <LoadingState label="Loading sessions" />
        ) : records(sessionsQuery.data).length ? (
          <div className="compact-list">
            {records(sessionsQuery.data).map((session) => (
              <div key={stringValue(session, "id")}>
                <span>
                  <strong>
                    {stringValue(session, "user_agent") ??
                      stringValue(session, "ip_address") ??
                      "Browser session"}
                  </strong>
                  <small>
                    Created {formatRelative(stringValue(session, "created_at"))} · expires{" "}
                    {formatRelative(stringValue(session, "expires_at"))}
                  </small>
                </span>
                <StatusBadge status={booleanValue(session, "active") ? "ACTIVE" : "EXPIRED"} />
              </div>
            ))}
          </div>
        ) : (
          <p className="muted-block">No sessions are recorded for this user.</p>
        )}
      </Dialog>
      <ConfirmDialog
        open={Boolean(disableTarget)}
        title={`Disable ${stringValue(disableTarget, "display_name")}?`}
        message={
          <>
            Disable user <strong>@{stringValue(disableTarget, "username")}</strong>? New sign-ins
            will be blocked; existing session handling remains server-authoritative.
          </>
        }
        confirmLabel="Disable user"
        onConfirm={() => {
          if (disableTarget) void toggleStatus(disableTarget, false);
        }}
        onClose={() => setDisableTarget(undefined)}
        busy={statusMutation.isPending}
      />
      <Dialog
        open={Boolean(resetTarget)}
        onClose={() => setResetTarget(undefined)}
        title={`Reset password for @${stringValue(resetTarget, "username")}`}
        description="The new password is submitted directly to the control plane and never logged."
      >
        <form onSubmit={resetForm.handleSubmit(resetPassword)}>
          <Field label="New password" error={resetForm.formState.errors.password?.message} required>
            <input
              type="password"
              autoComplete="new-password"
              autoFocus
              {...resetForm.register("password")}
            />
          </Field>
          <div className="dialog-actions">
            <Button type="button" variant="ghost" onClick={() => setResetTarget(undefined)}>
              Cancel
            </Button>
            <Button type="submit" disabled={passwordMutation.isPending}>
              Reset password
            </Button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}
