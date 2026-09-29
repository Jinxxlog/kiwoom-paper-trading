import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from data.api_budget import ApiBudgetBlockedError
from data.scheduler import (
    PeriodicCollector,
    ProcessLock,
    is_market_open,
    is_preopen_scan_time,
)


class FakeCollector:
    def __init__(self) -> None:
        self.collected: list[str] = []

    def collect_current_price(self, stock_code: str) -> object:
        if stock_code == "A999999":
            raise RuntimeError("expected failure")
        self.collected.append(stock_code)
        return type(
            "Result",
            (),
            {
                "stock_code": stock_code,
                "current_price": 1000,
                "volume": 100,
                "database_id": 1,
            },
        )()


class SchedulerTest(unittest.TestCase):
    def test_market_hours_use_seoul_weekday(self) -> None:
        seoul = timezone(timedelta(hours=9))
        self.assertTrue(is_market_open(datetime(2026, 6, 10, 9, 0, tzinfo=seoul)))
        self.assertTrue(is_market_open(datetime(2026, 6, 10, 15, 30, tzinfo=seoul)))
        self.assertFalse(is_market_open(datetime(2026, 6, 10, 15, 31, tzinfo=seoul)))
        self.assertFalse(is_market_open(datetime(2026, 6, 13, 10, 0, tzinfo=seoul)))

    def test_preopen_scan_window_uses_seoul_weekday(self) -> None:
        seoul = timezone(timedelta(hours=9))
        self.assertFalse(
            is_preopen_scan_time(datetime(2026, 6, 10, 8, 49, tzinfo=seoul))
        )
        self.assertTrue(
            is_preopen_scan_time(datetime(2026, 6, 10, 8, 50, tzinfo=seoul))
        )
        self.assertTrue(
            is_preopen_scan_time(datetime(2026, 6, 10, 8, 59, tzinfo=seoul))
        )
        self.assertFalse(
            is_preopen_scan_time(datetime(2026, 6, 10, 9, 0, tzinfo=seoul))
        )
        self.assertFalse(
            is_preopen_scan_time(datetime(2026, 6, 13, 8, 55, tzinfo=seoul))
        )

    def test_collection_continues_after_one_stock_fails(self) -> None:
        collector = FakeCollector()
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930", "A999999", "A000660"],
        )

        with self.assertLogs(level="ERROR") as logs:
            successes, failures = periodic.collect_once()

        self.assertEqual((successes, failures), (2, 1))
        self.assertEqual(collector.collected, ["A005930", "A000660"])
        self.assertIn("A999999 수집 실패", logs.output[0])

    def test_collection_deduplicates_stock_codes_within_cycle(self) -> None:
        collector = FakeCollector()
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930", "A005930", "A000660", "A005930"],
        )

        successes, failures = periodic.collect_once()

        self.assertEqual((successes, failures), (2, 0))
        self.assertEqual(collector.collected, ["A005930", "A000660"])

    def test_current_price_calls_are_spaced(self) -> None:
        collector = FakeCollector()
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930", "A000660"],
            current_price_call_spacing_seconds=0.25,
        )

        with (
            patch(
                "data.scheduler.time.monotonic",
                side_effect=[0.0, 0.10, 0.25],
            ),
            patch("data.scheduler.time.sleep") as sleep,
        ):
            successes, failures = periodic.collect_once()

        self.assertEqual((successes, failures), (2, 0))
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.15)

    def test_budget_block_is_recorded_as_skip_not_failure(self) -> None:
        events: list[tuple[str, dict]] = []

        class BudgetBlockedCollector(FakeCollector):
            def collect_current_price(self, stock_code: str) -> object:
                if stock_code == "A005930":
                    raise ApiBudgetBlockedError("blocked_by_backoff")
                return super().collect_current_price(stock_code)

        collector = BudgetBlockedCollector()
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930", "A000660"],
            event_callback=lambda event_type, payload: events.append(
                (event_type, payload)
            ),
        )

        successes, failures = periodic.collect_once()

        self.assertEqual((successes, failures), (1, 0))
        self.assertTrue(
            any(event_type == "stock_skipped" for event_type, _ in events)
        )

    def test_process_lock_rejects_second_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "collector.lock"
            with ProcessLock(lock_path):
                with self.assertRaises(RuntimeError):
                    with ProcessLock(lock_path):
                        pass
            self.assertFalse(lock_path.exists())

    def test_process_lock_recovers_dead_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "collector.lock"
            lock_path.write_text("99999999", encoding="ascii")

            with ProcessLock(lock_path) as lock:
                self.assertTrue(lock.recovered_stale)
                self.assertEqual(
                    lock_path.read_text(encoding="ascii"),
                    str(os.getpid()),
                )

            self.assertFalse(lock_path.exists())

    def test_process_lock_inspection_reports_current_pid_as_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "collector.lock"
            lock_path.write_text(str(os.getpid()), encoding="ascii")

            status = ProcessLock(lock_path).inspect()

            self.assertTrue(status.exists)
            self.assertTrue(status.active)
            self.assertFalse(status.stale)

    def test_after_collection_callback_runs_once(self) -> None:
        collector = FakeCollector()
        callbacks: list[str] = []
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930", "A000660"],
            after_collection=lambda: callbacks.append("risk"),
        )

        successes, failures = periodic.collect_once()

        self.assertEqual((successes, failures), (2, 0))
        self.assertEqual(callbacks, ["risk"])

    def test_collection_emits_runtime_events(self) -> None:
        collector = FakeCollector()
        events: list[tuple[str, dict]] = []
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930", "A999999"],
            event_callback=lambda event_type, payload: events.append(
                (event_type, payload)
            ),
        )

        with self.assertLogs(level="ERROR"):
            periodic.collect_once()

        self.assertEqual(events[0][0], "cycle_started")
        self.assertEqual(events[1][0], "stock_collected")
        self.assertEqual(events[2][0], "stock_failed")
        self.assertEqual(events[-1][0], "cycle_completed")
        self.assertEqual(events[-1][1]["failure_count"], 1)

    def test_run_uses_injected_market_calendar_check(self) -> None:
        collector = FakeCollector()
        sleep_calls: list[float] = []
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930"],
            market_open_check=lambda: False,
        )

        def stop_after_first_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            raise RuntimeError("stop")

        with self.assertRaisesRegex(RuntimeError, "stop"):
            periodic.run(
                5,
                market_hours_only=True,
                sleep=stop_after_first_sleep,
            )

        self.assertEqual(collector.collected, [])
        self.assertEqual(sleep_calls, [5])

    def test_preopen_callback_runs_without_regular_collection(self) -> None:
        collector = FakeCollector()
        callbacks: list[str] = []
        events: list[tuple[str, dict]] = []
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930"],
            event_callback=lambda event_type, payload: events.append(
                (event_type, payload)
            ),
            market_open_check=lambda: False,
            pre_market_callback=lambda: callbacks.append("preopen"),
            pre_market_check=lambda: True,
        )

        def stop_after_first_sleep(seconds: float) -> None:
            raise RuntimeError("stop")

        with self.assertRaisesRegex(RuntimeError, "stop"):
            periodic.run(
                5,
                market_hours_only=True,
                sleep=stop_after_first_sleep,
            )

        self.assertEqual(callbacks, ["preopen"])
        self.assertEqual(collector.collected, [])
        self.assertEqual(events[0][0], "pre_market_succeeded")

    def test_run_stops_when_scheduled_auto_stop_is_requested(self) -> None:
        collector = FakeCollector()
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930"],
        )

        periodic.run(
            5,
            market_hours_only=False,
            auto_stop_requested=lambda: True,
        )

        self.assertEqual(collector.collected, [])

    def test_run_never_passes_negative_delay_to_sleep(self) -> None:
        collector = FakeCollector()
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930"],
        )
        stop_checks = 0
        sleep_calls: list[float] = []

        def stop_after_deadline() -> bool:
            nonlocal stop_checks
            stop_checks += 1
            return stop_checks >= 3

        with patch(
            "data.scheduler.time.monotonic",
            side_effect=[0.0, 0.5, 1.1],
        ):
            periodic.run(
                1,
                market_hours_only=False,
                sleep=sleep_calls.append,
                stop_requested=stop_after_deadline,
            )

        self.assertEqual(collector.collected, ["A005930"])
        self.assertEqual(sleep_calls, [])

    def test_runtime_control_error_does_not_stop_collector(self) -> None:
        collector = FakeCollector()
        events: list[tuple[str, dict]] = []
        periodic = PeriodicCollector(
            collector,
            lambda: ["A005930"],
            event_callback=lambda event_type, payload: events.append(
                (event_type, payload)
            ),
        )
        auto_stop_checks = 0

        def flaky_auto_stop() -> bool:
            nonlocal auto_stop_checks
            auto_stop_checks += 1
            if auto_stop_checks == 1:
                raise RuntimeError("database is locked")
            return True

        with self.assertLogs(level="ERROR") as logs:
            periodic.run(
                1,
                market_hours_only=False,
                auto_stop_requested=flaky_auto_stop,
            )

        self.assertEqual(collector.collected, ["A005930"])
        self.assertIn("예약 자동 종료 확인 실패", logs.output[0])
        self.assertTrue(
            any(event_type == "runtime_control_check_failed" for event_type, _ in events)
        )


if __name__ == "__main__":
    unittest.main()
