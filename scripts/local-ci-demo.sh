#!/usr/bin/env bash
set -Eeuo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_directory/.." && pwd)"
demo_port="${LAB_DEMO_PORT:-18080}"
use_existing_agent="${LAB_DEMO_USE_EXISTING_AGENT:-0}"
output_directory="${LAB_DEMO_OUTPUT_DIR:-$project_root/build/phase4-ci-demo}"
temporary_directory="$(mktemp -d "${TMPDIR:-/tmp}/lab-platform-phase4.XXXXXX")"
agent_pid=""
ci_pid=""
tee_pid=""
ci_signal_status=""

cleanup() {
  local status="$?"
  if [[ -n "$ci_pid" ]] && kill -0 "$ci_pid" 2>/dev/null; then
    kill -TERM "$ci_pid" 2>/dev/null || true
    wait "$ci_pid" 2>/dev/null || true
  fi
  if [[ -n "$tee_pid" ]] && kill -0 "$tee_pid" 2>/dev/null; then
    kill -TERM "$tee_pid" 2>/dev/null || true
    wait "$tee_pid" 2>/dev/null || true
  fi
  if [[ -n "$agent_pid" ]] && kill -0 "$agent_pid" 2>/dev/null; then
    kill -TERM "$agent_pid" 2>/dev/null || true
    wait "$agent_pid" 2>/dev/null || true
  fi
  if [[ -n "$temporary_directory" && -d "$temporary_directory" ]]; then
    rm -rf -- "$temporary_directory"
  fi
  trap - EXIT
  exit "$status"
}

forward_ci_signal() {
  local signal="$1"
  local signal_status="$2"

  if [[ -n "$ci_pid" ]] && kill -0 "$ci_pid" 2>/dev/null; then
    if [[ -z "$ci_signal_status" ]]; then
      ci_signal_status="$signal_status"
    fi
    kill "-$signal" "$ci_pid" 2>/dev/null || true
    return
  fi

  exit "$signal_status"
}

trap cleanup EXIT
trap 'forward_ci_signal INT 130' INT
trap 'forward_ci_signal TERM 143' TERM

if [[ -n "${LABCTL_BIN:-}" ]]; then
  labctl_command=("$LABCTL_BIN")
elif [[ -x "$project_root/.venv/bin/labctl" ]]; then
  labctl_command=("$project_root/.venv/bin/labctl")
elif command -v labctl >/dev/null 2>&1; then
  labctl_command=(labctl)
else
  printf 'error: labctl is not installed; run uv sync --all-extras first\n' >&2
  exit 20
fi

if [[ -x "$project_root/.venv/bin/python" ]]; then
  python_command=("$project_root/.venv/bin/python")
else
  python_command=(python3)
fi

mkdir -p "$output_directory" "$temporary_directory/config" "$temporary_directory/workflows"
cp "$project_root/examples/firmware/demo.bin" "$output_directory/firmware.bin"

if [[ "$use_existing_agent" == "1" ]]; then
  export LAB_PLATFORM_SERVER="${LAB_PLATFORM_SERVER:-http://127.0.0.1:$demo_port}"
else
  if [[ -n "${LAB_AGENT_BIN:-}" ]]; then
    lab_agent_command=("$LAB_AGENT_BIN")
  elif [[ -x "$project_root/.venv/bin/lab-agent" ]]; then
    lab_agent_command=("$project_root/.venv/bin/lab-agent")
  elif command -v lab-agent >/dev/null 2>&1; then
    lab_agent_command=(lab-agent)
  else
    printf 'error: lab-agent is not installed; run uv sync --all-extras first\n' >&2
    exit 20
  fi

  cp "$project_root/config/agent.yaml" "$temporary_directory/config/agent.yaml"
  cp "$project_root/config/simlab.yaml" "$temporary_directory/config/simlab.yaml"
  cp "$project_root/examples/workflows/esp32-ci-test.yaml" \
    "$temporary_directory/workflows/esp32-ci-test.yaml"
  export LAB_PLATFORM_SERVER="http://127.0.0.1:$demo_port"

  "${lab_agent_command[@]}" \
    --config-dir "$temporary_directory/config" \
    --host 127.0.0.1 \
    --port "$demo_port" \
    >"$temporary_directory/agent.log" 2>&1 &
  agent_pid="$!"

  for _attempt in {1..100}; do
    if "${labctl_command[@]}" health --output json >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "$agent_pid" 2>/dev/null; then
      printf 'error: temporary Agent stopped during startup\n' >&2
      sed -n '1,200p' "$temporary_directory/agent.log" >&2
      exit 20
    fi
    sleep 0.1
  done
  if ! "${labctl_command[@]}" health --output json >/dev/null 2>&1; then
    printf 'error: temporary Agent did not become healthy\n' >&2
    sed -n '1,200p' "$temporary_directory/agent.log" >&2
    exit 20
  fi
