#!/usr/bin/env bash
set -Eeuo pipefail

require_value() {
  local name="$1"
  local value="${!name:-}"
  if [[ -z "$value" ]]; then
    printf 'error: required action value %s is empty\n' "$name" >&2
    exit 20
  fi
}

require_value LAB_PLATFORM_SERVER
require_value LAB_PLATFORM_TOKEN
require_value LAB_ACTION_WORKFLOW
require_value LAB_ACTION_FIRMWARE
require_value LAB_ACTION_EXPECTED_VERSION

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_directory/../.." && pwd)"

if [[ -n "${LABCTL_BIN:-}" ]]; then
  labctl_command=("$LABCTL_BIN")
elif command -v uv >/dev/null 2>&1; then
  labctl_command=(uv run --project "$project_root" --frozen labctl)
elif command -v labctl >/dev/null 2>&1; then
  labctl_command=(labctl)
else
  printf 'error: labctl or uv is required to run the Lab Platform action\n' >&2
  exit 20
fi

arguments=(
  ci run
  --workflow "$LAB_ACTION_WORKFLOW"
  --artifact "firmware=$LAB_ACTION_FIRMWARE"
  --input "expected_version=$LAB_ACTION_EXPECTED_VERSION"
  --wait-timeout "${LAB_ACTION_WAIT_TIMEOUT:-10m}"
  --reservation-duration "${LAB_ACTION_RESERVATION_DURATION:-30m}"
  --junit-output "${LAB_ACTION_JUNIT_OUTPUT:-hardware-results.xml}"
  --artifacts-directory "${LAB_ACTION_ARTIFACTS_DIRECTORY:-hardware-artifacts}"
)

required_capabilities=()
IFS=',' read -r -a required_capabilities <<< "${LAB_ACTION_REQUIRED_CAPABILITIES:-}"
for capability in "${required_capabilities[@]}"; do
  if [[ -n "$capability" ]]; then
    arguments+=(--require "capability=$capability")
  fi
done

required_labels=()
IFS=',' read -r -a required_labels <<< "${LAB_ACTION_REQUIRED_LABELS:-}"
for label in "${required_labels[@]}"; do
  if [[ -n "$label" ]]; then
    arguments+=(--label "$label")
  fi
done

append_boolean_argument() {
  local name="$1"
  local positive_flag="$2"
  local value="${!name:-true}"
  case "$value" in
    1|true|TRUE|True|yes|YES|Yes|on|ON|On)
      arguments+=("$positive_flag")
      ;;
    0|false|FALSE|False|no|NO|No|off|OFF|Off)
      arguments+=("--no-${positive_flag#--}")
      ;;
    *)
      printf 'error: %s must be true or false, got %s\n' "$name" "$value" >&2
      exit 20
      ;;
  esac
}

append_boolean_argument LAB_ACTION_ALLOW_SIMULATED --allow-simulated
append_boolean_argument LAB_ACTION_ALLOW_PHYSICAL --allow-physical

child_pid=""
forward_signal() {
  local signal_name="$1"
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -s "$signal_name" "$child_pid" 2>/dev/null || true
    wait "$child_pid" || true
  fi
  if [[ "$signal_name" == INT ]]; then
    exit 130
  fi
  exit 143
}
trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM

"${labctl_command[@]}" "${arguments[@]}" &
child_pid="$!"
set +e
wait "$child_pid"
status="$?"
set -e
trap - INT TERM
exit "$status"
