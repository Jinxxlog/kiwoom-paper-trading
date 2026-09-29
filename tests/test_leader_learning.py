import tempfile
import unittest
from datetime import date
from pathlib import Path

from db.database import Database
from screening.leader_learning import (
    LeaderOutcomeLearningEngine,
    LeaderOutcomeReportService,
)


class LeaderOutcomeLearningEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.engine = LeaderOutcomeLearningEngine(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_top_candidates_are_saved_with_same_and_next_day_outcomes(self) -> None:
        self.database.save_leader_stock_scores(
            [
                _leader_score(
                    "A005930",
                    "삼성전자",
                    total_score=95,
                    theme_name="반도체",
                    current_price=100,
                ),
                _leader_score(
                    "A000660",
                    "SK하이닉스",
                    total_score=80,
                    theme_name="반도체",
                    current_price=200,
                ),
            ]
        )
        self.database.save_candles(
            "test",
            "A005930",
            {"daily": True},
            [
                _daily("2026-06-19T00:00:00", 99, 107, 98, 104, 10_000),
                _daily("2026-06-22T00:00:00", 104, 108, 102, 106, 9_000),
            ],
            candle_type="daily",
        )
        self.database.save_candles(
            "test",
            "A000660",
            {"daily": True},
            [
                _daily("2026-06-19T00:00:00", 198, 202, 197, 199, 8_000),
                _daily("2026-06-22T00:00:00", 199, 204, 198, 203, 7_000),
            ],
            candle_type="daily",
        )

        result = self.engine.evaluate(trade_date=date(2026, 6, 19), limit=20)
        outcomes = self.database.latest_leader_candidate_outcomes(
            trade_date="2026-06-19",
            limit=20,
        )

        self.assertEqual(result.evaluated_count, 2)
        self.assertEqual(result.actual_leader_count, 1)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0]["candidate_rank"], 1)
        self.assertTrue(outcomes[0]["actual_leader"])
        self.assertEqual(outcomes[0]["actual_leader_reason"], "TOP20 성과 1위")
        self.assertAlmostEqual(outcomes[0]["same_day_max_up_rate"], 0.07)
        self.assertAlmostEqual(outcomes[0]["same_day_close_rate"], 0.04)
        self.assertEqual(outcomes[0]["next_day_date"], "2026-06-22")
        self.assertEqual(
            outcomes[0]["components"]["leader_components"]["ranks"][
                "trade_amount"
            ]["current_price"],
            100,
        )

    def test_reference_price_falls_back_to_same_day_open(self) -> None:
        self.database.save_leader_stock_scores(
            [
                _leader_score(
                    "A035420",
                    "NAVER",
                    total_score=88,
                    theme_name="AI",
                    current_price=None,
                ),
            ]
        )
        self.database.save_candles(
            "test",
            "A035420",
            {"daily": True},
            [_daily("2026-06-19T00:00:00", 50, 52, 49, 51, 5_000)],
            candle_type="daily",
        )

        self.engine.evaluate(trade_date=date(2026, 6, 19), limit=20)
        outcome = self.database.latest_leader_candidate_outcomes(
            trade_date="2026-06-19",
            limit=1,
        )[0]

        self.assertEqual(outcome["same_day_reference_price"], 50)
        self.assertAlmostEqual(outcome["same_day_max_up_rate"], 0.04)
        self.assertTrue(outcome["actual_leader"])

    def test_report_groups_theme_component_and_blocked_outcomes(self) -> None:
        self.database.save_leader_candidate_outcomes(
            [
                _outcome(
                    "A005930",
                    "삼성전자",
                    theme_name="반도체",
                    rank=1,
                    actual=True,
                    same_day_max=0.07,
                    next_day_max=0.08,
                    trade_amount_score=35,
                    theme_score=20,
                    blocked=False,
                ),
                _outcome(
                    "A000660",
                    "SK하이닉스",
                    theme_name="반도체",
                    rank=2,
                    actual=False,
                    same_day_max=0.01,
                    next_day_max=0.02,
                    trade_amount_score=30,
                    theme_score=18,
                    blocked=True,
                    blocked_reason="고점 클라이맥스 의심",
                ),
                _outcome(
                    "A035420",
                    "NAVER",
                    theme_name="AI",
                    rank=3,
                    actual=True,
                    same_day_max=0.06,
                    next_day_max=0.01,
                    trade_amount_score=20,
                    theme_score=25,
                    blocked=True,
                    blocked_reason="수급 이탈",
                ),
            ]
        )

        report = LeaderOutcomeReportService(self.database).report(
            trade_date=date(2026, 6, 19),
            limit=10,
        )

        self.assertEqual(report["summary"]["candidate_count"], 3)
        self.assertEqual(report["summary"]["actual_leader_count"], 2)
        self.assertEqual(report["summary"]["blocked_helpful_count"], 1)
        self.assertEqual(report["summary"]["blocked_missed_rally_count"], 1)
        theme = {row["theme_name"]: row for row in report["by_theme"]}
        self.assertEqual(theme["반도체"]["candidate_count"], 2)
        self.assertEqual(theme["반도체"]["actual_leader_count"], 1)
        self.assertEqual(theme["AI"]["hit_rate"], 1.0)
        components = {
            row["component"]: row for row in report["component_performance"]
        }
        self.assertEqual(components["theme_score"]["candidate_count"], 3)
        self.assertGreater(
            components["theme_score"]["average_performance_score"],
            0.0,
        )
        blocked = report["blocked_review"]
        self.assertEqual(blocked["candidate_count"], 2)
        self.assertEqual(blocked["helpful_count"], 1)
        self.assertEqual(blocked["missed_rally_count"], 1)
        reasons = {row["reason"]: row for row in blocked["by_reason"]}
        self.assertEqual(reasons["수급 이탈"]["missed_rally_count"], 1)
        self.assertEqual(reasons["고점 클라이맥스 의심"]["helpful_count"], 1)

    def test_report_splits_leader_candidates_by_stock_category(self) -> None:
        self.database.save_leader_candidate_outcomes(
            [
                _outcome(
                    "A005930",
                    "삼성전자",
                    theme_name="반도체",
                    rank=1,
                    actual=True,
                    same_day_max=0.07,
                    next_day_max=0.08,
                    trade_amount_score=35,
                    theme_score=20,
                    blocked=False,
                ),
                _outcome(
                    "A0039P0",
                    "테스트특수",
                    theme_name="스팩",
                    rank=2,
                    actual=True,
                    same_day_max=0.12,
                    next_day_max=0.02,
                    trade_amount_score=30,
                    theme_score=12,
                    blocked=True,
                    blocked_reason="특수코드 차단",
                ),
                _outcome(
                    "A122630",
                    "KODEX 레버리지",
                    theme_name="ETF",
                    rank=3,
                    actual=False,
                    same_day_max=0.03,
                    next_day_max=0.01,
                    trade_amount_score=25,
                    theme_score=8,
                    blocked=False,
                ),
            ]
        )

        report = LeaderOutcomeReportService(self.database).report(
            trade_date=date(2026, 6, 19),
            limit=10,
        )

        by_category = {
            row["category"]: row for row in report["by_stock_category"]
        }
        self.assertEqual(by_category["common_stock"]["candidate_count"], 1)
        self.assertEqual(by_category["common_stock"]["actual_leader_count"], 1)
        self.assertEqual(
            by_category["preferred_or_special"]["candidate_count"],
            1,
        )
        self.assertEqual(
            by_category["preferred_or_special"]["blocked_missed_rally_count"],
            1,
        )
        self.assertEqual(by_category["market_product"]["candidate_count"], 1)
        self.assertEqual(
            next(
                row for row in report["rows"] if row["stock_code"] == "A0039P0"
            )["stock_category"]["label"],
            "우선주/특수코드",
        )


