# Web dashboard

The dashboard is the browser client for the Lab Platform control plane. It is responsive,
keyboard navigable, permission aware, and combines day-to-day control with bounded operational
analytics. The current organisation and principal appear in the persistent shell; the live-state
indicator never substitutes for the status reported by the API.

## Routes

| Route | Purpose | Typical permission |
| --- | --- | --- |
| `/` | Overview counts, active work, degraded resources | authenticated |
| `/benches` | Search and filter visible bench inventory | `benches:read` |
| `/benches/:id` | Availability, reservation, activity, timeline, actions | resource-specific |
| `/reservations` | Active/scheduled reservations, cancellation, and queues | `benches:reserve` |
| `/workflows` | Definitions, recent runs, launch form | `workflows:read` / `workflows:run` |
| `/workflow-runs/:id` | Step progress, output, assertions, artifacts | `operations:read` |
| `/operations` and `/operations/:id` | Distributed operations and bounded serial text | `operations:read` |
| `/analytics` | Utilisation, availability, queue pressure, reliability, alerts, and fleet compatibility | `benches:read` + `operations:read` |
| `/ci-sessions` and `/ci-sessions/:id` | CI lifecycle, selection, results, artifacts | `ci:sessions:read` |
| `/agents` and `/agents/:id` | Presence, inventory, work, drain controls | `agents:read` |
| `/artifacts` | Search and download authorized artifacts | `artifacts:read` |
| `/audit` | Searchable activity and denied actions | `audit:read` |
| `/admin/*` | Users, teams, service accounts, roles and organisation | matching admin permission |

Navigation is a convenience filter. The server repeats the full authorisation decision for every
request, including requests made outside the dashboard.

## Screen diagrams

These diagrams identify the information hierarchy; exact wrapping changes at narrower widths.

### Overview

```text
┌ Organisation / principal ─────────────────────── Live | Polling | Stale ┐
│ Benches  Available  Reserved  Offline  Agents  Active operations       │
├ Active operations ────────────────┬ Degraded benches / Agents ─────────┤
│ status · progress · bench · actor │ health · last seen · uncertainty    │
├ Active reservations ──────────────┼ Failed workflows / CI sessions ────┤
│ owner · expiry · lease state      │ failure code · time · detail link   │
└───────────────────────────────────┴─────────────────────────────────────┘
```

### Bench inventory

```text
┌ Benches ─ Search ─ Agent ─ Status ─ Health ─ Kind ─ Labels ─ Available ┐
│ Name / global ID │ Agent │ status + text │ owner/expiry │ activity     │
│ ...virtualized or paged visible rows...                                │
└ Row activation opens bench detail; filters remain keyboard reachable. ┘
```

### Bench detail

```text
┌ Bench name · Agent · ONLINE/STALE ─── Reserve | Queue | Run | Actions ┐
├ Health / capabilities / firmware ───┬ Current reservation              ┤
├ Active operation / CI session ──────┼ Effective actions                ┤
├ Timeline: reservation, operation, health and Agent events             ┤
└ Related artifacts                                                       ┘
```

### Workflow run

```text
┌ Workflow · bench · server status ─────────────────── Cancel | JUnit ┐
│ Progress and current message                                           │
├ Ordered steps + duration + errors ───┬ Actor / reservation / Agent     ┤
├ Assertions ──────────────────────────┼ Artifacts                       ┤
└ Bounded untrusted-text run output ───┴ Connection confidence           ┘
```

### Agent detail

```text
┌ Agent · version · protocol · last seen ─── Refresh | Drain/Undrain ┐
├ Presence / location / labels ───────────┬ Workload summary             ┤
├ Visible bench inventory ────────────────┼ Active operations / leases   ┤
└ Timeline: connect, disconnect, drain, reconciliation                  ┘
```

### Operational analytics

```text
┌ Window ─ Utilisation ─ Availability ─ Queue depth/p95 ─ Reliability ┐
├ Open alerts: severity · condition · resource ─ Acknowledge | Resolve ┤
├ Bench │ utilisation │ availability │ success │ flaky/maintenance     │
└ Agent fleet: version · protocol · compatibility · upgrade state      ┘
```

Utilisation is weighted by available seconds, not averaged across bench percentages. Agent-offline
and maintenance time is excluded from available capacity. Empty samples display as unavailable,
not as zero. The Agent fleet section appears only with `agents:read`; alert actions appear only
with `benches:manage`. The API repeats those permission checks.

### User administration

```text
┌ Users ─ Search ───────────────────────────────────── Create user ┐
│ identity · source · status · organisation role · last login      │
├ Selected user: profile, direct role assignments, safe limitations │
└ Disable/enable and password reset use labelled confirmation flows ┘
```

### Audit log

```text
┌ Audit ─ actor ─ action ─ resource ─ outcome ─ time ─ request ID ┐
│ timestamp │ principal │ action │ target │ SUCCEEDED/DENIED/FAILED │
└ Detail: source, request correlation and secret-sanitized metadata ┘
```

## Interaction conventions

- Search fields and filters affect only data the server has already authorized.
- Mutations show a pending state, disable duplicate submission, and invalidate affected queries on
  success. Idempotency keys protect creation and distributed command requests.
- Release, cancel, drain, revoke, disable, reset, and delete actions use explicit confirmation.
- A `404` can mean absent or deliberately hidden; the UI does not disclose which.
- `UNKNOWN` and `RECONCILING` retain the last known details and offer reconciliation only when the
  permission map allows it.
- Serial and workflow logs are bounded and rendered inside `<pre>/<code>` text nodes.
- The reservation dialog supports immediate or future start time. Scheduled rows can be cancelled
  before activation; the server remains authoritative for overlap and queue-protection rules.

For the full operating sequence, use the [browser demonstration](PHASE_7_BROWSER_DEMO.md).
