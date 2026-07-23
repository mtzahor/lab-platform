from __future__ import annotations

import pytest
from lab_platform.cli.client import AgentClient
from lab_platform.cli.main import main, parse_duration


def _reservation(status: str = "active") -> dict[str, object]:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "bench_id": "bench-01",
        "owner": "alice",
        "status": status,
        "starts_at": "2026-07-20T10:00:00Z",
        "ends_at": "2026-07-20T10:30:00Z",
    }


def test_duration_parser_accepts_compound_values_and_rejects_invalid_input() -> None:
    assert parse_duration("30m") == 1800
    assert parse_duration("2H") == 7200
    assert parse_duration("1h30m15s") == 5415
    for invalid in ("", "0m", "30", "1h 30m", "tomorrow"):
        with pytest.raises(ValueError, match="duration"):
            parse_duration(invalid)


def test_reservation_queue_timeline_and_filter_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str, object]] = []

    def get(self: AgentClient, path: str, query: dict[str, object] | None = None) -> object:
        calls.append(("GET", path, query))
        if path.endswith("/timeline"):
            return {
                "items": [
                    {
                        "timestamp": "2026-07-20T10:00:00Z",
                        "category": "reservation",
                        "event_type": "RESERVATION_ACTIVATED",
                        "actor": "alice",
                        "summary": "alice reserved the bench",
                    }
                ]
            }
        if path.endswith("/queue"):
            return {
                "items": [
                    {
                        "position": 1,
                        "id": "queue-1",
                        "owner": "bob",
                        "requested_duration_seconds": 600,
                        "status": "waiting",
                    }
                ]
            }
        if path == "/api/v1/reservations":
            return {"items": [_reservation()]}
        if path.startswith("/api/v1/reservations/"):
            return _reservation()
        return {"items": []}

    def post(self: AgentClient, path: str, payload: dict[str, object]) -> object:
        calls.append(("POST", path, payload))
        if path.endswith("/queue"):
            return {
                "id": "queue-1",
                "bench_id": "bench-01",
                "owner": "alice",
                "status": "waiting",
                "position": 1,
            }
        status = (
            "released"
            if path.endswith("/release")
            else "cancelled"
            if path.endswith("/cancel")
            else "active"
        )
        return _reservation(status)

    def delete(self: AgentClient, path: str, payload: dict[str, object]) -> None:
        calls.append(("DELETE", path, payload))

    monkeypatch.setattr(AgentClient, "get", get)
    monkeypatch.setattr(AgentClient, "post", post)
    monkeypatch.setattr(AgentClient, "delete", delete)

    assert (
        main(
            [
                "bench",
                "list",
                "--online",
                "--available",
                "--capability",
                "firmware",
                "--label",
                "board=esp32",
            ]
        )
        == 0
    )
    assert calls[-1][2] == {
        "status": None,
        "capability": "firmware",
        "reserved": None,
        "online": True,
        "available": True,
        "label": ["board=esp32"],
    }
    assert main(["bench", "timeline", "bench-01"]) == 0
    assert "alice reserved the bench" in capsys.readouterr().out

    assert main(["reservation", "list", "--owner", "alice"]) == 0
    assert main(["reservation", "show", "00000000-0000-0000-0000-000000000001"]) == 0
    assert (
        main(
            [
                "reservation",
                "create",
                "bench-01",
                "--owner",
                "alice",
                "--duration",
                "1h30m",
                "--queue-if-busy",
                "--idempotency-key",
                "create-1",
            ]
        )
        == 0
    )
    assert calls[-1][2] == {
        "bench_id": "bench-01",
        "owner": "alice",
        "starts_at": None,
        "duration_seconds": 5400,
        "queue_if_busy": True,
        "idempotency_key": "create-1",
    }
    reservation_id = "00000000-0000-0000-0000-000000000001"
    assert (
        main(
            [
                "reservation",
                "extend",
                reservation_id,
                "--owner",
                "alice",
                "--duration",
                "15m",
                "--output",
                "json",
            ]
        )
        == 0
    )
    assert main(["reservation", "release", reservation_id, "--owner", "alice"]) == 0
    assert main(["reservation", "cancel", reservation_id, "--owner", "alice"]) == 0
    assert (
        main(
            [
                "reservation",
                "queue",
                "bench-01",
                "--owner",
                "alice",
                "--duration",
                "20m",
            ]
        )
        == 0
    )
    assert main(["reservation", "queue-list", "bench-01"]) == 0
    assert main(["reservation", "queue-cancel", "queue-1", "--owner", "alice"]) == 0
    output = capsys.readouterr().out
    assert "Reservation released" in output
    assert "Added to queue" in output
    assert "Cancelled queue entry" in output


def test_workflow_commands_and_inputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = {
        "name": "smoke-test",
        "version": 1,
        "description": "Smoke test",
        "requirements": {"capabilities": ["reset", "serial"]},
        "steps": [{"action": "reset"}, {"action": "probe"}],
    }
    run = {
        "id": "00000000-0000-0000-0000-000000000002",
        "workflow_name": "smoke-test",
        "status": "succeeded",
        "current_step": 1,
    }
    posted: list[tuple[str, dict[str, object]]] = []

    def get(self: AgentClient, path: str, query: dict[str, object] | None = None) -> object:
        if path == "/api/v1/workflows":
            return {"items": [workflow]}
        if path == "/api/v1/workflows/smoke-test":
            return workflow
        return run

    def post(self: AgentClient, path: str, payload: dict[str, object]) -> object:
        posted.append((path, payload))
        return run

    monkeypatch.setattr(AgentClient, "get", get)
    monkeypatch.setattr(AgentClient, "post", post)

    assert main(["workflow", "list"]) == 0
    assert main(["workflow", "show", "smoke-test"]) == 0
    assert (
        main(
            [
                "workflow",
                "run",
                "smoke-test",
                "--bench",
                "bench-01",
                "--owner",
                "alice",
                "--reserve",
                "30m",
                "--release-after",
                "--input",
                "firmware=demo.bin",
            ]
        )
        == 0
    )
    assert posted[-1][1]["reserve_duration_seconds"] == 1800
    assert posted[-1][1]["inputs"] == {"firmware": "demo.bin"}
    run_id = str(run["id"])
    assert main(["workflow", "watch", run_id, "--interval", "0"]) == 0
    assert main(["workflow", "cancel", run_id, "--owner", "alice"]) == 0
    assert "Workflow status: Succeeded" in capsys.readouterr().out

    assert (
        main(
            [
                "workflow",
                "run",
                "smoke-test",
                "--bench",
                "bench-01",
                "--owner",
                "alice",
                "--input",
                "invalid",
            ]
        )
        == 1
    )
    assert "key=value" in capsys.readouterr().err


def test_workflow_watch_failure_returns_operation_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        AgentClient,
        "get",
        lambda self, path, query=None: {"status": "failed", "current_step": None},
    )
    assert main(["workflow", "watch", "run-1", "--output", "json"]) == 7
