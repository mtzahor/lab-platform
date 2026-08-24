from __future__ import annotations

import pytest
from lab_platform.control_plane.observability import OperationalMetrics


def test_operational_metrics_render_bounded_prometheus_labels_and_legacy_gauges() -> None:
    metrics = OperationalMetrics()
    metrics.observe_http(
        method="get",
        route="/api/v1/agents/{agent_id:uuid}",
        status_code=200,
        duration_seconds=0.125,
    )
    metrics.observe_http(
        method="GET",
        route="/api/v1/agents/{agent_id:uuid}",
        status_code=200,
        duration_seconds=0.375,
    )
    metrics.record_background_failure('retention"worker')

    rendered = metrics.render_prometheus(
        {"agents_online": 3, "invalid-name": 7, "request_latency": float("inf")}
    )

    assert 'method="GET"' in rendered
    assert 'route="/api/v1/agents/{agent_id:uuid}"' in rendered
    assert "lab_platform_http_requests_total" in rendered
    assert " 2\n" in rendered
    assert "_sum" in rendered and " 0.500000000\n" in rendered
    assert 'worker="retention\\"worker"' in rendered
    assert 'lab_platform_runtime{name="agents_online"} 3' in rendered
    assert "agents_online 3" in rendered
    assert "invalid-name" not in rendered
    assert "request_latency" not in rendered


@pytest.mark.parametrize(
    ("route", "status", "duration"),
    [("relative", 200, 0.1), ("/route", 99, 0.1), ("/route", 200, -0.1)],
)
def test_operational_metrics_reject_invalid_observations(
    route: str,
    status: int,
    duration: float,
) -> None:
    with pytest.raises(ValueError):
        OperationalMetrics().observe_http(
            method="GET",
            route=route,
            status_code=status,
            duration_seconds=duration,
        )
