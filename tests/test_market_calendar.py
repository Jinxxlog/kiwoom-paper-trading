import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from data.market_calendar import MarketCalendar
from db.database import Database


class MarketCalendarTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.calendar = MarketCalendar(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_builtin_holiday_and_weekend_are_closed(self) -> None:
        self.assertFalse(self.calendar.is_trading_day(date(2026, 2, 17)))
        self.assertFalse(self.calendar.is_trading_day(date(2026, 6, 13)))
        self.assertTrue(self.calendar.is_trading_day(date(2026, 6, 12)))

    def test_manual_closure_and_exceptional_open_override_defaults(self) -> None:
        weekday = date(2026, 6, 12)
        weekend = date(2026, 6, 13)

        self.calendar.set_day(weekday, "closed", name="Temporary closure")
        self.calendar.set_day(weekend, "open", name="Exceptional session")

        self.assertFalse(self.calendar.is_trading_day(weekday))
        self.assertTrue(self.calendar.is_trading_day(weekend))
        rows = self.calendar.list_exceptions(2026)
        by_date = {row.market_date: row for row in rows}
        self.assertEqual(by_date[weekday].source, "manual")
        self.assertEqual(by_date[weekend].status, "open")

    def test_market_hours_include_calendar_decision(self) -> None:
        seoul = timezone(timedelta(hours=9))
        open_time = datetime(2026, 6, 12, 10, 0, tzinfo=seoul)
        self.assertTrue(self.calendar.is_market_open(open_time))

        self.calendar.set_day(date(2026, 6, 12), "closed")

        self.assertFalse(self.calendar.is_market_open(open_time))


if __name__ == "__main__":
    unittest.main()
