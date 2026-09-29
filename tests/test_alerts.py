import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import OperationsAlertSettings
from db.database import Database
from operations.alerts import (
    COLLECTION_DELAY,
    CONSECUTIVE_FAILURES,
    OperationsAlertService,
)
from operations.service import OperationsService


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


class OperationsAlertServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.operations = OperationsService(self.database)
        self.notifier = FakeNotifier()
        self.alerts = OperationsAlertService(
            self.database,
            OperationsAlertSettings(
                collection_delay_seconds=180,
                consecutive_failures=3,
            ),
            self.operations,
            self.notifier,
        )
        self.seoul = timezone(timedelta(hours=9))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_run(
        self,
        started_at: str,
        *,
        status: str = "success",
        failure_count: int = 0,
    ) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operation_runs(
                    run_type, status, started_at, completed_at,
                    success_count, failure_count, metadata_json
                )
                VALUES ('periodic_collect', ?, ?, ?, ?, ?, ?)
                """,
                (
                    status,
                    started_at,
                    started_at,
                    0 if failure_count else 1,
                    failure_count,
                    json.dumps({"interval_seconds": 60}),
                ),
            )
            return int(cursor.lastrowid)

    def test_collection_delay_alert_is_deduplicated_and_resolved(self) -> None:
        self._insert_run("2026-06-10T09:50:00")
        now = datetime(2026, 6, 10, 10, 0, tzinfo=self.seoul)

        self.alerts.evaluate(interval_seconds=60, now=now)
        self.alerts.evaluate(interval_seconds=60, now=now)

        active = self.alerts.list_alerts()
        delay = next(row for row in active if row["alert_key"] == COLLECTION_DELAY)
        self.assertEqual(delay["occurrence_count"], 2)
        self.assertFalse(self.operations.health()["healthy"])
        self.assertEqual(len(self.operations.health()["active_alerts"]), 1)
        triggered = [
            row
            for row in self.operations.list_events(module="alert")
            if row["event_type"] == "alert_triggered"
            and row["details"]["alert_key"] == COLLECTION_DELAY
        ]
        self.assertEqual(len(triggered), 1)
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("자동투자 경보", self.notifier.messages[0])

        self._insert_run("2026-06-10T09:59:00")
        self._insert_run("2026-06-10T10:00:00")
        self.alerts.evaluate(interval_seconds=60, now=now)

        self.assertFalse(
            any(
                row["alert_key"] == COLLECTION_DELAY
                for row in self.alerts.list_alerts()
            )
        )
        resolved = next(
            row
            for row in self.alerts.list_alerts(include_resolved=True)
            if row["alert_key"] == COLLECTION_DELAY
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertTrue(
            any("자동투자 경보 해제" in message for message in self.notifier.messages)
        )

    def test_consecutive_failure_alert_resolves_after_success(self) -> None:
        for minute in (0, 1, 2):
            self._insert_run(
                f"2026-06-10T10:0{minute}:00",
                status="failed",
                failure_count=1,
            )
        after_close = datetime(
            2026,
            6,
            10,
            16,
            0,
            tzinfo=self.seoul,
        )

        self.alerts.evaluate(interval_seconds=60, now=after_close)
        self.alerts.evaluate(interval_seconds=60, now=after_close)

        active = self.alerts.list_alerts()
        failure = next(
            row
            for row in active
            if row["alert_key"] == CONSECUTIVE_FAILURES
        )
        self.assertEqual(failure["severity"], "error")
        triggered = [
            row
            for row in self.operations.list_events(module="alert")
            if row["event_type"] == "alert_triggered"
            and row["details"]["alert_key"] == CONSECUTIVE_FAILURES
        ]
        self.assertEqual(len(triggered), 1)

        self._insert_run("2026-06-10T10:03:00")
        self.alerts.evaluate(interval_seconds=60, now=after_close)

        self.assertEqual(self.alerts.list_alerts(), [])
        resolved = [
            row
            for row in self.operations.list_events(module="alert")
            if row["event_type"] == "alert_resolved"
            and row["details"]["alert_key"] == CONSECUTIVE_FAILURES
        ]
        self.assertEqual(len(resolved), 1)


if __name__ == "__main__":
    unittest.main()
