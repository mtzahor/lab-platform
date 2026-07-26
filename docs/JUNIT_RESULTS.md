# JSON and JUnit results

Phase 4 converts workflow assertions and test-producing steps into provider-neutral test records.
Each record contains a name, `passed`, `failed`, `error`, or `skipped` status, duration in
milliseconds, an optional message, and structured details.

## CLI export

Inspect JSON:

```console
labctl workflow results WORKFLOW_RUN_ID --format json
```

Write JUnit XML atomically to a file:

```console
labctl workflow results WORKFLOW_RUN_ID \
  --format junit \
  --output hardware-results.xml
```

The one-command CI path can produce and publish the same file:

```console
labctl ci run \
  --workflow esp32-ci-test \
  --artifact firmware=build/firmware.bin \
  --input expected_version=0.5.0 \
  --allow-simulated \
  --no-allow-physical \
  --junit-output hardware-results.xml \
  --artifacts-directory hardware-artifacts
```

Always publish the JUnit path on test failure; a failed assertion is precisely when the report is
most useful.

## REST API

Both endpoints require a bearer token with `operations:read`:

| Method | Route | Response |
| --- | --- | --- |
| GET | `/api/v1/workflow-runs/{workflow_run_id}/results` | JSON workflow summary and `results` array |
| GET | `/api/v1/workflow-runs/{workflow_run_id}/results/junit` | `application/xml` JUnit document |

Example JSON:

```json
{
  "workflow_run_id": "86776fbe-16d6-4ccc-a876-f4ca8044dfa7",
  "workflow_name": "esp32-ci-test",
  "status": "succeeded",
  "results": [
    {
      "name": "Verify self-test",
      "status": "passed",
      "duration_ms": 4271,
      "message": null,
      "details": {
        "step_index": 3,
        "action": "assert_serial",
        "error_code": null,
        "output": {
          "pattern": "^SELF_TEST=PASS$",
          "matched": true
        },
        "artifact_ids": []
      }
    }
  ]
}
```

Direct export:

```console
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${LAB_PLATFORM_TOKEN}" \
  --output hardware-results.xml \
  "${LAB_PLATFORM_SERVER}/api/v1/workflow-runs/${WORKFLOW_RUN_ID}/results/junit"
```

## Mapping semantics

- Successful assertion/test steps become passing test cases.
- Assertion failures become failed test cases with the stable error message.
- Execution/backend errors become error test cases.
- Cancelled or intentionally unexecuted steps become skipped cases.
- Step names become test-case names; unnamed legacy steps use a deterministic action/index name.
- Durations are derived from persisted UTC start/completion timestamps and never negative.

The JUnit suite includes aggregate test/failure/error/skipped counts and duration. XML escaping is
applied to names, messages, and captured output. Full serial output remains a downloadable
artifact rather than being duplicated without bound into XML.

## CI provider publication

- GitHub Actions: upload `hardware-results.xml`; GitHub itself does not natively render JUnit
  without an additional reporter, but the file remains portable.
- GitLab CI: use `artifacts:reports:junit: hardware-results.xml`.
- Jenkins: use `junit testResults: 'hardware-results.xml'` inside `post { always { ... } }`.

JUnit represents hardware tests, while the CI session outcome also covers infrastructure and
cleanup. A suite may pass while the overall command exits 18 because reservation cleanup failed.
Always inspect the session outcome as well as the report.
