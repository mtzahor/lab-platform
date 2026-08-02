# Phase 5: distributed control plane and multi-Agent labs

Status: **reference implementation available in 0.6.0-alpha**. An independent control plane can
enroll multiple Agents, aggregate their benches, reserve and operate a remote bench, run a complete
workflow/CI session on the owning Agent, synchronize artifacts, and reconcile a disconnect without
executing the same command twice. PostgreSQL is the supported production control-plane store;
SQLite remains available for the loopback developer demonstration and Agent-local durable state.

The release is still an early distributed implementation. It is a single-control-plane reference
deployment with PostgreSQL persistence and development-mode per-Agent bearer credentials. It does
not provide control-plane HA. The normal test suite uses SimLab; the remote ESP32 path is
intentionally an opt-in manual hardware check.

## System boundary and authority

```text
labctl / CI provider / future UI
               |
        REST + API token
               v
      Central control plane
      | inventory and routing
      | reservations and CI
      | operation history
      | artifact mediation
      |
      +---- authenticated WebSocket ---- Agent: home-lab ---- SimLab / ESP32
      +---- authenticated WebSocket ---- Agent: office-lab -- SimLab / hardware
      `---- authenticated WebSocket ---- Agent: sim-cluster - SimLab
```

The control plane is authoritative for Agent registration, immutable Agent slugs, global bench
identity, global reservations, reservation leases, workflow and CI requests, user-facing remote
operations, routing, API authorization, and global audit history. An Agent is authoritative for
physical state, backend health, local device presence, local locks, safety decisions, command
execution, progress, and locally created artifacts.

Inventory, operation status, workflow progress, artifact metadata, and last-seen state are shared
facts that are reconciled. Sending a command does not make an operation successful: only a
terminal Agent event can do that. The system deliberately provides at-least-once delivery plus
idempotent execution, not exactly-once distributed execution.

## What Phase 5 implements

- A separately runnable `lab-control-plane` FastAPI application and an authenticated protocol
  `1.0` WebSocket gateway.
- Stable Agent IDs and immutable slugs; a bench is exposed globally as
  `<agent-slug>/<local-bench-id>` and an Agent display-name change cannot silently rename it.
- Random, hashed, expiring, revocable, one-time enrollment tokens and a distinct hashed credential
  for every Agent. Enrollment and credential rotation return plaintext once.
- Heartbeats, boot IDs, clock-skew checks, `ONLINE`/`DEGRADED`/`OFFLINE` transitions, drain mode,
  full inventory snapshots, and incremental inventory updates.
- One unified inventory with Agent/location/Agent-label/bench-label/capability/kind filtering.
- Persisted remote commands and operations, pre-execution acceptance, expiry, cancellation,
  progress, stable terminal outcomes, and an `UNKNOWN` state during an interrupted connection.
- A durable Agent command journal, durable local reservation leases, and a bounded durable event
  buffer with progress coalescing and terminal-event retention.
- Control-plane-owned reservations that become active only after the owning Agent confirms the
  current versioned lease. Renew/release use optimistic lease-version checks.
- Complete sequential workflow dispatch to one Agent, instead of network round trips for every
  hardware step.
- SHA-256-verified input and output artifact transfer over short-lived scoped HTTP capabilities,
  plus a bounded Agent cache with LRU cleanup and active-operation pinning.
- Distributed CI sessions using the same provider-neutral CLI for GitHub Actions, GitLab CI,
  Jenkins, and local use. Selection supports required Agent labels and a preferred location.
- Metrics and Agent timelines, protocol/recovery/failure suites, and a development-scale SimLab
  test covering 10 Agents, 1,000 benches, 100 concurrent routes, 500 queued CI sessions, and mass
  reconnect.

## Enrollment and connection lifecycle

1. An administrator creates a named, time-limited enrollment token. Its allowed labels become the
   enrolled Agent's labels.
2. `lab-agent connect` reads that token from an environment variable and calls the enrollment
   endpoint. It does not write the returned credential to disk.
3. The control plane atomically consumes the token, creates the Agent identity, stores only a
   credential verifier, and returns the Agent ID, credential, and Agent-specific gateway URL.
4. The operator stores the non-secret ID/gateway in YAML and provides the credential through the
   configured environment variable.
5. The Agent authenticates its WebSocket, sends `AGENT_HELLO`, negotiates protocol compatibility,
   receives `WELCOME`, publishes inventory, and then sends heartbeats and events.

Protocol and product versions are independent. Product `0.6.0-alpha` speaks protocol `1.0`.
Peers require the same protocol major version; a compatible minor version may add optional fields.
Every envelope has a UUID message ID, Agent ID, UTC timestamp, correlation ID, typed payload, and
per-connection sequence number. Unknown required message types, wrong directions, gaps, invalid
payloads, and incompatible protocol majors fail explicitly.

Transport sequence numbers restart with each authenticated connection. Durable command IDs,
idempotency keys, event IDs, reservation versions, and journal records provide replay protection
across reconnects and process restarts.

## Reservation, routing, and command safety

The control plane filters candidates to online, non-draining Agents and compatible, available
benches. It applies required Agent and bench labels, kind and capability constraints, then ranks a
preferred location/labels, load, least-recent use, and stable bench ID. Candidate selection and
the central reservation transaction are coordinated so concurrent callers cannot both own the
same bench.

A reservation is initially pending. The control plane sends `RESERVATION_ACTIVATED` with its
bench, owner, validity interval, and monotonically increasing lease version. Only a matching Agent
confirmation promotes it to active and makes it eligible for a mutating command. Release and
renewal follow the same versioned protocol. Old, expired, wrong-bench, or wrong-reservation leases
are rejected locally even if a delayed control-plane message arrives later.

For every remote mutation, the control plane persists a command before dispatch. The Agent checks
identity, expiry, lease, capability, drain state, local locks, payload shape, and local safety
policy, persists the command before acceptance, then executes it through typed backend methods.
There is no remote shell command or arbitrary executable payload. Receiving a known command ID or
idempotency key returns journaled state rather than repeating the physical action.

The public direct remote action set is deliberately narrow: `probe`, `reset`, `read-serial`, and
`flash`. Each route creates a durable global operation and routes by global bench ID, never by an
Agent URL. Reset and flash are mutations and therefore require an active, Agent-confirmed central
lease belonging to the supplied owner. Probe and serial read do not require a reservation, but the
Agent still applies capability, online-state, lock, drain, expiry, and safety checks. Flash stages
the firmware in control-plane artifact storage and requires both `workflows:run` and
`artifacts:write`; the other three actions require `workflows:run`. Legacy power action routes,
bench-local immediate reservation aliases, queues, bench timelines, and the event-list endpoint
remain standalone Agent compatibility features rather than control-plane routes.

## Disconnect, restart, and reconciliation

The default policy does not queue new mutating work for an offline Agent. Heartbeat timeout first
degrades the Agent and its benches; the offline timeout marks them offline, moves running remote
operations to `UNKNOWN`, and moves an active reservation lease to `UNKNOWN` for a bounded grace
period. It does not immediately invent a failure or release ownership to another caller.

On reconnect, the control plane requests a reconciliation report containing the Agent boot ID,
active and recent journal entries, local leases, inventory, and buffered-event count. Resolution
is deterministic:

- a terminal journal entry finalizes the matching global operation;
- a same-boot accepted/queued command may be safely redelivered with the same command ID;
- a matching current lease restores interrupted ownership;
- stale local leases are released and stale lease versions cannot replace newer ones;
- a changed boot ID distinguishes process restart from a short network break, so work missing from
  the restarted Agent journal is failed safely instead of assumed to be running;
- unresolved unknown operations fail only after the configured reconciliation deadline; and
- interrupted leases expire after their configured grace period, preventing indefinite leaks.

Important Agent events are written to SQLite before transmission. One exact `EVENT_BATCH` remains
in the buffer until the control plane has routed it and replies with a correlated `EVENT_ACK` and
watermark. A lost acknowledgment therefore causes replay of the same event IDs; control-plane
deduplication makes that replay harmless. The bounded buffer coalesces repetitive progress first,
retains terminal results/failures preferentially, and reports overflow after reconnect.

Administrators can drain an Agent before maintenance. Draining removes it from new reservation and
CI selection while allowing active work to finish; the Agent reaches `DRAINED` when idle. Undrain
restores eligibility. Optional queued-work cancellation is explicit.

## Workflows, CI, and artifacts

The control plane validates a workflow, chooses or verifies a global bench, confirms its lease,
stages input artifacts, and sends one `RUN_WORKFLOW` command. The owning Agent resolves only
server-issued artifact descriptors, pins cached inputs, executes all sequential steps under local
locks, captures serial output, and reports progress/results. This keeps physical safety and the
fine-grained execution loop on the Agent side of an unreliable link.

Artifact content never travels in the control WebSocket. Client uploads are streamed into
generated platform paths with declared-size limits and SHA-256 verification. The Agent downloads
through a short-lived capability scoped to that Agent and artifact, verifies the digest, and
caches immutable content by identity/digest. Agent outputs are first reported as metadata with a
local artifact ID; the control plane assigns the global ID and grants a scoped upload capability.
Upload completion is idempotent, and neither filenames nor protocol payloads become filesystem
paths.

Download bearer capabilities are delivery-attempt state, never durable command state. The central
database stores only the artifact identity, Agent scope, checksum, size, and safe target path.
Immediately before every initial dispatch or reconciled replay, the control plane issues a fresh
capability and hydrates an in-memory command envelope; the persisted command and idempotency
fingerprint exclude the transfer ID, URL, expiry, and plaintext token. A control-plane restart
therefore reissues a usable short-lived capability without writing token bytes into
`remote_commands`.

Distributed CI persists the CI session-to-workflow binding and cleanup state. It uses central
selection/reservation, heartbeat and timeout handling, the remote workflow command, global
operation status, and synchronized output artifacts. Provider details remain CLI metadata; no CI
provider calls a backend or Agent directly.

## Loopback SimLab demonstration

Requirements are Python 3.11+ and `uv`. The checked-in control-plane configuration enables
unencrypted transport only on loopback; it cannot be reused on a non-loopback bind.

Install and start the control plane:

```console
uv sync --all-extras
uv run lab-control-plane --config config/control-plane.yaml
```

In another terminal, bootstrap a client token. With an empty token store, only this all-scope first
token creation may be unauthenticated. The plaintext is printed once:

```console
source .venv/bin/activate
export LAB_PLATFORM_SERVER=http://127.0.0.1:8443
labctl token create \
  --name local-admin \
  --owner local-admin \
  --scope agents:read \
  --scope agents:admin \
  --scope benches:read \
  --scope reservations:write \
  --scope workflows:run \
  --scope operations:read \
  --scope artifacts:read \
  --scope artifacts:write \
  --scope ci:sessions