fi

if [[ "$use_existing_agent" != "1" ]]; then
  # A credential for another Agent must not block bootstrap of the temporary one.
  unset LAB_PLATFORM_TOKEN
fi

if [[ "$use_existing_agent" != "1" || -z "${LAB_PLATFORM_TOKEN:-}" ]]; then
  token_json="$(
    "${labctl_command[@]}" token create \
      --name local-ci-demo \
      --owner local-ci-demo \
      --scope ci:sessions \
      --scope benches:read \
      --scope reservations:write \
      --scope workflows:run \
      --scope operations:read \
      --scope artifacts:read \
      --scope artifacts:write \
      --output json
  )"
  LAB_PLATFORM_TOKEN="$(
    printf '%s' "$token_json" | \
      "${python_command[@]}" -c 'import json, sys; print(json.load(sys.stdin)["token"])'
  )"
  export LAB_PLATFORM_TOKEN
  printf 'Created a scoped API token for the demo Agent.\n'
else
  printf 'Using LAB_PLATFORM_TOKEN from the environment.\n'
fi

run_log="$output_directory/ci-run.log"
run_pipe="$temporary_directory/ci-run.pipe"
mkfifo "$run_pipe"

set +e
tee "$run_log" <"$run_pipe" &
tee_pid="$!"
"${python_command[@]}" -c '
import os
import signal
import sys

signal.signal(signal.SIGINT, signal.SIG_DFL)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])
' "${labctl_command[@]}" ci run \
  --workflow esp32-ci-test \
  --artifact "firmware=$output_directory/firmware.bin" \
  --input expected_version=0.5.0-demo \
  --require capability=firmware \
  --require capability=serial \
  --require capability=reset \
  --require capability=probe \
  --label board=esp32 \
  --allow-simulated \
  --no-allow-physical \
  --wait-timeout 2m \
  --reservation-duration 10m \
  --junit-output "$output_directory/hardware-results.xml" \
  --artifacts-directory "$output_directory/hardware-artifacts" \
  >"$run_pipe" 2>&1 &
ci_pid="$!"

while true; do
  wait "$ci_pid"
  ci_wait_status="$?"
  if kill -0 "$ci_pid" 2>/dev/null; then
    continue
  fi
  break
done
ci_pid=""

wait "$tee_pid"
tee_pid=""
set -e

if [[ -n "$ci_signal_status" ]]; then
  ci_status="$ci_signal_status"
else
  ci_status="$ci_wait_status"
fi

session_id="$(sed -n 's/^CI session created: //p' "$run_log" | head -n 1)"
bench_id="$(sed -n 's/^Assigned: //p' "$run_log" | head -n 1)"

if [[ -z "$ci_signal_status" && -n "$session_id" ]]; then
  "${labctl_command[@]}" ci session show "$session_id" --output json \
    >"$output_directory/session.json"
fi

if [[ "$ci_status" -eq 0 && -n "$bench_id" ]]; then
  bench_json="$("${labctl_command[@]}" bench show "$bench_id" --output json)"
  printf '%s' "$bench_json" | "${python_command[@]}" -c '
import json
import sys

bench = json.load(sys.stdin)
if bench.get("reserved_by") is not None:
    raise SystemExit("error: CI reservation was not released")
print("Verified release of {}.".format(bench.get("id", "selected bench")))
'
fi

printf 'Demo outputs: %s\n' "$output_directory"
exit "$ci_status"
