import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from db.database import Database
from domain.models import DecisionAction, StrategyDecision


class StrategyDecisionCompactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.version_id, self.config_id = self.database.register_strategy(
            "decision_compaction_test",
            "1.0.0",
            {"mode": "test"},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _decision(
        self,
        *,
        decided_at: str,
        action: DecisionAction = DecisionAction.HOLD,
        reason: str = "기본 돌파 전략 조건 미충족",
        score: str = "40",
    ) -> StrategyDecision:
        return StrategyDecision(
            strategy_name="decision_compaction_test",
            stock_code="A005930",
            decided_at=datetime.fromisoformat(decided_at),
            action=action,
            score=Decimal(score),
            confidence=Decimal("1"),
            reason=reason,
        )

    def test_exact_duplicate_reuses_one_decision(self) -> None:
        decision = self._decision(decided_at="2026-06-25T10:00:00")

        first_id, first_created = self.database.save_strategy_decision_record(
            decision,
            account_name=None,
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time="2026-06-25T10:00:00",
        )
        second_id, second_created = self.database.save_strategy_decision_record(
            decision,
            account_name=None,
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time="2026-06-25T10:00:00",
        )

        self.assertEqual(first_id, second_id)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, MAX(occurrence_count) AS occurrences "
                "FROM strategy_decisions"
            ).fetchone()
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["occurrences"], 2)

    def test_unchanged_hold_reuses_representative_until_state_changes(self) -> None:
        first = self._decision(decided_at="2026-06-25T10:00:00")
        repeated = self._decision(
            decided_at="2026-06-25T10:05:00",
            score="42",
        )
        changed = self._decision(
            decided_at="2026-06-25T10:10:00",
            reason="VWAP 상단 관찰",
            score="48",
        )

        first_id, _ = self.database.save_strategy_decision_record(
            first,
            account_name=None,
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time="2026-06-25T10:00:00",
            collapse_unchanged=True,
            state_key="breakout:hold:conditions_not_met",
        )
        repeated_id, repeated_created = (
            self.database.save_strategy_decision_record(
                repeated,
                account_name=None,
                strategy_version_id=self.version_id,
                strategy_config_id=self.config_id,
                source_data_time="2026-06-25T10:05:00",
                collapse_unchanged=True,
                state_key="breakout:hold:conditions_not_met",
            )
        )
        changed_id, changed_created = self.database.save_strategy_decision_record(
            changed,
            account_name=None,
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time="2026-06-25T10:10:00",
            collapse_unchanged=True,
            state_key="breakout:hold:vwap_watch",
        )

        self.assertEqual(first_id, repeated_id)
        self.assertFalse(repeated_created)
        self.assertNotEqual(first_id, changed_id)
        self.assertTrue(changed_created)

    def test_buy_and_blocked_events_remain_separate(self) -> None:
        for action, reason in (
            (DecisionAction.BUY, "주문 후보"),
            (DecisionAction.HOLD, "차트 위험 패턴 차단: VWAP 회복 실패"),
        ):
            first_id, _ = self.database.save_strategy_decision_record(
                self._decision(
                    decided_at="2026-06-25T10:00:00",
                    action=action,
                    reason=reason,
                    score="75" if action is DecisionAction.BUY else "30",
                ),
                account_name=None,
                strategy_version_id=self.version_id,
                strategy_config_id=self.config_id,
                source_data_time="2026-06-25T10:00:00",
                collapse_unchanged=False,
            )
            second_id, second_created = (
                self.database.save_strategy_decision_record(
                    self._decision(
                        decided_at="2026-06-25T10:05:00",
                        action=action,
                        reason=reason,
                        score="75" if action is DecisionAction.BUY else "30",
                    ),
                    account_name=None,
                    strategy_version_id=self.version_id,
                    strategy_config_id=self.config_id,
                    source_data_time="2026-06-25T10:05:00",
                    collapse_unchanged=False,
                )
            )
            self.assertNotEqual(first_id, second_id)
            self.assertTrue(second_created)

    def test_shadow_decisions_only_create_rows_on_status_change(self) -> None:
        first = self._decision(
            decided_at="2026-06-25T10:00:00",
            reason="종합 18.97점: 기준 미달",
            score="18.97",
        )
        repeated = self._decision(
            decided_at="2026-06-25T10:05:00",
            reason="종합 22.17점: 기준 미달",
            score="22.17",
        )
        first_ids = self.database.save_shadow_strategy_decisions(
            [first],
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time="2026-06-25T10:00:00",
            message="shadow",
            state_keys={"A005930": "composite:excluded:"},
        )
        repeated_ids = self.database.save_shadow_strategy_decisions(
            [repeated],
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time="2026-06-25T10:05:00",
            message="shadow",
            state_keys={"A005930": "composite:excluded:"},
        )
        buy = self._decision(
            decided_at="2026-06-25T10:10:00",
            action=DecisionAction.BUY,
            reason="종합 72.00점: shadow 매수 후보",
            score="72",
        )
        buy_ids = self.database.save_shadow_strategy_decisions(
            [buy],
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time="2026-06-25T10:10:00",
            message="shadow",
            state_keys={"A005930": "composite:buy_candidate:"},
        )

        self.assertEqual(first_ids["A005930"], repeated_ids["A005930"])
        self.assertNotEqual(first_ids["A005930"], buy_ids["A005930"])
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT action, occurrence_count
                FROM strategy_decisions
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["occurrence_count"], 2)
        self.assertEqual(rows[1]["action"], "buy")


if __name__ == "__main__":
    unittest.main()