export LAB_PLATFORM_TOKEN='<printed client token>'
```

Create and consume a one-time Agent enrollment token:

```console
labctl agent enrollment-token create \
  --name home-lab \
  --expires-in 30m \
  --allowed-label environment=development
export LAB_AGENT_ENROLLMENT_TOKEN='<printed enrollment token>'

lab-agent connect \
  --config config/agent.yaml \
  --control-plane http://127.0.0.1:8443 \
  --location home \
  --credential-env LAB_AGENT_HOME_CREDENTIAL
```

The final command prints a non-secret YAML fragment and the credential export command. Make a
per-Agent configuration directory so the checked-in SimLab labels are retained, then use that
fragment to edit the existing `control_plane` and `identity` blocks in its `agent.yaml`. Keep the
Agent name `home-lab`, choose a unique `agent.port`, and set
`control_plane.allow_insecure_loopback: true` for this loopback demo only. Because relative state
paths resolve inside the supplied configuration directory, each copy gets independent local state.
Then export the printed credential and start that Agent:

```console
mkdir -p config/home-agent
cp config/agent.yaml config/home-agent/agent.yaml
cp config/simlab.yaml config/home-agent/simlab.yaml
# Edit config/home-agent/agent.yaml with the printed identity/gateway fragment and unique port.
export LAB_AGENT_HOME_CREDENTIAL='<printed Agent credential>'
lab-agent --config-dir config/home-agent
```

Repeat enrollment with another name and separate paths/environment variable to add a second Agent.
The CLI remains pointed at the control plane; ordinary commands never contain an Agent URL:

```console
labctl agent list
labctl bench list --online --agent-label environment=development
labctl reservation create home-lab/bench-01 \
  --owner demo-user \
  --duration 30m \
  --output json
