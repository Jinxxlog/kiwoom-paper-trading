import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from data.universe_collection import UniverseDailyCollector
from data.universe_sync import (
    KindUniverseClient,
    ListedStock,
    UniverseSyncService,
)
from db.database import Database
from screening.engine import StockUniverseService


class FakeKindClient:
    def fetch(self) -> list[ListedStock]:
        return [
            ListedStock(
                stock_code="A005930",
                stock_name="삼성전자",
                market="KOSPI",
                sector="전자",
                listing_date="1975-06-11",
            ),
            ListedStock(
                stock_code="A000660",
                stock_name="SK하이닉스",
                market="KOSPI",
                sector="전자",
                listing_date="1996-12-26",
            ),
        ]


class FakeDailyCollector:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []
        self.fail_once: set[str] = set()

    def collect_daily_candles(
        self,
        stock_code: str,
        base_date: str,
        *,
        max_pages: int,
    ) -> object:
        del base_date, max_pages
        self.calls.append(stock_code)
        if stock_code in self.fail_once:
            self.fail_once.remove(stock_code)
            raise RuntimeError("temporary")
        start = date(2025, 1, 1)
        candles = []
        for index in range(200):
            close = 10_000 + index
            candles.append(
                {
                    "candle_time": datetime.combine(
                        start + timedelta(days=index),
                        datetime.min.time(),
                    ).isoformat(),
                    "open_price": close,
                    "high_price": close + 10,
                    "low_price": close - 10,
                    "close_price": close,
                    "volume": 100_000,
                    "trade_amount": close * 100_000,
                }
            )
        self.database.save_candles(
            "test",
            stock_code,
            {"test": True},
            candles,
            candle_type="daily",
        )
        return object()


class ShortHistoryCollector:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[str] = []

    def collect_daily_candles(
        self,
        stock_code: str,
        base_date: str,
        *,
        max_pages: int,
    ) -> object:
        del base_date, max_pages
        self.calls.append(stock_code)
        start = date(2026, 1, 1)
        candles = []
        for index in range(50):
            close = 10_000 + index
            candles.append(
                {
                    "candle_time": datetime.combine(
                        start + timedelta(days=index),
                        datetime.min.time(),
                    ).isoformat(),
                    "open_price": close,
                    "high_price": close + 10,
                    "low_price": close - 10,
                    "close_price": close,
                    "volume": 100_000,
                    "trade_amount": close * 100_000,
                }
            )
        self.database.save_candles(
            "test",
            stock_code,
            {"test": True},
            candles,
            candle_type="daily",
        )
        return SimpleNamespace(has_more=False)


class UniverseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_kind_html_parser_filters_markets_and_spac(self) -> None:
        html = """
        <table><tr><th>회사명</th><th>시장구분</th><th>종목코드</th>
        <th>업종</th><th>상장일</th></tr>
        <tr><td>삼성전자</td><td>유가</td><td>005930</td>
        <td>전자</td><td>1975-06-11</td></tr>
        <tr><td>테스트스팩</td><td>코스닥</td><td>123456</td>
        <td>금융</td><td>2026-01-01</td></tr>
        <tr><td>임시코드</td><td>코스닥</td><td>0017J0</td>
        <td>기계</td><td>2026-01-01</td></tr>
        <tr><td>코넥스회사</td><td>코넥스</td><td>999999</td>
        <td>기타</td><td>2026-01-01</td></tr></table>
        """

        stocks = KindUniverseClient.parse(html)

        self.assertEqual(len(stocks), 2)
        self.assertEqual(stocks[0].stock_code, "A005930")
        self.assertEqual(stocks[0].market, "KOSPI")
        self.assertEqual(stocks[1].stock_code, "A0017J0")

    def test_sync_registers_stocks_and_pending_collection_rows(self) -> None:
        result = UniverseSyncService(
            self.database,
            FakeKindClient(),
        ).sync()

        self.assertEqual(result.registered_count, 2)
        with self.database.connect() as connection:
            universe = connection.execute(
                "SELECT * FROM stock_universe ORDER BY stock_code"
            ).fetchall()
            statuses = connection.execute(
                "SELECT * FROM universe_collection_status ORDER BY stock_code"
            ).fetchall()
        self.assertEqual(len(universe), 2)
        self.assertEqual(universe[0]["source"], "kind")
        self.assertEqual(universe[0]["security_type"], "common_stock")
        self.assertEqual(len(statuses), 2)
        self.assertTrue(all(row["status"] == "pending" for row in statuses))

    def test_daily_collection_resumes_failed_stock_and_skips_completed(self) -> None:
        UniverseSyncService(self.database, FakeKindClient()).sync()
        fake = FakeDailyCollector(self.database)
        fake.fail_once.add("A000660")
        service = UniverseDailyCollector(self.database, fake)

        first = service.collect(
            base_date="20260614",
            target_bars=200,
            max_pages=3,
            delay_seconds=0,
        )
        second = service.collect(
            base_date="20260614",
            target_bars=200,
            max_pages=3,
            delay_seconds=0,
        )

        self.assertEqual(first.failed_count, 1)
        self.assertEqual(second.completed_count, 1)
        self.assertEqual(fake.calls.count("A005930"), 1)
        self.assertEqual(fake.calls.count("A000660"), 2)
        summary = service.summary()
        self.assertEqual(summary["completed_count"], 2)
        self.assertEqual(summary["failed_count"], 0)

    def test_collection_stops_before_next_stock(self) -> None:
        UniverseSyncService(self.database, FakeKindClient()).sync()
        fake = FakeDailyCollector(self.database)

        result = UniverseDailyCollector(self.database, fake).collect(
            base_date="20260614",
            target_bars=200,
            delay_seconds=0,
            stop_requested=lambda: True,
        )

        self.assertTrue(result.stopped)
        self.assertEqual(fake.calls, [])

    def test_short_listed_history_is_completed_when_api_is_exhausted(self) -> None:
        UniverseSyncService(self.database, FakeKindClient()).sync()
        collector = ShortHistoryCollector(self.database)

        first = UniverseDailyCollector(self.database, collector).collect(
            base_date="20260626",
            target_bars=200,
            max_pages=3,
            limit=1,
            delay_seconds=0,
        )
        second = UniverseDailyCollector(self.database, collector).collect(
            base_date="20260626",
            target_bars=200,
            max_pages=3,
            limit=1,
            delay_seconds=0,
        )

        self.assertEqual(first.completed_count, 1)
        self.assertEqual(first.available_history_completed_count, 1)
        self.assertEqual(second.attempted_count, 1)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT status, daily_bar_count, last_error
                FROM universe_collection_status
                WHERE stock_code = ?
                """,
                (collector.calls[0],),
            ).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["daily_bar_count"], 50)
        self.assertEqual(row["last_error"], "available_history_exhausted")

    def test_market_product_is_skipped_from_daily_collection(self) -> None:
        service = UniverseSyncService(self.database, FakeKindClient())
        service.sync()
        StockUniverseService(self.database).upsert(
            "A999999",
            stock_name="KODEX 테스트 ETF",
            market="KOSPI",
        )
        fake = FakeDailyCollector(self.database)

        UniverseDailyCollector(self.database, fake).collect(
            base_date="20260626",
            target_bars=200,
            max_pages=3,
            delay_seconds=0,
        )

        self.assertNotIn("A999999", fake.calls)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT status, last_error
                FROM universe_collection_status
                WHERE stock_code = 'A999999'
                """
            ).fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["last_error"], "market_product_excluded")


if __name__ == "__main__":
    unittest.main()
