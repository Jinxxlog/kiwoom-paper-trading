import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from db.database import Database
from operations.daily_check import DailyOperationsChecker


class DailyOperationsCheckerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.database.add_watchlist_stock("A005930", "Samsung")
        self.checker = DailyOperationsChecker(self.database)
        self.seoul = timezone(timedelta(hours=9))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_run(
        self,
        started_at: str,
        *,
        status: str = "success",
        failure_count: int = 0,
        interval_seconds: int = 12_000,
        auto_strategy: bool = True,
    ) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operation_runs(
                    run_type, status, started_at, completed_at,
                    success_count, failure_count, metadata_json
                )
                VALUES ('periodic_collect', ?, ?, ?, 1, ?, ?)
                """,
                (
                    status,
                    started_at,
                    started_at,
                    failure_count,
                    json.dumps(
                        {
                            "interval_seconds": interval_seconds,
                            "auto_strategy": auto_strategy,
                        }
                    ),
                ),
            )
            return int(cursor.lastrowid)

    def _insert_event(
        self,
        run_id: int,
        module: str,
        event_type: str,
        occurred_at: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO operation_events(
                    run_id, module, event_type, severity,
                    message, details_json, occurred_at
                )
                VALUES (?, ?, ?, 'info', 'test', '{}', ?)
                """,
                (run_id, module, event_type, occurred_at),
            )

    def _save_price(self, collected_at: datetime) -> None:
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 100},
            current_price=100,
            open_price=100,
            high_price=100,
            low_price=100,
            volume=100,
            collected_at=collected_at,
        )

    def test_complete_trading_day_passes(self) -> None:
        for clock in ("09:00:00", "12:00:00", "15:20:00"):
            timestamp = f"2026-06-10T{clock}"
            run_id = self._insert_run(timestamp)
            self._insert_event(
                run_id,
                "risk",
                "risk_evaluated",
                timestamp,
            )
            self._insert_event(
                run_id,
                "strategy",
                "strategy_evaluated",
                timestamp,
            )
            self._save_price(datetime.fromisoformat(timestamp))

        result = self.checker.check(
            date(2026, 6, 10),
            now=datetime(2026, 6, 10, 16, 0, tzinfo=self.seoul),
        )

        self.assertEqual(result.status, "passed")
        self.assertTrue(result.full_day_expected)
        self.assertTrue(all(item.status == "pass" for item in result.checks))
        self.assertEqual(result.summary["run_count"], 3)

    def test_late_start_failures_and_missing_prices_fail(self) -> None:
        self._insert_run(
            "2026-06-10T09:30:00",
            status="partial_failure",
            failure_count=1,
            interval_seconds=60,
            auto_strategy=False,
        )
        self._insert_run(
            "2026-06-10T14:00:00",
            interval_seconds=60,
            auto_strategy=False,
        )

        result = self.checker.check(
            date(2026, 6, 10),
            now=datetime(2026, 6, 10, 16, 0, tzinfo=self.seoul),
        )

        self.assertEqual(result.status, "failed")
        failed_codes = {
            item.code for item in result.checks if item.status == "fail"
        }
        self.assertIn("opening_coverage", failed_codes)
        self.assertIn("closing_coverage", failed_codes)
        self.assertIn("run_failures", failed_codes)
        self.assertIn("collection_gaps", failed_codes)
        self.assertIn("price_coverage", failed_codes)

    def test_current_day_before_close_is_in_progress(self) -> None:
        timestamp = "2026-06-10T09:00:00"
        run_id = self._insert_run(
            timestamp,
            interval_seconds=60,
            auto_strategy=False,
        )
        self._insert_event(run_id, "risk", "risk_evaluated", timestamp)
        self._save_price(datetime.fromisoformat(timestamp))

        result = self.checker.check(
            date(2026, 6, 10),
            now=datetime(2026, 6, 10, 10, 0, tzinfo=self.seoul),
            expected_interval_seconds=60,
        )

        self.assertEqual(result.status, "in_progress")
        self.assertFalse(result.full_day_expected)
        statuses = {item.code: item.status for item in result.checks}
        self.assertEqual(statuses["closing_coverage"], "pending")

    def test_non_trading_day_is_not_applicable(self) -> None:
        result = self.checker.check(
            date(2026, 6, 13),
            now=datetime(2026, 6, 13, 16, 0, tzinfo=self.seoul),
        )

        self.assertEqual(result.status, "not_applicable")
        self.assertFalse(result.trading_day)
        self.assertEqual(result.checks[0].status, "skip")

    def test_before_market_open_is_in_progress(self) -> None:
        result = self.checker.check(
            date(2026, 6, 10),
            now=datetime(2026, 6, 10, 8, 30, tzinfo=self.seoul),
        )

        self.assertEqual(result.status, "in_progress")
        self.assertEqual(result.checks[0].code, "waiting_for_open")
        self.assertEqual(result.checks[0].status, "pending")


if __name__ == "__main__":
    unittest.main()