labctl bench probe home-lab/bench-01 --owner demo-user --output json
labctl bench reset home-lab/bench-01 --owner demo-user
labctl operation watch OPERATION_ID
labctl reservation release RESERVATION_ID \
  --owner demo-user \
  --expected-lease-version LEASE_VERSION
```

Register and run the same CI workflow across the unified inventory:

```console
labctl workflow register examples/workflows/esp32-ci-test.yaml
mkdir -p build
printf 'phase-5-simulated-firmware\n' > build/firmware.bin
labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.6.0 \
  --agent-label environment=development \
  --no-allow-physical \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

Useful administrative and local diagnostic commands are:

```console
labctl agent drain AGENT_ID
labctl agent undrain AGENT_ID
labctl agent refresh AGENT_ID
labctl agent timeline AGENT_ID
labctl operation reconcile OPERATION_ID

lab-agent status --config-dir config/home-agent
lab-agent doctor --config-dir config/home-agent
lab-agent connection status --config-dir config/home-agent
lab-agent journal list --config-dir config/home-agent
lab-agent reconnect --config-dir config/home-agent
```

## Configuration and persistence

[`config/control-plane.yaml`](../config/control-plane.yaml) documents all central settings and is
the loopback SQLite demo. The production-oriented
[`config/control-plane.postgresql.yaml`](../config/control-plane.postgresql.yaml) uses PostgreSQL,
HTTPS/WSS, and non-loopback binding.
[`config/agent.yaml`](../config/agent.yaml) is a safe single-Agent default with distributed mode
disabled; enrollment prints the fields needed to enable it. Strict configuration rejects unknown
keys and unsafe non-loopback plaintext transport.

