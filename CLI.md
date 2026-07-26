# CLI

`labctl` is an HTTP-only client. It never constructs application services, SimLab, or physical
target adapters.

## Connection and credentials

Agent selection order is `--server`, `LAB_PLATFORM_SERVER`,
`~/.config/lab-platform/cli.yaml`, then `http://127.0.0.1:8080`.

```yaml
server: https://lab-agent.internal.example
```

Machine commands read the bearer token from `LAB_PLATFORM_TOKEN`. Select another environment
variable with `--token-env NAME`:

```console
export DEVICE_LAB_TOKEN='lp_...'
labctl --token-env DEVICE_LAB_TOKEN ci session show SESSION_ID
```

There is no plaintext token option. See [API tokens](docs/API_TOKENS.md).

## Phase 4 commands

### Tokens

```text
labctl token create --name NAME --owner OWNER --scope SCOPE [--scope SCOPE] [--expires-at RFC3339]
labctl token list
labctl token revoke TOKEN_ID
```

`token create` prints plaintext once. `token list` and `revoke` expose only public metadata.
On an empty token store, the first `token create` is unauthenticated and must include every
supported scope. Afterward, all three commands require an active all-scope bearer from the
configured token environment variable. Keep a backup all-scope credential because bootstrap never
reopens and the last-admin revocation guard cannot prevent expiry.

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
--allow-simulated | --no-allow-simulated
--allow-physical | --no-allow-physical
--wait-timeout 10m
--reservation-duration 30m
```

Both backend kinds are allowed by default. Use `--no-allow-physical` for SimLab-only CI and
`--no-allow-simulated --allow-physical --bench ID` for an explicit physical run.

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
  --input expected_version=0.5.0 \
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

## Existing bench and workflow commands

```text
labctl version
labctl health
labctl bench list [--online] [--available] [--capability NAME] [--label KEY=VALUE]
labctl bench show ID
labctl bench timeline ID [--category CATEGORY]
labctl bench reserve ID --owner OWNER
labctl bench release ID --owner OWNER
labctl bench power-on ID --owner OWNER
labctl bench power-off ID --owner OWNER
labctl bench power-cycle ID --owner OWNER
labctl bench flash ID FILE --owner OWNER [--version VERSION]
labctl bench probe ID --owner OWNER
labctl bench reset ID --owner OWNER
labctl bench serial read ID --owner OWNER [--timeout SECONDS] [--until REGEX] [--max-lines N]

labctl operation show ID
labctl operation list [--bench-id ID] [--status STATUS] [--type TYPE]
labctl operation watch ID [--interval SECONDS]
labctl operation cancel ID --owner OWNER
labctl event list [--bench-id ID] [--event-type TYPE]

labctl reservation list [--bench-id ID] [--owner OWNER] [--status STATUS]
labctl reservation show RESERVATION_ID
labctl reservation create BENCH --owner OWNER --duration 30m [--start RFC3339]
labctl reservation extend RESERVATION_ID --owner OWNER --duration 15m
labctl reservation release RESERVATION_ID --owner OWNER
labctl reservation cancel RESERVATION_ID --owner OWNER
labctl reservation queue BENCH --owner OWNER --duration 20m
labctl reservation queue-list BENCH
labctl reservation queue-cancel QUEUE_ID --owner OWNER

labctl workflow list
labctl workflow show NAME
labctl workflow run NAME --bench BENCH --owner OWNER [--reserve 30m] [--release-after]
labctl workflow watch RUN_ID [--interval SECONDS]
labctl workflow cancel RUN_ID --owner OWNER
```

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
