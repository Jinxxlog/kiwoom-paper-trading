import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from db.database import Database
from screening.leader import LeaderStockScoringEngine


class LeaderStockScoringEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.engine = LeaderStockScoringEngine(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_repeated_rankings_and_strong_theme_promote_leader(self) -> None:
        self._save_rank(
            "trade_amount",
            "ka10032",
            "A000660",
            "SK하이닉스",
            rank=1,
            trade_amount=80_000_000_000,
            change_rate=8.5,
        )
        self._save_rank("realtime_inquiry", "ka00198", "A000660", "SK하이닉스", rank=2)
        self._save_rank("expected_change", "ka10029", "A000660", "SK하이닉스", rank=3)
        self._save_rank("change_rate", "ka10027", "A000660", "SK하이닉스", rank=4)
        self.database.save_theme_groups(
            {"test": True},
            [
                {
                    "theme_code": "101",
                    "theme_name": "반도체",
                    "stock_count": 12,
                    "change_rate": 5.0,
                    "rising_stock_count": 10,
                    "falling_stock_count": 1,
                    "day_profit_rate": 4.5,
                    "main_stock": "SK하이닉스",
                }
            ],
            collected_at=datetime(2026, 6, 19, 9, 5),
        )
        self.database.save_theme_components(
            "101",
            {"test": True},
            [
                {
                    "stock_code": "A000660",
                    "stock_name": "SK하이닉스",
                    "current_price": 320_000,
                    "change_rate": 8.5,
                    "accumulated_volume": 1_000_000,
                }
            ],
            collected_at=datetime(2026, 6, 19, 9, 5),
        )
        self.database.upsert_theme_journal(
            theme_date="2026-06-19",
            theme_name="반도체",
            strength="strong",
            members=[
                {
                    "stock_code": "A000660",
                    "stock_name": "SK하이닉스",
                    "role": "leader",
                }
            ],
        )

        result = self.engine.score(trade_date=date(2026, 6, 19), limit=10)

        self.assertEqual(result.saved_count, 1)
        row = self.database.latest_leader_stock_scores(
            trade_date="2026-06-19",
            limit=1,
        )[0]
        self.assertEqual(row["stock_code"], "A000660")
        self.assertEqual(row["role"], "leader")
        self.assertGreaterEqual(row["total_score"], 85)
        self.assertGreater(row["trade_amount_score"], 0)

    def test_stock_without_trade_amount_rank_cannot_be_leader(self) -> None:
        self._save_rank("realtime_inquiry", "ka00198", "A005930", "삼성전자", rank=1)
        self._save_rank("expected_change", "ka10029", "A005930", "삼성전자", rank=1)
        self._save_rank("change_rate", "ka10027", "A005930", "삼성전자", rank=1)
        self.database.upsert_theme_journal(
            theme_date="2026-06-19",
            theme_name="AI",
            strength="strong",
            members=[
                {
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "role": "leader",
                }
            ],
        )

        self.engine.score(trade_date=date(2026, 6, 19), limit=10)

        row = self.database.latest_leader_stock_scores(
            trade_date="2026-06-19",
            limit=1,
        )[0]
        self.assertNotEqual(row["role"], "leader")
        self.assertEqual(row["blocked_reason"], "거래대금 상위권 아님")

    def test_overheated_trade_amount_leader_is_blocked(self) -> None:
        self._save_rank(
            "trade_amount",
            "ka10032",
            "A123456",
            "과열종목",
            rank=1,
            trade_amount=120_000_000_000,
            change_rate=30.0,
        )

        self.engine.score(trade_date=date(2026, 6, 19), limit=10)

        row = self.database.latest_leader_stock_scores(
            trade_date="2026-06-19",
            limit=1,
        )[0]
        self.assertEqual(row["role"], "blocked")
        self.assertEqual(row["blocked_reason"], "고점 클라이맥스 의심")

    def test_market_product_rank_item_is_excluded_from_leader_scoring(self) -> None:
        self._save_rank(
            "trade_amount",
            "ka10032",
            "A122630",
            "KODEX 레버리지",
            rank=1,
            trade_amount=200_000_000_000,
            change_rate=9.0,
        )
        self._save_rank(
            "trade_amount",
            "ka10032",
            "A000660",
            "SK하이닉스",
            rank=2,
            trade_amount=120_000_000_000,
            change_rate=6.0,
        )

        result = self.engine.score(trade_date=date(2026, 6, 19), limit=10)

        self.assertEqual(result.scored_count, 1)
        rows = self.database.latest_leader_stock_scores(
            trade_date="2026-06-19",
            limit=10,
        )
        self.assertEqual([row["stock_code"] for row in rows], ["A000660"])

    def test_supply_block_prevents_trade_amount_leader(self) -> None:
        self._save_rank(
            "trade_amount",
            "ka10032",
            "A000660",
            "SK하이닉스",
            rank=1,
            trade_amount=100_000_000_000,
            change_rate=7.0,
        )
        self.database.save_supply_score(
            {
                "stock_code": "A000660",
                "stock_name": "SK하이닉스",
                "evaluated_at": "2026-06-19T10:00:00",
                "total_score": 20.0,
                "broker_score": -60.0,
                "foreign_broker_score": -45.0,
                "investor_score": -50.0,
                "program_score": 0.0,
                "blocked": True,
                "blocked_reason": "상위 거래원 매도 압력",
                "reasons": ["상위 거래원 매도 우위"],
                "components": {"broker": {"pressure": -0.8}},
            }
        )

        self.engine.score(trade_date=date(2026, 6, 19), limit=10)

        row = self.database.latest_leader_stock_scores(
            trade_date="2026-06-19",
            limit=1,
        )[0]
        components = json.loads(row["components_json"])
        self.assertEqual(row["role"], "blocked")
        self.assertEqual(row["blocked_reason"], "상위 거래원 매도 압력")
        self.assertTrue(components["supply"]["blocked"])
        self.assertLess(components["supply_score"], 0)

    def test_latest_scores_dedupe_date_stock_without_losing_history(self) -> None:
        self.database.save_leader_stock_scores(
            [
                _leader_score_row(
                    "A000660",
                    "SK하이닉스",
                    scored_at="2026-06-19T09:00:00",
                    role="blocked",
                    total_score=40.0,
                    theme_name="반도체",
                    blocked=True,
                    blocked_reason="초기 차단",
                ),
                _leader_score_row(
                    "A000660",
                    "SK하이닉스",
                    scored_at="2026-06-19T09:10:00",
                    role="leader",
                    total_score=92.0,
                    theme_name="AI",
                    blocked=False,
                    blocked_reason=None,
                ),
            ]
        )

        rows = self.database.latest_leader_stock_scores(
            trade_date="2026-06-19",
            limit=10,
        )
        with self.database.connect() as connection:
            raw_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM leader_stock_scores
                WHERE trade_date = '2026-06-19'
                  AND stock_code = 'A000660'
                """
            ).fetchone()["count"]

        self.assertEqual(raw_count, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stock_code"], "A000660")
        self.assertEqual(rows[0]["role"], "leader")
        self.assertEqual(rows[0]["theme_name"], "AI")
        self.assertEqual(rows[0]["total_score"], 92.0)

    def _save_rank(
        self,
        rank_type: str,
        api_id: str,
        stock_code: str,
        stock_name: str,
        *,
        rank: int,
        trade_amount: int | None = None,
        change_rate: float | None = None,
    ) -> None:
        self.database.save_market_rank_snapshot(
            rank_type=rank_type,
            api_id=api_id,
            market_type="000",
            query_params={},
            payload={"test": True},
            items=[
                {
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "rank": rank,
                    "trade_amount": trade_amount,
                    "change_rate": change_rate,
                }
            ],
            collected_at=datetime(2026, 6, 19, 9, rank),
        )


def _leader_score_row(
    stock_code: str,
    stock_name: str,
    *,
    scored_at: str,
    role: str,
    total_score: float,
    theme_name: str,
    blocked: bool,
    blocked_reason: str | None,
) -> dict:
    return {
        "scored_at": scored_at,
        "trade_date": "2026-06-19",
        "stock_code": stock_code,
        "stock_name": stock_name,
        "theme_code": "101",
        "theme_name": theme_name,
        "role": role,
        "total_score": total_score,
        "rank_score": 20.0,
        "trade_amount_score": 35.0,
        "theme_score": 15.0,
        "persistence_score": 5.0,
        "chart_score": 10.0,
        "risk_score": -20.0 if blocked else 0.0,
        "blocked": blocked,
        "blocked_reason": blocked_reason,
        "reasons": ["test"],
        "components": {"test": True},
    }


if __name__ == "__main__":
    unittest.main()
