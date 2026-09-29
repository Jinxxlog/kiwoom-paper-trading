import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from db.database import Database
from main import (
    _candidate_collection_codes,
    _current_price_collection_plan,
    _current_price_priority,
    _market_detail_collection_codes,
    _minute_collection_codes,
    create_periodic_collector,
)
from data.api_budget import ApiCallPriority


class CandidateRuntimeTest(unittest.TestCase):
    @staticmethod
    def _current_price_settings(**overrides: object) -> SimpleNamespace:
        values = {
            "current_price_critical_interval_seconds": 30,
            "current_price_high_interval_seconds": 60,
            "current_price_normal_interval_seconds": 180,
            "current_price_low_interval_seconds": 300,
            "current_price_max_codes_per_cycle": 12,
            "current_price_call_spacing_seconds": 0,
            "current_price_pre_entry_max_age_minutes": 10,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_selected_candidates_and_held_positions_are_both_collected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            timestamp = datetime(2026, 6, 14, 8, 50).isoformat(
                timespec="seconds"
            )
            with database.connect() as connection:
                account_id = connection.execute(
                    """
                    INSERT INTO paper_accounts(
                        name, initial_cash, cash_balance, realized_pnl,
                        created_at, updated_at
                    )
                    VALUES ('default', 1000000, 900000, 0, ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO paper_positions(
                        account_id, stock_code, quantity, cost_basis, updated_at
                    )
                    VALUES (?, 'A005930', 1, 100000, ?)
                    """,
                    (account_id, timestamp),
                )
                run_id = connection.execute(
                    """
                    INSERT INTO candidate_runs(
                        as_of, universe_count, eligible_count, selected_count,
                        config_json, created_at
                    )
                    VALUES (?, 2, 2, 1, '{}', ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO candidate_scores(
                        run_id, stock_code, rank, selected, total_score,
                        technical_score, pattern_score,
                        relative_strength_score, liquidity_score,
                        watched, patterns_json, features_json, reasons_json,
                        evaluated_at
                    )
                    VALUES (?, 'A000660', 1, 1, 50, 20, 15, 5, 10,
                            0, '[]', '{}', '[]', ?)
                    """,
                    (run_id, timestamp),
                )

            codes = _candidate_collection_codes(database, 10)

            self.assertEqual(codes, ["A005930", "A000660"])

    def test_low_score_candidates_are_not_repeated_for_current_price(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            timestamp = datetime(2026, 6, 22, 9, 10).isoformat(
                timespec="seconds"
            )
            with database.connect() as connection:
                run_id = connection.execute(
                    """
                    INSERT INTO candidate_runs(
                        as_of, universe_count, eligible_count, selected_count,
                        config_json, created_at
                    )
                    VALUES (?, 3, 3, 3, '{}', ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                for stock_code, rank, score in (
                    ("A000001", 1, 45),
                    ("A000002", 15, 62),
                    ("A000003", 30, 20),
                ):
                    connection.execute(
                        """
                        INSERT INTO candidate_scores(
                            run_id, stock_code, rank, selected, total_score,
                            technical_score, pattern_score,
                            relative_strength_score, liquidity_score,
                            watched, patterns_json, features_json,
                            reasons_json, evaluated_at
                        )
                        VALUES (?, ?, ?, 1, ?, 0, 0, 0, 0, 0,
                                '[]', '{}', '[]', ?)
                        """,
                        (run_id, stock_code, rank, score, timestamp),
                    )

            codes = _candidate_collection_codes(database, 20)

            self.assertEqual(codes, ["A000001", "A000002"])

    def test_market_rank_candidates_get_high_priority_for_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            timestamp = datetime(2026, 6, 22, 9, 10)
            database.save_market_rank_snapshot(
                rank_type="trade_amount",
                api_id="ka10032",
                market_type="000",
                query_params={},
                payload={},
                items=[
                    {
                        "stock_code": "A122630",
                        "stock_name": "KODEX 레버리지",
                        "rank": 1,
                    },
                    {
                        "stock_code": "A000777",
                        "stock_name": "ranked",
                        "rank": 2,
                    }
                ],
                observed_at=timestamp.isoformat(timespec="seconds"),
                collected_at=timestamp,
            )

            codes = _candidate_collection_codes(database, 20)
            priority = _current_price_priority(database, "A000777")
            product_priority = _current_price_priority(database, "A122630")
            minute_plan = _minute_collection_codes(
                database,
                ["A122630", "A000777", "A999999"],
                max_codes=5,
            )
            detail_plan = _market_detail_collection_codes(
                database,
                ["A122630", "A000777", "A999999"],
                current=timestamp,
                cooldown_until={},
                max_codes=5,
                strict_top_only=True,
            )

            self.assertIn("A000777", codes)
            self.assertNotIn("A122630", codes)
            self.assertEqual(priority, ApiCallPriority.HIGH)
            self.assertEqual(product_priority, ApiCallPriority.LOW)
            self.assertEqual(minute_plan[0]["stock_code"], "A000777")
            self.assertEqual(minute_plan[0]["reason"], "market_rank_top")
            self.assertEqual([item["stock_code"] for item in detail_plan], ["A000777"])

    def test_current_price_plan_prioritizes_held_and_top_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            timestamp = "2026-06-25T09:10:00"
            with database.connect() as connection:
                account_id = connection.execute(
                    """
                    INSERT INTO paper_accounts(
                        name, initial_cash, cash_balance, realized_pnl,
                        created_at, updated_at
                    )
                    VALUES ('default', 1000000, 900000, 0, ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO paper_positions(
                        account_id, stock_code, quantity, cost_basis, updated_at
                    )
                    VALUES (?, 'A000003', 1, 100000, ?)
                    """,
                    (account_id, timestamp),
                )
                run_id = connection.execute(
                    """
                    INSERT INTO candidate_runs(
                        as_of, universe_count, eligible_count, selected_count,
                        config_json, created_at
                    )
                    VALUES (?, 3, 3, 3, '{}', ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                for stock_code, rank, score in (
                    ("A000001", 1, 80),
                    ("A000002", 9, 65),
                    ("A000003", 20, 40),
                ):
                    connection.execute(
                        """
                        INSERT INTO candidate_scores(
                            run_id, stock_code, rank, selected, total_score,
                            technical_score, pattern_score,
                            relative_strength_score, liquidity_score,
                            watched, patterns_json, features_json,
                            reasons_json, evaluated_at
                        )
                        VALUES (?, ?, ?, 1, ?, 0, 0, 0, 0, 0,
                                '[]', '{}', '[]', ?)
                        """,
                        (run_id, stock_code, rank, score, timestamp),
                    )

            plan = _current_price_collection_plan(
                database,
                ["A000002", "A000001", "A000003", "A000001"],
                current=datetime(2026, 6, 25, 9, 10),
                settings=self._current_price_settings(),
            )

            self.assertEqual(
                [item["stock_code"] for item in plan],
                ["A000003", "A000001", "A000002"],
            )
            self.assertEqual(plan[0]["priority"], ApiCallPriority.CRITICAL)
            self.assertEqual(plan[1]["priority"], ApiCallPriority.HIGH)
            self.assertEqual(plan[2]["priority"], ApiCallPriority.NORMAL)

    def test_current_price_plan_skips_fresh_candidates_by_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            timestamp = "2026-06-25T09:10:00"
            with database.connect() as connection:
                run_id = connection.execute(
                    """
                    INSERT INTO candidate_runs(
                        as_of, universe_count, eligible_count, selected_count,
                        config_json, created_at
                    )
                    VALUES (?, 2, 2, 2, '{}', ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                for stock_code, rank, score in (
                    ("A000001", 1, 80),
                    ("A000002", 9, 65),
                ):
                    connection.execute(
                        """
                        INSERT INTO candidate_scores(
                            run_id, stock_code, rank, selected, total_score,
                            technical_score, pattern_score,
                            relative_strength_score, liquidity_score,
                            watched, patterns_json, features_json,
                            reasons_json, evaluated_at
                        )
                        VALUES (?, ?, ?, 1, ?, 0, 0, 0, 0, 0,
                                '[]', '{}', '[]', ?)
                        """,
                        (run_id, stock_code, rank, score, timestamp),
                    )
            database.save_price_snapshot(
                "test",
                "A000001",
                {},
                current_price=1000,
                open_price=1000,
                high_price=1000,
                low_price=1000,
                volume=100,
                collected_at=datetime(2026, 6, 25, 9, 9, 30),
            )
            database.save_price_snapshot(
                "test",
                "A000002",
                {},
                current_price=1000,
                open_price=1000,
                high_price=1000,
                low_price=1000,
                volume=100,
                collected_at=datetime(2026, 6, 25, 9, 8, 0),
            )

            plan = _current_price_collection_plan(
                database,
                ["A000001", "A000002"],
                current=datetime(2026, 6, 25, 9, 10),
                settings=self._current_price_settings(),
            )

            self.assertEqual(plan, [])

    def test_current_price_plan_skips_general_candidates_during_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            timestamp = "2026-06-25T09:10:00"
            with database.connect() as connection:
                run_id = connection.execute(
                    """
                    INSERT INTO candidate_runs(
                        as_of, universe_count, eligible_count, selected_count,
                        config_json, created_at
                    )
                    VALUES (?, 2, 2, 2, '{}', ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                for stock_code, rank, score in (
                    ("A000001", 1, 80),
                    ("A000002", 9, 65),
                ):
                    connection.execute(
                        """
                        INSERT INTO candidate_scores(
                            run_id, stock_code, rank, selected, total_score,
                            technical_score, pattern_score,
                            relative_strength_score, liquidity_score,
                            watched, patterns_json, features_json,
                            reasons_json, evaluated_at
                        )
                        VALUES (?, ?, ?, 1, ?, 0, 0, 0, 0, 0,
                                '[]', '{}', '[]', ?)
                        """,
                        (run_id, stock_code, rank, score, timestamp),
                    )
            database.upsert_api_budget_window(
                api_group="current_price",
                api_id="ka10006",
                endpoint="/api/dostk/mrkcond",
                window_started_at=timestamp,
                window_seconds=60,
                limit_count=600,
                used_count=10,
                backoff_until="2026-06-25T09:11:00",
                updated_at=timestamp,
            )

            plan = _current_price_collection_plan(
                database,
                ["A000001", "A000002"],
                current=datetime(2026, 6, 25, 9, 10, 30),
                settings=self._current_price_settings(),
            )

            self.assertEqual(
                [item["stock_code"] for item in plan],
                ["A000001"],
            )
            self.assertTrue(plan[0]["backoff_active"])

    def test_candidate_mode_collects_themes_on_preopen_and_intraday(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = FakeThemeCollector()
            tracker = FakeOperationsTracker()
            candidate_settings = SimpleNamespace(
                theme_collection_interval_minutes=15,
                theme_collection_top_groups=7,
                realtime_rank_query_type="1",
                realtime_rank_collection_interval_minutes=5,
                realtime_rank_max_pages=1,
            )

            with (
                patch("main.create_market_collector", return_value=collector),
                patch("main.create_risk_manager", return_value=FakeRiskManager()),
                patch("main.create_breakout_strategy", return_value=FakeStrategy()),
                patch("main.IntradayVwapService", return_value=FakeIntradayVwap()),
                patch(
                    "main.MarketCalendar",
                    return_value=SimpleNamespace(is_market_open=lambda: True),
                ),
                patch("main._candidate_collection_codes", return_value=["A005930"]),
                patch("main.load_candidate_settings", return_value=candidate_settings),
                patch(
                    "main.load_market_data_settings",
                    return_value=SimpleNamespace(detail_collection_interval_minutes=5),
                ),
                patch(
                    "main.load_strategy_settings",
                    return_value=SimpleNamespace(minute_interval=5),
                ),
                patch(
                    "main.load_information_settings",
                    return_value=SimpleNamespace(
                        naver_client_id="",
                        naver_client_secret="",
                        dart_api_key="",
                    ),
                ),
                patch(
                    "main.load_ai_shadow_settings",
                    return_value=SimpleNamespace(enabled=False, interval_minutes=30),
                ),
                patch("main.CandidateScreeningEngine", FakeCandidateEngine),
                patch("main.SupplyScoringEngine", FakeSupplyScoringEngine),
                patch("main.LeaderStockScoringEngine", FakeLeaderScoringEngine),
            ):
                periodic = create_periodic_collector(
                    database,
                    operations_tracker=tracker,
                    candidate_mode=True,
                    candidate_limit=10,
                    preopen_scan=True,
                )

                periodic.pre_market_callback()
                periodic.collect_once()

            self.assertEqual(collector.theme_top_groups, [7, 7])
            self.assertEqual(collector.market_rank_types, ["trade_amount"])
            self.assertEqual(collector.realtime_rank_query_types, ["1"])
            self.assertIn(
                ("preopen", "preopen_candidate_scan_completed"),
                tracker.events,
            )
            self.assertIn(
                ("theme", "theme_candidate_scan_completed"),
                tracker.events,
            )
            self.assertIn(
                ("realtime_rank", "realtime_rank_collected"),
                tracker.events,
            )
            self.assertIn(
                ("market_rank", "trade_amount_rank_collected"),
                tracker.events,
            )
            self.assertIn(
                ("leader_supply", "leader_supply_scores_updated"),
                tracker.events,
            )
            self.assertEqual(collector.supply_calls, ["A005930"])

    def test_market_detail_rate_limit_aborts_cycle_and_cools_down_stock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            collector = FakeRateLimitedDetailCollector()
            tracker = FakeOperationsTracker()

            with (
                patch("main.create_market_collector", return_value=collector),
                patch("main.create_risk_manager", return_value=FakeRiskManager()),
                patch("main.create_breakout_strategy", return_value=FakeStrategy()),
                patch("main.IntradayVwapService", return_value=FakeIntradayVwap()),
                patch(
                    "main.MarketCalendar",
                    return_value=SimpleNamespace(is_market_open=lambda: True),
                ),
                patch(
                    "main.load_market_data_settings",
                    return_value=SimpleNamespace(
                        detail_collection_interval_minutes=5,
                        detail_collection_max_codes=2,
                        detail_failure_cooldown_minutes=15,
                        detail_abort_on_rate_limit=True,
                    ),
                ),
                patch(
                    "main.load_ai_shadow_settings",
                    return_value=SimpleNamespace(enabled=False, interval_minutes=30),
                ),
                patch("main.logging.exception"),
            ):
                periodic = create_periodic_collector(
                    database,
                    stock_codes=["A000001", "A000002", "A000003"],
                    with_depth=True,
                    with_trades=True,
                    operations_tracker=tracker,
                )

                periodic.collect_once()
                periodic.collect_once()

            self.assertEqual(
                collector.order_book_calls,
                ["A000001", "A000002", "A000003"],
            )
            self.assertEqual(collector.trade_strength_calls, ["A000002", "A000003"])
            self.assertIn(
                ("market_detail", "market_detail_rate_limited"),
                tracker.events,
            )
            self.assertIn(
                ("market_detail", "market_detail_collection_planned"),
                tracker.events,
            )

    def test_market_detail_codes_prioritize_held_leaders_and_top_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            timestamp = datetime(2026, 6, 22, 9, 15).isoformat(
                timespec="seconds"
            )
            with database.connect() as connection:
                account_id = connection.execute(
                    """
                    INSERT INTO paper_accounts(
                        name, initial_cash, cash_balance, realized_pnl,
                        created_at, updated_at
                    )
                    VALUES ('default', 1000000, 800000, 0, ?, ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO paper_positions(
                        account_id, stock_code, quantity, cost_basis, updated_at
                    )
                    VALUES (?, 'A000003', 1, 100000, ?)
                    """,
                    (account_id, timestamp),
                )
                run_id = connection.execute(
                    """
                    INSERT INTO candidate_runs(
                        as_of, universe_count, eligible_count, selected_count,
                        config_json, created_at
                    )
                    VALUES (?, 4, 4, 4, '{}', ?)
                    """,
                    (timestamp, timestamp),
                ).lastrowid
                for stock_code, rank, score in (
                    ("A000001", 1, 72),
                    ("A000002", 9, 65),
                    ("A000003", 20, 40),
                    ("A000004", 30, 20),
                ):
                    connection.execute(
                        """
                        INSERT INTO candidate_scores(
                            run_id, stock_code, rank, selected, total_score,
                            technical_score, pattern_score,
                            relative_strength_score, liquidity_score,
                            watched, patterns_json, features_json,
                            reasons_json, evaluated_at
                        )
                        VALUES (?, ?, ?, 1, ?, 0, 0, 0, 0, 0,
                                '[]', '{}', '[]', ?)
                        """,
                        (run_id, stock_code, rank, score, timestamp),
                    )
                connection.execute(
                    """
                    INSERT INTO leader_stock_scores(
                        scored_at, trade_date, stock_code, stock_name,
                        theme_code, theme_name, role, total_score,
                        rank_score, trade_amount_score, theme_score,
                        persistence_score, chart_score, risk_score,
                        blocked, blocked_reason, reasons_json,
                        components_json, created_at
                    )
                    VALUES (?, '2026-06-22', 'A000002', '리더후보',
                            NULL, '테마', 'leader', 88, 0, 0, 0, 0, 0, 0,
                            0, NULL, '[]', '{}', ?)
                    """,
                    (timestamp, timestamp),
                )

            plan = _market_detail_collection_codes(
                database,
                ["A000004", "A000001", "A000002", "A000003"],
                current=datetime(2026, 6, 22, 9, 15),
                cooldown_until={},
                max_codes=3,
            )

            self.assertEqual(
                [item["stock_code"] for item in plan],
                ["A000003", "A000002", "A000001"],
            )
            self.assertEqual(plan[0]["reason"], "held_position")
            self.assertEqual(plan[1]["reason"], "leader_role:leader")
            self.assertEqual(plan[2]["reason"], "top_candidate")


class FakeThemeCollector:
    def __init__(self) -> None:
        self.theme_top_groups: list[int] = []
        self.market_rank_types: list[str] = []
        self.realtime_rank_query_types: list[str] = []
        self.supply_calls: list[str] = []

    def collect_daily_candles(self, stock_code, base_date, *, max_pages=1):
        return SimpleNamespace(saved_count=0)

    def collect_current_price(self, stock_code: str) -> object:
        return SimpleNamespace(
            stock_code=stock_code,
            current_price=1000,
            volume=100,
            database_id=1,
        )

    def collect_minute_candles(
        self,
        stock_code: str,
        base_date: str,
        interval_minutes: int,
        *,
        max_pages: int = 1,
    ) -> object:
        return SimpleNamespace()

    def collect_theme_snapshot(self, *, top_groups: int = 10) -> object:
        self.theme_top_groups.append(top_groups)
        return SimpleNamespace(
            group_count=top_groups,
            saved_group_count=top_groups,
            component_count=20,
            saved_component_count=20,
            page_count=1,
        )

    def collect_realtime_item_ranking(
        self,
        *,
        query_type: str = "1",
        max_pages: int = 1,
    ) -> object:
        self.realtime_rank_query_types.append(query_type)
        return SimpleNamespace(
            query_type=query_type,
            received_count=2,
            saved_count=2,
            page_count=max_pages,
        )

    def collect_market_ranking(
        self,
        rank_type: str,
        *,
        market_type: str = "000",
        max_pages: int = 1,
    ) -> object:
        self.market_rank_types.append(rank_type)
        return SimpleNamespace(
            rank_type=rank_type,
            api_id="ka10032",
            received_count=2,
            saved_count=2,
            page_count=max_pages,
        )

    def collect_supply_sources(
        self,
        stock_code: str,
        *,
        start_date: str,
        end_date: str,
        max_pages: int = 1,
        include_foreign_rank: bool = True,
        **kwargs: object,
    ) -> object:
        del start_date, end_date, max_pages, include_foreign_rank, kwargs
        self.supply_calls.append(stock_code)
        return SimpleNamespace(stock_code=stock_code)


class FakeRateLimitedDetailCollector:
    def __init__(self) -> None:
        self.order_book_calls: list[str] = []
        self.trade_strength_calls: list[str] = []

    def collect_current_price(self, stock_code: str) -> object:
        return SimpleNamespace(
            stock_code=stock_code,
            current_price=1000,
            volume=100,
            database_id=1,
        )

    def collect_order_book(self, stock_code: str) -> object:
        self.order_book_calls.append(stock_code)
        if stock_code == "A000001":
            raise RuntimeError("429 Client Error: Too Many Requests")
        return SimpleNamespace(database_id=len(self.order_book_calls), level_count=10)

    def collect_trade_strength(
        self,
        stock_code: str,
        *,
        max_pages: int = 1,
    ) -> object:
        self.trade_strength_calls.append(stock_code)
        return SimpleNamespace(saved_count=1, page_count=max_pages)


class FakeRiskManager:
    def liquidate_market_close(self) -> list[object]:
        return []

    def evaluate_all_accounts(self) -> list[object]:
        return []


class FakeStrategy:
    def run(self, account_name: str, codes: list[str], *, execute: bool) -> list[dict]:
        return []


class FakeIntradayVwap:
    def evaluate(self, stock_code: str, *, interval_minutes: int) -> object:
        return SimpleNamespace()


class FakeOperationsTracker:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def record(
        self,
        component: str,
        event_type: str,
        severity: str,
        message: str,
        **kwargs: object,
    ) -> None:
        self.events.append((component, event_type))

    def handle(self, event_type: str, payload: dict[str, object]) -> None:
        pass


class FakeCandidateEngine:
    def __init__(self, database: Database, settings: object) -> None:
        pass

    def scan(self, *, limit: int | None = None) -> object:
        return SimpleNamespace(
            run_id=1,
            universe_count=1,
            eligible_count=1,
            selected_count=1,
        )


class FakeSupplyScoringEngine:
    def __init__(self, database: Database) -> None:
        pass

    def score(self, stock_codes: list[str]) -> object:
        return SimpleNamespace(
            evaluated_at="2026-06-22T09:10:00",
            scored_count=len(stock_codes),
            saved_count=len(stock_codes),
        )


class FakeLeaderScoringEngine:
    def __init__(self, database: Database) -> None:
        pass

    def score(self, *, trade_date: object = None, limit: int = 50) -> object:
        return SimpleNamespace(
            trade_date=str(trade_date),
            scored_count=limit,
            saved_count=limit,
        )


if __name__ == "__main__":
    unittest.main()