The control plane persists Agent identity/credentials, connections, inventory, commands,
operations, leases, reconciliation claims, workflows, CI bindings, artifact metadata/transfers,
protocol journals, timelines, and API tokens. The Agent persists its command journal, event buffer,
reservation leases, and verified cache content in local SQLite. Database migrations are mandatory;
the distributed demo and production deployment do not require manual schema edits.

For a production PostgreSQL deployment, have the deployment-secret system materialize the DSN in
the protected configuration file, or omit its password and provide credentials through a protected
libpq mechanism such as `PGPASSFILE`. Do not commit passwords, and percent-encode reserved
characters if credentials are embedded in a URL. Use `sslmode=require` at minimum; prefer
`sslmode=verify-full` plus `sslrootcert` when the deployment has a trusted database CA. The direct
Psycopg spelling is canonical:

```yaml
database:
  url: postgresql://lab@database.internal:5432/lab_platform?sslmode=require&connect_timeout=10
```

`postgresql+psycopg://` is accepted for operator familiarity and normalized internally. Before a
new deployment or binary upgrade, apply the idempotent migrations with the same protected config,
then start the service:

```console
lab-control-plane migrate --config /etc/lab-platform/control-plane.yaml
lab-control-plane --config /etc/lab-platform/control-plane.yaml
```

