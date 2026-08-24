# Version compatibility

Lab Platform versions the application, REST API, Agent protocol, and plugin API separately. An
identical application version is convenient but not required for every connected Agent; the
configured compatibility policy is authoritative.

## Versioned surfaces

| Surface | Phase 8 value | Compatibility rule |
| --- | --- | --- |
| application | `0.9.0-beta` development line | semantic version and declared release window |
| control-plane REST API | `v1` | clients use documented `v1` routes; breaking changes require a new API version |
| Agent protocol | `1.0` | the major must match; peers negotiate the lower shared minor |
| plugin API | `1.0` | plugins must target a supported declared plugin API |
| database schema | target `12`, minimum source `11` | application startup requires the current schema; migrations are explicit |
| backup format | `1` | restore also checks application, schema, and database backend |

Application, protocol, and schema compatibility are different checks. For example, an Agent with a
matching protocol major can still be rejected because its application version is below the
configured minimum.

## Inspect the deployment

```console
labctl version --all
```

The text report includes CLI, control plane, API, Agent protocol, plugin API, release channel,
edition, and every visible Agent's application/protocol/upgrade status. Use `--output json` for
automation. The same control-plane build information is available from `/api/v1/version`.

Before a control-plane change, also run:

```console
lab-control-plane upgrade check VERIFIED-BACKUP.tar.zst \
  --target-version 0.9.0-beta \
  --config /etc/lab-platform/control-plane.yaml
```

The check reports database, artifact storage, backup, and Agent blockers without installing an
upgrade.

## Agent policy

The production policy can be configured explicitly:

```yaml
compatibility:
  agents:
    minimum_supported_version: 0.8.0
    minimum_recommended_version: 0.8.0
    target_version: 0.9.0-beta
    maximum_supported_version: 0.9.999
```

Focused environment overrides are available for the hard minimum and target:

```dotenv
LAB_MINIMUM_AGENT_VERSION=0.8.0
LAB_TARGET_AGENT_VERSION=0.9.0-beta
```

The four values must be ordered from minimum supported through maximum supported. When
`minimum_supported_version` is omitted, the production profile uses the product's production
minimum; development may permit a broader local test window. Pin all four values in a controlled
production configuration so an application default change cannot silently alter fleet policy.

The reported states mean:

| State | Work allowed | Meaning |
| --- | --- | --- |
| **Up to date** | yes | at the target, or newer but still within the supported maximum |
| **Upgrade available** | yes | supported and recommended, but below the target |
| **Upgrade recommended** | yes | supported but below the recommended minimum |
| **Upgrade required** | no | below the hard minimum; upgrade before accepting work |
| **Unsupported** | no | invalid/newer-than-maximum application version or incompatible protocol |

Enrollment and gateway connection validate application and protocol compatibility. A rejected
Agent receives an actionable minimum/protocol reason; it must not be admitted and merely fail later
while holding a reservation.

Release channel is reported with each build but does not by itself make two versions compatible.
Do not use a nightly Agent in a stable fleet unless its exact application/protocol versions are
inside a deliberately reviewed policy.

## Protocol negotiation

Protocol versions use `<major>.<minor>`. Peers with the same major negotiate the lower minor, so a
`1.1` peer and a `1.0` peer use `1.0`. Different majors are incompatible. Unknown messages or
fields still follow the protocol's validation rules; minor negotiation is not permission to send
features the negotiated version does not define.

## Database and backup compatibility

Production startup checks migration history and refuses an empty, old, inconsistent, or future
schema. Operators use `db status`, `db check`, and `db migrate`; startup is not the migration plan.
The current source targets schema 12, accepts schema 11 as its minimum migration source, and
declares rollback as restore-backup required.

A backup reader accepts only its supported format and database backend, rejects a future
application/schema, and limits application restores to the current or previous minor line within
the same major. Those checks do not prove that a particular release supports application rollback;
read its release notes.

## Plugin compatibility

The community plugin API is `1.0`. Plugins should declare the API they were tested against and fail
with a clear diagnostic when it is unsupported. A plugin package version is independent of the
Lab Platform application version. No commercial package is required to use the community plugin
SDK.

## Upgrade coordination boundary

The control plane assesses and reports Agent versions; it does not run arbitrary remote package
installers. Operators deploy Agent updates through their existing image/package management, keep
Agent-local state and credentials, and canary reconnect/workflow behavior before a fleet rollout.
See [upgrading](UPGRADING.md).
