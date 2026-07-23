# Lab Platform

Lab Platform is a local-first system for safely reserving and controlling remote hardware
benches. Phase 3 lets several engineers share multiple simulated and physical benches through one
Agent, with timed reservations, persistent FIFO queues, deterministic scheduling, restart recovery,
history, and sequential workflows.

The current release is **0.4.0-alpha**.

## Quick start

Requirements: Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```console
git clone <repository-url> lab-platform
cd lab-platform
uv sync --all-extras
source .venv/bin/activate
lab-agent --config-dir config
```

In a second terminal, run the complete workflow:

```console
labctl health
labctl bench list
labctl reservation create bench-01 --owner demo-user --duration 30m
labctl bench power-cycle bench-01 --owner demo-user
labctl operation watch <operation-id>
labctl bench flash bench-01 examples/firmware/demo.bin --owner demo-user --version 1.1.0
labctl operation watch <operation-id>
labctl bench show bench-01
labctl event list --bench-id bench-01
labctl reservation release <reservation-id> --owner demo-user
```

Each mutating bench action returns an operation ID. Substitute that ID in the following `watch`
command. Add `--output json` to read commands for machine-readable output.

The Agent listens on `http://127.0.0.1:8080` by default. Select another Agent with `--server`, the
`LAB_PLATFORM_SERVER` environment variable, or `~/.config/lab-platform/cli.yaml`.

## Local state

The default configuration stores platform-owned state under `.lab-platform/`:

- `lab.db` contains catalog, reservation, queue, lock, workflow, operation, and event history.
- `artifacts/<sha256>/` contains uploaded firmware.

SimLab remains the source of truth for current simulated power and firmware state. Delete
`.lab-platform/` only when you intentionally want to clear local history.

## ESP32 DevKit V1

Connect one ESP32 DevKit V1 over a USB data cable, review
[`examples/esp32-local.yaml`](examples/esp32-local.yaml), and preferably configure its USB serial
number. Build the reference firmware as described in
[`docs/ESP32_SETUP.md`](docs/ESP32_SETUP.md), then start the real backend:

```console
lab-agent --config examples/esp32-local.yaml
```

The complete physical workflow uses the same operation API as SimLab:

```console
labctl bench list
labctl bench reserve esp32-devkit-01 --owner michael
labctl bench probe esp32-devkit-01 --owner michael
labctl bench flash esp32-devkit-01 ./firmware.bin --owner michael --version 0.1.0
labctl operation watch <operation-id>
labctl bench serial read esp32-devkit-01 --owner michael --until '^READY$'
labctl bench reset esp32-devkit-01 --owner michael
labctl bench release esp32-devkit-01 --owner michael
```

The target advertises firmware, serial, probe, and reset. It does not advertise physical power
control. Serial output is retained under
`.lab-platform/artifacts/operations/<operation-id>/serial.log`.

## Development

```console
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
```

See [Phase 3](docs/PHASE_3.md), [team demo](docs/TEAM_DEMO.md),
[reservations](docs/RESERVATIONS.md), [workflows](docs/WORKFLOWS.md),
[Phase 2](docs/PHASE_2.md), [ESP32 setup](docs/ESP32_SETUP.md),
[real-backend design](docs/REAL_BACKEND.md), [hardware testing](docs/HARDWARE_TESTING.md),
[serial troubleshooting](docs/TROUBLESHOOTING_SERIAL.md), [API.md](API.md), [CLI.md](CLI.md), and
[ARCHITECTURE.md](ARCHITECTURE.md).
