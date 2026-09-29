import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from db.database import Database
from operations.service import CollectorOperationsTracker, OperationsService


class OperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.service = OperationsService(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_run_and_events_are_saved_with_module_status(self) -> None:
        run_id = self.service.start_run(
            "periodic_collect",
            metadata={"interval_seconds": 60},
        )
        self.service.record_event(
            "collection",
            "stock_collected",
            "info",
            "수집 성공",
            run_id=run_id,
            stock_code="A005930",
            details={"database_id": 10},
        )
        self.service.record_event(
            "strategy",
            "strategy_failed",
            "error",
            "전략 실패",
            run_id=run_id,
        )
        self.service.finish_run(
            run_id,
            success_count=1,
            failure_count=1,
        )

        run = self.service.list_runs(limit=1)[0]
        self.assertEqual(run["status"], "partial_failure")
        self.assertEqual(run["metadata"]["interval_seconds"], 60)
        events = self.service.list_events(
            module="strategy",
            severity="error",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "전략 실패")
        statuses = {
            row["module"]: row for row in self.service.module_status()
        }
        self.assertEqual(statuses["collection"]["status"], "success")
        self.assertEqual(statuses["strategy"]["status"], "failed")

    def test_tracker_records_cycle_summary_and_health(self) -> None:
        tracker = CollectorOperationsTracker(
            self.service,
            "collect_once",
            metadata={"source": "test"},
        )

        tracker.handle("cycle_started", {"stock_count": 2})
        tracker.handle(
            "stock_collected",
            {
                "stock_code": "A005930",
                "message": "성공",
            },
        )
        tracker.handle(
            "stock_failed",
            {
                "stock_code": "A000660",
                "message": "실패",
            },
        )
        tracker.handle(
            "cycle_completed",
            {"success_count": 1, "failure_count": 1},
        )

        run = self.service.list_runs(limit=1)[0]
        self.assertEqual(run["status"], "partial_failure")
        events = self.service.list_events(module="collection")
        self.assertEqual(events[0]["event_type"], "cycle_completed")
        self.assertEqual(events[0]["severity"], "warning")
        health = self.service.health(stale_minutes=10)
        self.assertTrue(health["healthy"])

    def test_health_reports_failed_module(self) -> None:
        self.service.record_event(
            "risk",
            "risk_failed",
            "error",
            "리스크 평가 실패",
        )

        health = self.service.health()

        self.assertFalse(health["healthy"])
        self.assertFalse(health["checks"][0]["healthy"])

    def test_stale_running_run_is_recovered_as_failed(self) -> None:
        run_id = self.service.start_run("periodic_collect")
        now = datetime(2026, 6, 12, 12, 0, 0)
        old_started_at = (now - timedelta(minutes=30)).isoformat(
            timespec="seconds"
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE operation_runs SET started_at = ? WHERE id = ?",
                (old_started_at, run_id),
            )

        recovered = self.service.recover_running_runs(
            stale_minutes=10,
            now=now,
        )

        self.assertEqual(recovered, [run_id])
        run = self.service.list_runs(limit=1)[0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_count"], 1)
        self.assertIn("비정상 종료", run["message"])
        event = self.service.list_events(module="system", limit=1)[0]
        self.assertEqual(event["event_type"], "interrupted_runs_recovered")

    def test_recent_running_run_is_not_recovered(self) -> None:
        run_id = self.service.start_run("periodic_collect")
        now = datetime.now()

        recovered = self.service.recover_running_runs(
            stale_minutes=10,
            now=now,
        )

        self.assertEqual(recovered, [])
        self.assertEqual(self.service.list_runs(limit=1)[0]["id"], run_id)
        self.assertEqual(self.service.list_runs(limit=1)[0]["status"], "running")

    def test_tracker_calls_cycle_completed_callback(self) -> None:
        payloads: list[dict] = []
        tracker = CollectorOperationsTracker(
            self.service,
            "periodic_collect",
            cycle_completed_callback=payloads.append,
        )

        tracker.handle("cycle_started", {"stock_count": 1})
        tracker.handle(
            "cycle_completed",
            {"success_count": 1, "failure_count": 0},
        )

        self.assertEqual(
            payloads,
            [{"success_count": 1, "failure_count": 0}],
        )


if __name__ == "__main__":
    unittest.main()
