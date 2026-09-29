import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from db.database import Database
from domain.models import DecisionAction, StrategyDecision
from operations.replay_outcomes import ReplayOutcomeService


class ReplayOutcomeServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.version_id, self.config_id = self.database.register_strategy(
            "breakout",
            "test",
            {"test": True},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_date_batch_uses_representatives_and_defers_unmatured_horizons(
        self,
    ) -> None:
        mature_id = self._save_replay("A005930", "2026-06-10T10:00:00")
        pending_id = self._save_replay("A000660", "2026-06-10T12:00:00")
        self._save_replay("A035420", "2026-06-09T10:00:00")
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE strategy_decisions
                SET occurrence_count = 5,
                    last_seen_at = '2026-06-10T10:20:00'
                WHERE id = ?
                """,
                (mature_id,),
            )
        future = []
        for index in range(1, 25):
            minute = index * 5
            hour = 10 + minute // 60
            clock_minute = minute % 60
            price = 100 + index
            future.append(
                {
                    "candle_time": (
                        f"2026-06-10T{hour:02d}:{clock_minute:02d}:00"
                    ),
                    "open_price": price - 1,
                    "high_price": price + 1,
                    "low_price": price - 2,
                    "close_price": price,
                    "volume": 1_000,
                }
            )
        self.database.save_candles(
            "test",
            "A005930",
            {"future": True},
            future,
            candle_type="minute",
            interval_minutes=5,
        )

        result = ReplayOutcomeService(self.database).recompute(
            date(2026, 6, 10),
            now=datetime(2026, 6, 10, 12, 10),
        )

        self.assertEqual(result["representative_decisions"], 2)
        self.assertEqual(result["represented_occurrences"], 6)
        self.assertEqual(result["saved"], 3)
        self.assertEqual(result["pending"], 3)
        self.assertEqual(result["unavailable"], 0)
        self.assertEqual(result["status"], "pending")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT decision_id, horizon_minutes
                FROM decision_forward_outcomes
                ORDER BY decision_id, horizon_minutes
                """
            ).fetchall()
        self.assertEqual(
            [(int(row["decision_id"]), int(row["horizon_minutes"])) for row in rows],
            [(mature_id, 30), (mature_id, 60), (mature_id, 120)],
        )
        self.assertNotEqual(mature_id, pending_id)

    def _save_replay(self, stock_code: str, evaluated_at: str) -> int:
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code=stock_code,
            decided_at=datetime.fromisoformat(evaluated_at),
            action=DecisionAction.BUY,
            score=Decimal("80"),
            confidence=Decimal("0.8"),
            reason="test replay",
        )
        decision_id = self.database.save_strategy_decision(
            decision,
            account_name=None,
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time=evaluated_at,
        )
        self.database.save_strategy_replay_snapshot(
            decision_id=decision_id,
            stock_code=stock_code,
            evaluated_at=evaluated_at,
            action="buy",
            score=80.0,
            reason="test replay",
            candles=[
                {
                    "candle_type": "minute",
                    "interval_minutes": 5,
                    "candle_time": evaluated_at,
                    "open_price": 100,
                    "high_price": 101,
                    "low_price": 99,
                    "close_price": 100,
                    "volume": 1_000,
                    "relative_position": "before",
                }
            ],
        )
        return decision_id


if __name__ == "__main__":
    unittest.main()
