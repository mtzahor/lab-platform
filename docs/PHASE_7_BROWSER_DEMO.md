# Phase 7 browser demonstration

This hardware-free walkthrough demonstrates the 0.8.0-alpha browser cut line against a real
control plane, distributed Agent, and SimLab benches. It starts with one fully functional Agent;
repeat the enrollment/start step with isolated Agent state to build the full `home-lab`,
`office-lab`, and `simulation-cluster` topology.

## Resulting identities

| User | Access used in the demo |
| --- | --- |
| `michael` | organisation `OWNER`; identity/access administration and audit |
| `alice` | `OPERATOR` on `home-lab` plus workflow access |
| `bob` | `VIEWER` on the demo bench |
| `carol` | `LAB_ADMIN` on the demo Agent |

The complete target topology is:

```text
simlab-demo
├ home-lab
│  ├ esp32-devkit-01 (optional physical bench; never required here)
│  └ virtual-esp32-01
├ office-lab
│  └ virtual-stm32-01
└ simulation-cluster
   └ virtual-esp32-01..20
```

Global IDs come from the Agent configuration. Use the exact names returned by the inventory rather
than assuming the illustrative suffixes above.

## 1. Prepare the browser-enabled control plane

Use a disposable copy of `config/control-plane.yaml`. Set the identity organisation and remove
development auto-login so each browser role is genuinely authenticated:

```yaml
identity:
  default_organisation_slug: simlab-demo
  default_organisation_name: SimLab Demo

web:
  enabled: true
  public_url: http://127.0.0.1:8443
  api_base_url: /api/v1

development:
  enabled: true
  auto_login_user: null
  allow_insecure_agent_transport: true
```

Build, migrate, bootstrap the owner, and start the control plane:

```console
cd apps/web
npm ci
npm run build
cd ../..
uv sync --extra dev
uv run lab-control-plane migrate --config config/control-plane.yaml
uv run lab-control-plane bootstrap-admin \
  --config config/control-plane.yaml \
  --organisation-slug simlab-demo \
  --organisation-name "SimLab Demo" \
  --username michael \
  --display-name "Michael"
uv run lab-control-plane --config config/control-plane.yaml
```

Bootstrap prompts for Michael's password without echo. Do not place passwords or credentials in
shell history.

## 2. Connect SimLab

In another terminal, use Michael's native login to create a one-time enrollment credential:

```console
export LAB_PLATFORM_SERVER=http://127.0.0.1:8443
uv run labctl auth login --username michael --organisation simlab-demo
uv run labctl agent enrollment-token create --name home-lab --expires-in 30m
```

