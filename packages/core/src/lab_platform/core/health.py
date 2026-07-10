from __future__ import annotations

from lab_platform.models import HealthReport, HealthStatus

_STATUS_WEIGHT = {
    HealthStatus.HEALTHY: 0,
    HealthStatus.WARNING: 1,
    HealthStatus.UNHEALTHY: 2,
}


class HealthMonitor:
    def __init__(self) -> None:
        self._reports: dict[str, HealthReport] = {}

    def report(self, component: str, status: HealthStatus, message: str = "") -> None:
        self._reports[component] = HealthReport(
            component=component,
            status=status,
            message=message,
        )

    def component_statuses(self) -> list[HealthReport]:
        return [self._reports[name] for name in sorted(self._reports)]

    def overall_status(self) -> HealthStatus:
        if not self._reports:
            return HealthStatus.HEALTHY
        return max(
            (report.status for report in self._reports.values()),
            key=lambda status: _STATUS_WEIGHT[status],
        )
