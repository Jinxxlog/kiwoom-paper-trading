import unittest
from datetime import datetime
from decimal import Decimal

from domain.models import (
    DecisionAction,
    OrderRequest,
    OrderSide,
    ScoreComponent,
    StrategyDecision,
)


class DomainModelsTest(unittest.TestCase):
    def _decision(
        self,
        *,
        action: DecisionAction = DecisionAction.BUY,
        stock_code: str = "A005930",
    ) -> StrategyDecision:
        component = ScoreComponent(
            name="breakout",
            score=Decimal("100"),
            weight=Decimal("0.25"),
            reason="전일 고가 돌파",
        )
        return StrategyDecision(
            strategy_name="breakout",
            stock_code=stock_code,
            decided_at=datetime(2026, 6, 10, 10, 0),
            action=action,
            score=component.weighted_score,
            confidence=Decimal("1"),
            reason="매수 조건 충족",
            components=(component,),
        )

    def test_score_component_calculates_weighted_score(self) -> None:
        component = self._decision().components[0]

        self.assertEqual(component.weighted_score, Decimal("25.00"))

    def test_order_request_requires_matching_decision(self) -> None:
        with self.assertRaisesRegex(ValueError, "주문 방향"):
            OrderRequest(
                account_name="default",
                stock_code="A005930",
                side=OrderSide.SELL,
                quantity=1,
                decision=self._decision(),
            )

    def test_valid_order_request_keeps_strategy_decision(self) -> None:
        decision = self._decision()

        request = OrderRequest(
            account_name="default",
            stock_code="A005930",
            side=OrderSide.BUY,
            quantity=3,
            decision=decision,
            client_order_id="strategy:default:A005930:1",
        )

        self.assertIs(request.decision, decision)


if __name__ == "__main__":
    unittest.main()
