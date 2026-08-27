from __future__ import annotations

import importlib
from collections.abc import Mapping

from lab_platform.cli.client import AgentClient

cli = importlib.import_module("lab_platform.cli.main")


class RecordingClient(AgentClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post(
        self,
        path: str,
        payload: dict[str, object],
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> object:
        self.calls.append((path, payload))
        return {
            "bench_id": "esp32-validation-01",
            "status": "MAINTENANCE" if path.endswith("/start") else "HEALTHY",
        }


def test_labctl_bench_maintenance_start_and_end() -> None:
    client = RecordingClient()
    parser = cli._build_parser()

    start = parser.parse_args(
        [
            "bench",
            "maintenance",
            "start",
            "esp32-validation-01",
            "--reason",
            "Inspect USB cable",
        ]
    )
    end = parser.parse_args(["bench", "maintenance", "end", "esp32-validation-01"])

    assert cli._bench_command(client, start) == 0
    assert cli._bench_command(client, end) == 0
    assert client.calls == [
        (
            "/api/v1/operational/benches/esp32-validation-01/maintenance/start",
            {"reason": "Inspect USB cable"},
        ),
        (
            "/api/v1/operational/benches/esp32-validation-01/maintenance/end",
            {},
        ),
    ]
