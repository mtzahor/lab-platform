# CLI

`labctl` is an HTTP-only client. It never constructs application services, SimLab, or physical
target adapters.

## Connection and credentials

API endpoint selection order is `--server`, `LAB_PLATFORM_SERVER`,
`~/.config/lab-platform/cli.yaml`, then `http://127.0.0.1:8080`. Point it at the control plane for
distributed use or at a standalone Agent for the local compatibility API.

```yaml
server: https://lab-control.internal.example
```

Bearer credential precedence is:

1. `LAB_PLATFORM_TOKEN`, or the environment variable selected by `--token-env NAME`.
2. A server-scoped native OS credential saved by `labctl auth login`.
3. No credential.

For non-interactive machine commands, select another environment variable with `--token-env NAME`:

```console
export DEVICE_LAB_TOKEN='lp_...'
labctl --token-env DEVICE_LAB_TOKEN ci session show SESSION_ID
```

There is no plaintext token or password option. See [API tokens](docs/API_TOKENS.md),
[local authentication](docs/LOCAL_AUTH.md), and the
[Phase 6 security model](docs/SECURITY_MODEL.md).

## Phase 6 authentication commands

The Phase 6 client surface is:

```text
labctl auth login [--server URL] [--username USERNAME] [--organisation SLUG]
  [--output table|json]
labctl auth logout [--output table|json]
labctl auth status [--output table|json]
labctl auth whoami [--output table|json]
```

`auth login` prompts for an omitted username and always prompts for the password without echo. It
expects `POST /api/v1/auth/login` and stores one versioned session bundle under the selected server:
access/refresh tokens, session ID, access expiry, and maximum session expiry. No secret is printed,
including in JSON output. On macOS storage uses Keychain through `security`; on Linux it uses
`secret-tool`/Secret Service. No plaintext file fallback is available.

For a stored login, one `401 SESSION_EXPIRED` or `TOKEN_EXPIRED` response triggers a serialized
`POST /api/v1/auth/refresh`, safe native-bundle replacement, and one retry of the original request.
Existing raw-token Keychain/Secret Service entries remain readable and migrate to the bundle on
their first successful refresh. An environment token always wins, is never rewritten, and is not
automatically refreshed.

`auth status` and `whoami` validate the selected environment/stored bearer with
`GET /api/v1/auth/me`. `auth logout` posts an empty body to `/api/v1/auth/logout`; a stored session
bundle is deleted locally even when the server reports that its session is already invalid. An
environment token cannot be removed from the parent shell, so logout prints a warning.

The matching control-plane login/logout/refresh/me/session routes are implemented. A fresh
installation can create its first owner before login:

```console
lab-control-plane bootstrap-admin \
  --config config/control-plane.yaml \
  --username michael \
  --display-name "Michael Tzahor"
```

This prompts twice without echo. Use `--password-env NAME` for automation; `--password` does not
exist. The command refuses once an owner exists unless the privileged `--recovery` path is selected.

## Phase 6 administration commands

These identity-only commands require a Phase 6 session or service-account credential; a legacy
token cannot administer the identity surface:

