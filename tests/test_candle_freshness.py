import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from data.candle_freshness import CandleFreshness, CandleRefreshService
from db.database import Database
from paper.engine import PaperTradingError


class CandleFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")
        self.db.initialize()
        self.service = CandleFreshness(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, when, kind="daily", close=100):
        self.db.save_candles("test", "A005930", {}, [{
            "candle_time": when, "open_price": 100, "high_price": 110,
            "low_price": 90, "close_price": close, "volume": 1000,
        }], candle_type=kind, interval_minutes=5 if kind == "minute" else 0)

    def test_daily_target_does_not_use_in_progress_session(self):
        self.assertEqual(str(self.service.daily_target(datetime(2026,9,9,10))), "2026-09-08")
        self.assertEqual(str(self.service.daily_target(datetime(2026,9,9,17))), "2026-09-09")
        self.assertEqual(str(self.service.daily_target(datetime(2026,9,9,17),for_entry=True)), "2026-09-08")

    def test_weekend_holiday_and_timezone(self):
        self.assertEqual(str(self.service.daily_target(datetime(2026,8,18,8))), "2026-08-14")
        self.assertEqual(str(self.service.daily_target(datetime(2026,9,12,17))), "2026-09-11")
        self.assertEqual(str(self.service.daily_target(datetime(2026,9,9,8,tzinfo=timezone.utc))), "2026-09-09")

    def test_two_hundred_old_bars_do_not_mean_fresh(self):
        for i in range(200):
            self.save((datetime(2025,1,1)+timedelta(days=i)).isoformat())
        self.assertFalse(self.service.daily_status("A005930",datetime(2026,9,9,17))["ready"])

    def test_invalid_or_future_daily_is_not_ready(self):
        self.save("2026-09-08T00:00:00",close=0)
        self.save("2026-09-09T00:00:00")
        self.assertFalse(self.service.daily_status("A005930",datetime(2026,9,9,10))["ready"])

    def test_refresh_current_date_and_resume_without_refetch(self):
        calls=[]
        def collect(code, base_date, max_pages):
            calls.append(base_date)
            self.save("2026-09-09T00:00:00")
            return SimpleNamespace(saved_count=1)
        service=CandleRefreshService(self.db,SimpleNamespace(collect_daily_candles=collect))
        self.save("2026-06-12T00:00:00")
        self.assertTrue(service.daily("A005930",datetime(2026,9,9,17))["ready"])
        self.assertEqual(service.daily("A005930",datetime(2026,9,9,17))["action"],"already_current")
        self.assertEqual(calls,["20260909"])

    def test_success_response_with_stale_data_is_not_ready(self):
        collector=SimpleNamespace(collect_daily_candles=lambda *a,**k:SimpleNamespace(saved_count=600))
        self.save("2026-06-12T00:00:00")
        self.assertFalse(CandleRefreshService(self.db,collector).daily("A005930",datetime(2026,9,9,17))["ready"])

    def test_minute_ignores_future_and_incomplete_bars(self):
        self.save("2026-09-09T10:30:00","minute")
        self.save("2026-09-09T10:35:00","minute")
        self.save("2026-09-09T10:40:00","minute")
        result=self.service.minute_status("A005930",datetime(2026,9,9,10,37))
        self.assertEqual(result["latest_time"],"2026-09-09T10:30:00")
        self.assertTrue(result["ready"])

    def test_stale_minute_and_open_without_confirmed_bar_block(self):
        self.save("2026-09-08T15:30:00","minute")
        self.assertFalse(self.service.minute_status("A005930",datetime(2026,9,9,9,3))["ready"])
        self.save("2026-09-09T10:10:00","minute")
        self.assertFalse(self.service.minute_status("A005930",datetime(2026,9,9,10,40))["ready"])

    def test_close_and_entry_require_both_inputs(self):
        self.save("2026-09-09T15:30:00","minute")
        self.assertTrue(self.service.minute_status("A005930",datetime(2026,9,9,17))["ready"])
        with self.assertRaises(PaperTradingError):
            self.service.require_entry_data("A005930",datetime(2026,9,9,17))

    def test_runtime_factory_injects_freshness_gate(self):
        from unittest.mock import patch
        import main
        with patch("main.BreakoutStrategy") as constructor:
            main.create_breakout_strategy(self.db)
        self.assertIsInstance(constructor.call_args.kwargs["candle_freshness"],CandleFreshness)


if __name__ == "__main__":
    unittest.main()
