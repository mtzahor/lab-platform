# Audit log

Audit events answer who attempted an action, what resource was involved, when it happened, and
whether it succeeded, failed, or was denied.

## Event shape

Each event belongs to one organisation and records:

- timestamp and generated event ID;
- optional actor type, ID, and display name;
- action and resource type/ID;
- outcome: `SUCCEEDED`, `FAILED`, or `DENIED`;
- optional request ID, source IP, user agent, and safe reason;
- bounded structured metadata.

Anonymous failures may omit actor fields, but actor type and actor ID must either both be present or
both be absent.

## Implemented events

The live identity authentication service emits events for successful/failed local login, OIDC
success and provider/mapping failures once the organisation is known, logout, session revocation,
and API credential creation/revocation when audit is enabled. The central authorisation service
appends `PERMISSION_DENIED` when a Phase 6 permission check fails. The identity repository provides
organisation-scoped append, get, and filtered-list operations.

The identity administration service also records successful organisation updates; user
create/update/enable/disable/password reset; team create/update/delete/member changes;
service-account and credential changes; role assignment/revocation; bench/workflow access-policy
updates; and audit-list review. These events are attributable to the authenticated administrator.

Protected Phase 6 routes append success events for enrollment-token create/revoke and Agent
enrollment/revoke/drain/undrain/inventory refresh; bench probe/reset/serial/flash; reservation
create/renew/release/revoke; workflow register/start/cancel; operation cancel/reconciliation; CI
session create/start/heartbeat/cancel/finalize; and artifact upload/download/transfer/delete. Events
include the authenticated actor when a principal initiated the action; token-consumption enrollment
uses bounded system attribution. They store IDs rather than command payloads or artifact content.
Identity-backed audit reads also append `AUDIT_LOG_VIEWED`.

This covers the Phase 6 principal-facing action families. Raw Agent protocol work, automatic
background transitions, short-lived transfer-capability PUT/GET requests, and legacy-token
compatibility actions may use their Phase 5 event/journal/error boundary instead of a duplicate
principal audit event. A legacy token has no Phase 6 principal to attribute.

## Secret safety

Audit metadata is limited to 8 KiB. The sanitizer:

- drops keys containing password, token, secret, authorization, cookie, private-key, firmware,
  serial-log, or refresh-token markers;
- limits key length, string length, collection length, and nesting depth;
- converts only bounded JSON-safe values;
- marks the object as truncated when the encoded limit would be exceeded.

The model repeats key-name and size validation as a second boundary. Audit events must never hold
passwords, bearer tokens, API secrets, private keys, OIDC tokens, firmware contents, or complete
serial logs. A request ID or credential ID is safe; a credential secret is not.

## Append-only boundary and retention

Normal repository APIs insert events and expose no update/delete method. Direct database access can
still mutate rows; database-level immutability controls and external archival are deployment
responsibilities not implemented in this alpha.

The control-plane monitor enforces `audit.retention_days` (90 by default) by deleting up to 1,000
expired events per hourly maintenance pass. Candidate selection and deletion can be constrained to
one organisation in repository maintenance calls; the runtime maintenance pass covers the entire
control plane. The same maintenance path removes stale login-attempt rows after the configured
rate-limit window.

Retention is bounded, so a large pre-existing backlog may take multiple passes to drain. It deletes
rather than archives; external archival, legal holds, and database-level immutability remain
deployment responsibilities.

## Access and review

The `audit:read` permission is granted to organisation owner/admin, Lab Admin, and Auditor by the
current role mapping. Identity-only, organisation-scoped reads are implemented:

```http
GET /api/v1/audit-events?action=USER_CREATED&outcome=SUCCEEDED&limit=100
GET /api/v1/audit-events/{event_id}
```

List supports `action`, `outcome`, `after`, `before`, and a bounded `limit` up to 500. It appends an
`AUDIT_LOG_VIEWED` event containing safe filter/count metadata. The matching CLI is:

```console
labctl audit list \
  --action USER_CREATED \
  --outcome succeeded \
  --after 2026-08-01T00:00:00Z \
  --limit 100
labctl audit show EVENT_ID
```

CLI `--outcome` accepts `succeeded`, `failed`, or `denied`; it sends the corresponding uppercase
enum to the API. Both commands support table and JSON output.
