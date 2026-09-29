import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from config.settings import PaperTradingSettings, RiskSettings, StrategySettings
from db.database import Database, SCHEMA
from strategy.registry import register_breakout_strategy


class DatabaseMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "test.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _settings(self) -> tuple[
        StrategySettings,
        PaperTradingSettings,
        RiskSettings,
    ]:
        return (
            StrategySettings(
                volume_multiple=Decimal("2"),
                max_day_gain_rate=Decimal("0.15"),
                moving_average_period=20,
                minute_interval=5,
            ),
            PaperTradingSettings(
                initial_cash=1_000_000,
                commission_rate=Decimal("0"),
                sell_tax_rate=Decimal("0"),
                slippage_rate=Decimal("0"),
            ),
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.05"),
                max_consecutive_losses=3,
            ),
        )

    def test_migrations_are_sequential_and_idempotent(self) -> None:
        database = Database(self.path)

        database.initialize()
        database.initialize()

        history = database.migration_history()
        self.assertEqual(
            [row["version"] for row in history],
            [
                1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
                25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
            ],
        )
        with database.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            risk_events_sql = connection.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'paper_risk_events'
                """
            ).fetchone()["sql"]
            leader_scores_sql = connection.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'leader_stock_scores'
                """
            ).fetchone()["sql"]
        self.assertTrue(
            {
                "strategy_versions",
                "strategy_configs",
                "strategy_decisions",
                "score_components",
                "trade_plans",
                "order_requests",
                "order_executions",
                "paper_risk_state_events",
                "stock_universe",
                "candidate_runs",
                "candidate_scores",
                "pattern_performance_runs",
                "pattern_performance_results",
                "universe_collection_status",
                "intraday_pattern_evaluations",
                "dart_companies",
                "information_items",
                "information_score_snapshots",
                "ai_shadow_analyses",
                "theme_groups",
                "theme_components",
                "realtime_item_rankings",
                "market_rank_snapshots",
                "market_rank_items",
                "theme_journals",
                "theme_journal_members",
                "leader_stock_scores",
                "chart_setups",
                "intraday_trade_patterns",
                "entry_plans",
                "pattern_block_events",
                "partial_exit_plans",
                "partial_exit_fills",
                "breakeven_orders",
                "trailing_exit_state",
                "broker_flow_snapshots",
                "broker_flow_items",
                "investor_flow_snapshots",
                "investor_flow_items",
                "program_trade_snapshots",
                "supply_scores",
                "strategy_replay_snapshots",
                "decision_candle_snapshots",
                "decision_forward_outcomes",
                "leader_candidate_outcomes",
                "order_safety_events",
                "api_budget_events",
                "api_budget_windows",
                "partial_exit_adjustments",
                "reentry_locks",
                "strategy_armed_candidates",
                "market_mode_snapshots",
                "position_exit_states",
                "position_exit_state_events",
            }.issubset(tables)
        )
        self.assertIn("partial_take_profit", risk_events_sql)
        self.assertIn("support_break", risk_events_sql)
        self.assertIn("exit_class", risk_events_sql)
        self.assertIn("exit_reason_class", risk_events_sql)
        self.assertNotIn(
            "UNIQUE(trade_date, stock_code, theme_name)",
            leader_scores_sql,
        )
        with database.connect() as connection:
            decision_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(strategy_decisions)"
                ).fetchall()
            }
            reentry_lock_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(reentry_locks)"
                ).fetchall()
            }
        self.assertTrue(
            {
                "decision_state_key",
                "occurrence_count",
                "last_seen_at",
            }.issubset(decision_columns)
        )
        self.assertTrue(
            {
                "status",
                "setup_version",
                "setup_json",
                "released_at",
                "release_reason",
            }.issubset(reentry_lock_columns)
        )

    def test_new_exit_setup_supersedes_previous_reentry_lock(self) -> None:
        database = Database(self.path)
        database.initialize()

        first_id = database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2026-07-15T10:30:00",
            reason="exit_reentry_block:noise_stop",
            created_at="2026-07-15T09:20:00",
            setup={"exit_reason_class": "noise_stop"},
        )
        second_id = database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2026-07-15T11:00:00",
            reason="exit_reentry_block:structure_loss_stop",
            created_at="2026-07-15T09:40:00",
            setup={"exit_reason_class": "structure_loss_stop"},
        )

        pending = database.pending_reentry_lock(
            "A005930",
            as_of="2026-07-15T10:00:00",
        )
        self.assertIsNotNone(pending)
        self.assertEqual(pending["id"], second_id)
        self.assertEqual(pending["setup_version"], "v0.12.96")
        self.assertEqual(
            pending["setup"]["exit_reason_class"],
            "structure_loss_stop",
        )
        with database.connect() as connection:
            first = connection.execute(
                "SELECT status, release_reason FROM reentry_locks WHERE id = ?",
                (first_id,),
            ).fetchone()
        self.assertEqual(first["status"], "superseded")
        self.assertEqual(
            first["release_reason"],
            "superseded_by_newer_exit_setup",
        )

    def test_legacy_strategy_signal_is_backfilled_once(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.executescript(SCHEMA)
            timestamp = datetime(2026, 6, 10, 10, 40).isoformat(
                timespec="seconds"
            )
            account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, realized_pnl,
                    created_at, updated_at
                )
                VALUES ('default', 1000000, 1000000, 0, ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
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
                        2.1, 99, 0.105, 1, 1, 1, 1, 1,
                        'observe', NULL, '과거 신호', ?)
                """,
                (account_id, timestamp, timestamp),
            )

        database = Database(self.path)
        database.initialize()
        strategy, paper, risk = self._settings()
        register_breakout_strategy(database, strategy, paper, risk)
        register_breakout_strategy(database, strategy, paper, risk)

        with database.connect() as connection:
            decision = connection.execute(
                "SELECT * FROM strategy_decisions"
            ).fetchall()
            components = connection.execute(
                "SELECT * FROM score_components"
            ).fetchall()
        self.assertEqual(len(decision), 1)
        self.assertEqual(decision[0]["legacy_signal_id"], 1)
        self.assertEqual(decision[0]["score"], 100)
        self.assertEqual(len(components), 4)

    def test_decision_compaction_migration_upgrades_legacy_table(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """
                CREATE TABLE strategy_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    strategy_version_id INTEGER NOT NULL,
                    stock_code TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO strategy_decisions(
                    account_id, strategy_version_id, stock_code, decided_at,
                    action, reason, created_at, updated_at
                )
                VALUES (
                    NULL, 1, 'A005930', '2026-06-25T10:00:00',
                    'hold', '기본 돌파 전략 조건 미충족',
                    '2026-06-25T10:00:00', '2026-06-25T10:05:00'
                )
                """
            )
            Database._migrate_strategy_decision_state_compaction(connection)
            row = connection.execute(
                """
                SELECT decision_state_key, occurrence_count, last_seen_at
                FROM strategy_decisions
                """
            ).fetchone()

        self.assertEqual(
            row["decision_state_key"],
            "hold:기본 돌파 전략 조건 미충족",
        )
        self.assertEqual(row["occurrence_count"], 1)
        self.assertEqual(row["last_seen_at"], "2026-06-25T10:05:00")


if __name__ == "__main__":
    unittest.main()
