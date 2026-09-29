import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from config.settings import MarketModeGateSettings
from data.collector import MarketDataCollector
from data.watchlist import WatchlistService, normalize_stock_code
from db.database import Database
from kiwoom_rest_client import ApiPage
from operations.market_mode import MarketModeService


class FakeClient:
    def get_intraday_prices(self, stock_code: str) -> dict:
        return {
            "stk_cd": stock_code,
            "close_pric": "-71,500",
            "open_pric": "70,000",
            "high_pric": "72,000",
            "low_pric": "69,500",
            "trde_qty": "12,345",
            "return_code": 0,
        }


class FakeChartClient:
    def get_daily_chart(
        self,
        stock_code: str,
        base_date: str,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "stk_cd": stock_code,
                "stk_dt_pole_chart_qry": [
                    {
                        "dt": "20260610",
                        "open_pric": "70000",
                        "high_pric": "72000",
                        "low_pric": "69500",
                        "cur_prc": "71500",
                        "trde_qty": "12345",
                        "trde_prica": "987",
                    },
                    {
                        "dt": "20260609",
                        "open_pric": "69000",
                        "high_pric": "70500",
                        "low_pric": "68800",
                        "cur_prc": "70000",
                        "trde_qty": "10000",
                        "trde_prica": "700",
                    },
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )

    def get_minute_chart(
        self,
        stock_code: str,
        interval_minutes: int,
        base_date: str,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "stk_cd": stock_code,
                "stk_min_pole_chart_qry": [
                    {
                        "cntr_tm": "20260610153000",
                        "open_pric": "-71000",
                        "high_pric": "-71600",
                        "low_pric": "-70900",
                        "cur_prc": "-71500",
                        "trde_qty": "1234",
                        "acc_trde_qty": "12345",
                    }
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )


class FakeThemeClient:
    def __init__(self) -> None:
        self.theme_group_calls: list[dict[str, str]] = []

    def get_theme_groups(
        self,
        *,
        query_type: str = "0",
        stock_code: str = "",
        date_type: str = "10",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        self.theme_group_calls.append(
            {
                "query_type": query_type,
                "stock_code": stock_code,
                "date_type": date_type,
            }
        )
        return ApiPage(
            data={
                "thema_grp": [
                    {
                        "thema_grp_cd": "123",
                        "thema_nm": "AI",
                        "stk_num": "20",
                        "flu_rt": "5.3",
                        "rising_stk_num": "16",
                        "fall_stk_num": "2",
                        "dt_prft_rt": "4.1",
                        "main_stk": "삼성전자",
                    }
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )

    def get_theme_components(
        self,
        theme_code: str,
        *,
        date_type: str = "2",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "thema_comp_stk": [
                    {
                        "stk_cd": "005930",
                        "stk_nm": "삼성전자",
                        "cur_prc": "72,000",
                        "flu_rt": "6.2",
                        "acc_trde_qty": "1,000,000",
                    }
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )


class FakeRealtimeRankClient:
    def get_realtime_item_ranking(
        self,
        *,
        query_type: str = "1",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "item_inq_rank": [
                    {
                        "stk_nm": "삼성전자",
                        "bigd_rank": "1",
                        "rank_chg": "-1",
                        "rank_chg_sign": "-",
                        "past_curr_prc": "+72,000",
                        "base_comp_sign": "2",
                        "base_comp_chgr": "+1.23",
                        "prev_base_sign": "3",
                        "prev_base_chgr": "0.00",
                        "dt": "20260619",
                        "tm": "091501",
                        "stk_cd": "005930",
                    }
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )


class FakeMarketRankClient(FakeRealtimeRankClient):
    def get_expected_contract_change_ranking(
        self,
        *,
        market_type: str = "000",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "exp_cntr_flu_rt_upper": [
                    {
                        "stk_cd": "000660",
                        "stk_nm": "SK하이닉스",
                        "exp_cntr_pric": "+320,000",
                        "pred_pre": "+12,000",
                        "flu_rt": "+3.90",
                        "exp_cntr_qty": "80,000",
                        "sel_req": "40,000",
                        "buy_req": "95,000",
                    }
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )

    def get_trade_amount_ranking(
        self,
        *,
        market_type: str = "000",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "trde_prica_upper": [
                    {
                        "now_rank": "1",
                        "stk_cd": "000660_AL",
                        "stk_nm": "SK하이닉스",
                        "cur_prc": "+320,000",
                        "pred_pre": "+12,000",
                        "flu_rt": "+3.90",
                        "now_trde_qty": "1,500,000",
                        "trde_prica": "52000",
                    }
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )

    def get_change_rate_ranking(
        self,
        *,
        market_type: str = "000",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "pred_pre_flu_rt_upper": [
                    {
                        "stk_cd": "000660",
                        "stk_nm": "SK하이닉스",
                        "cur_prc": "+320,000",
                        "pred_pre": "+12,000",
                        "flu_rt": "+3.90",
                        "now_trde_qty": "1,500,000",
                    }
                ],
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )


class FakeSectorDailyClient:
    def __init__(self, *, latest_price: float = 119.0, trade_amount: str = "12000000"):
        self.latest_price = latest_price
        self.trade_amount = trade_amount

    def get_sector_daily_chart(
        self,
        sector_code: str,
        base_date: str,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
        priority=None,
    ) -> ApiPage:
        rows = []
        start = date(2026, 5, 22)
        for index in range(20):
            day = start + timedelta(days=index)
            price = 100.0 + index
            if index == 19:
                price = self.latest_price
            rows.append(
                {
                    "dt": day.strftime("%Y%m%d"),
                    "cur_prc": str(price),
                    "trde_prica": self.trade_amount if index == 19 else "5000000",
                }
            )
        return ApiPage(
            data={
                "inds_dt_pole_chart_qry": rows,
                "return_code": 0,
            },
            continuation=False,
            next_key=None,
        )


class DatabaseTest(unittest.TestCase):
    def test_collect_current_price_saves_raw_and_normalized_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeClient(), database)

            result = collector.collect_current_price("A005930")

            self.assertEqual(result.current_price, 71500)
            self.assertEqual(result.volume, 12345)
            with database.connect() as connection:
                raw_count = connection.execute(
                    "SELECT COUNT(*) FROM api_snapshots"
                ).fetchone()[0]
                row = connection.execute(
                    "SELECT * FROM price_snapshots"
                ).fetchone()
            self.assertEqual(raw_count, 1)
            self.assertEqual(row["stock_code"], "A005930")
            self.assertEqual(row["high_price"], 72000)

    def test_watchlist_add_disable_and_reactivate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            watchlist = WatchlistService(database)

            self.assertEqual(watchlist.add("005930", "삼성전자"), "A005930")
            self.assertEqual(watchlist.active_codes(), ["A005930"])
            self.assertTrue(watchlist.remove("A005930"))
            self.assertEqual(watchlist.active_codes(), [])

            watchlist.add("a005930")
            rows = database.list_watchlist()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stock_name"], "삼성전자")

    def test_invalid_stock_code_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_stock_code("삼성전자")

    def test_alphanumeric_krx_stock_code_is_supported(self) -> None:
        self.assertEqual(normalize_stock_code("0017j0"), "A0017J0")

    def test_daily_candles_are_upserted_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeChartClient(), database)

            first = collector.collect_daily_candles("A005930", "20260610")
            second = collector.collect_daily_candles("A005930", "20260610")

            self.assertEqual(first.received_count, 2)
            self.assertEqual(second.received_count, 2)
            with database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT candle_time, close_price, volume
                    FROM candles
                    WHERE candle_type = 'daily'
                    ORDER BY candle_time
                    """
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["candle_time"], "2026-06-10T00:00:00")
            self.assertEqual(rows[-1]["close_price"], 71500)

    def test_minute_candle_is_normalized_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeChartClient(), database)

            result = collector.collect_minute_candles(
                "A005930",
                "20260610",
                5,
            )

            self.assertEqual(result.received_count, 1)
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM candles WHERE candle_type = 'minute'"
                ).fetchone()
            self.assertEqual(row["interval_minutes"], 5)
            self.assertEqual(row["candle_time"], "2026-06-10T15:30:00")
            self.assertEqual(row["close_price"], 71500)
            self.assertEqual(row["accumulated_volume"], 12345)

    def test_theme_snapshot_is_normalized_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeThemeClient(), database)

            result = collector.collect_theme_snapshot(top_groups=1)

            self.assertEqual(result.group_count, 1)
            self.assertEqual(result.component_count, 1)
            with database.connect() as connection:
                group = connection.execute("SELECT * FROM theme_groups").fetchone()
                component = connection.execute(
                    "SELECT * FROM theme_components"
                ).fetchone()
            self.assertEqual(group["theme_code"], "123")
            self.assertEqual(group["theme_name"], "AI")
            self.assertEqual(group["change_rate"], 5.3)
            self.assertEqual(component["stock_code"], "A005930")
            self.assertEqual(component["current_price"], 72000)
            self.assertEqual(component["accumulated_volume"], 1_000_000)

    def test_stock_theme_snapshot_uses_stock_search_and_saves_components(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            client = FakeThemeClient()
            collector = MarketDataCollector(client, database)

            result = collector.collect_stock_theme_snapshot("A005930")

            self.assertEqual(result.stock_code, "A005930")
            self.assertEqual(result.group_count, 1)
            self.assertEqual(result.component_count, 1)
            self.assertEqual(
                client.theme_group_calls[0],
                {
                    "query_type": "2",
                    "stock_code": "005930",
                    "date_type": "10",
                },
            )
            with database.connect() as connection:
                group = connection.execute("SELECT * FROM theme_groups").fetchone()
                component = connection.execute(
                    "SELECT * FROM theme_components"
                ).fetchone()
            self.assertEqual(group["theme_code"], "123")
            self.assertEqual(component["stock_code"], "A005930")

    def test_realtime_item_ranking_is_normalized_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeRealtimeRankClient(), database)

            result = collector.collect_realtime_item_ranking(query_type="1")

            self.assertEqual(result.received_count, 1)
            self.assertEqual(result.saved_count, 1)
            rows = database.latest_realtime_item_rankings(query_type="1", limit=5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stock_code"], "A005930")
            self.assertEqual(rows[0]["stock_name"], "삼성전자")
            self.assertEqual(rows[0]["big_data_rank"], 1)
            self.assertEqual(rows[0]["rank_change"], -1)
            self.assertEqual(rows[0]["past_current_price"], 72000)
            self.assertEqual(rows[0]["base_compare_change_rate"], 1.23)
            self.assertEqual(rows[0]["observed_at"], "2026-06-19T09:15:01")

    def test_market_rank_snapshot_is_normalized_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeMarketRankClient(), database)

            result = collector.collect_market_ranking("trade_amount")

            self.assertEqual(result.api_id, "ka10032")
            self.assertEqual(result.received_count, 1)
            self.assertEqual(result.saved_count, 1)
            rows = database.latest_market_rank_items(
                rank_type="trade_amount",
                limit=5,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stock_code"], "A000660")
            self.assertEqual(rows[0]["rank"], 1)
            self.assertEqual(rows[0]["trade_amount"], 52_000_000_000)

    def test_kosdaq_market_mode_is_collected_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeSectorDailyClient(), database)

            result = collector.collect_kosdaq_market_mode(base_date="20260610")

            self.assertEqual(result.market, "KOSDAQ")
            self.assertEqual(result.mode, "risk_on")
            self.assertEqual(result.reliability, "reliable")
            self.assertTrue(result.above_ma20)
            self.assertEqual(result.trade_amount, 12_000_000_000_000)
            self.assertIsNotNone(result.moving_average_20)
            latest = database.latest_market_mode_snapshot("KOSDAQ")
            self.assertIsNotNone(latest)
            self.assertEqual(latest["mode"], "risk_on")
            self.assertEqual(latest["source"], "ka20006.kosdaq_daily_chart")

    def test_kosdaq_market_mode_risk_off_when_below_ma(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(
                FakeSectorDailyClient(latest_price=80.0, trade_amount="12000000"),
                database,
            )

            result = collector.collect_kosdaq_market_mode(base_date="20260610")

            self.assertEqual(result.mode, "risk_off")
            self.assertFalse(result.above_ma20)

    def test_auto_market_mode_without_snapshot_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            base = MarketModeGateSettings(
                enabled=True,
                manual_mode="risk_off",
                requested_mode="auto",
                source="auto_unavailable",
            )

            settings = MarketModeService(database).settings_from_base(base)

            self.assertEqual(settings.manual_mode, "risk_off")
            self.assertEqual(settings.source, "auto_unavailable")
            self.assertEqual(settings.order_effect, "fail_closed")
            self.assertEqual(str(settings.quantity_multiplier), "0")

    def test_auto_market_mode_snapshot_defaults_to_shadow_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = MarketDataCollector(FakeSectorDailyClient(), database)
            collector.collect_kosdaq_market_mode(base_date="20260610")
            base = MarketModeGateSettings(
                enabled=True,
                manual_mode="risk_off",
                requested_mode="auto",
                source="auto_unavailable",
            )

            with patch.dict("os.environ", {"MARKET_MODE_AUTO_APPLY_TO_PAPER": "false"}):
                settings = MarketModeService(database).settings_from_base(base)

            self.assertEqual(settings.manual_mode, "neutral")
            self.assertEqual(settings.source, "auto_shadow")
            self.assertEqual(settings.order_effect, "shadow_only")
            self.assertEqual(settings.shadow_mode, "risk_on")

    def test_auto_market_mode_apply_to_paper_uses_mode_policy(self) -> None:
        cases = [
            ("risk_on", FakeSectorDailyClient(), "1.0", "A/B 일부"),
            (
                "neutral",
                FakeSectorDailyClient(latest_price=119.0, trade_amount="7000000"),
                "0.5",
                "A 중심, 강한 B 1주",
            ),
            (
                "risk_off",
                FakeSectorDailyClient(latest_price=119.0, trade_amount="4000000"),
                "0.2",
                "A급 강한 돌파만",
            ),
        ]
        for expected_mode, client, expected_multiplier, expected_grades in cases:
            with self.subTest(mode=expected_mode):
                with tempfile.TemporaryDirectory() as temp_dir:
                    database = Database(Path(temp_dir) / "test.db")
                    database.initialize()
                    collector = MarketDataCollector(client, database)
                    result = collector.collect_kosdaq_market_mode(
                        base_date="20260610"
                    )
                    base = MarketModeGateSettings(
                        enabled=True,
                        manual_mode="risk_off",
                        requested_mode="auto",
                        source="auto_unavailable",
                    )

                    with patch.dict(
                        "os.environ",
                        {"MARKET_MODE_AUTO_APPLY_TO_PAPER": "true"},
                    ):
                        settings = MarketModeService(database).settings_from_base(base)

                    self.assertEqual(result.mode, expected_mode)
                    self.assertEqual(settings.manual_mode, expected_mode)
                    self.assertEqual(settings.source, "auto_kosdaq")
                    self.assertEqual(settings.order_effect, "paper_apply")
                    self.assertEqual(
                        str(settings.quantity_multiplier),
                        expected_multiplier,
                    )
                    self.assertEqual(
                        settings.allowed_entry_grades,
                        expected_grades,
                    )


if __name__ == "__main__":
    unittest.main()
