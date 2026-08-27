# Phase 9 / 1.0 production-readiness review

**Current decision: NOT READY for `1.0.0`.** The development version remains `0.9.0-beta` while
physical and long-running operational evidence is absent. This is an evidence register and review
runbook, not a release announcement.

The machine-readable authority is
[`release/phase9-readiness.yaml`](../release/phase9-readiness.yaml). Its 60 IDs map one-to-one to
the Phase 9 definition of done. `evidence_available` means an artifact can be reviewed; only
`passed` means a release reviewer accepted the criterion for the named release candidate.

The latest code audit also keeps criteria 12, 13, 32, and 37 at `partial`: composed-resource locks
are not wired into the durable reservation flow, operational duration/failure metric primitives
are not all fed by lifecycle events, and plugin failures are isolated but do not have controlled
runtime reinitialisation. Those are implementation gaps, not merely missing external evidence.

## Automated review

Run from a clean checkout with the release-candidate Python and Node versions:

```console
uv sync --all-extras --locked
.venv/bin/ruff check apps packages scripts tests
.venv/bin/mypy
.venv/bin/pytest
npm --prefix apps/web ci
npm --prefix apps/web test -- --run
npm --prefix apps/web run build
python scripts/export_openapi.py
git diff --exit-code -- docs/openapi.json docs/control-plane-openapi.json
python scripts/release/check_oss_boundary.py --root .
python scripts/release/check_phase9_readiness.py --allow-incomplete
```

Build the wheel/sdist and production images, verify release assets/SBOM/signatures, install into a
new environment, run the SimLab quickstart, and execute the published-image deployment acceptance
harness. Preserve command output and exact dependency/image digests.

## Public-interface freeze

Before the first RC, review owners sign off each surface against the inventory below:

| Surface | Authority | Freeze question |
| --- | --- | --- |
| REST API v1 | OpenAPI JSON and route tests | Are all removals, required-field additions, auth changes, and error changes intentional? |
| Agent protocol 1.x | protocol models/tests | Can older/newer supported Agents reconnect and replay without unsafe dispatch? |
| Plugin API 1.x | `lab_platform.plugin_sdk` and `PLUGIN_API.md` | Are metadata, lifecycle, capabilities, errors, timeouts, and compatibility final? |
| Workflow v1 | models/parser/examples | Do envelope, actions, legacy read path, and rejection errors match policy? |
| configuration v1 | strict models/examples | Are defaults safe and unsupported versions/keys rejected before startup? |
| CLI | command tree and `CLI.md` | Are core nouns, options, JSON output, and exit meanings stable? |
| database migrations | migration registry/acceptance | Does 0.9 upgrade preserve state and is downgrade restore-based? |
| artifact model | API/storage/backup | Are hashes, content types, paths, retention, and restore behavior stable? |
| error/capability vocabulary | models/docs | Are meanings unique, safe, and consistent across API, Agent, and plugins? |

The governing rules are in [the stability/deprecation policy](STABILITY_POLICY.md). Any accepted
exception names the migration and earliest removal release.

## Security review gate

The security review is incomplete until a reviewer records results for every item:

- authentication/session/token handling and secret redaction
- RBAC and organisation isolation, including denied cross-tenant enumeration
- Agent enrollment, credentials, reconnect, replay, and protocol size/rate limits
- artifact paths, upload names/content/size/hash, download authorization, and retention cleanup
- workflow input validation, capability authorization, fencing, and cancellation
- plugin entry-point/import trust, registration/config validation, and failure isolation
- external tool fixed argv, path/script validation, timeout/cancellation, output cap/redaction, and
  least-privilege service identity
- database queries/migrations/backups and restore authenticity
- web CSRF/CSP/cookies/SSE and dashboard permission boundaries
- audit completeness without secrets
- dependency, source, image, and SBOM vulnerability findings with dispositions

Automated security workflows and the Phase 8 checklist are supporting evidence, not automatic
completion of criterion 48. Critical/high findings must be fixed or explicitly release-blocking;
accepted lower-severity risk names owner, rationale, and review date.

## Production and recovery gate

The RC environment must pass:

- verified off-host backup and full restore at reference scale
- explicit migration from the final 0.9 release
- documented restore-based downgrade policy
- Agent reconnect/replay under restart and delayed/duplicate messages
- plugin failure isolation and recovery
- atomic composed-resource locks, fencing, expiry, and crash recovery
- stale reservation/operation recovery
- artifact-store and PostgreSQL interruption recovery
- audit/tenant/RBAC/retention isolation and cleanup
- Prometheus metrics, correlation IDs, alerts, health/readiness, and secret-safe logs
- reference load and minimum 24-hour soak using the frozen profile

Procedures and evidence fields are in [reliability validation](RELIABILITY_VALIDATION.md). Record
measured RPO/RTO and limitations; do not copy target values into observed fields.

## Hardware and final demo gate

Attach dated compatibility evidence for an ESP32, one second MCU family, and one independent
accessory. The reference small-lab demo uses:

```text
home-lab Agent
├── ESP32 DevKit V1
├── STM32 Nucleo-F446RE + ST-Link/OpenOCD
├── USB relay channel
├── optional J-Link
└── optional SocketCAN adapter
```

Reserve the composed ESP32 bench and run target/resource acquisition, power cycle, flash, serial
`READY`/`SELF_TEST`, optional CAN capture, artifact/JUnit storage, and complete release. While it is
reserved, a second acquisition of any exclusive bound resource must fail deterministically. Then
run the STM32 validation and a failure/replug recovery without disrupting the ESP32 plugin.

Physical results include exact board/accessory/tool/OS versions and raw artifacts as defined by
[hardware compatibility](HARDWARE_COMPATIBILITY.md). Simulator output cannot close physical gates.

## Documentation and community gate

Review product-oriented navigation, quickstart, self-hosting, Agent/hardware setup, workflows/CI,
dashboard, security, administration, plugins, compatibility, operations, backup/recovery,
upgrading, API, troubleshooting, and contributing. Verify copy/paste commands in a clean
environment and check every local link.

An official plugin documents maintainer, supported versions/OS/devices, test status, external
dependencies, limitations, failure modes, diagnostics, and troubleshooting. A `VERIFIED` matrix
status requires preserved evidence.

## Approval sequence

1. Create an RC from a clean signed commit; do not edit product/version claims to stable yet.
2. Run automated, install/upgrade, security, load/soak/recovery, physical, documentation, and final
   demo reviews against that exact RC.
3. Attach immutable evidence and have reviewers change only satisfied criteria to `passed`.
4. Run `python scripts/release/check_phase9_readiness.py`; it must pass without
   `--allow-incomplete`.
5. Re-run release metadata/assets/signature checks, then make the deliberate `1.0.0` version/tag
   change and publish through the stable channel.
6. Smoke the published artifacts and retain the release/evidence manifest.

If the RC changes materially, invalidate affected evidence and repeat it. A near-complete
checklist, passing unit suite, or calendar deadline does not authorize the tag.
