# Service accounts

Service accounts represent CI systems and automation independently of human users. They cannot use
password login and should receive direct resource-scoped roles.

Examples include GitHub Actions, GitLab CI, Jenkins, release automation, and deployment tooling.

## Model and status

A service account belongs to one organisation and records a name, optional description, status,
timestamps, and last-use time. Status is `ACTIVE`, `DISABLED`, or `REVOKED`. Authentication rejects
anything other than `ACTIVE` and also rejects a suspended or archived organisation.

After authentication, the account becomes a `SERVICE_ACCOUNT` principal and passes through the
same authorisation evaluator as a user. It does not receive an organisation membership or user
team expansion.

## Least-privilege CI design

Prefer direct assignments that name the workflow and bench/Agent the job needs. For example:

```text
github-ci
  -> WORKFLOW_RUNNER on workflow esp32-smoke-test
  -> OPERATOR on bench home-lab/virtual-esp32-01
```

A still narrower credential can intersect the resulting permissions. It cannot widen the service
account's roles. The workflow coordinator enforces `workflows:run` on the exact definition and
`benches:operate` on the selected bench before reservation, artifact transfer, or command dispatch,
so both assignments above are active parts of the least-privilege boundary. See
[API credentials](API_CREDENTIALS.md).

## Agent identities are different

An enrolled Lab Agent is infrastructure, not a service account. Agent enrollment and gateway
credentials remain separate from user/service credentials. The control plane authorises a caller,
then dispatches work over the Agent's existing authenticated channel. Identity-backed manual and
workflow remote commands—including CI-launched workflows—carry actor context and persist the exact
granting authorisation-decision snapshot. Principal-initiated CI/operation cancellation,
reconciliation, inventory-refresh, and drain/undrain controls also carry matching actor/snapshot
evidence. CI cancellation persists its exact `CI_SESSION` decision so a restart-time delivery retry
can reuse the initiating service account and snapshot; a timeout-originated cancellation cannot
later adopt that identity. System maintenance without identity evidence and legacy paths
deliberately omit those Phase 6 fields. The Agent does not independently rerun the full RBAC policy.

## Current implementation status

The model, persistence repository, credential issue/auth/revoke domain services, last-used updates,
role evaluation, and service-credential acceptance on protected control-plane routes are
implemented. Identity-only administration endpoints and the matching commands are also live:

```console
labctl service-account create \
  --name github-ci \
  --description "GitHub hardware workflow runner"
labctl service-account list
labctl service-account show github-ci

labctl role assign \
  --subject service-account:github-ci \
  --role workflow-runner \
  --resource workflow:esp32-smoke-test
labctl role assign \
  --subject service-account:github-ci \
  --role operator \
  --resource bench:home-lab/virtual-esp32-01

labctl service-account credential create \
  --service-account github-ci \
  --name github-main \
  --permission ci:sessions:create \
  --permission ci:sessions:read \
  --permission ci:sessions:cancel \
  --permission workflows:run \
  --permission benches:operate \
  --permission operations:read \
  --permission artifacts:read \
  --permission artifacts:write
```

`show`, `disable`, and `delete` accept an account UUID or name. Deletion marks the account revoked;
disablement immediately makes all of its credentials fail authentication. Operational routes
accept the issued token. Named Agent, bench, reservation, workflow, and operation routes resolve
exact trusted resources; the main collections filter each item; and workflow/CI services recheck
the current principal, resource, and durable ownership context. Protected command, reservation,
Agent lifecycle/enrollment, runtime lifecycle, and artifact services also recheck before side
effects. Artifact permissions inherit trusted parent resources. Credential restrictions narrow
every decision and must retain all permissions used by a multi-step CI launch.

The scoped assignments in this example grant artifact permission only through their workflow and
bench resources. A linked CI artifact requires session access plus the corresponding artifact
permission on both the exact workflow and selected bench. The credential restrictions retain those
permissions without widening the roles. Standalone artifacts without a resolvable trusted parent
are not exposed to a Phase 6 principal.

For CI, inert session creation accepts `ci:sessions:create` from any scoped assignment. Only the
principal stored on that session may start it; start additionally requires `ci:sessions:create`
and `workflows:run` on the exact workflow before the coordinator selects an authorised bench. The
owner may read or cancel its session using a corresponding scoped permission, while a different
principal in the same organisation needs the corresponding organisation-level grant.

## Rotation target

The supported rotation workflow is:

1. Issue a second, expiring credential.
2. Save the one-time plaintext in the CI provider's secret store.
3. Verify a least-privilege run and inspect its actor/audit records.
4. Revoke the old credential.
5. Confirm a new request with the old token fails.

Use `labctl service-account credential list --service-account github-ci` to identify the old UUID
and `labctl service-account credential revoke CREDENTIAL_ID` after the replacement succeeds.

The [Phase 6 team-access demo](PHASE_6_TEAM_DEMO.md) exercises this exact workflow/bench assignment,
narrowed credential, CI run, unrelated-action denial, and audit attribution end to end.
