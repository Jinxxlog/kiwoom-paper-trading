import unittest
from decimal import Decimal

from config.settings import RiskSettings
from risk.exit_policy import (
    EXIT_STOP_LOSS,
    EXIT_SUPPORT_BREAK,
    EXIT_TRAILING_STOP,
    EXIT_VOLUME_DRY_UP,
    MarketExitContext,
    evaluate_exit,
)


class ExitPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
        )

    @staticmethod
    def _context(
        *,
        atr: Decimal | None = None,
        strength: str = "neutral",
        score: int = 0,
        support: int | None = None,
        volume_ratio: Decimal | None = None,
    ) -> MarketExitContext:
        return MarketExitContext(
            atr=atr,
            moving_average=None,
            moving_average_slope=None,
            support_price=support,
            volume_ratio=volume_ratio,
            trend_strength=strength,
            trend_score=score,
        )

    def _evaluate(self, **overrides):
        values = {
            "average_cost": Decimal("10000"),
            "current_price": 10000,
            "observed_low": 10000,
            "observed_high": 10000,
            "highest_price": 10000,
            "trailing_active": False,
            "context": self._context(),
            "settings": self.settings,
        }
        values.update(overrides)
        return evaluate_exit(**values)

    def test_atr_stop_can_tighten_but_never_loosen_fixed_stop(self) -> None:
        loose = self._evaluate(context=self._context(atr=Decimal("300")))
        tight = self._evaluate(context=self._context(atr=Decimal("50")))

        self.assertEqual(loose.initial_stop_price, 9800)
        self.assertEqual(loose.atr_stop_price, 9400)
        self.assertEqual(loose.current_stop_price, 9800)
        self.assertEqual(tight.atr_stop_price, 9900)
        self.assertEqual(tight.current_stop_price, 9900)

    def test_breakeven_moves_stop_to_entry_after_two_percent_gain(self) -> None:
        plan = self._evaluate(
            current_price=10200,
            observed_low=10200,
            observed_high=10200,
        )

        self.assertTrue(plan.breakeven_active)
        self.assertEqual(plan.current_stop_price, 10000)

    def test_strong_trend_extends_take_profit_and_widens_trailing(self) -> None:
        plan = self._evaluate(
            current_price=10300,
            observed_low=10300,
            observed_high=10300,
            context=self._context(strength="strong", score=4),
        )

        self.assertEqual(plan.take_profit_price, 10800)
        self.assertEqual(plan.trailing_drawdown_rate, Decimal("0.025"))

    def test_disabled_take_profit_does_not_cap_profit(self) -> None:
        settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            take_profit_enabled=False,
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
        )

        plan = self._evaluate(
            settings=settings,
            current_price=10400,
            observed_low=10400,
            observed_high=10400,
        )

        self.assertIsNone(plan.take_profit_price)
        self.assertIsNone(plan.exit_reason)
        self.assertEqual(plan.expected_exit_reason, EXIT_TRAILING_STOP)

    def test_weak_trend_narrows_trailing(self) -> None:
        plan = self._evaluate(
            current_price=10300,
            observed_low=10300,
            observed_high=10300,
            context=self._context(strength="weak", score=-1),
        )

        self.assertEqual(plan.trailing_drawdown_rate, Decimal("0.01"))
        self.assertTrue(plan.trailing_active)

    def test_support_break_and_volume_dry_up_are_exit_candidates(self) -> None:
        support = self._evaluate(
            current_price=10100,
            observed_low=10100,
            observed_high=10100,
            context=self._context(
                strength="weak",
                score=-1,
                support=10200,
            ),
        )
        volume = self._evaluate(
            current_price=10100,
            observed_low=10100,
            observed_high=10100,
            context=self._context(
                strength="weak",
                score=-1,
                volume_ratio=Decimal("0.2"),
            ),
        )

        self.assertIsNone(support.exit_reason)
        self.assertEqual(support.expected_exit_reason, EXIT_SUPPORT_BREAK)
        self.assertIsNone(volume.exit_reason)
        self.assertEqual(volume.expected_exit_reason, EXIT_VOLUME_DRY_UP)

    def test_backtest_does_not_apply_new_same_bar_protection(self) -> None:
        plan = self._evaluate(
            observed_low=9800,
            observed_high=10300,
            defer_new_protection=True,
        )

        self.assertEqual(plan.exit_reason, EXIT_STOP_LOSS)
        self.assertEqual(plan.exit_price, 9800)
        self.assertTrue(plan.breakeven_active)
        self.assertTrue(plan.trailing_active)


if __name__ == "__main__":
    unittest.main()
