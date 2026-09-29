import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from db.database import Database
from operations.approval import PaperOperationApprovalService


class PaperOperationApprovalServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.database.add_watchlist_stock("A005930", "Samsung")
        self.service = PaperOperationApprovalService(self.database)
        self.seoul = timezone(timedelta(hours=9))
        self._seed_approved_period()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_approved_period(self) -> None:
        for day in ("2026-06-11", "2026-06-12"):
            for clock in ("09:00:00", "15:20:00"):
                timestamp = f"{day}T{clock}"
                run_id = self._insert_run(timestamp)
                self._insert_event(
                    run_id,
                    "risk",
                    "risk_evaluated",
                    timestamp,
                )
                self._insert_event(
                    run_id,
                    "strategy",
                    "strategy_evaluated",
                    timestamp,
                )
                self.database.save_price_snapshot(
                    "test",
                    "A005930",
                    {"price": 100},
                    current_price=100,
                    open_price=100,
                    high_price=100,
                    low_price=100,
                    volume=100,
                    collected_at=datetime.fromisoformat(timestamp),
                )
        with self.database.connect() as connection:
            account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, realized_pnl,
                    created_at, updated_at
                )
                VALUES ('default', 1000000, 1001000, 1000, ?, ?)
                """,
                ("2026-06-11T08:50:00", "2026-06-12T15:20:00"),
            ).lastrowid
            buy_order_id = self._insert_order(
                connection,
                account_id,
                "buy",
                "2026-06-11T10:00:00",
            )
            sell_order_id = self._insert_order(
                connection,
                account_id,
                "sell",
                "2026-06-12T15:20:00",
            )
            self._insert_fill(
                connection,
                buy_order_id,
                "buy",
                "2026-06-11T10:00:01",
                0,
            )
            self._insert_fill(
                connection,
                sell_order_id,
                "sell",
                "2026-06-12T15:20:01",
                1000,
            )
            connection.execute(
                """
                INSERT INTO strategy_signals(
                    account_id, stock_code, signal_time, current_price,
                    previous_high, previous_close, current_volume,
                    previous_volume, volume_ratio, moving_average,
                    day_gain_rate, volume_pass, breakout_pass,
                    moving_average_pass, chase_pass, signal_pass,
                    action, order_id, message, created_at
                )
                VALUES (?, 'A005930', '2026-06-11T10:00:00', 105,
                        100, 95, 2100, 1000, 2.1, 99, 0.10,
                        1, 1, 1, 1, 1, 'executed', ?, 'executed',
                        '2026-06-11T10:00:01')
                """,
                (account_id, buy_order_id),
            )

    def _insert_run(self, started_at: str) -> int:
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    """
                    INSERT INTO operation_runs(
                        run_type, status, started_at, completed_at,
                        success_count, failure_count, metadata_json
                    )
                    VALUES ('periodic_collect', 'success', ?, ?, 1, 0, ?)
                    """,
                    (
                        started_at,
                        started_at,
                        json.dumps(
                            {
                                "interval_seconds": 30000,
                                "auto_strategy": True,
                            }
                        ),
                    ),
                ).lastrowid
            )

    def _insert_event(
        self,
        run_id: int,
        module: str,
        event_type: str,
        occurred_at: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO operation_events(
                    run_id, module, event_type, severity,
                    message, details_json, occurred_at
                )
                VALUES (?, ?, ?, 'info', 'ok', '{}', ?)
                """,
                (run_id, module, event_type, occurred_at),
            )

    @staticmethod
    def _insert_order(
        connection: object,
        account_id: int,
        side: str,
        requested_at: str,
    ) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, stock_code, side, order_type,
                    quantity, status, requested_at, completed_at
                )
                VALUES (?, 'A005930', ?, 'market', 10, 'filled', ?, ?)
                """,
                (account_id, side, requested_at, requested_at),
            ).lastrowid
        )

    @staticmethod
    def _insert_fill(
        connection: object,
        order_id: int,
        side: str,
        filled_at: str,
        realized_pnl: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO paper_fills(
                order_id, stock_code, side, quantity, market_price,
                fill_price, gross_amount, commission, tax,
                slippage_amount, realized_pnl, price_time, filled_at
            )
            VALUES (?, 'A005930', ?, 10, 100, 100, 1000,
                    0, 0, 0, ?, ?, ?)
            """,
            (order_id, side, realized_pnl, filled_at, filled_at),
        )

    def test_approved_period_is_ready_for_manual_approval(self) -> None:
        result = self.service.evaluate(
            required_days=2,
            interval_seconds=30000,
            minimum_round_trips=1,
            now=datetime(2026, 6, 12, 16, 0, tzinfo=self.seoul),
        )

        self.assertEqual(result.decision, "ready_for_manual_approval")
        self.assertEqual(
            result.reviewed_dates,
            [date(2026, 6, 11), date(2026, 6, 12)],
        )
        self.assertTrue(all(check.status == "pass" for check in result.checks))
        self.assertGreaterEqual(len(result.manual_checklist), 5)

    def test_active_alert_rejects_approval(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO operation_alerts(
                    alert_key, status, severity, message,
                    first_triggered_at, last_triggered_at, details_json
                )
                VALUES (
                    'collection_delay', 'active', 'warning', 'delay',
                    '2026-06-12T10:00:00', '2026-06-12T10:00:00', '{}'
                )
                """
            )

        result = self.service.evaluate(
            required_days=2,
            interval_seconds=30000,
            now=datetime(2026, 6, 12, 16, 0, tzinfo=self.seoul),
        )

        self.assertEqual(result.decision, "not_ready")
        alert_check = next(
            check for check in result.checks if check.code == "active_alerts"
        )
        self.assertEqual(alert_check.status, "fail")

    def test_approval_evidence_is_exported_as_json(self) -> None:
        result = self.service.evaluate(
            required_days=2,
            interval_seconds=30000,
            now=datetime(2026, 6, 12, 16, 0, tzinfo=self.seoul),
        )
        output = Path(self.temp_dir.name) / "approval.json"

        path = self.service.export_json(result, output)

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["decision"], "ready_for_manual_approval")
        self.assertEqual(len(payload["reviewed_dates"]), 2)
        self.assertTrue(payload["manual_checklist"])


if __name__ == "__main__":
    unittest.main()
