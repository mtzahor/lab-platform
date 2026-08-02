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
uploads are owned by the session that will run them; access is restricted to the same API token
owner.

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

## Upload API

```http
POST /api/v1/artifacts
Authorization: Bearer <token with artifacts:write>
Idempotency-Key: github_actions:acme/device:123456789:firmware
Content-Type: multipart/form-data
```

Multipart fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `file` | yes | Streamed content |
| `ci_session_id` | yes | Owning CI session UUID |
| `name` | no | Logical display name; defaults to upload filename |
| `artifact_type` | no | Category; defaults to `firmware` |
| `sha256` | no | Expected lowercase hexadecimal digest |

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

| Method | Route | Required scope |
| --- | --- | --- |
| GET | `/api/v1/ci/sessions/{session_id}/artifacts` | `artifacts:read` |
| GET | `/api/v1/artifacts/{artifact_id}` | `artifacts:read` |
| GET | `/api/v1/artifacts/{artifact_id}/content` | `artifacts:read` |

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