Back up PostgreSQL before an upgrade and grant the service account only the schema privileges it
needs. SQLite is suitable for the checked-in single-process developer demo, not the recommended
production central store.

`GET /metrics` exposes control-plane inventory, presence, reconnect, heartbeat lag, command,
reconciliation, artifact-transfer, CI, and gateway counters. The local Agent `/metrics` endpoint
exposes connection state, reconnect attempts, queue/buffer sizes, active operations, bench health,
lease count, and journal size. Agent timelines carry correlation IDs for administrative diagnosis.

## Security posture and limits

Outside the loopback demo, configure an HTTPS public URL and the control-plane TLS certificate/key;
an HTTPS/WSS URL without those files is rejected. If a same-host reverse proxy terminates TLS, bind
the control-plane process to loopback and explicitly set
`development.allow_tls_termination_proxy: true` instead.
Agents then use WSS. Each Agent and API client has a separate revocable credential, and stored
token material is one-way hashed. Keep enrollment/API/Agent/transfer tokens, database credentials,
private keys, and artifact bytes out of logs. Agent YAML references a credential environment
variable and does not persist plaintext. Artifact transfer records contain only one-way token
hashes; plaintext download tokens exist only in the outbound delivery attempt and plaintext upload
tokens only in the active in-memory request path.

This is not a production-audited, internet-facing zero-trust service. In particular:

- mTLS/client certificates are a future replacement at the existing Agent-authentication boundary;
- there is one control plane, with no HA, consensus, active-active failover, or distributed DB;
- there are no organizations, tenants, SSO, advanced RBAC, secrets vault, or polished dashboard;
- artifact transfer is control-plane mediated, not peer-to-peer or resumable; and
- the real ESP32 route uses the existing explicit hardware configuration and test gate. Normal CI
  proves distributed behavior with SimLab and does not claim a physical run occurred.

Use a private network, real TLS, scoped credentials, protected environment-secret storage, backups,
and an external reverse proxy/firewall for any non-local evaluation.

## Verification and contracts

The checked-in Agent and control-plane OpenAPI contracts are
[`openapi.json`](openapi.json) and [`control-plane-openapi.json`](control-plane-openapi.json).
Run the non-hardware quality gate:

```console
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest -m "not hardware"
uv run python scripts/export_openapi.py --service agent --check docs/openapi.json
uv run python scripts/export_openapi.py --service control-plane --check \
  docs/control-plane-openapi.json
uv build
```

The adapter contract tests use a deterministic Psycopg-shaped test double. An optional live smoke
test applies every migration to an isolated PostgreSQL database and verifies its schema version.
Provide credentials through protected libpq configuration and opt in explicitly:

```console
export LAB_PLATFORM_TEST_POSTGRESQL_URL='postgresql://lab@127.0.0.1/lab_platform_test?sslmode=require'
uv run pytest tests/unit/test_phase5_postgresql.py -k optional_live_smoke
```

Protocol contract, distributed integration, recovery/fault-injection, persistence migration, CLI,
API, artifact-transfer, and scale suites cover the Phase 5 safety boundary. Local ESP32 pytest
scenarios require `LAB_PLATFORM_ENABLE_HARDWARE_TESTS=1`; the separate end-to-end distributed
smoke test requires `LAB_PLATFORM_ENABLE_DISTRIBUTED_HARDWARE_TESTS=1` plus an exact bench ID,
reviewed configuration, firmware path, and expected firmware version. Neither environment variable
authorizes an ordinary `labctl` command; manual physical selection instead requires an explicit
bench together with `--no-allow-simulated --allow-physical`. See
[hardware testing](HARDWARE_TESTING.md).