Follow the [Phase 5 Agent enrollment procedure](PHASE_5.md#loopback-simlab-demonstration) with the
checked SimLab configuration, a unique Agent data directory, the name `home-lab`, and the printed
one-time token. Keep the Agent running. Confirm inventory and capture exact IDs:

```console
uv run labctl agent list
uv run labctl bench list --online --label board=esp32
export AGENT_UUID='replace-with-home-lab-agent-uuid'
export SIM_BENCH='replace-with-visible-global-bench-id'
uv run labctl workflow register examples/workflows/esp32-ci-test.yaml
```

The text firmware fixture below is SimLab-only. Never flash it to a physical target:

```console
mkdir -p build
printf 'phase-7-simulated-firmware\n' > build/firmware.bin
```

For the expanded topology, repeat enrollment with unique slugs, database/artifact/data paths, and
ports. Configure one Agent offline and inject one SimLab health degradation before the admin tour.
The release flow itself needs only the first simulated bench.

## 3. Administrator browser tour

Open `http://127.0.0.1:8443/` and sign in as `michael`.

1. Confirm the account menu shows **SimLab Demo** and Michael.
2. On Overview, inspect Agent, bench, reservation, operation, queue, workflow failure, and CI counts.
3. Open Agents and the `home-lab` detail view; confirm version, protocol, last seen, and benches.
4. Under Administration → Users, create:
   - `alice`, display name `Alice Operator`, local authentication, membership `MEMBER`;
   - `bob`, display name `Bob Viewer`, local authentication, membership `MEMBER`;
   - `carol`, display name `Carol Lab Admin`, local authentication, membership `MEMBER`.
5. Create team `embedded`, add Alice, and create these assignments in Roles & access:
   - team `embedded`: `OPERATOR` on Agent `${AGENT_UUID}`;
   - Alice: `WORKFLOW_RUNNER` on workflow `esp32-ci-test`;
   - Bob: `VIEWER` on bench `${SIM_BENCH}`;
   - Carol: `LAB_ADMIN` on Agent `${AGENT_UUID}`.
6. Open Audit and confirm user/team/assignment events include Michael, target IDs, outcomes, and
   request IDs without plaintext passwords.
7. Sign out.

If an alpha administration form does not expose one of the exact assignment selectors, create it
with the matching `labctl role assign` command from [roles and permissions](ROLES_AND_PERMISSIONS.md),
then return to the browser and inspect effective access. The API, not a UI approximation, decides
the result.

## 4. Operator cut-line flow

Sign in as `alice`.

1. Filter Benches with `board=esp32`, Agent `home-lab`, kind SimLab/simulated, and available state.
2. Open `${SIM_BENCH}`. Confirm reserve/workflow controls are present and Agent-admin controls are
   absent.
3. Create a 30-minute reservation with a description. Verify the owner and countdown.
4. If another session already owns the bench, join its queue, inspect position, then leave the
   queue before continuing.
5. Open Workflows → `esp32-ci-test`, select the reserved bench, choose the existing reservation,
   upload `build/firmware.bin`, enter the expected version/input values required by the definition,
   and start the run while retaining the reservation after completion.
6. On the run page, watch step/progress changes. Observe the Live indicator; interrupt the event
   connection once and confirm Reconnecting/Polling does not change operation status.
7. Inspect serial output as text, assertions, errors, and artifacts. Download JUnit/results when
   complete.
8. Return to the bench and release the reservation using the confirmation dialog.
9. Sign out.

The workflow run must reuse the active reservation rather than attempting to reserve the bench a
second time. Refreshing or closing the tab does not cancel durable work.

## 5. Viewer denial

Sign in as `bob`.

1. Find and open `${SIM_BENCH}` and inspect visible status/history.
2. Confirm reserve, queue, flash, reset, serial mutation, workflow-run, and Agent drain controls are
   absent.
3. Attempt a direct restricted request from an API client using Bob's authenticated credential as
   documented in the Phase 6 team demo. Confirm the server returns a stable denial (or policy-hidden
   `404`) rather than relying on the missing browser button.
4. Sign out.

Sign back in as `michael`, open Audit, and find Bob's denied action by actor/outcome/request ID.

## 6. Lab-admin Agent control

Sign in as `carol`, open `home-lab`, and use the confirmed Drain action. Inventory stays visible,
but new work must not be dispatched. Use Undrain, verify the Agent returns to its normal state, and
sign out. Review both audit events as Michael.

## Acceptance record

Record the control-plane/frontend version, browser, database backend, Agent IDs/versions, selected
bench/workflow, operation ID, reservation ID, and relevant audit request IDs. Do not include
cookies, passwords, one-time credentials, firmware bytes, or complete serial output.

The demonstration passes when local browser login, permission-aware navigation, find/reserve/run,
live or polling progress, safe serial/artifact inspection, release, Viewer denial, Agent drain, and
audit correlation all work with SimLab and no physical hardware.

## Automated acceptance counterpart

The repository also carries two Playwright layers:

```console
cd apps/web
npx playwright install chromium
npm run e2e:all
```

The fixture layer checks deterministic browser rendering and permission-aware navigation. The
integration layer builds the production dashboard and starts an isolated real control plane with a
fresh SQLite database plus two independently enrolled SimLab Agent processes. Its single serial
scenario creates the Operator, Viewer, and Lab Admin identities and team/role assignment through
the browser, uses a real Agent for reserve/workflow/serial-artifact/release, proves the Viewer API
denial, drains an Agent, and finds the denial in audit history. Startup waits for both Agent health
endpoints; termination stops every child and deletes the temporary state. No checked local runtime
data or physical hardware is used.
