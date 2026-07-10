from __future__ import annotations

import json
import logging

import pytest
from lab_platform.logging import StructuredFormatter, get_logger
from lab_platform.models import Bench, Event
from lab_platform.simlab import SimLab
from pydantic import ValidationError


def test_models_are_strict_frozen_and_timestamped() -> None:
    bench = Bench(name=" bench-01 ")
    assert bench.name == "bench-01"
    with pytest.raises(ValidationError):
        Bench.model_validate({"name": "bench", "unknown": True})
    with pytest.raises(ValidationError):
        bench.name = "changed"
    assert Event(type="Ready").timestamp.tzinfo is not None


def test_structured_logging_has_required_fields_and_exception() -> None:
    formatter = StructuredFormatter()
    record = logging.LogRecord(
        "agent",
        logging.INFO,
        __file__,
        1,
        "ready %s",
        ("now",),
        None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["component"] == "agent"
    assert payload["level"] == "INFO"
    assert payload["message"] == "ready now"
    assert payload["timestamp"].endswith("+00:00")

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exception_record = logging.LogRecord(
            "agent",
            logging.ERROR,
            __file__,
            1,
            "failed",
            (),
            __import__("sys").exc_info(),
        )
    assert "RuntimeError: boom" in formatter.format(exception_record)


def test_get_logger_is_idempotent_and_updates_level() -> None:
    logger = get_logger("tests.structured", "INFO")
    initial_handlers = list(logger.handlers)
    same_logger = get_logger("tests.structured", "DEBUG")

    assert same_logger is logger
    assert logger.level == logging.DEBUG
    assert logger.propagate is False
    assert logger.handlers == initial_handlers
    assert initial_handlers[0].level == logging.DEBUG
    logger.handlers.clear()


def test_simlab_lifecycle_and_deterministic_patterns() -> None:
    async def scenario() -> None:
        simlab = SimLab(bench_count=6)
        assert simlab.benches() == []

        await simlab.start()
        benches = simlab.benches()
        assert [bench.name for bench in benches] == [
            "bench-01",
            "bench-02",
            "bench-03",
            "bench-04",
            "bench-05",
            "bench-06",
        ]
        assert benches[0].capabilities == ["Power", "Serial", "Firmware"]
        assert benches[2].capabilities[-1] == "Debugger"
        assert benches[5].capabilities == benches[0].capabilities
        assert benches[0].devices[0].name == "bench-01-controller"

        await simlab.shutdown()
        assert bool(simlab.started) is False
        assert simlab.benches() == []

        disabled = SimLab(enabled=False)
        await disabled.start()
        assert bool(disabled.started) is True
        assert disabled.benches() == []

    import asyncio

    asyncio.run(scenario())
