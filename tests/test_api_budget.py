import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from data.api_budget import (
    ApiBudgetBlockedError,
    ApiBudgetManager,
    ApiBudgetPolicy,
    ApiCallPriority,
)
from db.database import Database


class MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class ApiBudgetManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.clock = MutableClock(datetime(2026, 6, 22, 9, 10, 0))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_api_group_policy_consumes_window_budget(self) -> None:
        manager = ApiBudgetManager(self.database, clock=self.clock)

        decision = manager.before_call(
            api_id="ka10032",
            endpoint="/api/dostk/rkinfo",
            priority=ApiCallPriority.HIGH,
        )
        manager.after_call(decision, status_code=200)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.api_group, "market_rank")
        self.assertEqual(decision.used_count, 1)
        windows = self.database.api_budget_windows_status()
        self.assertEqual(windows[0]["api_group"], "market_rank")
        self.assertEqual(windows[0]["used_count"], 1)
        events = self.database.latest_api_budget_events()
        self.assertEqual(events[0]["status_code"], 200)
        self.assertTrue(events[0]["consumed"])
        self.assertFalse(events[0]["blocked"])

    def test_daily_chart_uses_separate_low_speed_budget(self) -> None:
        manager = ApiBudgetManager(self.database, clock=self.clock)

        decision = manager.before_call(
            api_id="ka10081",
            endpoint="/api/dostk/chart",
            priority=ApiCallPriority.NORMAL,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.api_group, "daily_chart")
        self.assertEqual(decision.limit_count, 120)

    def test_low_priority_is_blocked_when_remaining_budget_is_reserved(self) -> None:
        manager = ApiBudgetManager(
            self.database,
            clock=self.clock,
            policies={
                "market_rank": ApiBudgetPolicy(
                    "market_rank",
                    limit_count=2,
                    high_priority_reserve=1,
                ),
                "default": ApiBudgetPolicy("default", limit_count=10),
            },
        )

        first = manager.before_call(
            api_id="ka10032",
            endpoint="/api/dostk/rkinfo",
            priority=ApiCallPriority.HIGH,
        )
        manager.after_call(first, status_code=200)
        blocked = manager.before_call(
            api_id="ka10032",
            endpoint="/api/dostk/rkinfo",
            priority=ApiCallPriority.LOW,
        )
        high = manager.before_call(
            api_id="ka10032",
            endpoint="/api/dostk/rkinfo",
            priority=ApiCallPriority.HIGH,
        )

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "skipped_by_budget_reserved")
        self.assertTrue(high.allowed)
        self.assertEqual(high.used_count, 2)

    def test_429_sets_backoff_and_blocks_next_call(self) -> None:
        manager = ApiBudgetManager(self.database, clock=self.clock)
        decision = manager.before_call(
            api_id="ka10004",
            endpoint="/api/dostk/mrkcond",
            stock_code="A005930",
            priority=ApiCallPriority.HIGH,
        )

        state = manager.after_call(decision, status_code=429)
        blocked = manager.before_call(
            api_id="ka10004",
            endpoint="/api/dostk/mrkcond",
            stock_code="A005930",
            priority=ApiCallPriority.HIGH,
        )

        self.assertIsNotNone(state)
        self.assertEqual(state.backoff_seconds, 30)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "blocked_by_backoff")
        self.assertGreaterEqual(blocked.backoff_seconds, 29)
        events = self.database.latest_api_budget_events(limit=2)
        self.assertEqual(events[0]["reason"], "blocked_by_backoff")
        self.assertEqual(events[1]["status_code"], 429)

    def test_window_resets_after_policy_window_seconds(self) -> None:
        manager = ApiBudgetManager(
            self.database,
            clock=self.clock,
            policies={
                "current_price": ApiBudgetPolicy(
                    "current_price",
                    limit_count=1,
                    window_seconds=10,
                ),
                "default": ApiBudgetPolicy("default", limit_count=10),
            },
        )

        first = manager.before_call(
            api_id="ka10006",
            endpoint="/api/dostk/mrkcond",
            stock_code="A005930",
        )
        manager.after_call(first, status_code=200)
        exhausted = manager.before_call(
            api_id="ka10006",
            endpoint="/api/dostk/mrkcond",
            stock_code="A005930",
        )
        self.clock.advance(11)
        reset = manager.before_call(
            api_id="ka10006",
            endpoint="/api/dostk/mrkcond",
            stock_code="A005930",
        )

        self.assertFalse(exhausted.allowed)
        self.assertEqual(exhausted.reason, "skipped_by_budget")
        self.assertTrue(reset.allowed)
        self.assertEqual(reset.used_count, 1)

    def test_blocked_error_type_is_public_for_callers(self) -> None:
        self.assertTrue(issubclass(ApiBudgetBlockedError, RuntimeError))


if __name__ == "__main__":
    unittest.main()
