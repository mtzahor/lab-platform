# Phase 6 team-access demonstration

This walkthrough exercises the Phase 6 functional definition of done with one organisation, four
human users, two teams, one service account, exact resource roles, persisted access policies, an
ESP32-compatible SimLab workflow, a Viewer denial, and organisation-scoped audit review. It uses
the real `labctl` and control-plane surfaces; no owner string supplies authority for a Phase 6
principal.

## Resulting access model

| Subject | Assignment | Intended result |
| --- | --- | --- |
| `michael` | Organisation `OWNER` membership | Bootstrap and administer the demo |
| team `embedded` | `OPERATOR` on Agent `home-lab` | Members can operate its authorised benches |
| team `validation` | `WORKFLOW_RUNNER` on workflow `esp32-ci-test` | Members can launch that workflow and CI sessions |
| `alice` | Member of both teams | Run the ESP32 workflow on the SimLab bench |
| `bob` | `VIEWER` on exact SimLab bench | See it, but not reset/flash/mutate it |
| `carol` | `RESERVER` on exact SimLab bench | Reserve and release it, but not operate it |
| `github-ci` | `WORKFLOW_RUNNER` on the workflow and `OPERATOR` on the exact bench | Run only the intended CI path |

The bench and workflow use `RESTRICTED` visibility so ordinary organisation membership does not
silently widen these resource assignments.

## 1. Prepare the organisation and Agent

Use a disposable loopback deployment. Before its first identity-enabled start, set these values in
the demo control-plane configuration:

```yaml
identity:
  default_organisation_slug: simlab-demo
  default_organisation_name: SimLab Demo
```

For a clearer access test, omit `development.auto_login_user`; if it remains configured, startup
prints the expected warning and explicit bearer credentials still take precedence. Apply schema
v10, create the first owner offline, start the control plane, and log in:

```console
lab-control-plane migrate --config config/control-plane.yaml
lab-control-plane bootstrap-admin \
  --config config/control-plane.yaml \
  --username michael \
  --display-name "Michael"

export LAB_PLATFORM_SERVER=http://127.0.0.1:8443
labctl auth login --username michael --organisation simlab-demo
```

The bootstrap and login commands prompt for passwords without echo. Do not put a password or token
in shell history.

