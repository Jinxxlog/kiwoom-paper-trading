import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from config.settings import PaperTradingSettings, RiskSettings
from db.database import Database
from paper.engine import PaperTradingEngine, PaperTradingError


class PaperTradingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.settings = PaperTradingSettings(
            initial_cash=1_000_000,
            commission_rate=Decimal("0.001"),
            sell_tax_rate=Decimal("0.002"),
            slippage_rate=Decimal("0.001"),
        )
        self.engine = PaperTradingEngine(self.database, self.settings)
        self.engine.create_account()
        self._save_price(10_000)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save_price(self, price: int) -> None:
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": price},
            current_price=price,
            open_price=price,
            high_price=price,
            low_price=price,
            volume=100,
        )

    def test_buy_and_partial_sell_update_cash_position_and_pnl(self) -> None:
        buy = self.engine.place_market_order(
            "default",
            "A005930",
            "buy",
            10,
            reason="테스트 진입",
            client_order_id="buy-1",
        )
        self.assertEqual(buy.fill_price, 10010)
        self.assertEqual(buy.commission, 100)
        self.assertEqual(buy.cash_balance, 899800)

        self._save_price(11_000)
        sell = self.engine.place_market_order(
            "default",
            "A005930",
            "sell",
            4,
            reason="부분 익절",
            client_order_id="sell-1",
        )

        self.assertEqual(sell.fill_price, 10989)
        self.assertEqual(sell.commission, 43)
        self.assertEqual(sell.tax, 87)
        self.assertEqual(sell.realized_pnl, 3746)
        summary = self.engine.account_summary("default")
        self.assertEqual(summary["cash_balance"], 943626)
        self.assertEqual(summary["realized_pnl"], 3746)
        self.assertEqual(summary["positions"][0]["quantity"], 6)
        self.assertEqual(summary["positions"][0]["cost_basis"], 60120)

    def test_insufficient_cash_is_rejected_and_recorded(self) -> None:
        with self.assertRaises(PaperTradingError):
            self.engine.place_market_order(
                "default",
                "A005930",
                "buy",
                1000,
                client_order_id="too-large",
            )

        orders = self.engine.list_orders("default")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "rejected")
        self.assertIn("현금이 부족", orders[0]["rejection_reason"])
        summary = self.engine.account_summary("default")
        self.assertEqual(summary["cash_balance"], 1_000_000)

    def test_oversell_is_rejected(self) -> None:
        self.engine.place_market_order("default", "A005930", "buy", 2)

        with self.assertRaises(PaperTradingError):
            self.engine.place_market_order("default", "A005930", "sell", 3)

        summary = self.engine.account_summary("default")
        self.assertEqual(summary["positions"][0]["quantity"], 2)

    def test_full_sell_closes_position(self) -> None:
        self.engine.place_market_order("default", "A005930", "buy", 2)
        self.engine.place_market_order("default", "A005930", "sell", 2)

        summary = self.engine.account_summary("default")
        self.assertEqual(summary["positions"], [])
        self.assertEqual(len(self.engine.list_fills("default")), 2)

    def test_duplicate_client_order_id_is_blocked(self) -> None:
        self.engine.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="same-id",
        )

        with self.assertRaises(PaperTradingError):
            self.engine.place_market_order(
                "default",
                "A005930",
                "buy",
                1,
                client_order_id="same-id",
            )

        orders = self.engine.list_orders("default")
        self.assertEqual(len(orders), 1)

    def test_missing_price_is_rejected_without_order(self) -> None:
        with self.assertRaises(PaperTradingError):
            self.engine.place_market_order("default", "A000660", "buy", 1)
        self.assertEqual(self.engine.list_orders("default"), [])

    def test_stale_price_is_rejected_without_order(self) -> None:
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            max_price_age_seconds=60,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE price_snapshots
                SET collected_at = ?
                WHERE stock_code = 'A005930'
                """,
                (
                    (datetime.now() - timedelta(minutes=5)).isoformat(
                        timespec="seconds"
                    ),
                ),
            )

        with self.assertRaisesRegex(PaperTradingError, "오래된 가격"):
            engine.place_market_order("default", "A005930", "buy", 1)

        self.assertEqual(engine.list_orders("default"), [])

    def test_aware_price_timestamp_does_not_break_freshness_check(self) -> None:
        aware_now = datetime.now(timezone(timedelta(hours=9))).isoformat(
            timespec="seconds"
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE price_snapshots
                SET collected_at = ?
                WHERE stock_code = 'A005930'
                """,
                (aware_now,),
            )
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            max_price_age_seconds=600,
        )

        result = engine.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="aware-price-time",
        )

        self.assertEqual(result.side, "buy")

    def test_previous_day_consecutive_losses_do_not_block_today_buy(self) -> None:
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.99"),
                max_consecutive_losses=3,
                max_position_rate=Decimal("1.0"),
                max_positions=10,
            ),
        )
        yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
        with self.database.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = 'default'"
            ).fetchone()["id"]
            for index in range(3):
                timestamp = f"{yesterday}T10:0{index}:00"
                order_id = connection.execute(
                    """
                    INSERT INTO paper_orders(
                        account_id, client_order_id, stock_code, side,
                        quantity, status, reason, requested_at, completed_at
                    )
                    VALUES (?, ?, 'A005930', 'sell', 1, 'filled',
                            'previous day loss', ?, ?)
                    """,
                    (account_id, f"previous-loss-{index}", timestamp, timestamp),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO paper_fills(
                        order_id, stock_code, side, quantity, market_price,
                        fill_price, gross_amount, commission, tax,
                        slippage_amount, realized_pnl, price_time, filled_at
                    )
                    VALUES (?, 'A005930', 'sell', 1, 9000, 9000, 9000,
                            0, 0, 0, -1000, ?, ?)
                    """,
                    (order_id, timestamp, timestamp),
                )

        result = engine.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="today-buy-after-yesterday-losses",
        )

        self.assertEqual(result.side, "buy")

    def test_entry_gain_limit_uses_latest_intraday_rate_before_stale_daily_close(
        self,
    ) -> None:
        self.database.save_candles(
            "daily",
            "A122640",
            {"rows": []},
            [
                {
                    "candle_time": "2026-06-12T00:00:00",
                    "open_price": 28_300,
                    "high_price": 31_400,
                    "low_price": 27_000,
                    "close_price": 29_700,
                    "volume": 636_208,
                }
            ],
            candle_type="daily",
        )
        self.database.save_price_snapshot(
            "ka10006",
            "A122640",
            {
                "close_pric": "+41800",
                "pre": "+3200",
                "flu_rt": "+8.29",
            },
            current_price=41_800,
            open_price=39_500,
            high_price=42_450,
            low_price=38_350,
            volume=681_514,
        )
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.99"),
                max_entry_gain_rate=Decimal("0.15"),
                max_consecutive_losses=3,
                max_position_rate=Decimal("1.0"),
                max_positions=10,
            ),
        )

        result = engine.place_market_order(
            "default",
            "A122640",
            "buy",
            1,
            client_order_id="latest-intraday-gain-rate",
        )

        self.assertEqual(result.side, "buy")

    def test_zero_consecutive_loss_limit_does_not_block_reentry(self) -> None:
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.99"),
                max_consecutive_losses=0,
                max_position_rate=Decimal("1.0"),
                max_positions=10,
            ),
        )
        for index in range(3):
            self._save_price(10_000)
            engine.place_market_order(
                "default",
                "A005930",
                "buy",
                1,
                client_order_id=f"loss-buy-{index}",
            )
            self._save_price(9_000)
            engine.place_market_order(
                "default",
                "A005930",
                "sell",
                1,
                client_order_id=f"loss-sell-{index}",
            )

        self._save_price(10_000)
        result = engine.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="reentry-after-losses",
        )

        self.assertGreater(result.order_id, 0)
        self.assertEqual(engine.list_orders("default")[0]["status"], "filled")

    def test_stop_loss_cooldown_blocks_same_stock_reentry(self) -> None:
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.99"),
                max_consecutive_losses=0,
                stop_loss_cooldown_minutes=15,
                max_position_rate=Decimal("1.0"),
                max_positions=10,
            ),
        )
        with self.database.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = 'default'"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO paper_risk_events(
                    account_id, stock_code, event_type, trigger_price,
                    threshold_price, highest_price, order_id, status,
                    message, occurred_at
                )
                VALUES (?, 'A005930', 'stop_loss', 9800, 9800, 10000,
                        NULL, 'executed', 'test', ?)
                """,
                (account_id, datetime.now().isoformat(timespec="seconds")),
            )

        with self.assertRaisesRegex(PaperTradingError, "손절 후 재진입 대기"):
            engine.place_market_order(
                "default",
                "A005930",
                "buy",
                1,
                client_order_id="cooldown-reentry",
            )

    def test_reentry_lock_blocks_new_buy(self) -> None:
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.99"),
                max_consecutive_losses=0,
                max_position_rate=Decimal("1.0"),
                max_positions=10,
            ),
        )
        lock_id = self.database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2999-01-01T15:10:00",
            reason="partial_take_profit_reentry_block",
            created_at=datetime.now().isoformat(timespec="seconds"),
        )

        with self.assertRaisesRegex(PaperTradingError, "재진입 엔진 승인"):
            engine.place_market_order(
                "default",
                "A005930",
                "buy",
                1,
                client_order_id="locked-reentry",
            )

        with self.assertRaisesRegex(PaperTradingError, "재진입 엔진 승인"):
            engine.place_market_order(
                "default",
                "A005930",
                "buy",
                1,
                client_order_id="locked-reentry-risk-override",
                risk_override=True,
            )

        approved = engine.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="approved-reentry",
            reentry_approval_lock_id=lock_id,
        )
        self.assertEqual(approved.side, "buy")

    def test_daily_stop_loss_limit_blocks_new_buy_only(self) -> None:
        engine = PaperTradingEngine(
            self.database,
            self.settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.99"),
                max_consecutive_losses=0,
                max_daily_stop_losses=3,
                max_position_rate=Decimal("1.0"),
                max_positions=10,
            ),
        )
        today = datetime.now().date().isoformat()
        with self.database.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = 'default'"
            ).fetchone()["id"]
            for index in range(3):
                connection.execute(
                    """
                    INSERT INTO paper_risk_events(
                        account_id, stock_code, event_type, trigger_price,
                        threshold_price, highest_price, order_id, status,
                        message, occurred_at
                    )
                    VALUES (?, ?, 'stop_loss', 9800, 9800, 10000,
                            NULL, 'executed', 'test', ?)
                    """,
                    (
                        account_id,
                        f"A{index:06d}",
                        f"{today}T10:0{index}:00",
                    ),
                )

        with self.assertRaisesRegex(PaperTradingError, "일중 손절 횟수 한도"):
            engine.place_market_order(
                "default",
                "A005930",
                "buy",
                1,
                client_order_id="daily-stop-loss-limit",
            )

        override = engine.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="daily-stop-loss-override",
            risk_override=True,
        )
        self.assertEqual(override.side, "buy")

        self._save_price(10_100)
        seeding_engine = PaperTradingEngine(
            self.database,
            self.settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.99"),
                max_consecutive_losses=0,
                max_daily_stop_losses=0,
                max_position_rate=Decimal("1.0"),
                max_positions=10,
            ),
        )
        seeding_engine.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="seed-position-for-sell",
        )
        self._save_price(10_200)
        sell = engine.place_market_order(
            "default",
            "A005930",
            "sell",
            1,
            client_order_id="sell-not-blocked",
        )

        self.assertEqual(sell.side, "sell")


if __name__ == "__main__":
    unittest.main()
