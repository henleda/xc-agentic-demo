"""Token usage and daily budgets: Copperline's own control, separate from (and complementary to) XC rate limits.

In memory only. A restart or POST /v1/usage/reset clears it, and the budget day rolls over at 00:00 UTC.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime


def utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class Usage:
    def __init__(self, budgets: dict[str, int], clock: Callable[[], str] = utc_day) -> None:
        self.budgets = dict(budgets)
        self._clock = clock
        self._lock = threading.Lock()
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.day = self._clock()
        self.by_route: dict[str, dict[str, int]] = {}
        self.by_agent: dict[str, dict[str, int]] = {}
        self.calls = 0
        self.rejected_budget = 0

    def _roll_day(self) -> None:
        if self._clock() != self.day:
            self._reset_locked()

    def over_budget(self, route: str) -> bool:
        """True when the route has already used its whole budget for today. Checked BEFORE the model call, so one
        call can overshoot by its own size; that is how real gateways with post-hoc token counts behave too."""
        with self._lock:
            self._roll_day()
            used = self.by_route.get(route, {}).get("total", 0)
            over = used >= self.budgets.get(route, 0)
            if over:
                self.rejected_budget += 1
            return over

    def record(self, route: str, agent_id: str | None, tokens_in: int, tokens_out: int) -> None:
        with self._lock:
            self._roll_day()
            self.calls += 1
            for table, key in ((self.by_route, route), (self.by_agent, agent_id or "unknown")):
                row = table.setdefault(key, {"input": 0, "output": 0, "total": 0, "calls": 0})
                row["input"] += tokens_in
                row["output"] += tokens_out
                row["total"] += tokens_in + tokens_out
                row["calls"] += 1

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day()
            budgets = {
                route: {"limit": limit, "used": self.by_route.get(route, {}).get("total", 0)}
                for route, limit in self.budgets.items()
            }
            return {
                "day": self.day,
                "calls": self.calls,
                "rejected_budget": self.rejected_budget,
                "by_route": {route: dict(row) for route, row in self.by_route.items()},
                "by_agent": {agent: dict(row) for agent, row in self.by_agent.items()},
                "budgets": budgets,
            }