Enroll and start an Agent named `home-lab` with the checked-in SimLab configuration by following
the [Phase 5 loopback Agent steps](PHASE_5.md#loopback-simlab-demonstration). The default SimLab
labels every simulated bench `board=esp32`; this walkthrough uses its first global bench. Verify the
resources and substitute the displayed Agent UUID below:

```console
labctl agent list
labctl bench list --online --label board=esp32

export AGENT_UUID='replace-with-home-lab-agent-uuid'
export SIM_BENCH='home-lab/bench-01'
```

If the global bench ID differs, use the exact value returned by `bench list`. Register the shared
workflow and create a harmless SimLab firmware fixture:

```console
labctl workflow register examples/workflows/esp32-ci-test.yaml
mkdir -p build
printf 'phase-6-simulated-firmware\n' > build/firmware.bin
```

The same workflow can target an explicitly gated physical ESP32 bench, but do not flash the text
fixture to real hardware. Use a real firmware binary, explicit `--no-allow-simulated
--allow-physical`, and the hardware procedure in [ESP32 setup](ESP32_SETUP.md).

## 2. Provision users and teams

Create local users. Each command prompts for a new password twice:

```console
labctl user create \
  --username alice --display-name "Alice Operator" --organisation-role member
labctl user create \
  --username bob --display-name "Bob Viewer" --organisation-role member
labctl user create \
  --username carol --display-name "Carol Reserver" --organisation-role member

labctl team create --slug embedded --name "Embedded"
labctl team create --slug validation --name "Validation"
labctl team add-member embedded --user alice --role member
labctl team add-member validation --user alice --role member
```

Assign only the resources needed by each subject:

```console
labctl role assign \
  --subject team:embedded --role operator --resource "agent:${AGENT_UUID}"
labctl role assign \
  --subject team:validation --role workflow-runner --resource workflow:esp32-ci-test
labctl role assign \
  --subject user:bob --role viewer --resource "bench:${SIM_BENCH}"
labctl role assign \
  --subject user:carol --role reserver --resource "bench:${SIM_BENCH}"
```

Persist restrictive policies. The embedded team is listed explicitly for bench discovery; its
Operator Agent assignment still supplies mutation permissions:

```console
labctl access-policy bench set "$SIM_BENCH" \
  --visibility restricted \
  --operation-role operator \
  --allowed-team embedded
labctl access-policy workflow set esp32-ci-test --visibility restricted

labctl access-policy bench get "$SIM_BENCH"
labctl access-policy workflow get esp32-ci-test
```

Both `set` commands take effect immediately and append access-policy audit events.

## 3. Exercise the human roles

End Michael's stored session, log in as Alice, and run the ESP32-compatible workflow on SimLab:

```console
labctl auth logout
labctl auth login --username alice --organisation simlab-demo
labctl bench list --online
labctl ci run \
  --workflow esp32-ci-test \
  --bench "$SIM_BENCH" \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.7.0 \
  --allow-simulated \
  --no-allow-physical \
  --junit-output phase6-alice.xml \
  --artifacts-directory phase6-alice-artifacts
labctl auth logout
```

Alice succeeds because team expansion supplies both exact workflow and parent-Agent bench grants.
The workflow/CI/artifact services recheck those grants before reservation, upload, transfer, and
remote command dispatch.

Log in as Bob. He can discover the exact bench but cannot mutate it:

```console
labctl auth login --username bob --organisation simlab-demo
labctl bench list --online
labctl bench reset "$SIM_BENCH" --owner bob
labctl auth logout
```

The reset command must exit nonzero. With the default
`authorisation.hide_unauthorised_resources: true`, the client sees the normal hidden bench `404`;
with hiding disabled, it sees a structured `403 PERMISSION_DENIED`. Either way the organisation
audit log records `PERMISSION_DENIED` with Bob as actor and `benches:reset` as the required
permission.

Carol can reserve the bench but cannot operate it. Copy the returned reservation ID and lease
version into the placeholders before releasing it:

```console
labctl auth login --username carol --organisation simlab-demo
labctl reservation create "$SIM_BENCH" --owner carol --duration 10m

export RESERVATION_ID='replace-with-reservation-uuid'
export LEASE_VERSION='replace-with-confirmed-lease-version'
labctl reservation release "$RESERVATION_ID" \
  --owner carol \
  --expected-lease-version "$LEASE_VERSION"
labctl auth logout
```

## 4. Run the same path as a service account

Log back in as Michael, create the CI principal, and give it the same two exact resources rather
than an organisation-wide role:

```console
labctl auth login --username michael --organisation simlab-demo
labctl service-account create \
  --name github-ci \
  --description "Phase 6 SimLab CI"
labctl role assign \
  --subject service-account:github-ci \
  --role workflow-runner \
  --resource workflow:esp32-ci-test
labctl role assign \
  --subject service-account:github-ci \
  --role operator \
  --resource "bench:${SIM_BENCH}"

labctl service-account credential create \
  --service-account github-ci \
  --name phase6-demo \
  --permission ci:sessions:create \
  --permission ci:sessions:read \
  --permission ci:sessions:cancel \
  --permission workflows:run \
  --permission benches:operate \
  --permission operations:read \
  --permission artifacts:read \
  --permission artifacts:write
```

The credential is displayed once. In real CI, save it directly in the provider's protected secret
store. For this local demonstration, paste it through a hidden prompt so it does not enter shell
history:

```console
printf 'Paste the one-time github-ci credential: '
IFS= read -r -s LAB_PLATFORM_TOKEN
printf '\n'
export LAB_PLATFORM_TOKEN

labctl ci run \
  --workflow esp32-ci-test \
  --bench "$SIM_BENCH" \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.7.0 \
  --allow-simulated \
  --no-allow-physical \
  --junit-output phase6-service.xml \
  --artifacts-directory phase6-service-artifacts
```

Prove that the credential cannot widen its resource roles or restriction set:

```console
labctl agent drain "$AGENT_UUID"
```

That command must fail. Remove the environment credential; the stored Michael session becomes
active again because environment credentials have higher precedence but never overwrite it:

```console
unset LAB_PLATFORM_TOKEN
```

## 5. Review attribution and clean up the credential

Review representative successes and the Viewer denial:

```console
labctl audit list --action CI_SESSION_STARTED --outcome succeeded --limit 20 --output json
labctl audit list --action PERMISSION_DENIED --outcome denied --limit 20 --output json
labctl audit list --action CREDENTIAL_CREATED --outcome succeeded --limit 20 --output json
labctl audit list --action BENCH_ACCESS_POLICY_UPDATED --outcome succeeded --limit 20 --output json
```

Confirm that Alice and `github-ci` appear on their own CI starts, Bob appears on the reset denial,
and Michael appears as the actor that created the credential and policies. Audit metadata must contain
only bounded IDs and safe context—never passwords, bearer tokens, firmware bytes, or full serial
logs.

Finally, identify and revoke the demo credential:

```console
labctl service-account credential list --service-account github-ci
labctl service-account credential revoke CREDENTIAL_ID
labctl auth logout
```

## Definition-of-done checklist

- Human login, service-account authentication, and native/environment credential precedence were
  exercised.
- Team-derived Agent and workflow roles allowed Alice's ESP32-compatible SimLab CI run.
- Bob's exact Viewer role allowed discovery but denied mutation, with a safe client response and an
  attributable audit event.
- Carol's exact Reserver role allowed the reservation lifecycle without granting operation rights.
- The `github-ci` credential was narrowed to the end-to-end CI permissions and denied unrelated
  Agent administration.
- Workflow, selected bench, CI ownership, artifact parents, and tenant context were checked inside
  the protected services before side effects.
- Successes, denial, access-policy changes, and credential creation were visible in the same
  organisation's audit history.
