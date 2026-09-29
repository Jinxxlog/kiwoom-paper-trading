import unittest
from datetime import datetime
from decimal import Decimal

from config.settings import CompositeScoreSettings
from domain.models import DecisionAction
from strategy.composite import CompositeScoringEngine


class CompositeScoringEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CompositeScoringEngine(CompositeScoreSettings())
        self.now = datetime(2026, 6, 15, 10, 0)

    def test_strong_complete_candidate_is_shadow_buy(self) -> None:
        result = self.engine.evaluate(
            stock_code="A005930",
            decided_at=self.now,
            technical_score=40,
            pattern_score=75,
            relative_strength_score=20,
            liquidity_score=15,
            information_score=20,
            market_return=0.20,
            excluded_reason=None,
            information_item_count=3,
            has_features=True,
        )

        self.assertEqual(result.status, "buy_candidate")
        self.assertEqual(result.decision.action, DecisionAction.BUY)
        self.assertEqual(result.decision.score, Decimal("100.0000"))
        self.assertEqual(len(result.decision.components), 6)
        self.assertEqual(
            sum(component.weight for component in result.decision.components),
            Decimal("1.00"),
        )

    def test_block_reason_overrides_high_score(self) -> None:
        result = self.engine.evaluate(
            stock_code="A005930",
            decided_at=self.now,
            technical_score=40,
            pattern_score=75,
            relative_strength_score=20,
            liquidity_score=15,
            information_score=20,
            market_return=0.20,
            excluded_reason="뉴스·공시 진입 차단",
            information_item_count=3,
            has_features=True,
        )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.decision.action, DecisionAction.HOLD)
        self.assertIn("진입 차단", result.decision.reason)

    def test_missing_news_is_neutral_but_reduces_confidence(self) -> None:
        result = self.engine.evaluate(
            stock_code="A005930",
            decided_at=self.now,
            technical_score=10,
            pattern_score=0,
            relative_strength_score=0,
            liquidity_score=10,
            information_score=0,
            market_return=0.01,
            excluded_reason=None,
            information_item_count=0,
            has_features=True,
        )

        information = next(
            component
            for component in result.decision.components
            if component.name == "information"
        )
        self.assertEqual(information.score, Decimal("50.0"))
        self.assertEqual(result.decision.confidence, Decimal("0.9000"))


if __name__ == "__main__":
    unittest.main()
