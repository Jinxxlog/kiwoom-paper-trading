import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from db.database import Database
from operations.daily_report import DailyOperationsReport


class DailyOperationsReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.database.add_watchlist_stock("A005930", "Samsung")
        self.service = DailyOperationsReport(self.database)
        self.seoul = timezone(timedelta(hours=9))
        self._seed_day()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _seed_day(self) -> None:
        day = "2026-06-10"
        with self.database.connect() as connection:
            run_id = connection.execute(
                """
                INSERT INTO operation_runs(
                    run_type, status, started_at, completed_at,
                    success_count, failure_count, metadata_json
                )
                VALUES (
                    'periodic_collect', 'partial_failure',
                    ?, ?, 1, 1, ?
                )
                """,
                (
                    f"{day}T09:00:00",
                    f"{day}T09:00:05",
                    json.dumps(
                        {
                            "interval_seconds": 60,
                            "auto_strategy": True,
                        }
                    ),
                ),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO operation_events(
                    run_id, module, event_type, severity, stock_code,
                    message, details_json, occurred_at
                )
                VALUES (?, 'collection', 'stock_failed', 'error',
                        'A005930', 'API timeout', '{"attempts":3}', ?)
                """,
                (run_id, f"{day}T09:00:04"),
            )
            account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, realized_pnl,
                    created_at, updated_at
                )
                VALUES ('default', 1000000, 1001000, 1000, ?, ?)
                """,
                (f"{day}T08:50:00", f"{day}T10:00:00"),
            ).lastrowid
            order_id = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    requested_at, completed_at
                )
                VALUES (?, 'sell-1', 'A005930', 'sell',
                        'market', 10, 'filled', 'take profit', ?, ?)
                """,
                (
                    account_id,
                    f"{day}T10:00:00",
                    f"{day}T10:00:01",
                ),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, 'A005930', 'sell', 10, 110, 109, 1090,
                        10, 5, 10, 1000, ?, ?)
                """,
                (
                    order_id,
                    f"{day}T10:00:00",
                    f"{day}T10:00:01",
                ),
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
                VALUES (?, 'A005930', ?, 105, 100, 95, 2100, 1000,
                        2.1, 99, 0.10, 1, 1, 1, 1, 1,
                        'executed', ?, 'executed', ?)
                """,
                (
                    account_id,
                    f"{day}T09:30:00",
                    order_id,
                    f"{day}T09:30:01",
                ),
            )
            connection.execute(
                """
                INSERT INTO paper_risk_events(
                    account_id, stock_code, event_type, trigger_price,
                    threshold_price, highest_price, order_id, status,
                    message, occurred_at
                )
                VALUES (?, 'A005930', 'take_profit', 110, 104, 110,
                        ?, 'executed', 'take profit', ?)
                """,
                (account_id, order_id, f"{day}T10:00:01"),
            )
        for clock, price in (("09:00:00", 100), ("15:20:00", 110)):
            self.database.save_price_snapshot(
                "test",
                "A005930",
                {"price": price},
                current_price=price,
                open_price=price,
                high_price=price,
                low_price=price,
                volume=100,
                collected_at=datetime.fromisoformat(f"{day}T{clock}"),
            )

    def test_daily_report_aggregates_operations_and_trading(self) -> None:
        report = self.service.build(
            date(2026, 6, 10),
            now=datetime(2026, 6, 10, 16, 0, tzinfo=self.seoul),
            expected_interval_seconds=30_000,
        )

        self.assertEqual(report["report_date"], "2026-06-10")
        self.assertEqual(
            report["collection"]["summary"]["partial_runs"],
            1,
        )
        self.assertEqual(report["events"]["severity"]["error"], 1)
        self.assertEqual(
            report["events"]["notable"][0]["details"]["attempts"],
            3,
        )
        self.assertEqual(report["prices"][0]["snapshot_count"], 2)
        account = report["trading"]["accounts"][0]
        self.assertEqual(account["realized_pnl"], 1000)
        self.assertEqual(account["total_cost"], 25)
        self.assertEqual(
            report["strategy"]["summary"]["executed_count"],
            1,
        )
        self.assertEqual(report["risk"]["summary"]["executed_count"], 1)
        self.assertIn("forward_outcomes", report)
        self.assertEqual(
            report["forward_outcomes"]["representative_decisions"],
            0,
        )

    def test_daily_report_is_exported_as_json(self) -> None:
        output = Path(self.temp_dir.name) / "reports" / "daily.json"

        path = self.service.export_json(
            date(2026, 6, 10),
            output,
            now=datetime(2026, 6, 10, 16, 0, tzinfo=self.seoul),
            expected_interval_seconds=30_000,
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["report_date"], "2026-06-10")
        self.assertEqual(payload["trading"]["orders"][0]["status"], "filled")
        self.assertEqual(payload["events"]["notable"][0]["message"], "API timeout")
        self.assertIn("forward_outcomes", payload)


if __name__ == "__main__":
    unittest.main()
