# GitLab CI

GitLab CI uses the same `labctl ci run` orchestration as every other provider. The CLI detects
`GITLAB_CI`, pipeline ID, project path, commit SHA/ref, and user metadata; no GitLab-specific logic
exists in scheduling or workflow services.

## Configure variables

Create masked/protected CI/CD variables:

- `LAB_PLATFORM_SERVER`: the reachable Agent URL.
- `LAB_PLATFORM_TOKEN`: a scoped API token from [API tokens](API_TOKENS.md).

Use a runner on the trusted lab network. Do not expose the Phase 4 Agent directly to the public
internet.

## Complete job

The repository template is
[`integrations/gitlab/hardware-test.yml`](../integrations/gitlab/hardware-test.yml). A complete
pipeline can include it or copy this job:

```yaml
stages:
  - build
  - test

build-firmware:
  stage: build
  image: python:3.11
  script:
    - ./scripts/build-firmware.sh
  artifacts:
    paths:
      - build/firmware.bin

hardware-test:
  stage: test
  image: python:3.11
  tags:
    - lab-network
  before_script:
    - python -m pip install lab-platform==0.5.0a0
  script:
    - >-
      labctl ci run
      --workflow esp32-ci-test
      --artifact firmware=build/firmware.bin
      --input expected_version=$CI_COMMIT_SHA
      --require capability=firmware
      --require capability=serial
      --require capability=reset
      --require capability=probe
      --label board=esp32
      --allow-simulated
      --allow-physical
      --wait-timeout 10m
      --reservation-duration 30m
      --junit-output hardware-results.xml
      --artifacts-directory hardware-artifacts
  artifacts:
    when: always
    paths:
      - hardware-results.xml
      - hardware-artifacts/
    reports:
      junit: hardware-results.xml
```

The token is read from the environment; never append it to `script` arguments. `artifacts: when:
always` preserves diagnostics after a failed hardware assertion or cleanup error.

## SimLab-only required job

Use required labels and only the simulated backend flag:

```yaml
  script:
    - >-
      labctl ci run
      --workflow esp32-ci-test
      --artifact firmware=build/firmware.bin
      --input expected_version=$CI_COMMIT_SHA
      --label board=esp32
      --label location=simulation
      --allow-simulated
      --no-allow-physical
      --wait-timeout 2m
      --junit-output hardware-results.xml
      --artifacts-directory hardware-artifacts
```

## Manual physical job

Keep physical execution gated and explicit:

```yaml
physical-esp32:
  stage: test
  tags: [lab-network]
  when: manual
  allow_failure: false
  script:
    - >-
      labctl ci run
      --bench esp32-devkit-01
      --workflow esp32-ci-test
      --artifact firmware=build/firmware.bin
      --input expected_version=$CI_COMMIT_SHA
      --no-allow-simulated
      --allow-physical
      --junit-output hardware-results.xml
      --artifacts-directory hardware-artifacts
  artifacts:
    when: always
    paths: [hardware-results.xml, hardware-artifacts/]
    reports:
      junit: hardware-results.xml
```

## Cancellation and cleanup

When GitLab terminates the script, `labctl` handles `SIGTERM`, requests cancellation/finalization,
and returns code 16. The Agent's missed-heartbeat reaper is the fallback if the runner disappears.
Do not replace this with a hand-written reservation sequence that lacks cleanup. See
[CI cleanup](CI_CLEANUP.md).
