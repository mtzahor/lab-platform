# Public interface stability and deprecation

This policy defines the intended `1.x` contract. The repository remains on the `0.9.0-beta`
development line until the Phase 9 release gates pass; the documented surfaces are freeze
candidates, not a claim that `1.0.0` has shipped.

## Stable surfaces for 1.x

| Surface | Stable identifier | Compatibility promise |
| --- | --- | --- |
| REST API | `/api/v1` | Existing response fields/endpoints are not removed and required request fields are not added in a minor release. |
| Agent protocol | protocol major `1` | Existing message semantics and required fields remain compatible within the major. |
| Plugin API | `1.0` / major `1` | Public SDK models, lifecycle, capabilities, and error meanings remain source-compatible within the major. |
| Workflow documents | `apiVersion: lab.platform/v1`, `kind: Workflow` | Existing v1 fields and actions retain their meaning; optional fields/actions may be added. |
| Configuration | top-level `config_version: 1` | Existing version-1 keys retain their meaning; new keys have safe defaults or are optional. |
| CLI | core nouns below | Commands, primary arguments, exit meanings, and machine JSON field meanings remain compatible. |

Database tables, Python modules outside `lab_platform.plugin_sdk`, web component structure,
internal event layouts, and experimental endpoints explicitly labeled preview are not public
interfaces. Persistence migrations and backups remain operational compatibility commitments even
when their internal representation is not an API.

## REST API v1

Compatible minor changes may:

- add endpoints
- add optional request fields
- add response fields
- add enum values where clients are already required to tolerate unknown values
- introduce opt-in query behavior
- add headers that do not change the response meaning

A `1.x` minor release does not remove or rename an endpoint/field, add an unconditionally required
request field, change a field type/meaning, narrow accepted values, reuse an error code for a
different condition, or silently change authorization scope. A breaking change requires a new API
major and a parallel migration period.

Clients must ignore unknown response object fields and must not depend on object key order. They
must handle documented non-success status codes and unknown forward-compatible enum values. The
checked-in OpenAPI documents are release artifacts; CI must regenerate them and reject unintended
diffs before a release candidate.

## Stable CLI commands

The stable command families are:

```text
labctl bench
labctl reservation
labctl workflow
labctl operation
labctl ci
labctl agent
labctl auth
labctl plugin
```

Human-readable formatting may improve. Scripts should request JSON where offered and depend only
on documented fields and exit codes. A core command is not renamed or repurposed in `1.x`.
Deprecated aliases print a warning to stderr while preserving stdout machine output.

## Workflow schema v1

Every new checked-in workflow uses the envelope:

```yaml
apiVersion: lab.platform/v1
kind: Workflow
metadata:
  name: esp32-smoke-test
  version: 1
spec:
  requirements:
    capabilities: [flash, serial]
  steps:
    - action: flash
    - action: serial_expect
```

The parser rejects an unsupported `apiVersion` with
`WORKFLOW_SCHEMA_VERSION_UNSUPPORTED` and reports the supported versions. The legacy flat 0.9
document remains readable during the 1.x migration window and serializes back to the v1 envelope.
New examples and generated documents never emit the flat form.

Unknown actions or fields are not assumed safe. A future breaking workflow format uses a new
`apiVersion`; it does not reinterpret an existing v1 document.

## Configuration version 1

Major Agent and control-plane YAML files begin with:

```yaml
config_version: 1
```

Unknown configuration versions and unknown keys fail validation before services or hardware start.
Within version 1, additions are optional or have safe defaults. A key whose meaning must change is
introduced under a new name or a new configuration version.

Migration is reviewable and backup-first:

1. run the old binary's validation and backup procedure;
2. compare the installed release notes and example config;
3. add `config_version: 1` and translate renamed 0.9 keys explicitly;
4. run `lab-agent doctor` or `lab-control-plane config validate` with the new binary;
5. inspect the diff before startup;
6. keep the old config and database backup until acceptance passes.

An automated migrator, when supplied, writes a new file and never overwrites the source by
default. There is no generic automatic downgrade migration.

## Plugin API and capability names

Plugin API 1.x is backward compatible within major version 1. A plugin declares its exact API
version plus Agent bounds. Incompatible plugins are rejected locally without stopping compatible
plugins. New optional SDK model fields may be added with defaults; protocol method removals,
signature breaks, or changed error meanings wait for Plugin API 2.

The stable capability vocabulary is `probe`, `reset`, `power`, `serial`, `flash`, `debug`, `gpio`,
`can`, `capture`, `measure`, and `command`. The pre-1.0 `firmware` alias maps to `flash` and follows
the legacy migration window below.

## Deprecation lifecycle

The default public-API lifecycle is:

1. Announce deprecation in release notes and the relevant reference documentation.
2. Emit a structured runtime/CLI warning where practical, naming the replacement and earliest
   removal release.
3. Keep the behavior throughout the remaining `1.x` line.
4. Remove it no earlier than `2.0`.

An exceptional shorter window is permitted only for an exploitable security issue, data-loss or
hardware-safety risk, unlawful behavior, or a dependency no longer available. The advisory must
state why, affected versions, mitigation, replacement, and exact removal version/date. Silent
removal is not permitted.

Current migration items:

| Legacy surface | Replacement | Availability | Earliest removal |
| --- | --- | --- | --- |
| flat workflow YAML | `lab.platform/v1` workflow envelope | readable during 1.x | `2.0` |
| `firmware` capability name | `flash` | input/metadata alias during 1.x | `2.0` |
| pre-1.0 in-tree plugin lifecycle | `lab_platform.plugin_sdk` registration/driver lifecycle | compatibility adapter during 1.x | `2.0` |

## Error codes and behavior

Public error codes are identifiers, not prose. In a compatible release, their broad cause and
retry/safety meaning do not change. New codes may be added. Error messages and detail fields may
gain context, so clients must branch on code/status rather than exact English text.

Hardware failure and timeout codes never imply that a destructive operation is safe to retry.
Clients reacquire ownership/fencing state and probe the affected resource first.

## Review and exceptions

The `1.0` freeze review inventories REST/OpenAPI, Agent protocol, Plugin SDK, workflows,
configuration, CLI, database migrations, artifacts, error codes, and capabilities. Any exception
to this policy is recorded in release notes and
[`release/phase9-readiness.yaml`](../release/phase9-readiness.yaml) before release approval.
