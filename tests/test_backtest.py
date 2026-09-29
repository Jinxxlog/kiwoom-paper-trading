import tempfile
import unittest
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from backtest.engine import (
    BacktestConfig,
    BacktestEquityPoint,
    BacktestResult,
    BacktestTrade,
    BreakoutBacktester,
)
from backtest.comparison import BacktestComparisonService
from backtest.exporter import BacktestReportExporter
from backtest.report import BacktestReportService
from backtest.repository import BacktestRepository
from backtest.validation import BacktestDataValidator
from config.settings import RiskSettings, StrategySettings
from db.database import Database


class BacktestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.strategy_settings = StrategySettings(
            volume_multiple=Decimal("2"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=3,
            minute_interval=5,
        )
        self.risk_settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
            max_position_rate=Decimal("0.10"),
            max_positions=3,
            max_entry_gain_rate=Decimal("0.15"),
        )
        self.config = BacktestConfig(
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 10),
            initial_cash=1_000_000,
            commission_rate=Decimal("0"),
            sell_tax_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            interval_minutes=5,
        )
        self.database.save_candles(
            "test",
            "A005930",
            {"daily": True},
            [
                {
                    "candle_time": "2026-06-09T00:00:00",
                    "open_price": 95,
                    "high_price": 100,
                    "low_price": 94,
                    "close_price": 95,
                    "volume": 1_000,
                },
                {
                    "candle_time": "2026-06-10T00:00:00",
                    "open_price": 96,
                    "high_price": 110,
                    "low_price": 95,
                    "close_price": 108,
                    "volume": 3_000,
                },
            ],
            candle_type="daily",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save_minutes(self, rows: list[dict]) -> None:
        self.database.save_candles(
            "test",
            "A005930",
            {"minute": True},
            rows,
            candle_type="minute",
            interval_minutes=5,
        )

    def test_signal_executes_at_next_bar_open_without_future_reference(self) -> None:
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
                self._bar("09:15", 110, 111, 109, 110, 2_200),
            ]
        )

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)

        self.assertEqual(result.trade_count, 1)
        trade = result.trades[0]
        self.assertEqual(trade.entry_time, "2026-06-10T09:15:00")
        self.assertEqual(trade.entry_price, 110)
        self.assertEqual(trade.exit_reason, "end_of_test")

    def test_last_bar_signal_is_not_filled(self) -> None:
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
            ]
        )

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)

        self.assertEqual(result.trade_count, 0)
        self.assertEqual(result.final_equity, self.config.initial_cash)

    def test_stop_loss_wins_when_stop_and_take_are_touched_same_bar(self) -> None:
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
                self._bar("09:15", 100, 101, 99, 100, 2_200),
                self._bar("09:20", 100, 105, 97, 102, 2_300),
            ]
        )

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)

        self.assertEqual(result.trades[0].exit_reason, "stop_loss")
        self.assertLess(result.trades[0].realized_pnl, 0)

    def test_daily_loss_limit_blocks_reentry(self) -> None:
        limited_risk = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.001"),
            max_consecutive_losses=3,
            max_position_rate=Decimal("0.10"),
            max_positions=3,
            max_entry_gain_rate=Decimal("0.15"),
        )
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
                self._bar("09:15", 100, 101, 99, 100, 2_200),
                self._bar("09:20", 100, 101, 97, 101, 2_300),
                self._bar("09:25", 101, 102, 100, 101, 2_400),
            ]
        )

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            limited_risk,
        ).run(["A005930"], self.config)

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].exit_reason, "stop_loss")

    def test_previous_daily_is_used_without_current_day_daily_row(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM candles WHERE candle_time = ?",
                ("2026-06-10T00:00:00",),
            )
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
                self._bar("09:15", 102, 103, 101, 102, 2_200),
            ]
        )

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)

        self.assertEqual(result.trade_count, 1)

    def test_result_is_saved_and_loaded_with_trades_and_equity_curve(self) -> None:
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
                self._bar("09:15", 102, 103, 101, 102, 2_200),
            ]
        )
        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)
        repository = BacktestRepository(self.database)

        run_id = repository.save(
            ["A005930"],
            self.config,
            self.strategy_settings,
            self.risk_settings,
            result,
        )

        saved = repository.get_run(run_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["stock_codes"], ["A005930"])
        self.assertEqual(saved["trade_count"], 1)
        self.assertEqual(
            saved["strategy_settings"]["moving_average_period"],
            3,
        )
        trades = repository.list_trades(run_id)
        self.assertEqual(trades[0]["exit_reason"], "end_of_test")
        points = repository.list_equity_points(run_id)
        self.assertEqual(len(points), len(result.equity_curve))
        self.assertEqual(points[-1]["total_equity"], result.final_equity)

    def test_saved_run_history_is_newest_first(self) -> None:
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 99, 100, 98, 99, 1_100),
            ]
        )
        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)
        repository = BacktestRepository(self.database)
        first_id = repository.save(
            ["A005930"],
            self.config,
            self.strategy_settings,
            self.risk_settings,
            result,
        )
        second_id = repository.save(
            ["A005930"],
            self.config,
            self.strategy_settings,
            self.risk_settings,
            result,
        )

        rows = repository.list_runs(limit=1)

        self.assertEqual(rows[0]["id"], second_id)
        self.assertGreater(second_id, first_id)

    def test_report_groups_trades_and_costs(self) -> None:
        result = BacktestResult(
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            initial_cash=1_000_000,
            final_equity=1_001_500,
            total_return_rate=Decimal("0.0015"),
            max_drawdown_rate=Decimal("0.002"),
            trade_count=3,
            win_count=2,
            loss_count=1,
            win_rate=Decimal("0.666666"),
            payoff_ratio=Decimal("1.5"),
            trades=(
                self._trade(
                    "A005930",
                    "2026-06-10T09:00:00",
                    "2026-06-10T10:00:00",
                    1_000,
                    "take_profit",
                    10,
                    5,
                    20,
                ),
                self._trade(
                    "A005930",
                    "2026-06-10T11:00:00",
                    "2026-06-10T12:00:00",
                    -500,
                    "stop_loss",
                    10,
                    5,
                    20,
                ),
                self._trade(
                    "A000660",
                    "2026-06-10T13:00:00",
                    "2026-06-11T10:00:00",
                    1_000,
                    "take_profit",
                    20,
                    10,
                    40,
                ),
            ),
            equity_curve=(
                BacktestEquityPoint(
                    "2026-06-10T00:00:00",
                    1_000_000,
                    0,
                    1_000_000,
                ),
                BacktestEquityPoint(
                    "2026-06-11T10:00:00",
                    1_001_500,
                    0,
                    1_001_500,
                ),
            ),
        )
        repository = BacktestRepository(self.database)
        run_id = repository.save(
            ["A005930", "A000660"],
            self.config,
            self.strategy_settings,
            self.risk_settings,
            result,
        )

        report = BacktestReportService(repository).build(run_id)

        self.assertEqual(report["summary"]["net_pnl"], 1_500)
        self.assertEqual(report["summary"]["average_win"], 1_000)
        self.assertEqual(report["summary"]["average_loss"], -500)
        self.assertEqual(report["summary"]["total_commission"], 60)
        self.assertEqual(report["summary"]["total_tax"], 20)
        self.assertEqual(report["summary"]["total_slippage"], 80)
        samsung = next(
            row
            for row in report["by_stock"]
            if row["stock_code"] == "A005930"
        )
        self.assertEqual(samsung["trade_count"], 2)
        self.assertEqual(samsung["net_pnl"], 500)
        self.assertEqual(len(report["by_date"]), 2)
        take_profit = next(
            row
            for row in report["by_exit_reason"]
            if row["exit_reason"] == "take_profit"
        )
        self.assertEqual(take_profit["trade_count"], 2)
        self.assertEqual(take_profit["net_pnl"], 2_000)

    def test_json_and_csv_reports_are_exported(self) -> None:
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
                self._bar("09:15", 102, 103, 101, 102, 2_200),
            ]
        )
        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)
        repository = BacktestRepository(self.database)
        run_id = repository.save(
            ["A005930"],
            self.config,
            self.strategy_settings,
            self.risk_settings,
            result,
        )
        exporter = BacktestReportExporter(repository)
        output_root = Path(self.temp_dir.name) / "reports"

        json_paths = exporter.export(
            run_id,
            "json",
            output_root / "report.json",
        )
        csv_paths = exporter.export(
            run_id,
            "csv",
            output_root / "csv",
        )

        payload = json.loads(json_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["run"]["id"], run_id)
        self.assertEqual(len(payload["trades"]), 1)
        self.assertEqual(len(csv_paths), 7)
        self.assertTrue((output_root / "csv" / "trades.csv").exists())
        self.assertTrue((output_root / "csv" / "equity.csv").exists())

    def test_empty_trade_csv_still_contains_headers(self) -> None:
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 99, 100, 98, 99, 1_100),
            ]
        )
        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            self.risk_settings,
        ).run(["A005930"], self.config)
        repository = BacktestRepository(self.database)
        run_id = repository.save(
            ["A005930"],
            self.config,
            self.strategy_settings,
            self.risk_settings,
            result,
        )
        output = Path(self.temp_dir.name) / "empty-csv"

        BacktestReportExporter(repository).export(run_id, "csv", output)

        header = (output / "trades.csv").read_text(
            encoding="utf-8-sig"
        ).strip()
        self.assertIn("stock_code", header)
        self.assertIn("slippage_amount", header)

    def test_saved_runs_are_compared_and_differences_are_reported(self) -> None:
        repository = BacktestRepository(self.database)
        first = self._result_with_metrics(
            final_equity=1_010_000,
            total_return_rate=Decimal("0.01"),
            max_drawdown_rate=Decimal("0.03"),
            win_rate=Decimal("0.50"),
            payoff_ratio=Decimal("1.2"),
            realized_pnl=10_000,
            cost=100,
        )
        second = self._result_with_metrics(
            final_equity=1_020_000,
            total_return_rate=Decimal("0.02"),
            max_drawdown_rate=Decimal("0.04"),
            win_rate=Decimal("0.75"),
            payoff_ratio=Decimal("1.5"),
            realized_pnl=20_000,
            cost=200,
        )
        first_id = repository.save(
            ["A005930"],
            self.config,
            self.strategy_settings,
            self.risk_settings,
            first,
        )
        different_config = BacktestConfig(
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            initial_cash=2_000_000,
            commission_rate=Decimal("0"),
            sell_tax_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            interval_minutes=5,
        )
        second_id = repository.save(
            ["A005930"],
            different_config,
            self.strategy_settings,
            self.risk_settings,
            second,
        )

        comparison = BacktestComparisonService(
            BacktestReportService(repository)
        ).compare([first_id, second_id])

        self.assertEqual(
            comparison["leaders"]["highest_return"]["run_id"],
            second_id,
        )
        self.assertEqual(
            comparison["leaders"]["lowest_drawdown"]["run_id"],
            first_id,
        )
        self.assertEqual(
            comparison["leaders"]["lowest_cost"]["run_id"],
            first_id,
        )
        self.assertTrue(
            any("초기자금" in warning for warning in comparison["warnings"])
        )

    def test_compare_requires_two_unique_runs(self) -> None:
        with self.assertRaises(ValueError):
            BacktestComparisonService(
                BacktestReportService(BacktestRepository(self.database))
            ).compare([1, 1])

    def test_stronger_simultaneous_signal_wins_limited_slot(self) -> None:
        limited_risk = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
            max_position_rate=Decimal("0.10"),
            max_positions=1,
            max_entry_gain_rate=Decimal("0.15"),
        )
        self._save_second_stock_daily()
        self._save_minutes(
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 3_000),
                self._bar("09:15", 102, 103, 101, 102, 3_100),
            ]
        )
        self._save_stock_minutes(
            "A000660",
            [
                self._bar("09:00", 98, 99, 97, 98, 500),
                self._bar("09:05", 99, 100, 98, 99, 1_000),
                self._bar("09:10", 101, 102, 100, 101, 2_100),
                self._bar("09:15", 102, 103, 101, 102, 2_200),
            ],
        )

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            limited_risk,
        ).run(["A000660", "A005930"], self.config)

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].stock_code, "A005930")

    def test_simultaneous_entries_share_available_cash_equally(self) -> None:
        allocation_risk = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
            max_position_rate=Decimal("1"),
            max_positions=2,
            max_entry_gain_rate=Decimal("0.15"),
        )
        small_config = BacktestConfig(
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            initial_cash=1_000,
            commission_rate=Decimal("0"),
            sell_tax_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            interval_minutes=5,
        )
        self._save_second_stock_daily()
        rows = [
            self._bar("09:00", 98, 99, 97, 98, 500),
            self._bar("09:05", 99, 100, 98, 99, 1_000),
            self._bar("09:10", 101, 102, 100, 101, 2_100),
            self._bar("09:15", 101, 102, 100, 101, 2_200),
        ]
        self._save_minutes(rows)
        self._save_stock_minutes("A000660", rows)

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            allocation_risk,
        ).run(["A005930", "A000660"], small_config)

        self.assertEqual(result.trade_count, 2)
        self.assertEqual(
            sorted(trade.quantity for trade in result.trades),
            [4, 4],
        )

    def test_data_validation_detects_gaps_and_missing_previous_daily(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM candles WHERE candle_time = ?",
                ("2026-06-09T00:00:00",),
            )
        self._save_minutes(
            [
                self._bar("09:05", 98, 99, 97, 98, 500),
                self._bar("09:15", 99, 100, 98, 99, 1_000),
            ]
        )

        validation = BacktestDataValidator(self.database).validate(
            ["A005930"],
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            interval_minutes=5,
            moving_average_period=3,
        )

        codes = {issue.code for issue in validation.issues}
        self.assertTrue(validation.is_ready)
        self.assertIn("insufficient_minute_bars", codes)
        self.assertIn("late_day_start", codes)
        self.assertIn("early_day_end", codes)
        self.assertIn("minute_gap", codes)
        self.assertIn("missing_previous_daily", codes)

    def test_data_validation_errors_when_stock_has_no_minute_data(self) -> None:
        validation = BacktestDataValidator(self.database).validate(
            ["A000660"],
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            interval_minutes=5,
            moving_average_period=3,
        )

        self.assertFalse(validation.is_ready)
        self.assertEqual(validation.error_count, 1)
        self.assertEqual(
            validation.issues[0].code,
            "missing_minute_data",
        )

    def test_closing_auction_gap_is_not_reported_as_missing_data(self) -> None:
        self._save_minutes(
            [
                self._bar("15:10", 98, 99, 97, 98, 500),
                self._bar("15:15", 99, 100, 98, 99, 1_000),
                self._bar("15:30", 100, 101, 99, 100, 1_500),
            ]
        )

        validation = BacktestDataValidator(self.database).validate(
            ["A005930"],
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            interval_minutes=5,
            moving_average_period=3,
        )

        self.assertNotIn(
            "minute_gap",
            {issue.code for issue in validation.issues},
        )

    def test_validation_warns_when_stock_date_ranges_differ(self) -> None:
        self._save_second_stock_daily()
        self._save_minutes(
            [
                self._bar_on("2026-06-10", "09:00", 98, 99, 97, 98, 500),
                self._bar_on("2026-06-10", "09:05", 99, 100, 98, 99, 1_000),
                self._bar_on("2026-06-10", "09:10", 100, 101, 99, 100, 1_500),
            ]
        )
        self._save_stock_minutes(
            "A000660",
            [
                self._bar_on("2026-06-11", "09:00", 98, 99, 97, 98, 500),
                self._bar_on("2026-06-11", "09:05", 99, 100, 98, 99, 1_000),
                self._bar_on("2026-06-11", "09:10", 100, 101, 99, 100, 1_500),
            ],
        )

        validation = BacktestDataValidator(self.database).validate(
            ["A005930", "A000660"],
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 11),
            interval_minutes=5,
            moving_average_period=3,
        )

        codes = {issue.code for issue in validation.issues}
        self.assertIn("stock_range_mismatch", codes)
        self.assertIn("stock_day_mismatch", codes)

    def test_multi_day_multi_stock_regression_result_is_stable(self) -> None:
        regression_risk = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
            max_position_rate=Decimal("0.10"),
            max_positions=1,
            max_entry_gain_rate=Decimal("0.15"),
        )
        regression_config = BacktestConfig(
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 11),
            initial_cash=1_000_000,
            commission_rate=Decimal("0"),
            sell_tax_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            interval_minutes=5,
        )
        self._save_second_stock_daily()
        self._update_daily_for_regression("A005930")
        self._update_daily_for_regression("A000660")
        self._save_stock_minutes(
            "A005930",
            [
                self._bar_on("2026-06-10", "09:00", 98, 99, 97, 98, 500),
                self._bar_on("2026-06-10", "09:05", 99, 100, 98, 99, 1_000),
                self._bar_on("2026-06-10", "09:10", 101, 102, 100, 101, 3_000),
                self._bar_on("2026-06-10", "09:15", 100, 101, 99, 100, 3_100),
                self._bar_on("2026-06-10", "15:20", 104, 104, 104, 104, 4_000),
                self._bar_on("2026-06-11", "09:00", 98, 99, 97, 98, 500),
                self._bar_on("2026-06-11", "09:05", 99, 100, 98, 99, 700),
                self._bar_on("2026-06-11", "09:10", 99, 100, 98, 99, 900),
                self._bar_on("2026-06-11", "09:15", 99, 100, 98, 99, 1_000),
                self._bar_on("2026-06-11", "15:20", 99, 100, 98, 99, 1_100),
            ],
        )
        self._save_stock_minutes(
            "A000660",
            [
                self._bar_on("2026-06-10", "09:00", 98, 99, 97, 98, 500),
                self._bar_on("2026-06-10", "09:05", 99, 100, 98, 99, 1_000),
                self._bar_on("2026-06-10", "09:10", 101, 102, 100, 101, 2_100),
                self._bar_on("2026-06-10", "09:15", 100, 101, 99, 100, 2_200),
                self._bar_on("2026-06-10", "15:20", 100, 101, 99, 100, 2_300),
                self._bar_on("2026-06-11", "09:00", 98, 99, 97, 98, 500),
                self._bar_on("2026-06-11", "09:05", 99, 100, 98, 99, 1_000),
                self._bar_on("2026-06-11", "09:10", 101, 102, 100, 101, 3_000),
                self._bar_on("2026-06-11", "09:15", 100, 101, 99, 100, 3_100),
                self._bar_on("2026-06-11", "15:20", 96, 96, 96, 96, 4_000),
            ],
        )

        result = BreakoutBacktester(
            self.database,
            self.strategy_settings,
            regression_risk,
        ).run(["A005930", "A000660"], regression_config)

        self.assertEqual(result.trade_count, 2)
        self.assertEqual(
            [trade.stock_code for trade in result.trades],
            ["A005930", "A000660"],
        )
        self.assertEqual(
            [trade.quantity for trade in result.trades],
            [1_000, 1_004],
        )
        self.assertEqual(
            [trade.realized_pnl for trade in result.trades],
            [4_000, -4_016],
        )
        self.assertEqual(result.final_equity, 999_984)

    def _save_second_stock_daily(self) -> None:
        self.database.save_candles(
            "test",
            "A000660",
            {"daily": True},
            [
                {
                    "candle_time": "2026-06-09T00:00:00",
                    "open_price": 95,
                    "high_price": 100,
                    "low_price": 94,
                    "close_price": 95,
                    "volume": 1_000,
                },
                {
                    "candle_time": "2026-06-10T00:00:00",
                    "open_price": 96,
                    "high_price": 110,
                    "low_price": 95,
                    "close_price": 108,
                    "volume": 3_000,
                },
            ],
            candle_type="daily",
        )

    def _save_stock_minutes(
        self,
        stock_code: str,
        rows: list[dict],
    ) -> None:
        self.database.save_candles(
            "test",
            stock_code,
            {"minute": True},
            rows,
            candle_type="minute",
            interval_minutes=5,
        )

    def _update_daily_for_regression(self, stock_code: str) -> None:
        self.database.save_candles(
            "test",
            stock_code,
            {"daily": True},
            [
                {
                    "candle_time": "2026-06-10T00:00:00",
                    "open_price": 95,
                    "high_price": 100,
                    "low_price": 94,
                    "close_price": 95,
                    "volume": 1_000,
                }
            ],
            candle_type="daily",
        )

    def _result_with_metrics(
        self,
        *,
        final_equity: int,
        total_return_rate: Decimal,
        max_drawdown_rate: Decimal,
        win_rate: Decimal,
        payoff_ratio: Decimal,
        realized_pnl: int,
        cost: int,
    ) -> BacktestResult:
        trade = self._trade(
            "A005930",
            "2026-06-10T09:00:00",
            "2026-06-10T10:00:00",
            realized_pnl,
            "take_profit",
            cost // 4,
            cost // 4,
            cost // 2,
        )
        return BacktestResult(
            start_date=self.config.start_date,
            end_date=self.config.end_date,
            initial_cash=self.config.initial_cash,
            final_equity=final_equity,
            total_return_rate=total_return_rate,
            max_drawdown_rate=max_drawdown_rate,
            trade_count=1,
            win_count=1,
            loss_count=0,
            win_rate=win_rate,
            payoff_ratio=payoff_ratio,
            trades=(trade,),
            equity_curve=(
                BacktestEquityPoint(
                    "2026-06-10T00:00:00",
                    self.config.initial_cash,
                    0,
                    self.config.initial_cash,
                ),
                BacktestEquityPoint(
                    "2026-06-10T10:00:00",
                    final_equity,
                    0,
                    final_equity,
                ),
            ),
        )

    @staticmethod
    def _trade(
        stock_code: str,
        entry_time: str,
        exit_time: str,
        realized_pnl: int,
        exit_reason: str,
        entry_commission: int,
        exit_commission: int,
        slippage_amount: int,
    ) -> BacktestTrade:
        return BacktestTrade(
            stock_code=stock_code,
            entry_time=entry_time,
            exit_time=exit_time,
            quantity=10,
            entry_market_price=100,
            exit_market_price=110,
            entry_price=101,
            exit_price=109,
            entry_commission=entry_commission,
            exit_commission=exit_commission,
            tax=exit_commission,
            slippage_amount=slippage_amount,
            realized_pnl=realized_pnl,
            return_rate=Decimal(realized_pnl) / Decimal("10000"),
            exit_reason=exit_reason,
        )

    @staticmethod
    def _bar(
        hhmm: str,
        open_price: int,
        high_price: int,
        low_price: int,
        close_price: int,
        accumulated_volume: int,
    ) -> dict:
        return {
            "candle_time": f"2026-06-10T{hhmm}:00",
            "open_price": open_price,
            "high_price": high_price,
            "low_price": low_price,
            "close_price": close_price,
            "volume": 500,
            "accumulated_volume": accumulated_volume,
        }

    @staticmethod
    def _bar_on(
        candle_day: str,
        hhmm: str,
        open_price: int,
        high_price: int,
        low_price: int,
        close_price: int,
        accumulated_volume: int,
    ) -> dict:
        return {
            "candle_time": f"{candle_day}T{hhmm}:00",
            "open_price": open_price,
            "high_price": high_price,
            "low_price": low_price,
            "close_price": close_price,
            "volume": 500,
            "accumulated_volume": accumulated_volume,
        }


if __name__ == "__main__":
    unittest.main()
