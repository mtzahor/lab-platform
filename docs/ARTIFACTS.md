# CI artifacts

Artifacts carry firmware into a CI session and retain logs, reports, and metadata after it
finishes. An artifact record has a generated UUID, logical owner, display name, type, content type,
generated storage path, byte size, SHA-256 checksum, timestamps, optional expiry, and string
metadata.

In Phase 5 client content is stored by the control plane, then downloaded by the selected Agent
through a short-lived Agent/artifact-scoped capability. Agent outputs travel in the opposite
direction: metadata is announced first, the control plane assigns a global artifact ID and scoped
upload capability, and completion is idempotent. Content stays off the protocol WebSocket. The
Agent verifies SHA-256 and uses a bounded LRU cache that pins active inputs. The remaining sections
describe the compatible client artifact surface; see [Phase 5](PHASE_5.md#workflows-ci-and-artifacts)
for transfer/reconnect behavior.

The durable workflow or flash command contains only an artifact reference, Agent ID, checksum,
size, and safe target path. The control plane creates the transfer record and injects its URL,
expiry, and plaintext bearer capability only while constructing each outbound command envelope.
Those fields are not written to `remote_commands`; an `UNKNOWN` replay, including one after a
control-plane restart, receives a newly issued capability while retaining the same durable
idempotency identity.

Supported owner types are `ci_session`, `workflow_run`, `workflow_step`, and `operation`. CI input
uploads are owned by the session that will run them. The standalone Phase 4 API restricts them to
the same legacy token owner. The Phase 6 control plane instead resolves and authorises the durable
parent described below.

## Phase 6 parent-inherited access

Identity-facing control-plane upload/list/get/content/delete, CI artifact-list, and download-transfer
issuance calls pass through a protected artifact application service:

- operation and remote-command artifacts inherit the exact trusted bench, including its parent
  Agent relationship;
- workflow-run artifacts require a tenant-scoped run/definition and its actual bench;
- a linked CI-session artifact requires session access plus the corresponding `artifacts:*`
  permission on both the exact workflow and selected bench;
- an unlinked CI-session artifact requires session access plus the artifact permission from at least
  one scoped assignment; and
- a `workflow_step` UUID has no tenant-scoped trusted resolver yet, so Phase 6 named access and
  mutations fail closed and collection reads filter it.

List routes return `200` with inaccessible records omitted. Named/mutation denials are audited and
become the ordinary artifact `404` when `hide_unauthorised_resources` is enabled; otherwise they
remain structured `403` responses. Upload authorisation completes before the request stream is
consumed, and transfer issuance validates the target Agent in the same tenant before creating a
capability.

`DELETE /api/v1/artifacts/{artifact_id}` removes a platform-managed artifact after inherited
`artifacts:delete` authorisation and appends `ARTIFACT_DELETED`. Remote-artifact deletion is not
supported. Transfer-capability PUT/GET routes remain a separate short-lived Agent/artifact bearer
boundary. Legacy scope compatibility retains its historical access, including unresolved
workflow-step records, and is outside Phase 6 tenant guarantees.

## Upload through the one-command flow

`--artifact NAME=PATH` uploads a local file and makes its record available to the workflow input of
the same name:

```console
labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.6.0 \
  --label board=esp32 \
  --allow-simulated \
  --no-allow-physical
```

The workflow declares the corresponding input as `type: artifact` and uses it only as an exact
placeholder:

```yaml
inputs:
  firmware:
    type: artifact
    required: true

steps:
  - name: Flash firmware
    action: flash
    firmware: "${{ inputs.firmware }}"
```

Artifact placeholders cannot be concatenated into another path or expression.

## Lower-level CLI

For a session created separately:

```console
labctl ci upload SESSION_ID \
  build/firmware.bin \
  --name firmware \
  --artifact-type firmware

labctl ci artifacts SESSION_ID --output json
labctl ci download ARTIFACT_ID --output hardware-artifacts/serial.log
```

The CLI computes SHA-256 before upload and the Agent verifies content while streaming it. Use an
idempotency key when a custom integration may retry an upload; the same logical retry returns the
existing artifact.

## Control-plane upload API

```http
POST /api/v1/artifacts
Authorization: Bearer <token with artifacts:write>
Content-Type: multipart/form-data
```

Multipart fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `file` | yes | Streamed content |
| `owner_type` | yes | `ci_session`, `workflow_run`, `workflow_step`, or `operation` |
| `owner_id` | yes | Owning resource UUID |
| `artifact_type` | yes | Logical category such as `firmware` |
| `expected_sha256` | no | Expected lowercase hexadecimal digest |
| `idempotency_key` | no | Tenant/owner-scoped logical retry key |

Example response:

```json
{
  "id": "f07d3d83-7961-4449-a54c-7091e9404a87",
  "owner_type": "ci_session",
  "owner_id": "2bcc7686-2254-41cf-a171-a67e63468726",
  "name": "firmware.bin",
  "artifact_type": "firmware",
  "content_type": "application/octet-stream",
  "size_bytes": 421888,
  "sha256": "8c762c...",
  "created_at": "2026-07-23T09:00:00Z",
  "expires_at": null,
  "metadata": {}
}
```

## List, inspect, and download

| Method | Route | Legacy scope / Phase 6 decision |
| --- | --- | --- |
| GET | `/api/v1/ci/sessions/{session_id}/artifacts` | `artifacts:read` / session plus inherited parent read |
| GET | `/api/v1/artifacts` | `artifacts:read` / per-item inherited parent read |
| GET | `/api/v1/artifacts/{artifact_id}` | `artifacts:read` / inherited parent read |
| GET | `/api/v1/artifacts/{artifact_id}/content` | `artifacts:read` / inherited parent read |
| DELETE | `/api/v1/artifacts/{artifact_id}` | `artifacts:write` / inherited parent delete; platform records only |

Retrieve content without logging the bearer token:

```console
curl --fail --location \
  -H "Authorization: Bearer ${LAB_PLATFORM_TOKEN}" \
  --output hardware-artifacts/serial.log \
  "${LAB_PLATFORM_SERVER}/api/v1/artifacts/${ARTIFACT_ID}/content"
```

Prefer `labctl ci download` in shared runner logs because it builds the header internally.

## Typical outputs

- complete serial captures
- flashing logs
- device probe output
- workflow summaries and operation traces
- JSON test results and JUnit XML
- firmware checksums and metadata

Serial output is stored as per-step workflow artifacts and consolidated as the session-owned
`serial.log`; structured flashing progress and boot diagnostics are consolidated separately as
`flash.log`. The metadata database keeps only bounded recent records, not every line. Capture
enforces both a per-message byte limit and the configured total artifact limit while data arrives,
applies
`serial.redact_patterns` before persistence, and fails the step while preserving the complete
accepted prefix if a backend or limit error interrupts collection. Invalid byte sequences follow
`serial.decode_errors`.

## Safety and storage

The Agent does not trust an uploaded filename as a path. It normalizes the display name, rejects
path traversal, generates the storage path, streams into a temporary file, enforces the configured
size limit, verifies the checksum, and atomically places content outside source/static directories.
A filename never selects an executable or shell command, and workflows cannot execute uploaded
content as arbitrary code.

Default limits are 100 MB per CI upload and 50 MB per serial artifact. Adjust them only with a
corresponding request/body limit at the TLS proxy. Optional expiry can support a retention policy;
the Agent maintenance worker removes due managed content and metadata and emits one
`ARTIFACT_EXPIRED` audit event per record.

Treat artifacts as potentially sensitive. Configure `serial.redact_patterns`, restrict
`artifacts:read`, use a private storage directory, and avoid publishing hardware logs to a public
CI artifact store without review.