def _leader_score(
    stock_code: str,
    stock_name: str,
    *,
    total_score: float,
    theme_name: str,
    current_price: int | None,
) -> dict:
    ranks = {"trade_amount": {"rank": 1, "trade_amount": 60_000_000_000}}
    if current_price is not None:
        ranks["trade_amount"]["current_price"] = current_price
    return {
        "trade_date": "2026-06-19",
        "stock_code": stock_code,
        "stock_name": stock_name,
        "theme_code": "101",
        "theme_name": theme_name,
        "role": "leader",
        "total_score": total_score,
        "rank_score": 20.0,
        "trade_amount_score": 35.0,
        "theme_score": 20.0,
        "persistence_score": 5.0,
        "chart_score": 10.0,
        "risk_score": 0.0,
        "blocked": False,
        "blocked_reason": None,
        "reasons": ["테스트"],
        "components": {"ranks": ranks},
    }


def _daily(
    candle_time: str,
    open_price: int,
    high_price: int,
    low_price: int,
    close_price: int,
    volume: int,
) -> dict:
    return {
        "candle_time": candle_time,
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "close_price": close_price,
        "volume": volume,
    }


def _outcome(
    stock_code: str,
    stock_name: str,
    *,
    theme_name: str,
    rank: int,
    actual: bool,
    same_day_max: float,
    next_day_max: float,
    trade_amount_score: float,
    theme_score: float,
    blocked: bool,
    blocked_reason: str | None = None,
) -> dict:
    return {
        "trade_date": "2026-06-19",
        "stock_code": stock_code,
        "stock_name": stock_name,
        "candidate_rank": rank,
        "role_at_pick": "blocked" if blocked else "leader",
        "score_at_pick": 90.0 - rank,
        "theme_code": "101",
        "theme_name": theme_name,
        "components": {
            "trade_amount_score": trade_amount_score,
            "rank_score": 12.0,
            "theme_score": theme_score,
            "persistence_score": 3.0,
            "chart_score": 8.0,
            "risk_score": -20.0 if blocked else 0.0,
            "blocked": blocked,
            "blocked_reason": blocked_reason,
        },
        "same_day_reference_price": 100,
        "same_day_open_price": 100,
        "same_day_high_price": int(100 * (1 + same_day_max)),
        "same_day_low_price": 98,
        "same_day_close_price": 101,
        "same_day_volume": 1000,
        "same_day_max_up_rate": same_day_max,
        "same_day_close_rate": 0.01,
        "next_day_date": "2026-06-22",
        "next_day_open_price": 101,
        "next_day_high_price": int(100 * (1 + next_day_max)),
        "next_day_low_price": 99,
        "next_day_close_price": 102,
        "next_day_volume": 900,
        "next_day_max_up_rate": next_day_max,
        "next_day_close_rate": 0.02,
        "actual_leader": actual,
        "actual_leader_rank": rank,
        "actual_leader_reason": "테스트",
        "outcome": {"performance_score": max(same_day_max, next_day_max)},
    }