```text
labctl organisation show
labctl organisation update --name NAME

labctl user create --username USER --display-name NAME [--email EMAIL]
  [--organisation-role owner|admin|member|viewer]
  [--authentication-source local|oidc] [--password-env NAME]
labctl user list
labctl user show USER_ID_OR_USERNAME
labctl user disable USER_ID_OR_USERNAME
labctl user enable USER_ID_OR_USERNAME
labctl user reset-password USER_ID_OR_USERNAME [--password-env NAME]

labctl team create --slug SLUG --name NAME [--description TEXT]
labctl team list
labctl team show TEAM_ID_OR_SLUG_OR_NAME
labctl team add-member TEAM --user USER [--role manager|member|viewer]
labctl team remove-member TEAM --user USER
labctl team delete TEAM

labctl service-account create --name NAME [--description TEXT]
labctl service-account list
labctl service-account show ACCOUNT_ID_OR_NAME
labctl service-account disable ACCOUNT_ID_OR_NAME
labctl service-account delete ACCOUNT_ID_OR_NAME
labctl service-account credential create --service-account ACCOUNT --name NAME
  [--expires-at RFC3339] [--allowed-ip CIDR] [--permission PERMISSION]
labctl service-account credential list --service-account ACCOUNT
labctl service-account credential revoke CREDENTIAL_ID

labctl role list
labctl role permissions ROLE
labctl role assign --subject TYPE:NAME --role ROLE --resource TYPE:ID
  [--expires-at RFC3339]
labctl role revoke ASSIGNMENT_ID
labctl role effective --subject TYPE:NAME --resource TYPE:ID
  [--permission PERMISSION] [--parent-agent-id AGENT_UUID]

labctl access-policy bench get BENCH_ID
labctl access-policy bench set BENCH_ID
  --visibility private|organisation|restricted
  [--reservation-role ROLE] [--operation-role ROLE]
  [--allowed-team TEAM]
labctl access-policy workflow get WORKFLOW_ID
labctl access-policy workflow set WORKFLOW_ID
  --visibility organisation|restricted|admin-only

labctl audit list [--action ACTION] [--outcome succeeded|failed|denied]
  [--after ISO8601] [--before ISO8601] [--limit N]
labctl audit show EVENT_ID
```

`user create` defaults to `local`: it prompts twice for a password unless `--password-env NAME` is
provided. With `--authentication-source oidc`, the CLI neither prompts nor accepts
`--password-env`; the user is pre-provisioned for provider login. Password reset is valid only for
local users.

User passwords are hidden prompts unless a named environment variable is selected. Credential
creation prints the bearer exactly once; list/show output excludes its stored hash. Name/slug
references are resolved to organisation-scoped UUIDs before mutation. Audit output is
organisation-scoped and requires `audit:read`; list defaults to 100 events and supports action,
outcome, and ISO-8601 time filters.

