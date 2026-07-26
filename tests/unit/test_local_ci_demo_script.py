from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("sent_signal", "expected_status", "expected_name"),
    [
        (signal.SIGINT, 130, "INT"),
        (signal.SIGTERM, 143, "TERM"),
    ],
)
def test_local_ci_demo_forwards_signal_without_logging_token(
    tmp_path: Path,
    sent_signal: signal.Signals,
    expected_status: int,
    expected_name: str,
) -> None:
    fake_labctl = tmp_path / "fake-labctl"
    fake_agent = tmp_path / "fake-agent"
    agent_ready_file = tmp_path / "agent-ready"
    agent_signal_file = tmp_path / "agent-signal"
    ready_file = tmp_path / "ci-ready"
    signal_file = tmp_path / "ci-signal"
    output_directory = tmp_path / "output"
    token = "demo-token-that-must-stay-secret"
    stale_token = "credential-for-another-agent"
    fake_labctl.write_text(
        f"""#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == "health" ]]; then
  [[ -f "$LAB_DEMO_TEST_AGENT_READY_FILE" ]]
  exit
fi
if [[ "${{1:-}}" == "token" && "${{2:-}}" == "create" ]]; then
  if [[ -n "${{LAB_PLATFORM_TOKEN:-}}" ]]; then
    printf 'stale token was sent during bootstrap\n' >&2
    exit 98
  fi
  printf '{{"token":"{token}"}}\n'
  exit 0
fi
if [[ "${{1:-}}" == "ci" && "${{2:-}}" == "session" ]]; then
  printf 'session show must not run after a signal\n' >&2
  exit 99
fi
if [[ "${{1:-}}" == "ci" && "${{2:-}}" == "run" ]]; then
  for argument in "$@"; do
    if [[ "$argument" == "$LAB_PLATFORM_TOKEN" ]]; then
      printf 'token leaked into argv\n' >&2
      exit 99
    fi
  done
  trap 'printf "INT\\n" >"$LAB_DEMO_TEST_SIGNAL_FILE"; exit 16' INT
  trap 'printf "TERM\\n" >"$LAB_DEMO_TEST_SIGNAL_FILE"; exit 16' TERM
  printf 'ready\n' >"$LAB_DEMO_TEST_READY_FILE"
  printf 'CI session created: session-test\n'
  printf 'fake CI run started\n'
  while true; do sleep 0.05; done
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_labctl.chmod(0o755)
    fake_agent.write_text(
        """#!/usr/bin/env bash
set -eu
trap 'printf "TERM\\n" >"$LAB_DEMO_TEST_AGENT_SIGNAL_FILE"; exit 0' TERM
printf 'ready\n' >"$LAB_DEMO_TEST_AGENT_READY_FILE"
while true; do sleep 0.05; done
""",
        encoding="utf-8",
    )
    fake_agent.chmod(0o755)
    environment = {
        **os.environ,
        "LAB_AGENT_BIN": str(fake_agent),
        "LABCTL_BIN": str(fake_labctl),
        "LAB_DEMO_OUTPUT_DIR": str(output_directory),
        "LAB_DEMO_TEST_AGENT_READY_FILE": str(agent_ready_file),
        "LAB_DEMO_TEST_AGENT_SIGNAL_FILE": str(agent_signal_file),
        "LAB_DEMO_TEST_READY_FILE": str(ready_file),
        "LAB_DEMO_TEST_SIGNAL_FILE": str(signal_file),
        "LAB_DEMO_USE_EXISTING_AGENT": "0",
        "LAB_PLATFORM_TOKEN": stale_token,
    }

    process = subprocess.Popen(
        [str(ROOT / "scripts/local-ci-demo.sh")],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_file.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError("fake labctl did not start")
            time.sleep(0.01)
        assert process.poll() is None

        process.send_signal(sent_signal)
        output, _ = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == expected_status
    assert signal_file.read_text(encoding="utf-8") == f"{expected_name}\n"
    assert agent_signal_file.read_text(encoding="utf-8") == "TERM\n"
    assert token not in output
    assert stale_token not in output
    assert token not in (output_directory / "ci-run.log").read_text(encoding="utf-8")
    assert stale_token not in (output_directory / "ci-run.log").read_text(encoding="utf-8")