Access-policy `get` reports whether the result comes from configuration defaults or a persisted
record. Bench `set` requires `benches:manage` on the exact bench; workflow `set` requires
`workflows:manage` on the exact workflow. `--allowed-team` accepts a same-organisation UUID, slug,
or name and may be repeated. Each `set` replaces the complete policy, takes effect immediately,
and appends an attributable audit event. See
[roles and permissions](docs/ROLES_AND_PERMISSIONS.md#access-policies).

## Phase 5 distributed commands

### Agent administration

```text
labctl agent list [--status STATUS] [--location LOCATION] [--label KEY=VALUE]
  [--version VERSION]
labctl agent show AGENT_ID
labctl agent enrollment-token create --name NAME [--expires-in 30m]
  [--allowed-label KEY=VALUE]
labctl agent enrollment-token list
labctl agent enrollment-token revoke TOKEN_ID
labctl agent drain AGENT_ID [--cancel-queued-work]
labctl agent undrain AGENT_ID
labctl agent revoke AGENT_ID
labctl agent refresh AGENT_ID
labctl agent timeline AGENT_ID [--severity SEVERITY] [--event-type TYPE]
```

Enrollment-token and Agent credentials are printed once and are never accepted as ordinary CLI
arguments. `lab-agent connect` reads the one-time token from `LAB_AGENT_ENROLLMENT_TOKEN` (or a
named environment variable), enrolls, and prints the non-secret identity configuration plus the
credential export command:

```text
lab-agent connect --control-plane URL [--enrollment-token-env NAME]
  [--credential-env NAME] [--location LOCATION]
  [--config FILE | --config-dir DIRECTORY]
```

The Agent keeps its local diagnostic API separate from the public control-plane API:

```text
lab-agent status [--url LOCAL_AGENT_URL]
lab-agent doctor
lab-agent connection status [--url LOCAL_AGENT_URL]
lab-agent journal list [--status STATUS] [--limit N]
lab-agent reconnect [--url LOCAL_AGENT_URL]
```

### Distributed inventory, reservations, workflows, and operations

Global bench IDs use `<agent-slug>/<local-bench-id>`. Ordinary routing uses the central API; the
CLI does not select or contact an Agent endpoint.

```text
labctl bench list [--agent AGENT_ID] [--location LOCATION]
  [--agent-label KEY=VALUE] [--label KEY=VALUE] [--capability NAME]

labctl reservation list [--agent AGENT_ID] [--state STATE]
labctl reservation create GLOBAL_BENCH --owner OWNER --duration 30m
  [--lease-ttl 5m] [--idempotency-key KEY] [--metadata KEY=VALUE]
labctl reservation renew RESERVATION_ID --owner OWNER
  --expected-lease-version VERSION [--lease-ttl 5m]
labctl reservation release RESERVATION_ID --owner OWNER
  --expected-lease-version VERSION

labctl workflow register DEFINITION.yaml
labctl workflow run NAME --owner OWNER [--bench GLOBAL_BENCH]
  [--kind simulated|physical] [--location LOCATION]
  [--bench-label KEY=VALUE] [--agent-label KEY=VALUE]
  [--reservation-duration 30m] [--lease-ttl 5m]
  [--command-timeout 1h] [--input NAME=VALUE]
labctl workflow watch OPERATION_ID [--interval SECONDS]
labctl workflow cancel OPERATION_ID --owner OWNER
labctl workflow results OPERATION_ID [--format json|junit] [--output PATH]

labctl operation list [--bench-id GLOBAL_BENCH] [--status STATUS]
labctl operation show OPERATION_ID
labctl operation watch OPERATION_ID
labctl operation cancel OPERATION_ID --owner OWNER
labctl operation reconcile OPERATION_ID
```

Distributed reservation creation returns only after the owning Agent has confirmed the local
lease. Renewal and release use the observed lease version so a delayed caller cannot overwrite a
newer ownership decision. A distributed workflow owns its reservation lifecycle and executes as
one remote command on the selected Agent.

With a Phase 6 session or service credential, workflow/direct-action commands and the
`agent drain`, `agent undrain`, `agent refresh`, `operation reconcile`, and `operation cancel`
controls carry the authenticated actor and exact decision snapshot to the Agent. Exact idempotent
command/workflow retries retain the evidence accepted with the original work even if the refreshed
HTTP request has a new session or decision-snapshot handle; the stable principal, tenant, request
content, and required permissions must remain valid under credential restrictions, and workflow
replay also fingerprints the effective restrictions. Automatic/background work without an
originating Phase 6 decision and legacy calls do not manufacture actor/snapshot fields; a
principal-requested CI cancellation persists evidence that restart-time delivery retry can reuse.

### Direct remote bench actions

The control plane supports the portable ESP32/SimLab action set directly. Use the global bench ID;
the CLI still contacts only the configured control-plane URL:

```text
labctl bench probe GLOBAL_BENCH --owner OWNER
labctl bench reset GLOBAL_BENCH --owner OWNER
labctl bench serial read GLOBAL_BENCH --owner OWNER
  [--timeout SECONDS] [--until REGEX] [--max-lines N]
labctl bench flash GLOBAL_BENCH FILE --owner OWNER [--version VERSION]
```

All four actions create durable distributed operations. `probe` and `serial read` wait and print
their confirmed result; `reset` and `flash` return the operation ID for `operation watch`.
`reset` and `flash` require an active, Agent-confirmed central reservation owned by `--owner`.
The read-only `probe` and `serial read` routes do not require a reservation, but the owning Agent
still enforces capability, online-state, lock, drain, expiry, and local safety checks. For a Phase 6
principal, probe requires `benches:operate`, reset requires `benches:reset`, serial requires
`benches:serial`, and flash requires `benches:flash` plus `artifacts:write`, all on the exact bench.
A legacy token instead uses the compatible `workflows:run` scope, plus `artifacts:write` for flash.

The distributed control plane intentionally does not expose the legacy power action routes. Those
commands are available only through a standalone Agent's compatibility API; distributed workflows
remain limited to their declared portable action set.

## Shared Phase 4/5 commands

### Tokens

```text
labctl token create --name NAME --owner OWNER --scope SCOPE [--scope SCOPE] [--expires-at RFC3339]
labctl token list
labctl token revoke TOKEN_ID
```

`token create` prints plaintext once. `token list` and `revoke` expose only public metadata.
On either empty token store, the first `token create` is unauthenticated and must include every
scope supported by that endpoint. That means nine scopes, including `agents:read` and
`agents:admin`, at the control plane; the standalone Agent retains its seven-scope Phase 4 set.
After bootstrap, control-plane token administration requires `agents:admin`, while standalone
Agent token administration requires all seven local scopes. Keep a backup administrator credential
because bootstrap never reopens and the last-admin revocation guard cannot prevent expiry.

### CI sessions

```text
labctl ci session create [BENCH REQUEST OPTIONS] [METADATA OPTIONS] [--idempotency-key KEY]
labctl ci session show SESSION_ID
labctl ci session watch SESSION_ID [--interval SECONDS]
labctl ci session cancel SESSION_ID
labctl ci session finalize SESSION_ID [--idempotency-key KEY]
```

Metadata options are `--external-run-id`, `--repository`, `--ref`, `--commit-sha`, and `--actor`.
Unspecified values are detected from GitHub Actions, GitLab CI, Jenkins, or the local environment.

Bench request options are:

```text
--bench ID
--require capability=NAME                 # repeatable
--label KEY=VALUE                         # repeatable, required match
--prefer-label KEY=VALUE                  # repeatable, ranking only
--agent-label KEY=VALUE                   # repeatable, required Agent match
--preferred-location LOCATION             # Agent ranking preference
--allow-simulated | --no-allow-simulated
--allow-physical | --no-allow-physical
--wait-timeout 10m
--reservation-duration 30m
```

Simulated benches are allowed by default; physical benches require explicit `--allow-physical`.
Use `--no-allow-simulated --allow-physical --bench ID` for an explicit physical run.

### One-command CI flow

```text
labctl ci run --workflow NAME
  [--artifact INPUT=PATH] [--input NAME=VALUE]
  [BENCH REQUEST OPTIONS]
  [--junit-output PATH]
  [--artifacts-directory DIRECTORY]
  [--interval SECONDS]
```

Example:

```console
labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.6.0 \
  --require capability=firmware \
  --require capability=serial \
  --require capability=reset \
  --require capability=probe \
  --label board=esp32 \
  --allow-simulated \
  --no-allow-physical \
  --wait-timeout 10m \
  --reservation-duration 30m \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

This creates a session, selects/reserves a bench, uploads inputs, maintains a heartbeat, launches
the workflow, polls progress, finalizes cleanup, writes JUnit, downloads artifacts, publishes
provider outputs/summary, and returns a stable CI exit code. `SIGINT` and `SIGTERM` request
server-side cancellation before the client exits.

### Artifacts and results

```text
labctl ci upload SESSION_ID PATH [--name NAME] [--artifact-type TYPE]
  [--sha256 HEX] [--idempotency-key KEY]
labctl ci artifacts SESSION_ID
labctl ci download ARTIFACT_ID --output PATH
labctl workflow results WORKFLOW_RUN_ID [--format json|junit] [--output PATH]
```

`ci upload` computes SHA-256 when `--sha256` is omitted. `workflow results` writes the requested
format atomically when `--output` is present and otherwise prints it to stdout.

## Endpoint compatibility

The following command families work against both HTTP applications, although the control plane
uses global bench IDs and returns distributed operation records:

```text
labctl version
labctl health
labctl bench list
labctl bench show ID
labctl bench flash ID FILE --owner OWNER [--version VERSION]
labctl bench probe ID --owner OWNER
labctl bench reset ID --owner OWNER
labctl bench serial read ID --owner OWNER [--timeout SECONDS] [--until REGEX] [--max-lines N]

labctl operation show ID
labctl operation list [--bench-id ID] [--status STATUS]
labctl operation watch ID [--interval SECONDS]
labctl operation cancel ID --owner OWNER

labctl workflow list
labctl workflow show NAME
labctl workflow register DEFINITION.yaml
labctl workflow run NAME --bench BENCH --owner OWNER
labctl workflow watch RUN_OR_OPERATION_ID [--interval SECONDS]
labctl workflow cancel RUN_OR_OPERATION_ID --owner OWNER
labctl workflow results RUN_OR_OPERATION_ID [--format json|junit] [--output PATH]
```

The control-plane workflow ID is its global distributed operation ID. Compatibility commands use a
standalone Agent's local workflow-run ID.

### Standalone Agent-only legacy commands

These routes are retained by the standalone Agent and are not control-plane endpoints:

```text
labctl bench timeline ID [--category CATEGORY]
labctl bench reserve ID --owner OWNER
labctl bench release ID --owner OWNER
labctl bench power-on ID --owner OWNER
labctl bench power-off ID --owner OWNER
labctl bench power-cycle ID --owner OWNER
labctl bench list [--available] [--reserved | --no-reserved]
labctl operation list [--type TYPE]
labctl event list [--bench-id ID] [--event-type TYPE]

labctl reservation extend RESERVATION_ID --owner OWNER --duration 15m
labctl reservation cancel RESERVATION_ID --owner OWNER
labctl reservation queue BENCH --owner OWNER --duration 20m
labctl reservation queue-list BENCH
labctl reservation queue-cancel QUEUE_ID --owner OWNER

labctl reservation create LOCAL_BENCH --owner OWNER --duration 30m
  [--start RFC3339] [--queue-if-busy]
labctl workflow run NAME --bench LOCAL_BENCH --owner OWNER
  [--reservation-id ID] [--release-after]
```

For the control plane, use `reservation create`, `renew`, and `release` with a global bench ID and
an observed lease version as shown above. Use `agent timeline`, `operation list`, and reservation
history for distributed diagnostics; `bench timeline` and `event list` are local compatibility
views. A standalone-only route is absent from the control plane and fails with `404` or `405`.
The parser also retains the local-only `bench list --available/--reserved` and
`operation list --type` filters; the control plane does not implement those filter semantics, so
do not use them for distributed selection.

Durations accept `30m`, `2h`, and compound forms such as `1h30m`. `--input key=value` is
repeatable. Phase 4 workflow definitions type-check values and allow only
`${{ inputs.<name> }}`; legacy Phase 3 definitions retain literal `${name}` substitution.

## Output

Read commands accept `--output table` (default) or `--output json`. For example:

```console
labctl ci session show SESSION_ID --output json
labctl token create --name demo --owner demo --scope ci:sessions --output json
```

`workflow results --output` and `ci download --output` use `--output` as a destination path rather
than a presentation mode.

Before upload, the CLI verifies the file is a readable, non-empty regular file no larger than 100
MB and calculates SHA-256. The Agent independently applies its configured size and checksum rules.

## Exit codes

Non-CI commands retain:

| Code | Meaning |
| ---: | --- |
| 0 | success |
| 1 | unexpected/invalid data |
| 2 | CLI usage |
| 3 | not found |
| 4 | state conflict |
| 5 | ownership/permission failure |
| 6 | backend or connection unavailable |
| 7 | failed/cancelled operation or workflow |

CI commands use stable automation codes:

| Code | Meaning |
| ---: | --- |
| 0 | success |
| 10 | workflow failed |
| 11 | hardware test failed |
| 12 | immediate zero-wait bench selection failed |
| 13 | positive bench-wait deadline elapsed |
| 14 | authentication failed |
| 15 | artifact upload failed |
| 16 | workflow cancelled |
| 17 | session timed out |
| 18 | cleanup failed |
| 19 | backend unavailable |
| 20 | client or protocol error |

See [CI troubleshooting](docs/CI_TROUBLESHOOTING.md) for diagnostics and policy guidance.
