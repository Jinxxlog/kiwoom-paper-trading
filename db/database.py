import json
import sqlite3
from hashlib import sha256
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from domain.models import OrderRequest, ScoreComponent, StrategyDecision, TradePlan
from screening.stock_category import (
    STOCK_CATEGORY_LABELS,
    stock_category_label,
    stock_category_payload,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS api_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_id TEXT NOT NULL,
    stock_code TEXT,
    collected_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_snapshots_lookup
ON api_snapshots(api_id, stock_code, collected_at);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    current_price INTEGER,
    open_price INTEGER,
    high_price INTEGER,
    low_price INTEGER,
    volume INTEGER,
    raw_snapshot_id INTEGER NOT NULL,
    FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_lookup
ON price_snapshots(stock_code, collected_at);

CREATE TABLE IF NOT EXISTS watchlist (
    stock_code TEXT PRIMARY KEY,
    stock_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watchlist_enabled
ON watchlist(enabled, stock_code);

CREATE TABLE IF NOT EXISTS candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    candle_type TEXT NOT NULL CHECK(candle_type IN ('daily', 'minute')),
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    candle_time TEXT NOT NULL,
    open_price INTEGER,
    high_price INTEGER,
    low_price INTEGER,
    close_price INTEGER,
    volume INTEGER,
    accumulated_volume INTEGER,
    trade_amount INTEGER,
    raw_snapshot_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id),
    UNIQUE(stock_code, candle_type, interval_minutes, candle_time)
);

CREATE INDEX IF NOT EXISTS idx_candles_lookup
ON candles(stock_code, candle_type, interval_minutes, candle_time);

CREATE TABLE IF NOT EXISTS paper_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    initial_cash INTEGER NOT NULL CHECK(initial_cash >= 0),
    cash_balance INTEGER NOT NULL CHECK(cash_balance >= 0),
    realized_pnl INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    client_order_id TEXT,
    stock_code TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    order_type TEXT NOT NULL DEFAULT 'market' CHECK(order_type = 'market'),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL CHECK(status IN ('filled', 'rejected')),
    reason TEXT,
    rejection_reason TEXT,
    requested_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
    UNIQUE(account_id, client_order_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_account
ON paper_orders(account_id, requested_at, id);

CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE,
    stock_code TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    market_price INTEGER NOT NULL CHECK(market_price > 0),
    fill_price INTEGER NOT NULL CHECK(fill_price > 0),
    gross_amount INTEGER NOT NULL CHECK(gross_amount >= 0),
    commission INTEGER NOT NULL CHECK(commission >= 0),
    tax INTEGER NOT NULL CHECK(tax >= 0),
    slippage_amount INTEGER NOT NULL CHECK(slippage_amount >= 0),
    realized_pnl INTEGER NOT NULL DEFAULT 0,
    price_time TEXT NOT NULL,
    filled_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES paper_orders(id)
);

CREATE INDEX IF NOT EXISTS idx_paper_fills_stock
ON paper_fills(stock_code, filled_at, id);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    cost_basis INTEGER NOT NULL CHECK(cost_basis >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, stock_code),
    FOREIGN KEY(account_id) REFERENCES paper_accounts(id)
);

CREATE TABLE IF NOT EXISTS paper_risk_profiles (
    account_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    stop_loss_rate REAL NOT NULL,
    take_profit_rate REAL NOT NULL,
    trailing_activation_rate REAL NOT NULL,
    trailing_drawdown_rate REAL NOT NULL,
    highest_price INTEGER NOT NULL CHECK(highest_price > 0),
    trailing_active INTEGER NOT NULL DEFAULT 0 CHECK(trailing_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, stock_code),
    FOREIGN KEY(account_id) REFERENCES paper_accounts(id)
);

CREATE TABLE IF NOT EXISTS paper_risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(
        event_type IN (
            'stop_loss', 'take_profit', 'trailing_stop',
            'support_break', 'volume_dry_up', 'market_close',
            'partial_take_profit'
        )
    ),
    trigger_price INTEGER NOT NULL CHECK(trigger_price > 0),
    threshold_price INTEGER NOT NULL CHECK(threshold_price > 0),
    highest_price INTEGER NOT NULL CHECK(highest_price > 0),
    order_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('executed', 'failed')),
    exit_class TEXT,
    exit_reason_class TEXT,
    setup_invalidated INTEGER NOT NULL DEFAULT 0 CHECK(setup_invalidated IN (0, 1)),
    classification_json TEXT NOT NULL DEFAULT '{}',
    message TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
    FOREIGN KEY(order_id) REFERENCES paper_orders(id)
);

CREATE INDEX IF NOT EXISTS idx_paper_risk_events_account
ON paper_risk_events(account_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS strategy_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    current_price INTEGER NOT NULL,
    previous_high INTEGER,
    previous_close INTEGER,
    current_volume INTEGER,
    previous_volume INTEGER,
    volume_ratio REAL,
    moving_average REAL,
    day_gain_rate REAL,
    volume_pass INTEGER NOT NULL CHECK(volume_pass IN (0, 1)),
    breakout_pass INTEGER NOT NULL CHECK(breakout_pass IN (0, 1)),
    moving_average_pass INTEGER NOT NULL CHECK(moving_average_pass IN (0, 1)),
    chase_pass INTEGER NOT NULL CHECK(chase_pass IN (0, 1)),
    signal_pass INTEGER NOT NULL CHECK(signal_pass IN (0, 1)),
    action TEXT NOT NULL CHECK(
        action IN ('observe', 'skipped', 'executed', 'failed')
    ),
    order_id INTEGER,
    message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
    FOREIGN KEY(order_id) REFERENCES paper_orders(id),
    UNIQUE(account_id, stock_code, signal_time)
);

CREATE INDEX IF NOT EXISTS idx_strategy_signals_account
ON strategy_signals(account_id, signal_time, id);

CREATE TABLE IF NOT EXISTS order_book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    quote_time TEXT,
    best_ask_price INTEGER,
    best_ask_quantity INTEGER,
    best_bid_price INTEGER,
    best_bid_quantity INTEGER,
    total_ask_quantity INTEGER,
    total_bid_quantity INTEGER,
    raw_snapshot_id INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_lookup
ON order_book_snapshots(stock_code, collected_at, id);

CREATE TABLE IF NOT EXISTS order_book_levels (
    snapshot_id INTEGER NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('ask', 'bid')),
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 10),
    price INTEGER,
    quantity INTEGER,
    quantity_change INTEGER,
    PRIMARY KEY(snapshot_id, side, level),
    FOREIGN KEY(snapshot_id) REFERENCES order_book_snapshots(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trade_strength_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    trade_time TEXT NOT NULL,
    current_price INTEGER,
    trade_quantity INTEGER,
    accumulated_volume INTEGER,
    accumulated_amount INTEGER,
    strength REAL,
    ask_price INTEGER,
    bid_price INTEGER,
    exchange_type TEXT,
    raw_snapshot_id INTEGER NOT NULL,
    collected_at TEXT NOT NULL,
    FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id),
    UNIQUE(stock_code, trade_time, exchange_type, trade_quantity)
);

CREATE INDEX IF NOT EXISTS idx_trade_strength_lookup
ON trade_strength_snapshots(stock_code, trade_time, id);

CREATE TABLE IF NOT EXISTS real_account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    deposit INTEGER,
    withdrawable_amount INTEGER,
    orderable_cash INTEGER,
    total_purchase_amount INTEGER,
    total_evaluation_amount INTEGER,
    total_profit_loss INTEGER,
    total_profit_rate REAL,
    estimated_assets INTEGER,
    raw_balance_snapshot_id INTEGER NOT NULL,
    raw_holdings_snapshot_id INTEGER NOT NULL,
    FOREIGN KEY(raw_balance_snapshot_id) REFERENCES api_snapshots(id),
    FOREIGN KEY(raw_holdings_snapshot_id) REFERENCES api_snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_real_account_snapshots_time
ON real_account_snapshots(collected_at, id);

CREATE TABLE IF NOT EXISTS real_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_snapshot_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    quantity INTEGER,
    tradable_quantity INTEGER,
    current_price INTEGER,
    purchase_price INTEGER,
    purchase_amount INTEGER,
    evaluation_amount INTEGER,
    evaluation_profit_loss INTEGER,
    profit_rate REAL,
    FOREIGN KEY(account_snapshot_id) REFERENCES real_account_snapshots(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_real_positions_snapshot
ON real_positions(account_snapshot_id, stock_code);

CREATE TABLE IF NOT EXISTS real_order_execution_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    order_date TEXT,
    order_number TEXT,
    stock_code TEXT,
    stock_name TEXT,
    trade_type TEXT,
    order_type_name TEXT,
    order_quantity INTEGER,
    order_price INTEGER,
    executed_quantity INTEGER,
    executed_price INTEGER,
    remaining_quantity INTEGER,
    order_time TEXT,
    status TEXT,
    original_order_number TEXT,
    exchange_type TEXT,
    raw_snapshot_id INTEGER NOT NULL,
    FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_real_order_execution_lookup
ON real_order_execution_snapshots(order_date, order_number, collected_at, id);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0),
    stock_codes_json TEXT NOT NULL,
    initial_cash INTEGER NOT NULL CHECK(initial_cash > 0),
    commission_rate REAL NOT NULL,
    sell_tax_rate REAL NOT NULL,
    slippage_rate REAL NOT NULL,
    strategy_settings_json TEXT NOT NULL,
    risk_settings_json TEXT NOT NULL,
    final_equity INTEGER NOT NULL CHECK(final_equity >= 0),
    total_return_rate REAL NOT NULL,
    max_drawdown_rate REAL NOT NULL,
    trade_count INTEGER NOT NULL CHECK(trade_count >= 0),
    win_count INTEGER NOT NULL CHECK(win_count >= 0),
    loss_count INTEGER NOT NULL CHECK(loss_count >= 0),
    win_rate REAL NOT NULL,
    payoff_ratio REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_created
ON backtest_runs(created_at, id);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    stock_code TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    entry_market_price INTEGER NOT NULL CHECK(entry_market_price > 0),
    exit_market_price INTEGER NOT NULL CHECK(exit_market_price > 0),
    entry_price INTEGER NOT NULL CHECK(entry_price > 0),
    exit_price INTEGER NOT NULL CHECK(exit_price > 0),
    entry_commission INTEGER NOT NULL CHECK(entry_commission >= 0),
    exit_commission INTEGER NOT NULL CHECK(exit_commission >= 0),
    tax INTEGER NOT NULL CHECK(tax >= 0),
    slippage_amount INTEGER NOT NULL CHECK(slippage_amount >= 0),
    realized_pnl INTEGER NOT NULL,
    return_rate REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE,
    UNIQUE(run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_backtest_trades_run
ON backtest_trades(run_id, sequence);

CREATE TABLE IF NOT EXISTS backtest_equity_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    point_time TEXT NOT NULL,
    cash_balance INTEGER NOT NULL CHECK(cash_balance >= 0),
    market_value INTEGER NOT NULL CHECK(market_value >= 0),
    total_equity INTEGER NOT NULL CHECK(total_equity >= 0),
    FOREIGN KEY(run_id) REFERENCES backtest_runs(id) ON DELETE CASCADE,
    UNIQUE(run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_backtest_equity_run
ON backtest_equity_points(run_id, sequence);

CREATE TABLE IF NOT EXISTS operation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('running', 'success', 'partial_failure', 'failed')
    ),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    success_count INTEGER NOT NULL DEFAULT 0 CHECK(success_count >= 0),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_operation_runs_status
ON operation_runs(status, started_at, id);

CREATE TABLE IF NOT EXISTS operation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    module TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'error')),
    stock_code TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES operation_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_operation_events_lookup
ON operation_events(module, severity, occurred_at, id);

CREATE TABLE IF NOT EXISTS operation_alerts (
    alert_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('active', 'resolved')),
    severity TEXT NOT NULL CHECK(severity IN ('warning', 'error')),
    message TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count > 0),
    first_triggered_at TEXT NOT NULL,
    last_triggered_at TEXT NOT NULL,
    resolved_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_operation_alerts_status
ON operation_alerts(status, severity, last_triggered_at);

CREATE TABLE IF NOT EXISTS operation_module_status (
    module TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('success', 'failed')),
    last_success_at TEXT,
    last_failure_at TEXT,
    last_message TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_calendar_days (
    market_date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    name TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_market_calendar_days_status
ON market_calendar_days(status, market_date);

CREATE TABLE IF NOT EXISTS market_mode_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    market_date TEXT NOT NULL,
    index_value REAL,
    moving_average_20 REAL,
    above_ma20 INTEGER NOT NULL DEFAULT 0 CHECK(above_ma20 IN (0, 1)),
    trade_amount INTEGER,
    mode TEXT NOT NULL CHECK(mode IN ('risk_on', 'neutral', 'risk_off')),
    reliability TEXT NOT NULL CHECK(
        reliability IN ('reliable', 'partial', 'unavailable')
    ),
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    raw_snapshot_id INTEGER,
    collected_at TEXT NOT NULL,
    FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_market_mode_snapshots_latest
ON market_mode_snapshots(market, collected_at DESC, id DESC);
"""


COMMON_RECORDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(strategy_name, version)
);

CREATE INDEX IF NOT EXISTS idx_strategy_versions_active
ON strategy_versions(active, strategy_name, version);

CREATE TABLE IF NOT EXISTS strategy_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_version_id INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    config_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(strategy_version_id) REFERENCES strategy_versions(id),
    UNIQUE(strategy_version_id, config_hash)
);

CREATE INDEX IF NOT EXISTS idx_strategy_configs_active
ON strategy_configs(strategy_version_id, active, effective_from, id);

CREATE TABLE IF NOT EXISTS strategy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    strategy_version_id INTEGER NOT NULL,
    strategy_config_id INTEGER,
    legacy_signal_id INTEGER,
    stock_code TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    source_data_time TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('buy', 'sell', 'hold')),
    score REAL NOT NULL CHECK(score BETWEEN -100 AND 100),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    reason TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(
        outcome IN ('evaluated', 'observe', 'skipped', 'executed', 'failed')
    ),
    message TEXT,
    decision_state_key TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count > 0),
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
    FOREIGN KEY(strategy_version_id) REFERENCES strategy_versions(id),
    FOREIGN KEY(strategy_config_id) REFERENCES strategy_configs(id),
    FOREIGN KEY(legacy_signal_id) REFERENCES strategy_signals(id),
    UNIQUE(legacy_signal_id),
    UNIQUE(
        account_id, strategy_version_id, stock_code, source_data_time, action
    )
);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_lookup
ON strategy_decisions(stock_code, decided_at, id);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_recent
ON strategy_decisions(decided_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_account
ON strategy_decisions(account_id, decided_at, id);

CREATE TABLE IF NOT EXISTS score_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    score REAL NOT NULL CHECK(score BETWEEN -100 AND 100),
    weight REAL NOT NULL CHECK(weight BETWEEN 0 AND 1),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    weighted_score REAL NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id) ON DELETE CASCADE,
    UNIQUE(decision_id, name, source)
);

CREATE INDEX IF NOT EXISTS idx_score_components_decision
ON score_components(decision_id, id);

CREATE TABLE IF NOT EXISTS trade_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL UNIQUE,
    stock_code TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    entry_tags_json TEXT NOT NULL DEFAULT '[]',
    entry_price INTEGER NOT NULL CHECK(entry_price > 0),
    planned_quantity INTEGER NOT NULL CHECK(planned_quantity >= 0),
    stop_loss_price INTEGER NOT NULL CHECK(stop_loss_price > 0),
    stop_loss_reason TEXT NOT NULL,
    first_take_profit_price INTEGER NOT NULL CHECK(first_take_profit_price > 0),
    first_take_profit_reason TEXT NOT NULL,
    partial_exit_rate REAL NOT NULL CHECK(
        partial_exit_rate > 0 AND partial_exit_rate <= 1
    ),
    breakeven_price INTEGER NOT NULL CHECK(breakeven_price > 0),
    breakeven_trigger_price INTEGER NOT NULL CHECK(
        breakeven_trigger_price > 0
    ),
    trailing_rule TEXT NOT NULL,
    invalidation_reason TEXT NOT NULL,
    reward_risk_ratio REAL NOT NULL,
    setup_json TEXT NOT NULL DEFAULT '{}',
    partial_exit_executed INTEGER NOT NULL DEFAULT 0 CHECK(
        partial_exit_executed IN (0, 1)
    ),
    partial_exit_quantity INTEGER,
    partial_exit_order_id INTEGER,
    partial_exit_price INTEGER,
    partial_exit_at TEXT,
    breakeven_activated INTEGER NOT NULL DEFAULT 0 CHECK(
        breakeven_activated IN (0, 1)
    ),
    breakeven_activated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_plans_stock
ON trade_plans(stock_code, entry_type, id);

CREATE TABLE IF NOT EXISTS order_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL,
    account_name TEXT NOT NULL,
    execution_mode TEXT NOT NULL CHECK(execution_mode IN ('paper', 'live')),
    stock_code TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    order_type TEXT NOT NULL CHECK(order_type IN ('market')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    client_order_id TEXT,
    reason TEXT,
    status TEXT NOT NULL CHECK(
        status IN ('pending', 'submitted', 'filled', 'rejected', 'failed')
    ),
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id),
    UNIQUE(execution_mode, account_name, client_order_id)
);

CREATE INDEX IF NOT EXISTS idx_order_requests_decision
ON order_requests(decision_id, requested_at, id);

CREATE TABLE IF NOT EXISTS order_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_request_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('submitted', 'filled', 'rejected', 'failed')
    ),
    internal_order_id INTEGER,
    broker_order_id TEXT,
    fill_price INTEGER,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    message TEXT,
    executed_at TEXT NOT NULL,
    FOREIGN KEY(order_request_id) REFERENCES order_requests(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_order_executions_request
ON order_executions(order_request_id, executed_at, id);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)
            self._apply_migrations(connection)

    @staticmethod
    def _apply_migrations(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        migrations = (
            (1, "backtest_execution_prices", Database._migrate_backtest_columns),
            (2, "common_strategy_records", Database._migrate_common_records),
            (3, "dynamic_exit_state", Database._migrate_dynamic_exit_state),
            (4, "technical_candidate_screening", Database._migrate_candidate_screening),
            (5, "universe_collection_progress", Database._migrate_universe_collection),
            (6, "intraday_vwap_patterns", Database._migrate_intraday_vwap_patterns),
            (7, "intraday_liquidity_sweep", Database._migrate_intraday_liquidity_sweep),
            (8, "intraday_fair_value_gap", Database._migrate_intraday_fair_value_gap),
            (9, "intraday_order_block", Database._migrate_intraday_order_block),
            (10, "news_disclosure_scoring", Database._migrate_information_scoring),
            (11, "composite_shadow_scoring", Database._migrate_composite_scoring),
            (12, "ai_shadow_analysis", Database._migrate_ai_shadow_analysis),
            (13, "trade_plan_records", Database._migrate_trade_plan_records),
            (14, "trade_plan_execution_state", Database._migrate_trade_plan_state),
            (15, "theme_selection_records", Database._migrate_theme_records),
            (16, "realtime_item_rank_records", Database._migrate_realtime_rank_records),
            (17, "leader_theme_records", Database._migrate_leader_theme_records),
            (18, "chart_trading_records", Database._migrate_chart_trading_records),
            (19, "partial_exit_engine_records", Database._migrate_partial_exit_records),
            (20, "supply_flow_records", Database._migrate_supply_flow_records),
            (21, "strategy_replay_records", Database._migrate_strategy_replay_records),
            (
                22,
                "strategy_forward_outcome_horizons",
                Database._migrate_strategy_forward_outcome_horizons,
            ),
            (
                23,
                "leader_candidate_outcomes",
                Database._migrate_leader_candidate_outcomes,
            ),
            (
                24,
                "order_safety_events",
                Database._migrate_order_safety_events,
            ),
            (
                25,
                "api_budget_records",
                Database._migrate_api_budget_records,
            ),
            (
                26,
                "advanced_partial_exit_records",
                Database._migrate_advanced_partial_exit_records,
            ),
            (
                27,
                "program_trade_and_forward_maturity",
                Database._migrate_program_trade_and_forward_maturity,
            ),
            (
                28,
                "forward_outcome_candle_coverage",
                Database._migrate_forward_outcome_candle_coverage,
            ),
            (
                29,
                "strategy_decision_state_compaction",
                Database._migrate_strategy_decision_state_compaction,
            ),
            (
                30,
                "kosdaq_market_mode_snapshots",
                Database._migrate_kosdaq_market_mode_snapshots,
            ),
            (
                31,
                "paper_risk_event_exit_types",
                Database._migrate_paper_risk_event_exit_types,
            ),
            (
                32,
                "leader_stock_score_timeseries",
                Database._migrate_leader_stock_score_timeseries,
            ),
            (
                33,
                "risk_exit_taxonomy",
                Database._migrate_risk_exit_taxonomy,
            ),
            (
                34,
                "a_plus_armed_candidates",
                Database._migrate_a_plus_armed_candidates,
            ),
            (
                35,
                "persistent_position_exit_state",
                Database._migrate_persistent_position_exit_state,
            ),
            (
                36,
                "persistent_reentry_engine_lock",
                Database._migrate_persistent_reentry_engine_lock,
            ),
        )
        applied = {
            int(row["version"])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        for version, name, migration in migrations:
            if version in applied:
                continue
            migration(connection)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, name, datetime.now().isoformat(timespec="seconds")),
            )

    @staticmethod
    def _migrate_backtest_columns(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(backtest_trades)"
            ).fetchall()
        }
        migrations = {
            "entry_market_price": (
                "ALTER TABLE backtest_trades "
                "ADD COLUMN entry_market_price INTEGER"
            ),
            "exit_market_price": (
                "ALTER TABLE backtest_trades "
                "ADD COLUMN exit_market_price INTEGER"
            ),
            "slippage_amount": (
                "ALTER TABLE backtest_trades "
                "ADD COLUMN slippage_amount INTEGER NOT NULL DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    @staticmethod
    def _migrate_common_records(connection: sqlite3.Connection) -> None:
        connection.executescript(COMMON_RECORDS_SCHEMA)

    @staticmethod
    def _migrate_strategy_decision_state_compaction(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(strategy_decisions)"
            ).fetchall()
        }
        if "decision_state_key" not in columns:
            connection.execute(
                "ALTER TABLE strategy_decisions ADD COLUMN decision_state_key TEXT"
            )
        if "occurrence_count" not in columns:
            connection.execute(
                "ALTER TABLE strategy_decisions "
                "ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1"
            )
        if "last_seen_at" not in columns:
            connection.execute(
                "ALTER TABLE strategy_decisions ADD COLUMN last_seen_at TEXT"
            )
        connection.execute(
            """
            UPDATE strategy_decisions
            SET decision_state_key = action || ':' || reason
            WHERE decision_state_key IS NULL
            """
        )
        connection.execute(
            """
            UPDATE strategy_decisions
            SET last_seen_at = COALESCE(last_seen_at, updated_at, created_at)
            WHERE last_seen_at IS NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_strategy_decisions_state
            ON strategy_decisions(
                account_id, strategy_version_id, stock_code, decided_at, id
            )
            """
        )

    @staticmethod
    def _migrate_paper_risk_event_exit_types(
        connection: sqlite3.Connection,
    ) -> None:
        row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'paper_risk_events'
            """
        ).fetchone()
        current_sql = str(row["sql"] if row is not None else "")
        if "partial_take_profit" in current_sql and "support_break" in current_sql:
            return
        connection.executescript(
            """
            ALTER TABLE paper_risk_events RENAME TO paper_risk_events_old;

            CREATE TABLE paper_risk_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(
                    event_type IN (
                        'stop_loss', 'take_profit', 'trailing_stop',
                        'support_break', 'volume_dry_up', 'market_close',
                        'partial_take_profit'
                    )
                ),
                trigger_price INTEGER NOT NULL CHECK(trigger_price > 0),
                threshold_price INTEGER NOT NULL CHECK(threshold_price > 0),
                highest_price INTEGER NOT NULL CHECK(highest_price > 0),
                order_id INTEGER,
                status TEXT NOT NULL CHECK(status IN ('executed', 'failed')),
                message TEXT,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
                FOREIGN KEY(order_id) REFERENCES paper_orders(id)
            );

            INSERT INTO paper_risk_events(
                id, account_id, stock_code, event_type, trigger_price,
                threshold_price, highest_price, order_id, status, message,
                occurred_at
            )
            SELECT id, account_id, stock_code, event_type, trigger_price,
                   threshold_price, highest_price, order_id, status, message,
                   occurred_at
            FROM paper_risk_events_old;

            DROP TABLE paper_risk_events_old;

            CREATE INDEX IF NOT EXISTS idx_paper_risk_events_account
            ON paper_risk_events(account_id, occurred_at, id);
            """
        )

    @staticmethod
    def _migrate_risk_exit_taxonomy(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(paper_risk_events)"
            ).fetchall()
        }
        migrations = {
            "exit_class": "TEXT",
            "exit_reason_class": "TEXT",
            "setup_invalidated": (
                "INTEGER NOT NULL DEFAULT 0 CHECK(setup_invalidated IN (0, 1))"
            ),
            "classification_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in migrations.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE paper_risk_events ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            UPDATE paper_risk_events
            SET exit_class = CASE
                WHEN event_type = 'partial_take_profit' THEN
                    CASE
                        WHEN COALESCE((
                            SELECT SUM(f.realized_pnl)
                            FROM paper_fills f
                            WHERE f.order_id = paper_risk_events.order_id
                              AND f.side = 'sell'
                        ), 0) >= 0 THEN 'partial_take_profit'
                        ELSE 'partial_loss_exit'
                    END
                WHEN event_type = 'trailing_stop' THEN
                    CASE
                        WHEN COALESCE((
                            SELECT SUM(f.realized_pnl)
                            FROM paper_fills f
                            WHERE f.order_id = paper_risk_events.order_id
                              AND f.side = 'sell'
                        ), 0) >= 0 THEN 'trailing_profit_lock'
                        ELSE 'trailing_loss_stop'
                    END
                WHEN event_type = 'stop_loss' THEN
                    CASE
                        WHEN COALESCE((
                            SELECT SUM(f.realized_pnl)
                            FROM paper_fills f
                            WHERE f.order_id = paper_risk_events.order_id
                              AND f.side = 'sell'
                        ), 0) >= 0 THEN 'profit_protect_stop'
                        ELSE 'loss_stop'
                    END
                ELSE event_type
            END
            WHERE exit_class IS NULL
            """
        )
        connection.execute(
            """
            UPDATE paper_risk_events
            SET exit_reason_class = CASE
                WHEN event_type = 'stop_loss' AND exit_class = 'profit_protect_stop'
                    THEN 'profit_protect_stop'
                WHEN event_type = 'stop_loss'
                    THEN 'loss_stop'
                WHEN event_type = 'trailing_stop'
                    THEN exit_class
                ELSE event_type
            END
            WHERE exit_reason_class IS NULL
            """
        )
        connection.execute(
            """
            UPDATE paper_risk_events
            SET classification_json = '{}'
            WHERE classification_json IS NULL OR classification_json = ''
            """
        )

    @staticmethod
    def _migrate_dynamic_exit_state(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(paper_risk_profiles)"
            ).fetchall()
        }
        migrations = {
            "initial_stop_price": "INTEGER",
            "atr_value": "REAL",
            "atr_stop_price": "INTEGER",
            "current_stop_price": "INTEGER",
            "current_take_profit_price": "INTEGER",
            "breakeven_active": "INTEGER NOT NULL DEFAULT 0",
            "trend_strength": "TEXT NOT NULL DEFAULT 'neutral'",
            "trend_score": "INTEGER NOT NULL DEFAULT 0",
            "current_trailing_drawdown_rate": "REAL",
            "support_price": "INTEGER",
            "volume_ratio": "REAL",
            "expected_exit_reason": "TEXT",
            "last_rule_reason": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE paper_risk_profiles "
                    f"ADD COLUMN {name} {definition}"
                )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_risk_state_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                current_price INTEGER NOT NULL CHECK(current_price > 0),
                highest_price INTEGER NOT NULL CHECK(highest_price > 0),
                initial_stop_price INTEGER NOT NULL CHECK(initial_stop_price > 0),
                atr_value REAL,
                atr_stop_price INTEGER,
                current_stop_price INTEGER NOT NULL CHECK(current_stop_price > 0),
                current_take_profit_price INTEGER,
                trailing_price INTEGER,
                trailing_active INTEGER NOT NULL CHECK(trailing_active IN (0, 1)),
                breakeven_active INTEGER NOT NULL CHECK(breakeven_active IN (0, 1)),
                trend_strength TEXT NOT NULL CHECK(
                    trend_strength IN ('strong', 'neutral', 'weak')
                ),
                trend_score INTEGER NOT NULL,
                trailing_drawdown_rate REAL NOT NULL,
                support_price INTEGER,
                volume_ratio REAL,
                expected_exit_reason TEXT,
                triggered_exit_reason TEXT,
                rule_reason TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES paper_accounts(id)
            );

            CREATE INDEX IF NOT EXISTS idx_paper_risk_state_events_lookup
            ON paper_risk_state_events(
                account_id, stock_code, evaluated_at, id
            );
            """
        )

    @staticmethod
    def _migrate_candidate_screening(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS stock_universe (
                stock_code TEXT PRIMARY KEY,
                stock_name TEXT,
                market TEXT,
                sector TEXT,
                tradable INTEGER NOT NULL DEFAULT 1 CHECK(tradable IN (0, 1)),
                management INTEGER NOT NULL DEFAULT 0 CHECK(management IN (0, 1)),
                halted INTEGER NOT NULL DEFAULT 0 CHECK(halted IN (0, 1)),
                overheated INTEGER NOT NULL DEFAULT 0 CHECK(overheated IN (0, 1)),
                source TEXT NOT NULL DEFAULT 'manual',
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_stock_universe_filter
            ON stock_universe(tradable, management, halted, overheated, stock_code);

            CREATE TABLE IF NOT EXISTS candidate_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of TEXT NOT NULL,
                universe_count INTEGER NOT NULL,
                eligible_count INTEGER NOT NULL,
                selected_count INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_candidate_runs_as_of
            ON candidate_runs(as_of, id);

            CREATE TABLE IF NOT EXISTS candidate_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                rank INTEGER,
                selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
                excluded_reason TEXT,
                total_score REAL NOT NULL,
                technical_score REAL NOT NULL,
                pattern_score REAL NOT NULL,
                relative_strength_score REAL NOT NULL,
                liquidity_score REAL NOT NULL,
                close_price INTEGER,
                rsi REAL,
                atr REAL,
                relative_strength REAL,
                watched INTEGER NOT NULL CHECK(watched IN (0, 1)),
                patterns_json TEXT NOT NULL,
                features_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                score_change REAL,
                evaluated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES candidate_runs(id) ON DELETE CASCADE,
                UNIQUE(run_id, stock_code)
            );

            CREATE INDEX IF NOT EXISTS idx_candidate_scores_rank
            ON candidate_scores(run_id, selected, rank, total_score);

            CREATE INDEX IF NOT EXISTS idx_candidate_scores_stock
            ON candidate_scores(stock_code, evaluated_at, id);

            CREATE TABLE IF NOT EXISTS pattern_performance_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                horizon_days INTEGER NOT NULL CHECK(horizon_days > 0),
                stock_count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pattern_performance_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                pattern_name TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL,
                win_rate REAL,
                average_forward_return REAL,
                contribution REAL,
                FOREIGN KEY(run_id) REFERENCES pattern_performance_runs(id)
                    ON DELETE CASCADE,
                UNIQUE(run_id, pattern_name)
            );

            CREATE INDEX IF NOT EXISTS idx_pattern_performance_results
            ON pattern_performance_results(run_id, pattern_name);

            INSERT INTO stock_universe(
                stock_code, stock_name, source, updated_at
            )
            SELECT stock_code, MAX(stock_name), 'existing_data',
                   datetime('now', 'localtime')
            FROM (
                SELECT stock_code, stock_name FROM watchlist
                UNION ALL
                SELECT DISTINCT stock_code, NULL FROM candles
            )
            GROUP BY stock_code
            ON CONFLICT(stock_code) DO UPDATE SET
                stock_name = COALESCE(stock_universe.stock_name, excluded.stock_name);
            """
        )

    @staticmethod
    def _migrate_universe_collection(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(stock_universe)"
            ).fetchall()
        }
        additions = {
            "listing_date": "TEXT",
            "security_type": "TEXT NOT NULL DEFAULT 'common_stock'",
            "last_synced_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE stock_universe ADD COLUMN {name} {definition}"
                )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS universe_collection_status (
                stock_code TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(
                    status IN ('pending', 'running', 'completed', 'failed', 'skipped')
                ),
                target_bars INTEGER NOT NULL CHECK(target_bars > 0),
                daily_bar_count INTEGER NOT NULL DEFAULT 0 CHECK(daily_bar_count >= 0),
                latest_candle_time TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                last_error TEXT,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(stock_code) REFERENCES stock_universe(stock_code)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_universe_collection_status
            ON universe_collection_status(status, daily_bar_count, updated_at);
            """
        )

    @staticmethod
    def _migrate_intraday_vwap_patterns(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS intraday_pattern_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                session_date TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL CHECK(interval_minutes > 0),
                source_data_time TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                current_price REAL NOT NULL CHECK(current_price > 0),
                vwap REAL NOT NULL CHECK(vwap > 0),
                vwap_distance_rate REAL NOT NULL,
                reclaim_state TEXT NOT NULL CHECK(
                    reclaim_state IN (
                        'forming', 'ready', 'confirmed', 'failed'
                    )
                ),
                reclaim_score REAL NOT NULL,
                bounce_state TEXT NOT NULL CHECK(
                    bounce_state IN (
                        'forming', 'ready', 'confirmed', 'failed'
                    )
                ),
                bounce_score REAL NOT NULL,
                details_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                UNIQUE(stock_code, interval_minutes, source_data_time)
            );

            CREATE INDEX IF NOT EXISTS idx_intraday_pattern_latest
            ON intraday_pattern_evaluations(
                stock_code, interval_minutes, source_data_time DESC, id DESC
            );

            CREATE INDEX IF NOT EXISTS idx_intraday_pattern_session
            ON intraday_pattern_evaluations(
                session_date, interval_minutes, evaluated_at
            );
            """
        )

    @staticmethod
    def _migrate_intraday_liquidity_sweep(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(intraday_pattern_evaluations)"
            ).fetchall()
        }
        additions = {
            "liquidity_sweep_state": "TEXT NOT NULL DEFAULT 'forming'",
            "liquidity_sweep_side": "TEXT NOT NULL DEFAULT 'none'",
            "liquidity_sweep_score": "REAL NOT NULL DEFAULT 0",
            "liquidity_level": "REAL",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    "ALTER TABLE intraday_pattern_evaluations "
                    f"ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _migrate_intraday_fair_value_gap(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(intraday_pattern_evaluations)"
            ).fetchall()
        }
        additions = {
            "fvg_state": "TEXT NOT NULL DEFAULT 'forming'",
            "fvg_side": "TEXT NOT NULL DEFAULT 'none'",
            "fvg_score": "REAL NOT NULL DEFAULT 0",
            "fvg_lower": "REAL",
            "fvg_upper": "REAL",
            "fvg_fill_rate": "REAL",
            "fvg_formed_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    "ALTER TABLE intraday_pattern_evaluations "
                    f"ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _migrate_intraday_order_block(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(intraday_pattern_evaluations)"
            ).fetchall()
        }
        additions = {
            "order_block_state": "TEXT NOT NULL DEFAULT 'forming'",
            "order_block_side": "TEXT NOT NULL DEFAULT 'none'",
            "order_block_score": "REAL NOT NULL DEFAULT 0",
            "order_block_lower": "REAL",
            "order_block_upper": "REAL",
            "order_block_formed_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    "ALTER TABLE intraday_pattern_evaluations "
                    f"ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _migrate_information_scoring(
        connection: sqlite3.Connection,
    ) -> None:
        candidate_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(candidate_scores)"
            ).fetchall()
        }
        additions = {
            "information_score": "REAL NOT NULL DEFAULT 0",
            "information_blocked": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in candidate_columns:
                connection.execute(
                    f"ALTER TABLE candidate_scores ADD COLUMN {name} {definition}"
                )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS dart_companies (
                corp_code TEXT PRIMARY KEY,
                corp_name TEXT NOT NULL,
                corp_eng_name TEXT,
                stock_code TEXT,
                modify_date TEXT,
                synced_at TEXT NOT NULL,
                UNIQUE(stock_code)
            );

            CREATE INDEX IF NOT EXISTS idx_dart_companies_stock
            ON dart_companies(stock_code, corp_code);

            CREATE TABLE IF NOT EXISTS information_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL CHECK(source IN ('dart', 'naver')),
                source_item_id TEXT NOT NULL,
                stock_code TEXT,
                corp_code TEXT,
                title TEXT NOT NULL,
                summary TEXT,
                url TEXT,
                original_url TEXT,
                normalized_title TEXT NOT NULL,
                canonical_url TEXT,
                logical_key TEXT NOT NULL,
                published_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                event_type TEXT NOT NULL,
                sentiment TEXT NOT NULL CHECK(
                    sentiment IN ('positive', 'neutral', 'negative')
                ),
                base_score REAL NOT NULL,
                importance REAL NOT NULL CHECK(importance BETWEEN 0 AND 1),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                entry_blocked INTEGER NOT NULL DEFAULT 0
                    CHECK(entry_blocked IN (0, 1)),
                raw_json TEXT NOT NULL,
                UNIQUE(source, source_item_id)
            );

            CREATE INDEX IF NOT EXISTS idx_information_items_stock_time
            ON information_items(stock_code, available_at, published_at, id);

            CREATE INDEX IF NOT EXISTS idx_information_items_dedup
            ON information_items(source, canonical_url, normalized_title);

            CREATE INDEX IF NOT EXISTS idx_information_items_logical
            ON information_items(source, logical_key, published_at, id);

            CREATE TABLE IF NOT EXISTS information_score_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                as_of TEXT NOT NULL,
                total_score REAL NOT NULL,
                positive_score REAL NOT NULL,
                negative_score REAL NOT NULL,
                entry_blocked INTEGER NOT NULL CHECK(entry_blocked IN (0, 1)),
                item_count INTEGER NOT NULL CHECK(item_count >= 0),
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(stock_code, as_of)
            );

            CREATE INDEX IF NOT EXISTS idx_information_score_snapshots
            ON information_score_snapshots(as_of, total_score, stock_code);
            """
        )

    @staticmethod
    def _migrate_composite_scoring(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(candidate_scores)"
            ).fetchall()
        }
        additions = {
            "composite_score": "REAL",
            "composite_status": "TEXT",
            "composite_confidence": "REAL",
            "composite_decision_id": "INTEGER",
            "information_item_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE candidate_scores ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_candidate_scores_composite
            ON candidate_scores(
                run_id, composite_status, composite_score DESC, stock_code
            )
            """
        )

    @staticmethod
    def _migrate_ai_shadow_analysis(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_shadow_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_run_id INTEGER,
                composite_decision_id INTEGER,
                stock_code TEXT NOT NULL,
                source_data_time TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('success', 'failed', 'skipped')
                ),
                algorithm_action TEXT NOT NULL,
                algorithm_score REAL NOT NULL,
                ai_action TEXT,
                ai_score REAL,
                ai_confidence REAL,
                agreement TEXT CHECK(
                    agreement IN ('agree', 'disagree', 'partial')
                    OR agreement IS NULL
                ),
                summary TEXT,
                risks_json TEXT NOT NULL DEFAULT '[]',
                catalysts_json TEXT NOT NULL DEFAULT '[]',
                input_tokens INTEGER,
                output_tokens INTEGER,
                estimated_cost REAL,
                latency_ms INTEGER,
                response_id TEXT,
                raw_response_json TEXT,
                error_type TEXT,
                error_message TEXT,
                evaluated_horizon_days INTEGER,
                forward_return REAL,
                performance_status TEXT NOT NULL DEFAULT 'pending' CHECK(
                    performance_status IN (
                        'pending', 'correct', 'incorrect', 'neutral'
                    )
                ),
                evaluated_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(
                    provider, model, prompt_version,
                    stock_code, source_data_time
                ),
                FOREIGN KEY(candidate_run_id)
                    REFERENCES candidate_runs(id) ON DELETE SET NULL,
                FOREIGN KEY(composite_decision_id)
                    REFERENCES strategy_decisions(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ai_shadow_latest
            ON ai_shadow_analyses(created_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_ai_shadow_stock
            ON ai_shadow_analyses(stock_code, source_data_time, id);
            """
        )

    @staticmethod
    def _migrate_trade_plan_records(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS trade_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL UNIQUE,
                stock_code TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                entry_tags_json TEXT NOT NULL DEFAULT '[]',
                entry_price INTEGER NOT NULL CHECK(entry_price > 0),
                planned_quantity INTEGER NOT NULL CHECK(planned_quantity >= 0),
                stop_loss_price INTEGER NOT NULL CHECK(stop_loss_price > 0),
                stop_loss_reason TEXT NOT NULL,
                first_take_profit_price INTEGER NOT NULL CHECK(
                    first_take_profit_price > 0
                ),
                first_take_profit_reason TEXT NOT NULL,
                partial_exit_rate REAL NOT NULL CHECK(
                    partial_exit_rate > 0 AND partial_exit_rate <= 1
                ),
                breakeven_price INTEGER NOT NULL CHECK(breakeven_price > 0),
                breakeven_trigger_price INTEGER NOT NULL CHECK(
                    breakeven_trigger_price > 0
                ),
                trailing_rule TEXT NOT NULL,
                invalidation_reason TEXT NOT NULL,
                reward_risk_ratio REAL NOT NULL,
                setup_json TEXT NOT NULL DEFAULT '{}',
                partial_exit_executed INTEGER NOT NULL DEFAULT 0 CHECK(
                    partial_exit_executed IN (0, 1)
                ),
                partial_exit_quantity INTEGER,
                partial_exit_order_id INTEGER,
                partial_exit_price INTEGER,
                partial_exit_at TEXT,
                breakeven_activated INTEGER NOT NULL DEFAULT 0 CHECK(
                    breakeven_activated IN (0, 1)
                ),
                breakeven_activated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(decision_id)
                    REFERENCES strategy_decisions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_trade_plans_stock
            ON trade_plans(stock_code, entry_type, id);
            """
        )

    @staticmethod
    def _migrate_trade_plan_state(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(trade_plans)"
            ).fetchall()
        }
        additions = {
            "partial_exit_executed": "INTEGER NOT NULL DEFAULT 0",
            "partial_exit_quantity": "INTEGER",
            "partial_exit_order_id": "INTEGER",
            "partial_exit_price": "INTEGER",
            "partial_exit_at": "TEXT",
            "breakeven_activated": "INTEGER NOT NULL DEFAULT 0",
            "breakeven_activated_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE trade_plans ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _migrate_theme_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS theme_groups (
                theme_code TEXT PRIMARY KEY,
                theme_name TEXT NOT NULL,
                stock_count INTEGER,
                change_rate REAL,
                rising_stock_count INTEGER,
                falling_stock_count INTEGER,
                day_profit_rate REAL,
                main_stock TEXT,
                raw_snapshot_id INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_theme_groups_collected
            ON theme_groups(collected_at, change_rate, theme_code);

            CREATE TABLE IF NOT EXISTS theme_components (
                theme_code TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                current_price INTEGER,
                change_rate REAL,
                accumulated_volume INTEGER,
                raw_snapshot_id INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(theme_code, stock_code),
                FOREIGN KEY(theme_code) REFERENCES theme_groups(theme_code)
                    ON DELETE CASCADE,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_theme_components_stock
            ON theme_components(stock_code, collected_at, theme_code);
            """
        )

    @staticmethod
    def _migrate_realtime_rank_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS realtime_item_rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_type TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                big_data_rank INTEGER,
                rank_change INTEGER,
                rank_change_sign TEXT,
                past_current_price INTEGER,
                base_compare_sign TEXT,
                base_compare_change_rate REAL,
                previous_base_sign TEXT,
                previous_base_change_rate REAL,
                ranking_date TEXT,
                ranking_time TEXT,
                observed_at TEXT NOT NULL,
                raw_snapshot_id INTEGER NOT NULL,
                collected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id),
                UNIQUE(query_type, stock_code, observed_at)
            );

            CREATE INDEX IF NOT EXISTS idx_realtime_item_rankings_latest
            ON realtime_item_rankings(query_type, observed_at, big_data_rank, stock_code);

            CREATE INDEX IF NOT EXISTS idx_realtime_item_rankings_stock
            ON realtime_item_rankings(stock_code, observed_at, query_type);
            """
        )

    @staticmethod
    def _migrate_leader_theme_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_rank_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rank_type TEXT NOT NULL,
                api_id TEXT NOT NULL,
                market_type TEXT NOT NULL,
                query_params_json TEXT NOT NULL DEFAULT '{}',
                raw_snapshot_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_market_rank_snapshots_latest
            ON market_rank_snapshots(rank_type, observed_at, id);

            CREATE TABLE IF NOT EXISTS market_rank_items (
                snapshot_id INTEGER NOT NULL,
                rank_type TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                rank INTEGER,
                previous_rank INTEGER,
                rank_change INTEGER,
                current_price INTEGER,
                change_price INTEGER,
                change_rate REAL,
                trade_volume INTEGER,
                previous_trade_volume INTEGER,
                trade_amount INTEGER,
                expected_price INTEGER,
                expected_volume INTEGER,
                sell_quantity INTEGER,
                buy_quantity INTEGER,
                trade_strength REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                collected_at TEXT NOT NULL,
                PRIMARY KEY(snapshot_id, stock_code),
                FOREIGN KEY(snapshot_id) REFERENCES market_rank_snapshots(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_market_rank_items_stock
            ON market_rank_items(stock_code, rank_type, collected_at);

            CREATE INDEX IF NOT EXISTS idx_market_rank_items_rank
            ON market_rank_items(rank_type, rank, trade_amount, stock_code);

            CREATE TABLE IF NOT EXISTS theme_journals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                theme_date TEXT NOT NULL,
                theme_name TEXT NOT NULL,
                strength TEXT NOT NULL CHECK(strength IN ('strong', 'medium', 'weak', 'none')),
                source TEXT NOT NULL DEFAULT 'manual',
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(theme_date, theme_name)
            );

            CREATE INDEX IF NOT EXISTS idx_theme_journals_date
            ON theme_journals(theme_date, strength, theme_name);

            CREATE TABLE IF NOT EXISTS theme_journal_members (
                journal_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                role TEXT NOT NULL CHECK(role IN ('leader', 'second', 'follower', 'watch', 'blocked')),
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(journal_id, stock_code),
                FOREIGN KEY(journal_id) REFERENCES theme_journals(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_theme_journal_members_stock
            ON theme_journal_members(stock_code, role);

            CREATE TABLE IF NOT EXISTS leader_stock_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scored_at TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                theme_code TEXT,
                theme_name TEXT,
                role TEXT NOT NULL CHECK(role IN ('leader', 'second', 'follower', 'watch', 'blocked')),
                total_score REAL NOT NULL,
                rank_score REAL NOT NULL,
                trade_amount_score REAL NOT NULL,
                theme_score REAL NOT NULL,
                persistence_score REAL NOT NULL,
                chart_score REAL NOT NULL,
                risk_score REAL NOT NULL,
                blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
                blocked_reason TEXT,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                components_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(trade_date, stock_code, theme_name)
            );

            CREATE INDEX IF NOT EXISTS idx_leader_stock_scores_latest
            ON leader_stock_scores(trade_date, total_score, role, stock_code);
            """
        )

    @staticmethod
    def _migrate_chart_trading_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS chart_setups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL UNIQUE,
                stock_code TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                setup_name TEXT NOT NULL,
                setup_score REAL NOT NULL,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_chart_setups_stock
            ON chart_setups(stock_code, evaluated_at, setup_name);

            CREATE TABLE IF NOT EXISTS intraday_trade_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL UNIQUE,
                stock_code TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                trigger_name TEXT NOT NULL,
                trigger_state TEXT NOT NULL,
                trigger_score REAL NOT NULL,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_intraday_trade_patterns_stock
            ON intraday_trade_patterns(stock_code, evaluated_at, trigger_name);

            CREATE TABLE IF NOT EXISTS entry_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL UNIQUE,
                stock_code TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                entry_price INTEGER NOT NULL,
                invalidation_price INTEGER,
                first_take_profit_price INTEGER,
                second_take_profit_price INTEGER,
                force_entry INTEGER NOT NULL CHECK(force_entry IN (0, 1)),
                total_chart_score REAL NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_entry_plans_stock
            ON entry_plans(stock_code, created_at, entry_type);

            CREATE TABLE IF NOT EXISTS pattern_block_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                risk_score REAL NOT NULL,
                events_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_pattern_block_events_stock
            ON pattern_block_events(stock_code, evaluated_at, reason);
            """
        )

    @staticmethod
    def _migrate_partial_exit_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS partial_exit_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_plan_id INTEGER NOT NULL UNIQUE,
                decision_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                planned_quantity INTEGER NOT NULL CHECK(planned_quantity >= 0),
                first_exit_rate REAL NOT NULL CHECK(
                    first_exit_rate > 0 AND first_exit_rate < 1
                ),
                first_exit_quantity INTEGER NOT NULL CHECK(
                    first_exit_quantity >= 0
                ),
                residual_quantity INTEGER NOT NULL CHECK(
                    residual_quantity >= 0
                ),
                first_target_price INTEGER NOT NULL CHECK(
                    first_target_price > 0
                ),
                breakeven_price INTEGER NOT NULL CHECK(breakeven_price > 0),
                breakeven_trigger_price INTEGER NOT NULL CHECK(
                    breakeven_trigger_price > 0
                ),
                residual_exit_rule TEXT NOT NULL,
                trailing_rule TEXT NOT NULL,
                invalidation_price INTEGER,
                second_target_price INTEGER,
                status TEXT NOT NULL DEFAULT 'planned',
                first_exit_completed INTEGER NOT NULL DEFAULT 0 CHECK(
                    first_exit_completed IN (0, 1)
                ),
                breakeven_activated INTEGER NOT NULL DEFAULT 0 CHECK(
                    breakeven_activated IN (0, 1)
                ),
                current_zone TEXT NOT NULL DEFAULT 'below_first_target',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_partial_exit_plans_stock
            ON partial_exit_plans(stock_code, status, updated_at);

            CREATE TABLE IF NOT EXISTS partial_exit_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partial_exit_plan_id INTEGER NOT NULL,
                trade_plan_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                exit_stage TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                order_id INTEGER,
                fill_price INTEGER,
                filled_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(partial_exit_plan_id)
                    REFERENCES partial_exit_plans(id) ON DELETE CASCADE,
                FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_partial_exit_fills_plan
            ON partial_exit_fills(trade_plan_id, exit_stage, filled_at);

            CREATE TABLE IF NOT EXISTS breakeven_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partial_exit_plan_id INTEGER NOT NULL,
                trade_plan_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                breakeven_price INTEGER NOT NULL CHECK(breakeven_price > 0),
                order_id INTEGER,
                status TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(partial_exit_plan_id)
                    REFERENCES partial_exit_plans(id) ON DELETE CASCADE,
                FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_breakeven_orders_plan
            ON breakeven_orders(trade_plan_id, activated_at);

            CREATE TABLE IF NOT EXISTS trailing_exit_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partial_exit_plan_id INTEGER NOT NULL UNIQUE,
                trade_plan_id INTEGER NOT NULL UNIQUE,
                stock_code TEXT NOT NULL,
                trailing_rule TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0, 1)),
                reference_price INTEGER,
                trail_price INTEGER,
                current_price INTEGER,
                exit_condition TEXT,
                state_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(partial_exit_plan_id)
                    REFERENCES partial_exit_plans(id) ON DELETE CASCADE,
                FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_trailing_exit_state_stock
            ON trailing_exit_state(stock_code, active, updated_at);
            """
        )

    @staticmethod
    def _migrate_persistent_position_exit_state(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS position_exit_states (
                account_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                trade_plan_id INTEGER,
                state TEXT NOT NULL,
                previous_state TEXT,
                state_entered_at TEXT NOT NULL,
                entry_highest_price INTEGER NOT NULL CHECK(entry_highest_price > 0),
                recent_higher_low INTEGER,
                protection_stop_price INTEGER,
                vwap_warning_at TEXT,
                vwap_warning_count INTEGER NOT NULL DEFAULT 0,
                state_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, stock_code),
                FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
                FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id)
                    ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_position_exit_states_lookup
            ON position_exit_states(state, updated_at, stock_code);

            CREATE TABLE IF NOT EXISTS position_exit_state_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                trade_plan_id INTEGER,
                from_state TEXT,
                to_state TEXT NOT NULL,
                entry_highest_price INTEGER NOT NULL,
                recent_higher_low INTEGER,
                protection_stop_price INTEGER,
                vwap_warning_at TEXT,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                transitioned_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
                FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id)
                    ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_position_exit_state_events_lookup
            ON position_exit_state_events(
                account_id, stock_code, transitioned_at, id
            );
            """
        )

    @staticmethod
    def _migrate_persistent_reentry_engine_lock(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reentry_locks)"
            ).fetchall()
        }
        additions = {
            "status": (
                "TEXT NOT NULL DEFAULT 'active' "
                "CHECK(status IN ('active', 'released', 'superseded'))"
            ),
            "setup_version": "TEXT NOT NULL DEFAULT 'legacy'",
            "setup_json": "TEXT NOT NULL DEFAULT '{}'",
            "released_at": "TEXT",
            "release_reason": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE reentry_locks ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            UPDATE reentry_locks
            SET status = 'released',
                released_at = COALESCE(released_at, lock_until),
                release_reason = COALESCE(
                    release_reason, 'legacy_time_expired_before_v0.12.96'
                )
            WHERE status = 'active'
              AND datetime(replace(lock_until, 'T', ' ')) <= datetime('now')
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reentry_locks_pending
            ON reentry_locks(stock_code, status, created_at, id)
            """
        )

    @staticmethod
    def _migrate_supply_flow_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS broker_flow_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT,
                api_id TEXT NOT NULL,
                flow_type TEXT NOT NULL,
                query_params_json TEXT NOT NULL DEFAULT '{}',
                foreign_sell_estimated INTEGER,
                foreign_buy_estimated INTEGER,
                foreign_net_estimated INTEGER,
                raw_snapshot_id INTEGER,
                observed_at TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_broker_flow_snapshots_latest
            ON broker_flow_snapshots(stock_code, flow_type, observed_at, id);

            CREATE TABLE IF NOT EXISTS broker_flow_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                stock_code TEXT,
                stock_name TEXT,
                side TEXT NOT NULL,
                rank INTEGER,
                broker_code TEXT,
                broker_name TEXT,
                quantity INTEGER,
                quantity_change INTEGER,
                amount INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                collected_at TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES broker_flow_snapshots(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_broker_flow_items_stock
            ON broker_flow_items(stock_code, side, rank, collected_at);

            CREATE TABLE IF NOT EXISTS investor_flow_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT,
                api_id TEXT NOT NULL,
                flow_type TEXT NOT NULL,
                market_type TEXT,
                query_params_json TEXT NOT NULL DEFAULT '{}',
                raw_snapshot_id INTEGER,
                observed_at TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_investor_flow_snapshots_latest
            ON investor_flow_snapshots(stock_code, flow_type, observed_at, id);

            CREATE TABLE IF NOT EXISTS investor_flow_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                stock_code TEXT,
                stock_name TEXT,
                observed_key TEXT,
                current_price INTEGER,
                change_price INTEGER,
                change_rate REAL,
                trade_volume INTEGER,
                trade_amount INTEGER,
                individual INTEGER,
                foreigner INTEGER,
                institution INTEGER,
                finance INTEGER,
                insurance INTEGER,
                trust INTEGER,
                other_finance INTEGER,
                bank INTEGER,
                pension INTEGER,
                private_fund INTEGER,
                nation INTEGER,
                other_corp INTEGER,
                natfor INTEGER,
                sell_value INTEGER,
                buy_value INTEGER,
                net_value INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                collected_at TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES investor_flow_snapshots(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_investor_flow_items_stock
            ON investor_flow_items(stock_code, observed_key, collected_at);

            CREATE TABLE IF NOT EXISTS supply_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                evaluated_at TEXT NOT NULL,
                total_score REAL NOT NULL,
                broker_score REAL NOT NULL,
                foreign_broker_score REAL NOT NULL,
                investor_score REAL NOT NULL,
                program_score REAL NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
                blocked_reason TEXT,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                components_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(stock_code, evaluated_at)
            );

            CREATE INDEX IF NOT EXISTS idx_supply_scores_latest
            ON supply_scores(evaluated_at, total_score, blocked, stock_code);
            """
        )

    @staticmethod
    def _migrate_program_trade_and_forward_maturity(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS program_trade_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                api_id TEXT NOT NULL,
                observed_date TEXT,
                current_price INTEGER,
                change_price INTEGER,
                change_rate REAL,
                trade_volume INTEGER,
                sell_amount INTEGER,
                buy_amount INTEGER,
                net_buy_amount INTEGER,
                net_buy_amount_change INTEGER,
                sell_quantity INTEGER,
                buy_quantity INTEGER,
                net_buy_quantity INTEGER,
                net_buy_quantity_change INTEGER,
                base_price_time TEXT,
                exchange_type TEXT,
                query_params_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                raw_snapshot_id INTEGER,
                collected_at TEXT NOT NULL,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_program_trade_snapshots_latest
            ON program_trade_snapshots(
                stock_code, observed_date, collected_at, id
            );

            DELETE FROM decision_forward_outcomes
            WHERE EXISTS (
                SELECT 1
                FROM strategy_decisions d
                WHERE d.id = decision_forward_outcomes.decision_id
                  AND datetime(decision_forward_outcomes.evaluated_at)
                      < datetime(
                          d.decided_at,
                          '+' || decision_forward_outcomes.horizon_minutes
                              || ' minutes'
                      )
            );
            """
        )

    @staticmethod
    def _migrate_forward_outcome_candle_coverage(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            DELETE FROM decision_forward_outcomes
            WHERE EXISTS (
                SELECT 1
                FROM strategy_decisions d
                WHERE d.id = decision_forward_outcomes.decision_id
                  AND COALESCE(
                      datetime((
                          SELECT MAX(c.candle_time)
                          FROM candles c
                          WHERE c.stock_code =
                                decision_forward_outcomes.stock_code
                            AND c.candle_type = 'minute'
                            AND c.candle_time > d.decided_at
                            AND c.candle_time <= datetime(
                                d.decided_at,
                                '+' ||
                                decision_forward_outcomes.horizon_minutes ||
                                ' minutes'
                            )
                      )),
                      ''
                  ) < datetime(
                      d.decided_at,
                      '+' ||
                      MAX(
                          decision_forward_outcomes.horizon_minutes - 5,
                          0
                      ) ||
                      ' minutes'
                  )
            )
            """
        )

    @staticmethod
    def _migrate_strategy_replay_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategy_replay_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL UNIQUE,
                stock_code TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                action TEXT NOT NULL,
                score REAL NOT NULL,
                reason TEXT NOT NULL,
                leader_score_json TEXT NOT NULL DEFAULT '{}',
                supply_score_json TEXT NOT NULL DEFAULT '{}',
                chart_setup_json TEXT NOT NULL DEFAULT '{}',
                intraday_trigger_json TEXT NOT NULL DEFAULT '{}',
                block_reasons_json TEXT NOT NULL DEFAULT '[]',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

CREATE INDEX IF NOT EXISTS idx_strategy_replay_stock
ON strategy_replay_snapshots(stock_code, evaluated_at, action);

CREATE INDEX IF NOT EXISTS idx_strategy_replay_recent
ON strategy_replay_snapshots(evaluated_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS decision_candle_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                candle_type TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL DEFAULT 0,
                candle_time TEXT NOT NULL,
                open_price INTEGER,
                high_price INTEGER,
                low_price INTEGER,
                close_price INTEGER,
                volume INTEGER,
                relative_position TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_decision_candles_decision
            ON decision_candle_snapshots(decision_id, candle_type, candle_time);

            CREATE TABLE IF NOT EXISTS decision_forward_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                max_up_rate REAL,
                max_down_rate REAL,
                close_rate REAL,
                actual_order_status TEXT,
                actual_outcome TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                evaluated_at TEXT NOT NULL,
                UNIQUE(decision_id, horizon_minutes),
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );
            """
        )

    @staticmethod
    def _migrate_strategy_forward_outcome_horizons(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS decision_forward_outcomes_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                max_up_rate REAL,
                max_down_rate REAL,
                close_rate REAL,
                actual_order_status TEXT,
                actual_outcome TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                evaluated_at TEXT NOT NULL,
                UNIQUE(decision_id, horizon_minutes),
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE CASCADE
            );

            INSERT OR IGNORE INTO decision_forward_outcomes_new(
                decision_id, stock_code, horizon_minutes,
                max_up_rate, max_down_rate, close_rate,
                actual_order_status, actual_outcome,
                details_json, evaluated_at
            )
            SELECT decision_id, stock_code, horizon_minutes,
                   max_up_rate, max_down_rate, close_rate,
                   actual_order_status, actual_outcome,
                   details_json, evaluated_at
            FROM decision_forward_outcomes;

            DROP TABLE decision_forward_outcomes;

            ALTER TABLE decision_forward_outcomes_new
            RENAME TO decision_forward_outcomes;
            """
        )

    @staticmethod
    def _migrate_leader_candidate_outcomes(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leader_candidate_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluated_at TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                candidate_rank INTEGER NOT NULL,
                role_at_pick TEXT NOT NULL,
                score_at_pick REAL NOT NULL,
                theme_code TEXT,
                theme_name TEXT NOT NULL DEFAULT '',
                components_json TEXT NOT NULL DEFAULT '{}',
                same_day_reference_price INTEGER,
                same_day_open_price INTEGER,
                same_day_high_price INTEGER,
                same_day_low_price INTEGER,
                same_day_close_price INTEGER,
                same_day_volume INTEGER,
                same_day_max_up_rate REAL,
                same_day_close_rate REAL,
                next_day_date TEXT,
                next_day_open_price INTEGER,
                next_day_high_price INTEGER,
                next_day_low_price INTEGER,
                next_day_close_price INTEGER,
                next_day_volume INTEGER,
                next_day_max_up_rate REAL,
                next_day_close_rate REAL,
                actual_leader INTEGER NOT NULL CHECK(actual_leader IN (0, 1)),
                actual_leader_rank INTEGER,
                actual_leader_reason TEXT NOT NULL,
                outcome_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(trade_date, stock_code, theme_name)
            );

            CREATE INDEX IF NOT EXISTS idx_leader_candidate_outcomes_date
            ON leader_candidate_outcomes(
                trade_date, actual_leader, actual_leader_rank, candidate_rank
            );

            CREATE INDEX IF NOT EXISTS idx_leader_candidate_outcomes_theme
            ON leader_candidate_outcomes(trade_date, theme_name, actual_leader);
            """
        )

    @staticmethod
    def _migrate_leader_stock_score_timeseries(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS leader_stock_scores_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scored_at TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                theme_code TEXT,
                theme_name TEXT,
                role TEXT NOT NULL CHECK(role IN ('leader', 'second', 'follower', 'watch', 'blocked')),
                total_score REAL NOT NULL,
                rank_score REAL NOT NULL,
                trade_amount_score REAL NOT NULL,
                theme_score REAL NOT NULL,
                persistence_score REAL NOT NULL,
                chart_score REAL NOT NULL,
                risk_score REAL NOT NULL,
                blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
                blocked_reason TEXT,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                components_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            INSERT INTO leader_stock_scores_new(
                id, scored_at, trade_date, stock_code, stock_name,
                theme_code, theme_name, role, total_score, rank_score,
                trade_amount_score, theme_score, persistence_score,
                chart_score, risk_score, blocked, blocked_reason,
                reasons_json, components_json, created_at
            )
            SELECT id, scored_at, trade_date, stock_code, stock_name,
                   theme_code, theme_name, role, total_score, rank_score,
                   trade_amount_score, theme_score, persistence_score,
                   chart_score, risk_score, blocked, blocked_reason,
                   reasons_json, components_json, created_at
            FROM leader_stock_scores;

            DROP TABLE leader_stock_scores;

            ALTER TABLE leader_stock_scores_new
            RENAME TO leader_stock_scores;

            CREATE INDEX IF NOT EXISTS idx_leader_stock_scores_rank
            ON leader_stock_scores(
                trade_date, blocked, total_score, trade_amount_score,
                rank_score, stock_code
            );

            CREATE INDEX IF NOT EXISTS idx_leader_stock_scores_latest
            ON leader_stock_scores(trade_date, stock_code, scored_at, id);
            """
        )

    @staticmethod
    def _migrate_order_safety_events(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS order_safety_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER,
                account_name TEXT NOT NULL,
                execution_mode TEXT NOT NULL CHECK(execution_mode IN ('paper', 'live', 'dry_run')),
                stock_code TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'error')),
                passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
                reason TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '{}',
                occurred_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES strategy_decisions(id)
                    ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_order_safety_events_lookup
            ON order_safety_events(
                occurred_at, execution_mode, account_name, stock_code, event_type
            );
            """
        )

    @staticmethod
    def _migrate_kosdaq_market_mode_snapshots(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_mode_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                market_date TEXT NOT NULL,
                index_value REAL,
                moving_average_20 REAL,
                above_ma20 INTEGER NOT NULL DEFAULT 0 CHECK(above_ma20 IN (0, 1)),
                trade_amount INTEGER,
                mode TEXT NOT NULL CHECK(mode IN ('risk_on', 'neutral', 'risk_off')),
                reliability TEXT NOT NULL CHECK(
                    reliability IN ('reliable', 'partial', 'unavailable')
                ),
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                raw_snapshot_id INTEGER,
                collected_at TEXT NOT NULL,
                FOREIGN KEY(raw_snapshot_id) REFERENCES api_snapshots(id)
            );

            CREATE INDEX IF NOT EXISTS idx_market_mode_snapshots_latest
            ON market_mode_snapshots(market, collected_at DESC, id DESC);
            """
        )

    @staticmethod
    def _migrate_api_budget_records(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_budget_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_group TEXT NOT NULL,
                api_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                status_code INTEGER,
                stock_code TEXT,
                priority TEXT NOT NULL,
                consumed INTEGER NOT NULL CHECK(consumed IN (0, 1)),
                blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
                backoff_seconds INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_api_budget_events_lookup
            ON api_budget_events(api_group, occurred_at, blocked, status_code);

            CREATE INDEX IF NOT EXISTS idx_api_budget_events_stock
            ON api_budget_events(stock_code, occurred_at, api_id);

            CREATE TABLE IF NOT EXISTS api_budget_windows (
                api_group TEXT PRIMARY KEY,
                api_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                window_started_at TEXT NOT NULL,
                window_seconds INTEGER NOT NULL CHECK(window_seconds > 0),
                limit_count INTEGER NOT NULL CHECK(limit_count >= 0),
                used_count INTEGER NOT NULL CHECK(used_count >= 0),
                backoff_until TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_api_budget_windows_backoff
            ON api_budget_windows(backoff_until, api_group);
            """
        )

    @staticmethod
    def _migrate_advanced_partial_exit_records(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS partial_exit_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_plan_id INTEGER NOT NULL,
                partial_exit_plan_id INTEGER,
                stock_code TEXT NOT NULL,
                adjustment_type TEXT NOT NULL,
                old_target INTEGER,
                new_target INTEGER,
                reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(trade_plan_id) REFERENCES trade_plans(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(partial_exit_plan_id)
                    REFERENCES partial_exit_plans(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_partial_exit_adjustments_plan
            ON partial_exit_adjustments(trade_plan_id, adjustment_type, created_at);

            CREATE INDEX IF NOT EXISTS idx_partial_exit_adjustments_stock
            ON partial_exit_adjustments(stock_code, created_at);

            CREATE TABLE IF NOT EXISTS reentry_locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT NOT NULL,
                lock_until TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_decision_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_decision_id)
                    REFERENCES strategy_decisions(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_reentry_locks_stock
            ON reentry_locks(stock_code, lock_until);
            """
        )

    @staticmethod
    def _migrate_a_plus_armed_candidates(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategy_armed_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                trading_day TEXT NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('armed', 'entered', 'disarmed', 'expired')
                ),
                first_armed_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                trigger_type TEXT,
                triggered_at TEXT,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES paper_accounts(id),
                UNIQUE(account_id, stock_code, trading_day)
            );

            CREATE INDEX IF NOT EXISTS idx_strategy_armed_candidates_active
            ON strategy_armed_candidates(
                account_id, trading_day, status, expires_at, stock_code
            );
            """
        )

    def migration_history(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT version, name, applied_at
                FROM schema_migrations
                ORDER BY version
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {
                str(key): Database._json_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [Database._json_value(item) for item in value]
        return value

    def register_strategy(
        self,
        strategy_name: str,
        version: str,
        config: dict[str, Any],
        *,
        description: Optional[str] = None,
    ) -> tuple[int, int]:
        normalized_name = strategy_name.strip()
        normalized_version = version.strip()
        if not normalized_name or not normalized_version:
            raise ValueError("전략 이름과 버전은 비어 있을 수 없습니다.")
        timestamp = datetime.now().isoformat(timespec="seconds")
        config_json = json.dumps(
            self._json_value(config),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        config_hash = sha256(config_json.encode("utf-8")).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE strategy_versions
                SET active = 0, updated_at = ?
                WHERE strategy_name = ? AND version != ?
                """,
                (timestamp, normalized_name, normalized_version),
            )
            connection.execute(
                """
                INSERT INTO strategy_versions(
                    strategy_name, version, description, active,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(strategy_name, version) DO UPDATE SET
                    description = excluded.description,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_name,
                    normalized_version,
                    description,
                    timestamp,
                    timestamp,
                ),
            )
            version_row = connection.execute(
                """
                SELECT id
                FROM strategy_versions
                WHERE strategy_name = ? AND version = ?
                """,
                (normalized_name, normalized_version),
            ).fetchone()
            version_id = int(version_row["id"])
            connection.execute(
                """
                UPDATE strategy_configs
                SET active = 0
                WHERE strategy_version_id = ? AND config_hash != ?
                """,
                (version_id, config_hash),
            )
            connection.execute(
                """
                INSERT INTO strategy_configs(
                    strategy_version_id, config_hash, config_json,
                    active, effective_from, created_at
                )
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(strategy_version_id, config_hash) DO UPDATE SET
                    active = 1
                """,
                (
                    version_id,
                    config_hash,
                    config_json,
                    timestamp,
                    timestamp,
                ),
            )
            config_row = connection.execute(
                """
                SELECT id
                FROM strategy_configs
                WHERE strategy_version_id = ? AND config_hash = ?
                """,
                (version_id, config_hash),
            ).fetchone()
            return version_id, int(config_row["id"])

    def save_strategy_decision(
        self,
        decision: StrategyDecision,
        *,
        account_name: Optional[str],
        strategy_version_id: int,
        strategy_config_id: Optional[int],
        source_data_time: str,
        outcome: str = "evaluated",
        message: Optional[str] = None,
        legacy_signal_id: Optional[int] = None,
    ) -> int:
        decision_id, _ = self.save_strategy_decision_record(
            decision,
            account_name=account_name,
            strategy_version_id=strategy_version_id,
            strategy_config_id=strategy_config_id,
            source_data_time=source_data_time,
            outcome=outcome,
            message=message,
            legacy_signal_id=legacy_signal_id,
        )
        return decision_id

    def save_strategy_decision_record(
        self,
        decision: StrategyDecision,
        *,
        account_name: Optional[str],
        strategy_version_id: int,
        strategy_config_id: Optional[int],
        source_data_time: str,
        outcome: str = "evaluated",
        message: Optional[str] = None,
        legacy_signal_id: Optional[int] = None,
        collapse_unchanged: bool = False,
        state_key: Optional[str] = None,
    ) -> tuple[int, bool]:
        if outcome not in {
            "evaluated",
            "observe",
            "skipped",
            "executed",
            "failed",
        }:
            raise ValueError(f"지원하지 않는 판단 결과입니다: {outcome}")
        timestamp = datetime.now().isoformat(timespec="seconds")
        normalized_state_key = (
            str(state_key).strip()
            if state_key is not None and str(state_key).strip()
            else (
                f"{decision.strategy_name}:{decision.action.value}:"
                f"{decision.reason}"
            )
        )
        with self.connect() as connection:
            account_id = None
            if account_name is not None:
                account = connection.execute(
                    "SELECT id FROM paper_accounts WHERE name = ?",
                    (account_name,),
                ).fetchone()
                if account is None:
                    raise ValueError(f"모의계좌를 찾을 수 없습니다: {account_name}")
                account_id = int(account["id"])

            existing = connection.execute(
                """
                SELECT id
                FROM strategy_decisions
                WHERE account_id IS ?
                  AND strategy_version_id = ?
                  AND stock_code = ?
                  AND source_data_time = ?
                  AND action = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    account_id,
                    strategy_version_id,
                    decision.stock_code,
                    source_data_time,
                    decision.action.value,
                ),
            ).fetchone()
            if existing is None and collapse_unchanged:
                latest = connection.execute(
                    """
                    SELECT id, decision_state_key
                    FROM strategy_decisions
                    WHERE account_id IS ?
                      AND strategy_version_id = ?
                      AND stock_code = ?
                      AND date(decided_at) = date(?)
                    ORDER BY decided_at DESC, id DESC
                    LIMIT 1
                    """,
                    (
                        account_id,
                        strategy_version_id,
                        decision.stock_code,
                        decision.decided_at.isoformat(timespec="seconds"),
                    ),
                ).fetchone()
                if (
                    latest is not None
                    and str(latest["decision_state_key"] or "")
                    == normalized_state_key
                ):
                    existing = latest

            created = existing is None
            if created:
                connection.execute(
                    """
                    INSERT INTO strategy_decisions(
                        account_id, strategy_version_id, strategy_config_id,
                        legacy_signal_id, stock_code, decided_at,
                        source_data_time, action, score, confidence, reason,
                        outcome, message, decision_state_key, occurrence_count,
                        last_seen_at, created_at, updated_at
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                        ?, ?, ?
                    )
                    ON CONFLICT(
                        account_id, strategy_version_id, stock_code,
                        source_data_time, action
                    ) DO UPDATE SET
                        strategy_config_id = excluded.strategy_config_id,
                        score = excluded.score,
                        confidence = excluded.confidence,
                        reason = excluded.reason,
                        decision_state_key = excluded.decision_state_key,
                        occurrence_count = strategy_decisions.occurrence_count + 1,
                        last_seen_at = excluded.last_seen_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        account_id,
                        strategy_version_id,
                        strategy_config_id,
                        legacy_signal_id,
                        decision.stock_code,
                        decision.decided_at.isoformat(timespec="seconds"),
                        source_data_time,
                        decision.action.value,
                        float(decision.score),
                        float(decision.confidence),
                        decision.reason,
                        outcome,
                        message,
                        normalized_state_key,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                decision_row = connection.execute(
                    """
                    SELECT id
                    FROM strategy_decisions
                    WHERE account_id IS ?
                      AND strategy_version_id = ?
                      AND stock_code = ?
                      AND source_data_time = ?
                      AND action = ?
                    """,
                    (
                        account_id,
                        strategy_version_id,
                        decision.stock_code,
                        source_data_time,
                        decision.action.value,
                    ),
                ).fetchone()
                decision_id = int(decision_row["id"])
            else:
                decision_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE strategy_decisions
                    SET occurrence_count = occurrence_count + 1,
                        last_seen_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, timestamp, decision_id),
                )

            connection.executemany(
                """
                INSERT INTO score_components(
                    decision_id, name, score, weight, confidence,
                    weighted_score, source, reason, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                ON CONFLICT(decision_id, name, source) DO UPDATE SET
                    score = excluded.score,
                    weight = excluded.weight,
                    confidence = excluded.confidence,
                    weighted_score = excluded.weighted_score,
                    reason = excluded.reason
                """,
                [
                    (
                        decision_id,
                        component.name,
                        float(component.score),
                        float(component.weight),
                        float(component.confidence),
                        float(component.weighted_score),
                        component.source,
                        component.reason,
                        timestamp,
                    )
                    for component in decision.components
                ],
            )
            return decision_id, created

    def save_trade_plan(self, plan: TradePlan) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trade_plans(
                    decision_id, stock_code, entry_type, entry_tags_json,
                    entry_price, planned_quantity, stop_loss_price,
                    stop_loss_reason, first_take_profit_price,
                    first_take_profit_reason, partial_exit_rate,
                    breakeven_price, breakeven_trigger_price, trailing_rule,
                    invalidation_reason, reward_risk_ratio, setup_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    stock_code = excluded.stock_code,
                    entry_type = excluded.entry_type,
                    entry_tags_json = excluded.entry_tags_json,
                    entry_price = excluded.entry_price,
                    planned_quantity = excluded.planned_quantity,
                    stop_loss_price = excluded.stop_loss_price,
                    stop_loss_reason = excluded.stop_loss_reason,
                    first_take_profit_price = excluded.first_take_profit_price,
                    first_take_profit_reason = excluded.first_take_profit_reason,
                    partial_exit_rate = excluded.partial_exit_rate,
                    breakeven_price = excluded.breakeven_price,
                    breakeven_trigger_price = excluded.breakeven_trigger_price,
                    trailing_rule = excluded.trailing_rule,
                    invalidation_reason = excluded.invalidation_reason,
                    reward_risk_ratio = excluded.reward_risk_ratio,
                    setup_json = excluded.setup_json,
                    updated_at = excluded.updated_at
                """,
                (
                    plan.decision_id,
                    plan.stock_code,
                    plan.entry_type,
                    json.dumps(plan.entry_tags, ensure_ascii=False),
                    plan.entry_price,
                    plan.planned_quantity,
                    plan.stop_loss_price,
                    plan.stop_loss_reason,
                    plan.first_take_profit_price,
                    plan.first_take_profit_reason,
                    float(plan.partial_exit_rate),
                    plan.breakeven_price,
                    plan.breakeven_trigger_price,
                    plan.trailing_rule,
                    plan.invalidation_reason,
                    float(plan.reward_risk_ratio),
                    json.dumps(
                        self._json_value(plan.setup),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT id
                FROM trade_plans
                WHERE decision_id = ?
                """,
                (plan.decision_id,),
            ).fetchone()
            plan_id = int(row["id"] if row is not None else cursor.lastrowid)
            self._upsert_partial_exit_plan(
                connection,
                plan_id=plan_id,
                plan=plan,
                timestamp=timestamp,
            )
            return plan_id

    @staticmethod
    def _upsert_partial_exit_plan(
        connection: sqlite3.Connection,
        *,
        plan_id: int,
        plan: TradePlan,
        timestamp: str,
    ) -> None:
        setup = plan.setup if isinstance(plan.setup, dict) else {}
        strategy_name = str(setup.get("partial_exit_strategy") or plan.entry_type)
        residual_exit_rule = str(
            setup.get("residual_exit_rule") or "breakeven_then_trailing"
        )
        trailing_rule = str(setup.get("partial_trailing_rule") or plan.trailing_rule)
        second_target = setup.get("second_take_profit_price")
        invalidation = setup.get("invalidation_price") or plan.stop_loss_price
        planned_quantity = int(plan.planned_quantity)
        first_exit_quantity = int(
            (
                Decimal(planned_quantity)
                * Decimal(str(plan.partial_exit_rate))
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        first_exit_quantity = min(
            first_exit_quantity,
            max(0, planned_quantity - 1),
        )
        residual_quantity = max(0, planned_quantity - first_exit_quantity)
        details = {
            "entry_tags": list(plan.entry_tags),
            "first_take_profit_reason": plan.first_take_profit_reason,
            "stop_loss_reason": plan.stop_loss_reason,
            "setup": setup,
        }
        connection.execute(
            """
            INSERT INTO partial_exit_plans(
                trade_plan_id, decision_id, stock_code, entry_type,
                strategy_name, planned_quantity, first_exit_rate,
                first_exit_quantity, residual_quantity, first_target_price,
                breakeven_price, breakeven_trigger_price, residual_exit_rule,
                trailing_rule, invalidation_price, second_target_price,
                details_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_plan_id) DO UPDATE SET
                decision_id = excluded.decision_id,
                stock_code = excluded.stock_code,
                entry_type = excluded.entry_type,
                strategy_name = excluded.strategy_name,
                planned_quantity = excluded.planned_quantity,
                first_exit_rate = excluded.first_exit_rate,
                first_exit_quantity = excluded.first_exit_quantity,
                residual_quantity = excluded.residual_quantity,
                first_target_price = excluded.first_target_price,
                breakeven_price = excluded.breakeven_price,
                breakeven_trigger_price = excluded.breakeven_trigger_price,
                residual_exit_rule = excluded.residual_exit_rule,
                trailing_rule = excluded.trailing_rule,
                invalidation_price = excluded.invalidation_price,
                second_target_price = excluded.second_target_price,
                details_json = excluded.details_json,
                updated_at = excluded.updated_at
            """,
            (
                plan_id,
                plan.decision_id,
                plan.stock_code,
                plan.entry_type,
                strategy_name,
                planned_quantity,
                float(plan.partial_exit_rate),
                first_exit_quantity,
                residual_quantity,
                plan.first_take_profit_price,
                plan.breakeven_price,
                plan.breakeven_trigger_price,
                residual_exit_rule,
                trailing_rule,
                int(invalidation) if invalidation is not None else None,
                int(second_target) if second_target is not None else None,
                json.dumps(
                    Database._json_value(details),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            "SELECT id FROM partial_exit_plans WHERE trade_plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return
        connection.execute(
            """
            INSERT INTO trailing_exit_state(
                partial_exit_plan_id, trade_plan_id, stock_code, trailing_rule,
                active, exit_condition, state_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 0, ?, '{}', ?, ?)
            ON CONFLICT(trade_plan_id) DO UPDATE SET
                trailing_rule = excluded.trailing_rule,
                exit_condition = excluded.exit_condition,
                updated_at = excluded.updated_at
            """,
            (
                int(row["id"]),
                plan_id,
                plan.stock_code,
                trailing_rule,
                residual_exit_rule,
                timestamp,
                timestamp,
            ),
        )

    def trade_plan_for_decision(self, decision_id: int) -> Optional[dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM trade_plans
                WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        plan = dict(row)
        plan["entry_tags"] = json.loads(plan.pop("entry_tags_json") or "[]")
        plan["setup"] = json.loads(plan.pop("setup_json") or "{}")
        return plan

    def mark_trade_plan_partial_exit(
        self,
        plan_id: int,
        *,
        quantity: int,
        order_id: Optional[int],
        fill_price: Optional[int],
        executed_at: Optional[str] = None,
        activate_breakeven: bool = True,
    ) -> None:
        timestamp = executed_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE trade_plans
                SET partial_exit_executed = 1,
                    partial_exit_quantity = ?,
                    partial_exit_order_id = ?,
                    partial_exit_price = ?,
                    partial_exit_at = ?,
                    breakeven_activated = ?,
                    breakeven_activated_at = CASE
                        WHEN ? = 1 THEN COALESCE(breakeven_activated_at, ?)
                        ELSE breakeven_activated_at
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    quantity,
                    order_id,
                    fill_price,
                    timestamp,
                    int(activate_breakeven),
                    int(activate_breakeven),
                    timestamp,
                    timestamp,
                    plan_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"매매 계획을 찾을 수 없습니다: {plan_id}")

    def mark_trade_plan_breakeven(
        self,
        plan_id: int,
        *,
        activated_at: Optional[str] = None,
    ) -> None:
        timestamp = activated_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE trade_plans
                SET breakeven_activated = 1,
                    breakeven_activated_at = COALESCE(
                        breakeven_activated_at, ?
                    ),
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, plan_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"매매 계획을 찾을 수 없습니다: {plan_id}")

    def record_partial_exit_fill(
        self,
        plan_id: int,
        *,
        quantity: int,
        order_id: Optional[int],
        fill_price: Optional[int],
        residual_quantity: Optional[int] = None,
        filled_at: Optional[str] = None,
        activate_breakeven: bool = True,
    ) -> None:
        timestamp = filled_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, stock_code, breakeven_price
                FROM partial_exit_plans
                WHERE trade_plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                return
            partial_plan_id = int(row["id"])
            connection.execute(
                """
                UPDATE partial_exit_plans
                SET status = 'partial_taken',
                    first_exit_completed = 1,
                    residual_quantity = COALESCE(?, residual_quantity),
                    breakeven_activated = ?,
                    current_zone = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    residual_quantity,
                    int(activate_breakeven),
                    (
                        "breakeven_active"
                        if activate_breakeven
                        else "trend_runner"
                    ),
                    timestamp,
                    partial_plan_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO partial_exit_fills(
                    partial_exit_plan_id, trade_plan_id, stock_code,
                    exit_stage, quantity, order_id, fill_price,
                    filled_at, created_at
                )
                VALUES (?, ?, ?, 'first_take_profit', ?, ?, ?, ?, ?)
                """,
                (
                    partial_plan_id,
                    plan_id,
                    row["stock_code"],
                    quantity,
                    order_id,
                    fill_price,
                    timestamp,
                    timestamp,
                ),
            )
            if activate_breakeven:
                connection.execute(
                    """
                    INSERT INTO breakeven_orders(
                        partial_exit_plan_id, trade_plan_id, stock_code,
                        breakeven_price, order_id, status, activated_at,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'activated_after_partial', ?, ?)
                    """,
                    (
                        partial_plan_id,
                        plan_id,
                        row["stock_code"],
                        int(row["breakeven_price"]),
                        order_id,
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute(
                """
                UPDATE trailing_exit_state
                    SET active = ?,
                    updated_at = ?
                WHERE trade_plan_id = ?
                """,
                (int(activate_breakeven), timestamp, plan_id),
            )

    def position_exit_state(
        self,
        *,
        account_id: int,
        stock_code: str,
        trade_plan_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM position_exit_states
                WHERE account_id = ? AND stock_code = ?
                """,
                (account_id, stock_code),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if trade_plan_id is not None:
            stored_plan_id = result.get("trade_plan_id")
            if stored_plan_id is None or int(stored_plan_id) != int(trade_plan_id):
                return None
        try:
            result["state_data"] = json.loads(result.pop("state_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["state_data"] = {}
        return result

    def save_position_exit_state(
        self,
        *,
        account_id: int,
        stock_code: str,
        trade_plan_id: Optional[int],
        state: str,
        entry_highest_price: int,
        recent_higher_low: Optional[int],
        protection_stop_price: Optional[int],
        vwap_warning_at: Optional[str],
        vwap_warning_count: int,
        state_data: Optional[dict[str, Any]] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        timestamp = updated_at or datetime.now().isoformat(timespec="seconds")
        encoded = json.dumps(
            self._json_value(state_data or {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT trade_plan_id, state, previous_state, state_entered_at
                FROM position_exit_states
                WHERE account_id = ? AND stock_code = ?
                """,
                (account_id, stock_code),
            ).fetchone()
            same_plan = bool(
                existing is not None
                and (
                    (trade_plan_id is None and existing["trade_plan_id"] is None)
                    or (
                        trade_plan_id is not None
                        and existing["trade_plan_id"] is not None
                        and int(existing["trade_plan_id"]) == int(trade_plan_id)
                    )
                )
            )
            from_state = str(existing["state"]) if same_plan else None
            changed = not same_plan or from_state != state
            previous_state = (
                from_state
                if changed
                else (
                    str(existing["previous_state"])
                    if existing["previous_state"] is not None
                    else None
                )
            )
            entered_at = (
                str(existing["state_entered_at"])
                if same_plan and not changed
                else timestamp
            )
            connection.execute(
                """
                INSERT INTO position_exit_states(
                    account_id, stock_code, trade_plan_id, state,
                    previous_state, state_entered_at, entry_highest_price,
                    recent_higher_low, protection_stop_price,
                    vwap_warning_at, vwap_warning_count, state_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, stock_code) DO UPDATE SET
                    trade_plan_id = excluded.trade_plan_id,
                    state = excluded.state,
                    previous_state = excluded.previous_state,
                    state_entered_at = excluded.state_entered_at,
                    entry_highest_price = excluded.entry_highest_price,
                    recent_higher_low = excluded.recent_higher_low,
                    protection_stop_price = excluded.protection_stop_price,
                    vwap_warning_at = excluded.vwap_warning_at,
                    vwap_warning_count = excluded.vwap_warning_count,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    stock_code,
                    trade_plan_id,
                    state,
                    previous_state,
                    entered_at,
                    entry_highest_price,
                    recent_higher_low,
                    protection_stop_price,
                    vwap_warning_at,
                    vwap_warning_count,
                    encoded,
                    timestamp,
                    timestamp,
                ),
            )
            if changed:
                connection.execute(
                    """
                    INSERT INTO position_exit_state_events(
                        account_id, stock_code, trade_plan_id,
                        from_state, to_state, entry_highest_price,
                        recent_higher_low, protection_stop_price,
                        vwap_warning_at, evidence_json, transitioned_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        stock_code,
                        trade_plan_id,
                        from_state,
                        state,
                        entry_highest_price,
                        recent_higher_low,
                        protection_stop_price,
                        vwap_warning_at,
                        encoded,
                        timestamp,
                    ),
                )

    def record_trade_plan_breakeven(
        self,
        plan_id: int,
        *,
        activated_at: Optional[str] = None,
    ) -> None:
        timestamp = activated_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, stock_code, breakeven_price
                FROM partial_exit_plans
                WHERE trade_plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                return
            connection.execute(
                """
                UPDATE partial_exit_plans
                SET breakeven_activated = 1,
                    current_zone = 'breakeven_active',
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, int(row["id"])),
            )
            connection.execute(
                """
                INSERT INTO breakeven_orders(
                    partial_exit_plan_id, trade_plan_id, stock_code,
                    breakeven_price, status, activated_at, created_at
                )
                VALUES (?, ?, ?, ?, 'activated', ?, ?)
                """,
                (
                    int(row["id"]),
                    plan_id,
                    row["stock_code"],
                    int(row["breakeven_price"]),
                    timestamp,
                    timestamp,
                ),
            )

    def update_partial_exit_state(
        self,
        plan_id: int,
        *,
        current_price: int,
        highest_price: int,
        trail_price: Optional[int],
        trailing_active: bool,
        current_zone: str,
        exit_condition: Optional[str],
        state: Optional[dict[str, Any]] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        timestamp = updated_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id
                FROM partial_exit_plans
                WHERE trade_plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                return
            partial_plan_id = int(row["id"])
            connection.execute(
                """
                UPDATE partial_exit_plans
                SET current_zone = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (current_zone, timestamp, partial_plan_id),
            )
            connection.execute(
                """
                UPDATE trailing_exit_state
                SET active = ?,
                    reference_price = ?,
                    trail_price = ?,
                    current_price = ?,
                    exit_condition = ?,
                    state_json = ?,
                    updated_at = ?
                WHERE trade_plan_id = ?
                """,
                (
                    int(trailing_active),
                    highest_price,
                    trail_price,
                    current_price,
                    exit_condition,
                    json.dumps(
                        self._json_value(state or {}),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    plan_id,
                ),
            )

    def record_partial_exit_adjustment(
        self,
        plan_id: int,
        *,
        adjustment_type: str,
        old_target: Optional[int],
        new_target: Optional[int],
        reason: str,
        evidence: Optional[dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> int:
        timestamp = created_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT tp.stock_code, tp.setup_json, pep.id AS partial_exit_plan_id
                FROM trade_plans tp
                LEFT JOIN partial_exit_plans pep ON pep.trade_plan_id = tp.id
                WHERE tp.id = ?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"매매 계획을 찾을 수 없습니다: {plan_id}")
            try:
                setup = json.loads(row["setup_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                setup = {}
            if not isinstance(setup, dict):
                setup = {}
            adjustment = {
                "adjustment_type": adjustment_type,
                "old_target": old_target,
                "new_target": new_target,
                "reason": reason,
                "evidence": evidence or {},
                "adjusted_at": timestamp,
            }
            setup["partial_exit_adjustment"] = adjustment
            if new_target is not None:
                setup["adjusted_second_take_profit_price"] = int(new_target)
            if adjustment_type == "delay_second_take_profit":
                setup["second_take_profit_deferred"] = True
            cursor = connection.execute(
                """
                INSERT INTO partial_exit_adjustments(
                    trade_plan_id, partial_exit_plan_id, stock_code,
                    adjustment_type, old_target, new_target, reason,
                    evidence_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    row["partial_exit_plan_id"],
                    row["stock_code"],
                    adjustment_type,
                    old_target,
                    new_target,
                    reason,
                    json.dumps(
                        self._json_value(evidence or {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE trade_plans
                SET setup_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        self._json_value(setup),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    plan_id,
                ),
            )
            if new_target is not None:
                connection.execute(
                    """
                    UPDATE partial_exit_plans
                    SET second_target_price = ?, updated_at = ?
                    WHERE trade_plan_id = ?
                    """,
                    (new_target, timestamp, plan_id),
                )
            return int(cursor.lastrowid)

    def update_trade_plan_setup(
        self,
        plan_id: int,
        updates: Dict[str, Any],
        *,
        updated_at: Optional[str] = None,
    ) -> None:
        timestamp = updated_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT setup_json FROM trade_plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"매매 계획을 찾을 수 없습니다: {plan_id}")
            try:
                setup = json.loads(row["setup_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                setup = {}
            if not isinstance(setup, dict):
                setup = {}
            setup.update(self._json_value(updates))
            connection.execute(
                """
                UPDATE trade_plans
                SET setup_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        self._json_value(setup),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    plan_id,
                ),
            )

    def save_reentry_lock(
        self,
        *,
        stock_code: str,
        lock_until: str,
        reason: str,
        source_decision_id: Optional[int] = None,
        created_at: Optional[str] = None,
        setup_version: str = "v0.12.96",
        setup: Optional[dict[str, Any]] = None,
    ) -> int:
        timestamp = created_at or datetime.now().isoformat(timespec="seconds")
        encoded_setup = json.dumps(
            self._json_value(setup or {}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE reentry_locks
                SET status = 'superseded',
                    released_at = ?,
                    release_reason = 'superseded_by_newer_exit_setup',
                    updated_at = ?
                WHERE stock_code = ? AND status = 'active'
                """,
                (timestamp, timestamp, stock_code),
            )
            cursor = connection.execute(
                """
                INSERT INTO reentry_locks(
                    stock_code, lock_until, reason, source_decision_id,
                    created_at, updated_at, status, setup_version, setup_json
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    stock_code,
                    lock_until,
                    reason,
                    source_decision_id,
                    timestamp,
                    timestamp,
                    setup_version,
                    encoded_setup,
                ),
            )
            return int(cursor.lastrowid)

    def active_reentry_lock(
        self,
        stock_code: str,
        *,
        as_of: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        timestamp = as_of or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM reentry_locks
                WHERE stock_code = ?
                  AND status = 'active'
                  AND lock_until > ?
                ORDER BY lock_until DESC, id DESC
                LIMIT 1
                """,
                (stock_code, timestamp),
            ).fetchone()
        return dict(row) if row is not None else None

    def pending_reentry_lock(
        self,
        stock_code: str,
        *,
        as_of: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        timestamp = as_of or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM reentry_locks
                WHERE stock_code = ?
                  AND status = 'active'
                  AND datetime(created_at) <= datetime(?)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code, timestamp),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["setup"] = json.loads(result.pop("setup_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["setup"] = {}
        return result

    def release_reentry_lock(
        self,
        lock_id: int,
        *,
        release_reason: str,
        released_at: Optional[str] = None,
    ) -> bool:
        timestamp = released_at or datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE reentry_locks
                SET status = 'released',
                    lock_until = MIN(lock_until, ?),
                    released_at = ?,
                    release_reason = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (
                    timestamp,
                    timestamp,
                    release_reason,
                    timestamp,
                    lock_id,
                ),
            )
        return cursor.rowcount == 1

    def expire_reentry_lock(
        self,
        stock_code: str,
        *,
        reason_prefix: Optional[str] = None,
        expired_at: Optional[str] = None,
    ) -> int:
        timestamp = expired_at or datetime.now().isoformat(timespec="seconds")
        filters = ["stock_code = ?", "status = 'active'"]
        parameters: list[Any] = [stock_code]
        if reason_prefix is not None:
            filters.append("reason LIKE ?")
            parameters.append(f"{reason_prefix}%")
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE reentry_locks
                SET status = 'released',
                    lock_until = MIN(lock_until, ?),
                    released_at = ?,
                    release_reason = 'legacy_expire_reentry_lock',
                    updated_at = ?
                WHERE {" AND ".join(filters)}
                """,
                (timestamp, timestamp, timestamp, *parameters),
            )
            return int(cursor.rowcount)

    def save_armed_candidate(
        self,
        *,
        account_id: int,
        stock_code: str,
        trading_day: str,
        status: str,
        observed_at: str,
        expires_at: str,
        evidence: dict[str, Any],
        trigger_type: Optional[str] = None,
        triggered_at: Optional[str] = None,
    ) -> int:
        if status not in {"armed", "entered", "disarmed", "expired"}:
            raise ValueError(f"unsupported armed candidate status: {status}")
        payload = json.dumps(
            self._json_value(evidence),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_armed_candidates(
                    account_id, stock_code, trading_day, status,
                    first_armed_at, last_seen_at, expires_at,
                    trigger_type, triggered_at, evidence_json,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, stock_code, trading_day) DO UPDATE SET
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    expires_at = excluded.expires_at,
                    trigger_type = excluded.trigger_type,
                    triggered_at = excluded.triggered_at,
                    evidence_json = excluded.evidence_json,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    stock_code,
                    trading_day,
                    status,
                    observed_at,
                    observed_at,
                    expires_at,
                    trigger_type,
                    triggered_at,
                    payload,
                    observed_at,
                    observed_at,
                ),
            )
            row = connection.execute(
                """
                SELECT id
                FROM strategy_armed_candidates
                WHERE account_id = ? AND stock_code = ? AND trading_day = ?
                """,
                (account_id, stock_code, trading_day),
            ).fetchone()
        return int(row["id"])

    def armed_candidate(
        self,
        *,
        account_id: int,
        stock_code: str,
        trading_day: str,
    ) -> Optional[dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM strategy_armed_candidates
                WHERE account_id = ? AND stock_code = ? AND trading_day = ?
                LIMIT 1
                """,
                (account_id, stock_code, trading_day),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["evidence"] = {}
        return item

    def armed_candidates_for_day(
        self,
        trading_day: str,
        *,
        account_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        filters = ["trading_day = ?"]
        parameters: list[Any] = [trading_day]
        if account_id is not None:
            filters.append("account_id = ?")
            parameters.append(account_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM strategy_armed_candidates
                WHERE {" AND ".join(filters)}
                ORDER BY first_armed_at, id
                """,
                tuple(parameters),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(
                    item.pop("evidence_json") or "{}"
                )
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
            result.append(item)
        return result

    def latest_partial_exit_adjustments(
        self,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM partial_exit_adjustments
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["evidence"] = {}
            result.append(item)
        return result

    def save_shadow_strategy_decisions(
        self,
        decisions: list[StrategyDecision],
        *,
        strategy_version_id: int,
        strategy_config_id: int,
        source_data_time: str,
        message: str,
        state_keys: Optional[dict[str, str]] = None,
    ) -> dict[str, int]:
        timestamp = datetime.now().isoformat(timespec="seconds")
        saved: dict[str, int] = {}
        with self.connect() as connection:
            for decision in decisions:
                state_key = (
                    (state_keys or {}).get(decision.stock_code)
                    or (
                        f"{decision.strategy_name}:{decision.action.value}:"
                        f"{decision.reason}"
                    )
                )
                row = connection.execute(
                    """
                    SELECT id, decision_state_key
                    FROM strategy_decisions
                    WHERE account_id IS NULL
                      AND strategy_version_id = ?
                      AND stock_code = ?
                      AND source_data_time = ?
                      AND action = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        strategy_version_id,
                        decision.stock_code,
                        source_data_time,
                        decision.action.value,
                    ),
                ).fetchone()
                if row is None:
                    latest = connection.execute(
                        """
                        SELECT id, decision_state_key
                        FROM strategy_decisions
                        WHERE account_id IS NULL
                          AND strategy_version_id = ?
                          AND stock_code = ?
                          AND date(decided_at) = date(?)
                        ORDER BY decided_at DESC, id DESC
                        LIMIT 1
                        """,
                        (
                            strategy_version_id,
                            decision.stock_code,
                            decision.decided_at.isoformat(timespec="seconds"),
                        ),
                    ).fetchone()
                    if (
                        latest is not None
                        and str(latest["decision_state_key"] or "") == state_key
                    ):
                        row = latest
                if row is None:
                    decision_id = int(
                        connection.execute(
                            """
                            INSERT INTO strategy_decisions(
                                account_id, strategy_version_id,
                                strategy_config_id, stock_code, decided_at,
                                source_data_time, action, score, confidence,
                                reason, outcome, message, decision_state_key,
                                occurrence_count, last_seen_at,
                                created_at, updated_at
                            )
                            VALUES (
                                NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                'observe', ?, ?, 1, ?, ?, ?
                            )
                            """,
                            (
                                strategy_version_id,
                                strategy_config_id,
                                decision.stock_code,
                                decision.decided_at.isoformat(timespec="seconds"),
                                source_data_time,
                                decision.action.value,
                                float(decision.score),
                                float(decision.confidence),
                                decision.reason,
                                message,
                                state_key,
                                timestamp,
                                timestamp,
                                timestamp,
                            ),
                        ).lastrowid
                    )
                else:
                    decision_id = int(row["id"])
                    connection.execute(
                        """
                        UPDATE strategy_decisions
                        SET strategy_config_id = ?, score = ?,
                            confidence = ?, reason = ?, outcome = 'observe',
                            message = ?, decision_state_key = ?,
                            occurrence_count = occurrence_count + 1,
                            last_seen_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            strategy_config_id,
                            float(decision.score),
                            float(decision.confidence),
                            decision.reason,
                            message,
                            state_key,
                            timestamp,
                            timestamp,
                            decision_id,
                        ),
                    )
                saved[decision.stock_code] = decision_id
                connection.executemany(
                    """
                    INSERT INTO score_components(
                        decision_id, name, score, weight, confidence,
                        weighted_score, source, reason,
                        metadata_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                    ON CONFLICT(decision_id, name, source) DO UPDATE SET
                        score = excluded.score,
                        weight = excluded.weight,
                        confidence = excluded.confidence,
                        weighted_score = excluded.weighted_score,
                        reason = excluded.reason
                    """,
                    [
                        (
                            decision_id,
                            component.name,
                            float(component.score),
                            float(component.weight),
                            float(component.confidence),
                            float(component.weighted_score),
                            component.source,
                            component.reason,
                            timestamp,
                        )
                        for component in decision.components
                    ],
                )
        return saved

    def update_strategy_decision(
        self,
        decision_id: int,
        *,
        outcome: str,
        message: Optional[str] = None,
        legacy_signal_id: Optional[int] = None,
    ) -> None:
        if outcome not in {"observe", "skipped", "executed", "failed"}:
            raise ValueError(f"지원하지 않는 판단 결과입니다: {outcome}")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE strategy_decisions
                SET outcome = ?,
                    message = ?,
                    legacy_signal_id = COALESCE(legacy_signal_id, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    outcome,
                    message,
                    legacy_signal_id,
                    datetime.now().isoformat(timespec="seconds"),
                    decision_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"전략 판단을 찾을 수 없습니다: {decision_id}")

    def save_order_request(
        self,
        request: OrderRequest,
        *,
        decision_id: int,
        execution_mode: str,
    ) -> int:
        if execution_mode not in {"paper", "live"}:
            raise ValueError(f"지원하지 않는 실행 모드입니다: {execution_mode}")
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO order_requests(
                    decision_id, account_name, execution_mode, stock_code,
                    side, order_type, quantity, client_order_id, reason,
                    status, requested_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    decision_id,
                    request.account_name,
                    execution_mode,
                    request.stock_code,
                    request.side.value,
                    request.order_type.value,
                    request.quantity,
                    request.client_order_id,
                    request.reason or request.decision.reason,
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def save_order_execution(
        self,
        order_request_id: int,
        *,
        status: str,
        quantity: int,
        internal_order_id: Optional[int] = None,
        broker_order_id: Optional[str] = None,
        fill_price: Optional[int] = None,
        message: Optional[str] = None,
    ) -> int:
        if status not in {"submitted", "filled", "rejected", "failed"}:
            raise ValueError(f"지원하지 않는 주문 실행 상태입니다: {status}")
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO order_executions(
                    order_request_id, status, internal_order_id,
                    broker_order_id, fill_price, quantity, message, executed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_request_id,
                    status,
                    internal_order_id,
                    broker_order_id,
                    fill_price,
                    quantity,
                    message,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE order_requests
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, timestamp, order_request_id),
            )
            return int(cursor.lastrowid)

    def save_order_safety_event(
        self,
        *,
        account_name: str,
        stock_code: str,
        event_type: str,
        reason: str,
        decision_id: Optional[int] = None,
        execution_mode: str = "paper",
        severity: str = "warning",
        passed: bool = False,
        context: Optional[Dict[str, Any]] = None,
    ) -> int:
        if execution_mode not in {"paper", "live", "dry_run"}:
            raise ValueError(f"지원하지 않는 실행 모드입니다: {execution_mode}")
        if severity not in {"info", "warning", "error"}:
            raise ValueError(f"지원하지 않는 안전 이벤트 등급입니다: {severity}")
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO order_safety_events(
                    decision_id, account_name, execution_mode, stock_code,
                    event_type, severity, passed, reason, context_json,
                    occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    account_name,
                    execution_mode,
                    stock_code,
                    event_type,
                    severity,
                    int(bool(passed)),
                    reason,
                    json.dumps(context or {}, ensure_ascii=False, sort_keys=True),
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def latest_order_safety_events(
        self,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM order_safety_events
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["context"] = json.loads(item.pop("context_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["context"] = {}
            item["passed"] = bool(item.get("passed"))
            result.append(item)
        return result

    def live_order_safety_state(
        self,
        *,
        account_name: str,
        stock_code: str,
        client_order_id: Optional[str] = None,
        as_of_date: Optional[str] = None,
        failure_window: int = 5,
    ) -> dict[str, Any]:
        if failure_window < 1:
            raise ValueError("실패 조회 건수는 1 이상이어야 합니다.")
        day = as_of_date or datetime.now().date().isoformat()
        day_compact = day.replace("-", "")
        normalized_client_order_id = (
            client_order_id.strip()
            if client_order_id is not None and client_order_id.strip()
            else None
        )
        with self.connect() as connection:
            duplicate_request_count = 0
            if normalized_client_order_id is not None:
                duplicate_request_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM order_requests
                        WHERE execution_mode = 'live'
                          AND account_name = ?
                          AND client_order_id = ?
                        """,
                        (account_name, normalized_client_order_id),
                    ).fetchone()[0]
                    or 0
                )
            daily_live_buy_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM order_requests
                    WHERE execution_mode = 'live'
                      AND account_name = ?
                      AND side = 'buy'
                      AND substr(requested_at, 1, 10) = ?
                    """,
                    (account_name, day),
                ).fetchone()[0]
                or 0
            )
            same_stock_live_buy_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM order_requests
                    WHERE execution_mode = 'live'
                      AND account_name = ?
                      AND stock_code = ?
                      AND side = 'buy'
                      AND substr(requested_at, 1, 10) = ?
                    """,
                    (account_name, stock_code, day),
                ).fetchone()[0]
                or 0
            )
            pending_live_same_stock_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM order_requests
                    WHERE execution_mode = 'live'
                      AND account_name = ?
                      AND stock_code = ?
                      AND status IN ('pending', 'submitted')
                    """,
                    (account_name, stock_code),
                ).fetchone()[0]
                or 0
            )
            open_real_order_count = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT COALESCE(order_number, id))
                    FROM real_order_execution_snapshots
                    WHERE stock_code = ?
                      AND remaining_quantity > 0
                      AND (
                        order_date = ?
                        OR order_date = ?
                        OR substr(order_time, 1, 8) = ?
                      )
                      AND COALESCE(status, '') NOT LIKE '%취소%'
                      AND COALESCE(status, '') NOT LIKE '%거부%'
                      AND COALESCE(status, '') NOT LIKE '%거절%'
                    """,
                    (stock_code, day, day_compact, day_compact),
                ).fetchone()[0]
                or 0
            )
            failure_rows = connection.execute(
                """
                SELECT passed, event_type, occurred_at
                FROM order_safety_events
                WHERE execution_mode = 'live'
                  AND account_name = ?
                  AND substr(occurred_at, 1, 10) = ?
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
                (account_name, day, failure_window),
            ).fetchall()

        consecutive_live_failure_count = 0
        for row in failure_rows:
            if bool(row["passed"]):
                break
            consecutive_live_failure_count += 1

        return {
            "as_of_date": day,
            "duplicate_request_count": duplicate_request_count,
            "daily_live_buy_count": daily_live_buy_count,
            "same_stock_live_buy_count": same_stock_live_buy_count,
            "pending_live_same_stock_count": pending_live_same_stock_count,
            "open_real_order_count": open_real_order_count,
            "consecutive_live_failure_count": consecutive_live_failure_count,
        }

    def get_api_budget_window(self, api_group: str) -> Optional[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM api_budget_windows
                WHERE api_group = ?
                """,
                (api_group,),
            ).fetchone()

    def upsert_api_budget_window(
        self,
        *,
        api_group: str,
        api_id: str,
        endpoint: str,
        window_started_at: str,
        window_seconds: int,
        limit_count: int,
        used_count: int,
        backoff_until: Optional[str],
        updated_at: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO api_budget_windows(
                    api_group, api_id, endpoint, window_started_at,
                    window_seconds, limit_count, used_count, backoff_until,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(api_group) DO UPDATE SET
                    api_id = excluded.api_id,
                    endpoint = excluded.endpoint,
                    window_started_at = excluded.window_started_at,
                    window_seconds = excluded.window_seconds,
                    limit_count = excluded.limit_count,
                    used_count = excluded.used_count,
                    backoff_until = excluded.backoff_until,
                    updated_at = excluded.updated_at
                """,
                (
                    api_group,
                    api_id,
                    endpoint,
                    window_started_at,
                    window_seconds,
                    limit_count,
                    used_count,
                    backoff_until,
                    updated_at,
                ),
            )

    def set_api_budget_backoff(
        self,
        *,
        api_group: str,
        api_id: str,
        endpoint: str,
        backoff_until: str,
        updated_at: str,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM api_budget_windows
                WHERE api_group = ?
                """,
                (api_group,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO api_budget_windows(
                        api_group, api_id, endpoint, window_started_at,
                        window_seconds, limit_count, used_count,
                        backoff_until, updated_at
                    )
                    VALUES (?, ?, ?, ?, 60, 0, 0, ?, ?)
                    """,
                    (api_group, api_id, endpoint, updated_at, backoff_until, updated_at),
                )
                return
            connection.execute(
                """
                UPDATE api_budget_windows
                SET api_id = ?, endpoint = ?, backoff_until = ?, updated_at = ?
                WHERE api_group = ?
                """,
                (api_id, endpoint, backoff_until, updated_at, api_group),
            )

    def save_api_budget_event(
        self,
        *,
        decision: Any,
        status_code: Optional[int],
        occurred_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (occurred_at or datetime.now()).isoformat(timespec="seconds")
        priority = getattr(decision.priority, "name", str(decision.priority))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO api_budget_events(
                    api_group, api_id, endpoint, status_code, stock_code,
                    priority, consumed, blocked, backoff_seconds, reason,
                    occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.api_group,
                    decision.api_id,
                    decision.endpoint,
                    status_code,
                    decision.stock_code,
                    priority,
                    int(bool(decision.consumed)),
                    int(bool(decision.blocked)),
                    int(decision.backoff_seconds or 0),
                    decision.reason,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def latest_api_budget_events(
        self,
        *,
        limit: int = 20,
        api_group: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        filters = []
        params: list[Any] = []
        if api_group is not None:
            filters.append("api_group = ?")
            params.append(api_group)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM api_budget_events
                {where}
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["consumed"] = bool(item.get("consumed"))
            item["blocked"] = bool(item.get("blocked"))
            result.append(item)
        return result

    def api_budget_windows_status(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *,
                       MAX(0, limit_count - used_count) AS remaining_count
                FROM api_budget_windows
                ORDER BY api_group
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def backfill_legacy_strategy_signals(
        self,
        *,
        strategy_version_id: int,
        strategy_config_id: int,
    ) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.account_id, s.stock_code, s.signal_time,
                       s.volume_pass, s.breakout_pass, s.moving_average_pass,
                       s.chase_pass, s.signal_pass, s.action, s.message
                FROM strategy_signals s
                LEFT JOIN strategy_decisions d ON d.legacy_signal_id = s.id
                WHERE d.id IS NULL
                ORDER BY s.id
                """
            ).fetchall()
            for row in rows:
                passed_count = sum(
                    int(row[name])
                    for name in (
                        "volume_pass",
                        "breakout_pass",
                        "moving_average_pass",
                        "chase_pass",
                    )
                )
                score = float(Decimal(passed_count) * Decimal("25"))
                action = "buy" if row["signal_pass"] else "hold"
                cursor = connection.execute(
                    """
                    INSERT INTO strategy_decisions(
                        account_id, strategy_version_id, strategy_config_id,
                        legacy_signal_id, stock_code, decided_at,
                        source_data_time, action, score, confidence, reason,
                        outcome, message, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["account_id"],
                        strategy_version_id,
                        strategy_config_id,
                        row["id"],
                        row["stock_code"],
                        row["signal_time"],
                        row["signal_time"],
                        action,
                        score,
                        "기존 strategy_signals 호환 기록",
                        row["action"],
                        row["message"],
                        timestamp,
                        timestamp,
                    ),
                )
                decision_id = int(cursor.lastrowid)
                components = (
                    ("volume", row["volume_pass"], "거래량 조건"),
                    ("breakout", row["breakout_pass"], "돌파 조건"),
                    (
                        "moving_average",
                        row["moving_average_pass"],
                        "이동평균 조건",
                    ),
                    ("chase_limit", row["chase_pass"], "추격 제한"),
                )
                connection.executemany(
                    """
                    INSERT INTO score_components(
                        decision_id, name, score, weight, confidence,
                        weighted_score, source, reason, metadata_json,
                        created_at
                    )
                    VALUES (?, ?, ?, 0.25, 1, ?, 'legacy', ?, '{}', ?)
                    """,
                    [
                        (
                            decision_id,
                            name,
                            100 if passed else 0,
                            25 if passed else 0,
                            reason,
                            timestamp,
                        )
                        for name, passed, reason in components
                    ],
                )
            return len(rows)

    @staticmethod
    def _insert_api_snapshot(
        connection: sqlite3.Connection,
        api_id: str,
        stock_code: Optional[str],
        collected_at: str,
        payload: Dict[str, Any],
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO api_snapshots(api_id, stock_code, collected_at, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                api_id,
                stock_code,
                collected_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return int(cursor.lastrowid)

    def save_order_book(
        self,
        stock_code: str,
        payload: Dict[str, Any],
        levels: list[Dict[str, Any]],
        *,
        quote_time: Optional[str],
        best_ask_price: Optional[int],
        best_ask_quantity: Optional[int],
        best_bid_price: Optional[int],
        best_bid_quantity: Optional[int],
        total_ask_quantity: Optional[int],
        total_bid_quantity: Optional[int],
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, "ka10004", stock_code, timestamp, payload
            )
            cursor = connection.execute(
                """
                INSERT INTO order_book_snapshots(
                    stock_code, quote_time, best_ask_price, best_ask_quantity,
                    best_bid_price, best_bid_quantity, total_ask_quantity,
                    total_bid_quantity, raw_snapshot_id, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stock_code,
                    quote_time,
                    best_ask_price,
                    best_ask_quantity,
                    best_bid_price,
                    best_bid_quantity,
                    total_ask_quantity,
                    total_bid_quantity,
                    raw_id,
                    timestamp,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO order_book_levels(
                    snapshot_id, side, level, price, quantity, quantity_change
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        level["side"],
                        level["level"],
                        level.get("price"),
                        level.get("quantity"),
                        level.get("quantity_change"),
                    )
                    for level in levels
                ],
            )
            return snapshot_id

    def save_trade_strength(
        self,
        stock_code: str,
        payload: Dict[str, Any],
        trades: list[Dict[str, Any]],
        *,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, "ka10003", stock_code, timestamp, payload
            )
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO trade_strength_snapshots(
                    stock_code, trade_time, current_price, trade_quantity,
                    accumulated_volume, accumulated_amount, strength,
                    ask_price, bid_price, exchange_type, raw_snapshot_id,
                    collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    stock_code, trade_time, exchange_type, trade_quantity
                ) DO UPDATE SET
                    current_price = excluded.current_price,
                    accumulated_volume = excluded.accumulated_volume,
                    accumulated_amount = excluded.accumulated_amount,
                    strength = excluded.strength,
                    ask_price = excluded.ask_price,
                    bid_price = excluded.bid_price,
                    raw_snapshot_id = excluded.raw_snapshot_id,
                    collected_at = excluded.collected_at
                """,
                [
                    (
                        stock_code,
                        trade["trade_time"],
                        trade.get("current_price"),
                        trade.get("trade_quantity"),
                        trade.get("accumulated_volume"),
                        trade.get("accumulated_amount"),
                        trade.get("strength"),
                        trade.get("ask_price"),
                        trade.get("bid_price"),
                        trade.get("exchange_type", ""),
                        raw_id,
                        timestamp,
                    )
                    for trade in trades
                ],
            )
            return connection.total_changes - before

    def save_theme_groups(
        self,
        payload: Dict[str, Any],
        groups: list[Dict[str, Any]],
        *,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, "ka90001", None, timestamp, payload
            )
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO theme_groups(
                    theme_code, theme_name, stock_count, change_rate,
                    rising_stock_count, falling_stock_count, day_profit_rate,
                    main_stock, raw_snapshot_id, collected_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_code) DO UPDATE SET
                    theme_name = excluded.theme_name,
                    stock_count = excluded.stock_count,
                    change_rate = excluded.change_rate,
                    rising_stock_count = excluded.rising_stock_count,
                    falling_stock_count = excluded.falling_stock_count,
                    day_profit_rate = excluded.day_profit_rate,
                    main_stock = excluded.main_stock,
                    raw_snapshot_id = excluded.raw_snapshot_id,
                    collected_at = excluded.collected_at,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        str(group["theme_code"]),
                        str(group["theme_name"]),
                        group.get("stock_count"),
                        group.get("change_rate"),
                        group.get("rising_stock_count"),
                        group.get("falling_stock_count"),
                        group.get("day_profit_rate"),
                        group.get("main_stock"),
                        raw_id,
                        timestamp,
                        timestamp,
                    )
                    for group in groups
                    if group.get("theme_code") and group.get("theme_name")
                ],
            )
            return connection.total_changes - before

    def save_theme_components(
        self,
        theme_code: str,
        payload: Dict[str, Any],
        components: list[Dict[str, Any]],
        *,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, "ka90002", str(theme_code), timestamp, payload
            )
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO theme_components(
                    theme_code, stock_code, stock_name, current_price,
                    change_rate, accumulated_volume, raw_snapshot_id,
                    collected_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_code, stock_code) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    current_price = excluded.current_price,
                    change_rate = excluded.change_rate,
                    accumulated_volume = excluded.accumulated_volume,
                    raw_snapshot_id = excluded.raw_snapshot_id,
                    collected_at = excluded.collected_at,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        str(theme_code),
                        str(component["stock_code"]),
                        component.get("stock_name"),
                        component.get("current_price"),
                        component.get("change_rate"),
                        component.get("accumulated_volume"),
                        raw_id,
                        timestamp,
                        timestamp,
                    )
                    for component in components
                    if component.get("stock_code")
                ],
            )
            return connection.total_changes - before

    def save_realtime_item_rankings(
        self,
        query_type: str,
        payload: Dict[str, Any],
        rankings: list[Dict[str, Any]],
        *,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, "ka00198", None, timestamp, payload
            )
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO realtime_item_rankings(
                    query_type, stock_code, stock_name, big_data_rank,
                    rank_change, rank_change_sign, past_current_price,
                    base_compare_sign, base_compare_change_rate,
                    previous_base_sign, previous_base_change_rate,
                    ranking_date, ranking_time, observed_at,
                    raw_snapshot_id, collected_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_type, stock_code, observed_at) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    big_data_rank = excluded.big_data_rank,
                    rank_change = excluded.rank_change,
                    rank_change_sign = excluded.rank_change_sign,
                    past_current_price = excluded.past_current_price,
                    base_compare_sign = excluded.base_compare_sign,
                    base_compare_change_rate = excluded.base_compare_change_rate,
                    previous_base_sign = excluded.previous_base_sign,
                    previous_base_change_rate = excluded.previous_base_change_rate,
                    ranking_date = excluded.ranking_date,
                    ranking_time = excluded.ranking_time,
                    raw_snapshot_id = excluded.raw_snapshot_id,
                    collected_at = excluded.collected_at,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        str(query_type),
                        str(row["stock_code"]),
                        row.get("stock_name"),
                        row.get("big_data_rank"),
                        row.get("rank_change"),
                        row.get("rank_change_sign"),
                        row.get("past_current_price"),
                        row.get("base_compare_sign"),
                        row.get("base_compare_change_rate"),
                        row.get("previous_base_sign"),
                        row.get("previous_base_change_rate"),
                        row.get("ranking_date"),
                        row.get("ranking_time"),
                        self._ranking_observed_at(row, timestamp),
                        raw_id,
                        timestamp,
                        timestamp,
                    )
                    for row in rankings
                    if row.get("stock_code")
                ],
            )
            return connection.total_changes - before

    @staticmethod
    def _ranking_observed_at(row: Dict[str, Any], fallback: str) -> str:
        ranking_date = str(row.get("ranking_date") or "")
        ranking_time = str(row.get("ranking_time") or "")
        try:
            return datetime.strptime(
                ranking_date + ranking_time[:6],
                "%Y%m%d%H%M%S",
            ).isoformat(timespec="seconds")
        except ValueError:
            return fallback

    def latest_realtime_item_rankings(
        self,
        *,
        query_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        filters = []
        parameters: list[Any] = []
        if query_type is not None:
            filters.append("query_type = ?")
            parameters.append(query_type)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        with self.connect() as connection:
            latest = connection.execute(
                f"""
                SELECT MAX(observed_at) AS observed_at
                FROM realtime_item_rankings
                {where}
                """,
                parameters,
            ).fetchone()
            if latest is None or latest["observed_at"] is None:
                return []
            observed_at = latest["observed_at"]
            row_filters = ["observed_at = ?"]
            row_parameters: list[Any] = [observed_at]
            if query_type is not None:
                row_filters.append("query_type = ?")
                row_parameters.append(query_type)
            row_parameters.append(limit)
            return list(
                connection.execute(
                    f"""
                    SELECT query_type, stock_code, stock_name, big_data_rank,
                           rank_change, rank_change_sign, past_current_price,
                           base_compare_sign, base_compare_change_rate,
                           previous_base_sign, previous_base_change_rate,
                           ranking_date, ranking_time, observed_at, collected_at
                    FROM realtime_item_rankings
                    WHERE {" AND ".join(row_filters)}
                    ORDER BY big_data_rank IS NULL, big_data_rank, stock_code
                    LIMIT ?
                    """,
                    row_parameters,
                ).fetchall()
            )

    def save_market_rank_snapshot(
        self,
        *,
        rank_type: str,
        api_id: str,
        market_type: str,
        query_params: Dict[str, Any],
        payload: Dict[str, Any],
        items: list[Dict[str, Any]],
        observed_at: Optional[str] = None,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        observed = observed_at or timestamp
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, api_id, None, timestamp, payload
            )
            snapshot_id = connection.execute(
                """
                INSERT INTO market_rank_snapshots(
                    rank_type, api_id, market_type, query_params_json,
                    raw_snapshot_id, observed_at, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rank_type,
                    api_id,
                    market_type,
                    json.dumps(query_params, ensure_ascii=False, sort_keys=True),
                    raw_id,
                    observed,
                    timestamp,
                ),
            ).lastrowid
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO market_rank_items(
                    snapshot_id, rank_type, stock_code, stock_name, rank,
                    previous_rank, rank_change, current_price, change_price,
                    change_rate, trade_volume, previous_trade_volume,
                    trade_amount, expected_price, expected_volume,
                    sell_quantity, buy_quantity, trade_strength,
                    metadata_json, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        rank_type,
                        item.get("stock_code"),
                        item.get("stock_name"),
                        item.get("rank"),
                        item.get("previous_rank"),
                        item.get("rank_change"),
                        item.get("current_price"),
                        item.get("change_price"),
                        item.get("change_rate"),
                        item.get("trade_volume"),
                        item.get("previous_trade_volume"),
                        item.get("trade_amount"),
                        item.get("expected_price"),
                        item.get("expected_volume"),
                        item.get("sell_quantity"),
                        item.get("buy_quantity"),
                        item.get("trade_strength"),
                        json.dumps(
                            item.get("metadata") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        timestamp,
                    )
                    for item in items
                    if item.get("stock_code")
                ],
            )
            return connection.total_changes - before

    def save_market_mode_snapshot(
        self,
        *,
        market: str,
        market_date: str,
        index_value: float | None,
        moving_average_20: float | None,
        above_ma20: bool,
        trade_amount: int | None,
        mode: str,
        reliability: str,
        reason: str,
        source: str,
        details: Dict[str, Any] | None = None,
        payload: Dict[str, Any] | None = None,
        api_id: str = "market_mode",
        collected_at: Optional[datetime] = None,
    ) -> int:
        if mode not in {"risk_on", "neutral", "risk_off"}:
            raise ValueError(f"지원하지 않는 시장 모드입니다: {mode}")
        if reliability not in {"reliable", "partial", "unavailable"}:
            raise ValueError(f"지원하지 않는 신뢰도입니다: {reliability}")
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        market_key = market.strip().upper()
        with self.connect() as connection:
            raw_id = None
            if payload is not None:
                raw_id = self._insert_api_snapshot(
                    connection,
                    api_id,
                    market_key,
                    timestamp,
                    payload,
                )
            return int(
                connection.execute(
                    """
                    INSERT INTO market_mode_snapshots(
                        market, market_date, index_value, moving_average_20,
                        above_ma20, trade_amount, mode, reliability, reason,
                        source, details_json, raw_snapshot_id, collected_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_key,
                        market_date,
                        index_value,
                        moving_average_20,
                        1 if above_ma20 else 0,
                        trade_amount,
                        mode,
                        reliability,
                        reason,
                        source,
                        json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                        raw_id,
                        timestamp,
                    ),
                ).lastrowid
            )

    def latest_market_mode_snapshot(
        self,
        market: str = "KOSDAQ",
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT *
                FROM market_mode_snapshots
                WHERE market = ?
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (market.strip().upper(),),
            ).fetchone()

    def recent_market_mode_snapshots(
        self,
        market: str = "KOSDAQ",
        *,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT *
                    FROM market_mode_snapshots
                    WHERE market = ?
                    ORDER BY collected_at DESC, id DESC
                    LIMIT ?
                    """,
                    (market.strip().upper(), limit),
                ).fetchall()
            )

    def latest_market_rank_items(
        self,
        *,
        rank_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            params: list[Any] = []
            type_filter = ""
            if rank_type is not None:
                type_filter = "WHERE rank_type = ?"
                params.append(rank_type)
            snapshots = connection.execute(
                f"""
                SELECT rank_type, MAX(id) AS snapshot_id
                FROM market_rank_snapshots
                {type_filter}
                GROUP BY rank_type
                """,
                params,
            ).fetchall()
            snapshot_ids = [int(row["snapshot_id"]) for row in snapshots]
            if not snapshot_ids:
                return []
            placeholders = ",".join("?" for _ in snapshot_ids)
            return list(
                connection.execute(
                    f"""
                    SELECT s.observed_at, s.market_type, i.*
                    FROM market_rank_items i
                    JOIN market_rank_snapshots s ON s.id = i.snapshot_id
                    WHERE i.snapshot_id IN ({placeholders})
                    ORDER BY i.rank_type, i.rank IS NULL, i.rank, i.stock_code
                    LIMIT ?
                    """,
                    [*snapshot_ids, limit],
                ).fetchall()
            )

    def save_broker_flow_snapshot(
        self,
        *,
        stock_code: Optional[str],
        api_id: str,
        flow_type: str,
        query_params: Dict[str, Any],
        payload: Dict[str, Any],
        items: list[Dict[str, Any]],
        foreign_sell_estimated: Optional[int] = None,
        foreign_buy_estimated: Optional[int] = None,
        observed_at: Optional[str] = None,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        observed = observed_at or timestamp
        foreign_net = None
        if foreign_sell_estimated is not None or foreign_buy_estimated is not None:
            foreign_net = (foreign_buy_estimated or 0) + (foreign_sell_estimated or 0)
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, api_id, stock_code, timestamp, payload
            )
            snapshot_id = connection.execute(
                """
                INSERT INTO broker_flow_snapshots(
                    stock_code, api_id, flow_type, query_params_json,
                    foreign_sell_estimated, foreign_buy_estimated,
                    foreign_net_estimated, raw_snapshot_id, observed_at,
                    collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stock_code,
                    api_id,
                    flow_type,
                    json.dumps(query_params, ensure_ascii=False, sort_keys=True),
                    foreign_sell_estimated,
                    foreign_buy_estimated,
                    foreign_net,
                    raw_id,
                    observed,
                    timestamp,
                ),
            ).lastrowid
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO broker_flow_items(
                    snapshot_id, stock_code, stock_name, side, rank,
                    broker_code, broker_name, quantity, quantity_change,
                    amount, metadata_json, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item.get("stock_code") or stock_code,
                        item.get("stock_name"),
                        item["side"],
                        item.get("rank"),
                        item.get("broker_code"),
                        item.get("broker_name"),
                        item.get("quantity"),
                        item.get("quantity_change"),
                        item.get("amount"),
                        json.dumps(
                            item.get("metadata") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        timestamp,
                    )
                    for item in items
                    if item.get("side")
                ],
            )
            return connection.total_changes - before

    def save_investor_flow_snapshot(
        self,
        *,
        stock_code: Optional[str],
        api_id: str,
        flow_type: str,
        market_type: Optional[str],
        query_params: Dict[str, Any],
        payload: Dict[str, Any],
        items: list[Dict[str, Any]],
        observed_at: Optional[str] = None,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        observed = observed_at or timestamp
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, api_id, stock_code, timestamp, payload
            )
            snapshot_id = connection.execute(
                """
                INSERT INTO investor_flow_snapshots(
                    stock_code, api_id, flow_type, market_type,
                    query_params_json, raw_snapshot_id, observed_at,
                    collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stock_code,
                    api_id,
                    flow_type,
                    market_type,
                    json.dumps(query_params, ensure_ascii=False, sort_keys=True),
                    raw_id,
                    observed,
                    timestamp,
                ),
            ).lastrowid
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO investor_flow_items(
                    snapshot_id, stock_code, stock_name, observed_key,
                    current_price, change_price, change_rate, trade_volume,
                    trade_amount, individual, foreigner, institution, finance,
                    insurance, trust, other_finance, bank, pension,
                    private_fund, nation, other_corp, natfor, sell_value,
                    buy_value, net_value, metadata_json, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        item.get("stock_code") or stock_code,
                        item.get("stock_name"),
                        item.get("observed_key"),
                        item.get("current_price"),
                        item.get("change_price"),
                        item.get("change_rate"),
                        item.get("trade_volume"),
                        item.get("trade_amount"),
                        item.get("individual"),
                        item.get("foreigner"),
                        item.get("institution"),
                        item.get("finance"),
                        item.get("insurance"),
                        item.get("trust"),
                        item.get("other_finance"),
                        item.get("bank"),
                        item.get("pension"),
                        item.get("private_fund"),
                        item.get("nation"),
                        item.get("other_corp"),
                        item.get("natfor"),
                        item.get("sell_value"),
                        item.get("buy_value"),
                        item.get("net_value"),
                        json.dumps(
                            item.get("metadata") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        timestamp,
                    )
                    for item in items
                ],
            )
            return connection.total_changes - before

    def save_program_trade_snapshots(
        self,
        *,
        stock_code: str,
        api_id: str,
        query_params: Dict[str, Any],
        payload: Dict[str, Any],
        items: list[Dict[str, Any]],
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, api_id, stock_code, timestamp, payload
            )
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO program_trade_snapshots(
                    stock_code, api_id, observed_date, current_price,
                    change_price, change_rate, trade_volume, sell_amount,
                    buy_amount, net_buy_amount, net_buy_amount_change,
                    sell_quantity, buy_quantity, net_buy_quantity,
                    net_buy_quantity_change, base_price_time, exchange_type,
                    query_params_json, metadata_json, raw_snapshot_id,
                    collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?)
                """,
                [
                    (
                        item.get("stock_code") or stock_code,
                        api_id,
                        item.get("observed_date"),
                        item.get("current_price"),
                        item.get("change_price"),
                        item.get("change_rate"),
                        item.get("trade_volume"),
                        item.get("sell_amount"),
                        item.get("buy_amount"),
                        item.get("net_buy_amount"),
                        item.get("net_buy_amount_change"),
                        item.get("sell_quantity"),
                        item.get("buy_quantity"),
                        item.get("net_buy_quantity"),
                        item.get("net_buy_quantity_change"),
                        item.get("base_price_time"),
                        item.get("exchange_type"),
                        json.dumps(
                            query_params,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            item.get("metadata") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        raw_id,
                        timestamp,
                    )
                    for item in items
                ],
            )
            return connection.total_changes - before

    def save_supply_score(self, score: Dict[str, Any]) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            return int(
                connection.execute(
                    """
                    INSERT INTO supply_scores(
                        stock_code, stock_name, evaluated_at, total_score,
                        broker_score, foreign_broker_score, investor_score,
                        program_score, blocked, blocked_reason, reasons_json,
                        components_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stock_code, evaluated_at) DO UPDATE SET
                        stock_name = excluded.stock_name,
                        total_score = excluded.total_score,
                        broker_score = excluded.broker_score,
                        foreign_broker_score = excluded.foreign_broker_score,
                        investor_score = excluded.investor_score,
                        program_score = excluded.program_score,
                        blocked = excluded.blocked,
                        blocked_reason = excluded.blocked_reason,
                        reasons_json = excluded.reasons_json,
                        components_json = excluded.components_json
                    """,
                    (
                        score["stock_code"],
                        score.get("stock_name"),
                        score["evaluated_at"],
                        score["total_score"],
                        score["broker_score"],
                        score["foreign_broker_score"],
                        score["investor_score"],
                        score.get("program_score", 0),
                        int(bool(score.get("blocked"))),
                        score.get("blocked_reason"),
                        json.dumps(score.get("reasons") or [], ensure_ascii=False),
                        json.dumps(
                            score.get("components") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        timestamp,
                    ),
                ).lastrowid
            )

    def latest_supply_scores(
        self,
        *,
        stock_code: Optional[str] = None,
        limit: int = 20,
        include_components: bool = True,
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        filters = []
        parameters: list[Any] = []
        if stock_code is not None:
            filters.append("stock_code = ?")
            parameters.append(stock_code)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        with self.connect() as connection:
            latest = connection.execute(
                f"""
                SELECT MAX(evaluated_at) AS evaluated_at
                FROM supply_scores
                {where}
                """,
                parameters,
            ).fetchone()
            if latest is None or latest["evaluated_at"] is None:
                return []
            row_filters = ["evaluated_at = ?"]
            row_parameters: list[Any] = [latest["evaluated_at"]]
            if stock_code is not None:
                row_filters.append("stock_code = ?")
                row_parameters.append(stock_code)
            row_parameters.append(limit)
            columns = "*" if include_components else (
                "id, stock_code, stock_name, evaluated_at, total_score, "
                "broker_score, foreign_broker_score, investor_score, "
                "program_score, blocked, blocked_reason, reasons_json, created_at"
            )
            return list(
                connection.execute(
                    f"""
                    SELECT {columns}
                    FROM supply_scores
                    WHERE {" AND ".join(row_filters)}
                    ORDER BY blocked, total_score DESC, stock_code
                    LIMIT ?
                    """,
                    row_parameters,
                ).fetchall()
            )

    def save_strategy_replay_snapshot(
        self,
        *,
        decision_id: int,
        stock_code: str,
        evaluated_at: str,
        action: str,
        score: float,
        reason: str,
        leader_score: Optional[Dict[str, Any]] = None,
        supply_score: Optional[Dict[str, Any]] = None,
        chart_setup: Optional[Dict[str, Any]] = None,
        intraday_trigger: Optional[Dict[str, Any]] = None,
        block_reasons: Optional[list[str]] = None,
        details: Optional[Dict[str, Any]] = None,
        candles: Optional[list[Dict[str, Any]]] = None,
    ) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            snapshot_id = connection.execute(
                """
                INSERT INTO strategy_replay_snapshots(
                    decision_id, stock_code, evaluated_at, action, score,
                    reason, leader_score_json, supply_score_json,
                    chart_setup_json, intraday_trigger_json,
                    block_reasons_json, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    stock_code = excluded.stock_code,
                    evaluated_at = excluded.evaluated_at,
                    action = excluded.action,
                    score = excluded.score,
                    reason = excluded.reason,
                    leader_score_json = excluded.leader_score_json,
                    supply_score_json = excluded.supply_score_json,
                    chart_setup_json = excluded.chart_setup_json,
                    intraday_trigger_json = excluded.intraday_trigger_json,
                    block_reasons_json = excluded.block_reasons_json,
                    details_json = excluded.details_json
                """,
                (
                    decision_id,
                    stock_code,
                    evaluated_at,
                    action,
                    score,
                    reason,
                    json.dumps(leader_score or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(supply_score or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(chart_setup or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        intraday_trigger or {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(block_reasons or [], ensure_ascii=False),
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    timestamp,
                ),
            ).lastrowid
            if candles is not None:
                connection.execute(
                    "DELETE FROM decision_candle_snapshots WHERE decision_id = ?",
                    (decision_id,),
                )
                connection.executemany(
                    """
                    INSERT INTO decision_candle_snapshots(
                        decision_id, stock_code, candle_type, interval_minutes,
                        candle_time, open_price, high_price, low_price,
                        close_price, volume, relative_position, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            decision_id,
                            stock_code,
                            candle["candle_type"],
                            candle.get("interval_minutes", 0),
                            candle["candle_time"],
                            candle.get("open_price"),
                            candle.get("high_price"),
                            candle.get("low_price"),
                            candle.get("close_price"),
                            candle.get("volume"),
                            candle.get("relative_position", "context"),
                            timestamp,
                        )
                        for candle in candles
                    ],
                )
            return int(snapshot_id or decision_id)

    def latest_strategy_replays(
        self,
        *,
        limit: int = 20,
        stock_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        action: Optional[str] = None,
        block_reason: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        filters = []
        parameters: list[Any] = []
        if stock_code:
            filters.append("replay.stock_code = ?")
            parameters.append(stock_code)
        if start_date:
            filters.append("substr(replay.evaluated_at, 1, 10) >= ?")
            parameters.append(start_date)
        if end_date:
            filters.append("substr(replay.evaluated_at, 1, 10) <= ?")
            parameters.append(end_date)
        if action:
            filters.append("lower(replay.action) = ?")
            parameters.append(action.lower())
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        fetch_limit = limit if not block_reason else max(limit * 20, 500)
        parameters.append(fetch_limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT replay.*, universe.stock_name AS stock_name
                FROM strategy_replay_snapshots replay
                LEFT JOIN stock_universe universe
                  ON universe.stock_code = replay.stock_code
                {where}
                ORDER BY replay.evaluated_at DESC, replay.id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            decision_ids = [int(row["decision_id"]) for row in rows]
            candles_by_decision: dict[int, list[dict[str, Any]]] = {
                decision_id: [] for decision_id in decision_ids
            }
            if decision_ids:
                placeholders = ",".join("?" for _ in decision_ids)
                candle_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM decision_candle_snapshots
                    WHERE decision_id IN ({placeholders})
                    ORDER BY decision_id, candle_type, candle_time
                    """,
                    decision_ids,
                ).fetchall()
                for row in candle_rows:
                    item = dict(row)
                    candles_by_decision[int(row["decision_id"])].append(item)
                outcome_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM decision_forward_outcomes
                    WHERE decision_id IN ({placeholders})
                    ORDER BY decision_id, horizon_minutes
                    """,
                    decision_ids,
                ).fetchall()
                outcomes_by_decision: dict[int, list[dict[str, Any]]] = {
                    decision_id: [] for decision_id in decision_ids
                }
                for row in outcome_rows:
                    item = dict(row)
                    try:
                        item["details"] = json.loads(
                            item.pop("details_json") or "{}"
                        )
                    except (TypeError, json.JSONDecodeError):
                        item["details"] = {}
                    outcomes_by_decision[int(row["decision_id"])].append(item)
            else:
                outcomes_by_decision = {}
        result = []
        for row in rows:
            item = dict(row)
            for source, target, fallback in (
                ("leader_score_json", "leader_score", {}),
                ("supply_score_json", "supply_score", {}),
                ("chart_setup_json", "chart_setup", {}),
                ("intraday_trigger_json", "intraday_trigger", {}),
                ("block_reasons_json", "block_reasons", []),
                ("details_json", "details", {}),
            ):
                try:
                    item[target] = json.loads(item.pop(source) or json.dumps(fallback))
                except (TypeError, json.JSONDecodeError):
                    item[target] = fallback
            item["candles"] = candles_by_decision.get(int(row["decision_id"]), [])
            item["forward_outcomes"] = outcomes_by_decision.get(
                int(row["decision_id"]),
                [],
            )
            if block_reason:
                supply_reason = str(
                    (item.get("supply_score") or {}).get("blocked_reason") or ""
                ).strip()
                reasons = {
                    str(reason).strip()
                    for reason in (item.get("block_reasons") or [])
                    if str(reason).strip()
                }
                if block_reason != supply_reason and block_reason not in reasons:
                    continue
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def strategy_replay_review(
        self,
        *,
        limit: int = 30,
        stock_code: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        action: Optional[str] = None,
        block_reason: Optional[str] = None,
    ) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        replays = self.latest_strategy_replays(
            limit=limit,
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            action=action,
            block_reason=block_reason,
        )
        decision_ids = [
            int(replay["decision_id"])
            for replay in replays
            if replay.get("decision_id") is not None
        ]
        trade_plans: dict[int, dict[str, Any]] = {}
        if decision_ids:
            placeholders = ",".join("?" for _ in decision_ids)
            with self.connect() as connection:
                plan_rows = connection.execute(
                    f"""
                    SELECT *
                    FROM trade_plans
                    WHERE decision_id IN ({placeholders})
                    ORDER BY decision_id, id
                    """,
                    decision_ids,
                ).fetchall()
            for plan_row in plan_rows:
                plan = dict(plan_row)
                try:
                    plan["entry_tags"] = json.loads(
                        plan.pop("entry_tags_json") or "[]"
                    )
                except (TypeError, json.JSONDecodeError):
                    plan["entry_tags"] = []
                try:
                    plan["setup"] = json.loads(plan.pop("setup_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    plan["setup"] = {}
                trade_plans[int(plan_row["decision_id"])] = plan
        summary: dict[str, Any] = {
            "total": len(replays),
            "buy_decisions": 0,
            "blocked_decisions": 0,
            "entered_then_rallied": 0,
            "entered_then_dropped": 0,
            "missed_rally_after_block": 0,
            "block_was_helpful": 0,
            "block_neutral": 0,
            "average_max_up_rate": None,
            "average_max_down_rate": None,
        }
        reason_groups: dict[str, dict[str, Any]] = {}
        category_groups: dict[str, dict[str, Any]] = {
            key: self._stock_category_replay_group(key)
            for key in STOCK_CATEGORY_LABELS
        }
        max_up_rates: list[float] = []
        max_down_rates: list[float] = []
        rows: list[dict[str, Any]] = []
        for replay in replays:
            stock_category = stock_category_payload(
                replay.get("stock_code"),
                replay.get("stock_name"),
            )
            action = str(replay.get("action") or "")
            if action.lower() == "buy":
                summary["buy_decisions"] += 1
            supply = replay.get("supply_score") or {}
            block_reasons = replay.get("block_reasons") or []
            blocked = bool(supply.get("blocked")) or bool(block_reasons)
            if blocked:
                summary["blocked_decisions"] += 1
            outcome = self._representative_replay_outcome(
                replay.get("forward_outcomes") or []
            )
            actual = str(outcome.get("actual_outcome") or "") if outcome else ""
            if actual in summary:
                summary[actual] += 1
            if outcome:
                if outcome.get("max_up_rate") is not None:
                    max_up_rates.append(float(outcome["max_up_rate"]))
                if outcome.get("max_down_rate") is not None:
                    max_down_rates.append(float(outcome["max_down_rate"]))
            category_group = category_groups[stock_category["key"]]
            category_group["count"] += 1
            if actual in (
                "missed_rally_after_block",
                "block_was_helpful",
                "block_neutral",
                "entered_then_rallied",
                "entered_then_dropped",
            ):
                category_group[actual] += 1
            if outcome and outcome.get("max_up_rate") is not None:
                category_group["max_up_rate_sum"] += float(outcome["max_up_rate"])
                category_group["max_up_rate_count"] += 1
            reason = (
                str(supply.get("blocked_reason") or "").strip()
                or (str(block_reasons[0]).strip() if block_reasons else "")
                or ("진입 차단" if blocked else "")
            )
            if reason:
                group = reason_groups.setdefault(
                    reason,
                    {
                        "reason": reason,
                        "count": 0,
                        "missed_rally_after_block": 0,
                        "block_was_helpful": 0,
                        "block_neutral": 0,
                        "max_up_rate_sum": 0.0,
                        "max_up_rate_count": 0,
                    },
                )
                group["count"] += 1
                if actual in (
                    "missed_rally_after_block",
                    "block_was_helpful",
                    "block_neutral",
                ):
                    group[actual] += 1
                if outcome and outcome.get("max_up_rate") is not None:
                    group["max_up_rate_sum"] += float(outcome["max_up_rate"])
                    group["max_up_rate_count"] += 1
            row = {
                "decision_id": replay.get("decision_id"),
                "stock_code": replay.get("stock_code"),
                "stock_name": replay.get("stock_name"),
                "stock_category": stock_category,
                "evaluated_at": replay.get("evaluated_at"),
                "action": action,
                "score": replay.get("score"),
                "reason": replay.get("reason"),
                "blocked": blocked,
                "block_reasons": block_reasons,
                "supply_score": supply,
                "leader_score": replay.get("leader_score") or {},
                "chart_setup": replay.get("chart_setup") or {},
                "intraday_trigger": replay.get("intraday_trigger") or {},
                "primary_outcome": outcome,
                "forward_outcomes": replay.get("forward_outcomes") or [],
                "candles": replay.get("candles") or [],
                "trade_plan": trade_plans.get(int(replay["decision_id"])),
            }
            rows.append(row)
        if max_up_rates:
            summary["average_max_up_rate"] = sum(max_up_rates) / len(max_up_rates)
        if max_down_rates:
            summary["average_max_down_rate"] = (
                sum(max_down_rates) / len(max_down_rates)
            )
        groups = []
        for group in reason_groups.values():
            count = int(group.pop("max_up_rate_count"))
            total = float(group.pop("max_up_rate_sum"))
            group["average_max_up_rate"] = total / count if count else None
            groups.append(group)
        groups.sort(
            key=lambda item: (
                int(item["missed_rally_after_block"]),
                int(item["count"]),
            ),
            reverse=True,
        )
        category_stats = self._finalize_stock_category_replay_groups(
            category_groups,
        )
        return {
            "summary": summary,
            "by_block_reason": groups[:10],
            "by_stock_category": category_stats,
            "filters": self._strategy_replay_filter_options(),
            "applied_filters": {
                "stock_code": stock_code,
                "start_date": start_date,
                "end_date": end_date,
                "action": action,
                "block_reason": block_reason,
            },
            "long_term_stats": self._strategy_forward_long_term_stats(),
            "rows": rows,
        }

    def _strategy_replay_filter_options(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code, action, evaluated_at,
                       supply_score_json, block_reasons_json
                FROM strategy_replay_snapshots
                ORDER BY evaluated_at DESC, id DESC
                LIMIT 5000
                """
            ).fetchall()
        stock_codes = set()
        actions = set()
        block_reasons = set()
        dates = []
        for row in rows:
            if row["stock_code"]:
                stock_codes.add(str(row["stock_code"]))
            if row["action"]:
                actions.add(str(row["action"]))
            if row["evaluated_at"]:
                dates.append(str(row["evaluated_at"])[:10])
            supply = self._loads_json(row["supply_score_json"], {})
            supply_reason = str(supply.get("blocked_reason") or "").strip()
            if supply_reason:
                block_reasons.add(supply_reason)
            for reason in self._loads_json(row["block_reasons_json"], []):
                text = str(reason).strip()
                if text:
                    block_reasons.add(text)
        return {
            "stock_codes": sorted(stock_codes),
            "actions": sorted(actions),
            "block_reasons": sorted(block_reasons),
            "min_date": min(dates) if dates else None,
            "max_date": max(dates) if dates else None,
        }

    def _strategy_forward_long_term_stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT o.decision_id, o.stock_code, o.horizon_minutes,
                       o.max_up_rate, o.max_down_rate, o.close_rate,
                       o.actual_outcome, d.action, d.outcome AS decision_outcome,
                       tp.entry_type, tp.setup_json,
                       rs.supply_score_json, rs.block_reasons_json,
                       universe.stock_name
                FROM decision_forward_outcomes o
                JOIN strategy_decisions d ON d.id = o.decision_id
                LEFT JOIN trade_plans tp ON tp.decision_id = d.id
                LEFT JOIN strategy_replay_snapshots rs
                  ON rs.decision_id = d.id
                LEFT JOIN stock_universe universe
                  ON universe.stock_code = o.stock_code
                ORDER BY o.evaluated_at DESC, o.decision_id DESC
                LIMIT 2000
                """
            ).fetchall()
        by_entry_type: dict[str, dict[str, Any]] = {}
        by_supply_block_reason: dict[str, dict[str, Any]] = {}
        by_stock_category: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            entry_type = str(item.get("entry_type") or "none")
            self._accumulate_forward_stat(by_entry_type, entry_type, item)
            category = stock_category_payload(
                item.get("stock_code"),
                item.get("stock_name"),
            )
            self._accumulate_forward_stat(
                by_stock_category,
                category["key"],
                item,
            )
            supply = self._loads_json(item.get("supply_score_json"), {})
            block_reasons = self._loads_json(item.get("block_reasons_json"), [])
            reason = str(supply.get("blocked_reason") or "").strip()
            if not reason and block_reasons:
                reason = str(block_reasons[0]).strip()
            if reason:
                self._accumulate_forward_stat(
                    by_supply_block_reason,
                    reason,
                    item,
                )
        return {
            "by_entry_type": self._finalize_forward_stats(by_entry_type)[:20],
            "by_supply_block_reason": self._finalize_forward_stats(
                by_supply_block_reason
            )[:20],
            "by_stock_category": self._with_stock_category_labels(
                self._finalize_forward_stats(by_stock_category)
            ),
        }

    @staticmethod
    def _stock_category_replay_group(key: str) -> dict[str, Any]:
        return {
            "category": key,
            "label": stock_category_label(key),
            "count": 0,
            "missed_rally_after_block": 0,
            "block_was_helpful": 0,
            "block_neutral": 0,
            "entered_then_rallied": 0,
            "entered_then_dropped": 0,
            "max_up_rate_sum": 0.0,
            "max_up_rate_count": 0,
        }

    @staticmethod
    def _finalize_stock_category_replay_groups(
        groups: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result = []
        for key in STOCK_CATEGORY_LABELS:
            group = dict(groups[key])
            count = int(group.pop("max_up_rate_count"))
            total = float(group.pop("max_up_rate_sum"))
            group["average_max_up_rate"] = total / count if count else None
            result.append(group)
        return result

    @staticmethod
    def _with_stock_category_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key = {str(row.get("key")): row for row in rows}
        result = []
        for key, label in STOCK_CATEGORY_LABELS.items():
            row = by_key.get(key)
            if row is None:
                row = {
                    "key": key,
                    "count": 0,
                    "missed_rally_after_block": 0,
                    "block_was_helpful": 0,
                    "entered_then_rallied": 0,
                    "entered_then_dropped": 0,
                    "average_max_up_rate": None,
                    "average_max_down_rate": None,
                }
            row["label"] = label
            result.append(row)
        return result

    @staticmethod
    def _accumulate_forward_stat(
        groups: dict[str, dict[str, Any]],
        key: str,
        item: dict[str, Any],
    ) -> None:
        group = groups.setdefault(
            key,
            {
                "key": key,
                "count": 0,
                "missed_rally_after_block": 0,
                "block_was_helpful": 0,
                "entered_then_rallied": 0,
                "entered_then_dropped": 0,
                "max_up_rate_sum": 0.0,
                "max_up_rate_count": 0,
                "max_down_rate_sum": 0.0,
                "max_down_rate_count": 0,
            },
        )
        group["count"] += 1
        actual = str(item.get("actual_outcome") or "")
        if actual in group:
            group[actual] += 1
        if item.get("max_up_rate") is not None:
            group["max_up_rate_sum"] += float(item["max_up_rate"])
            group["max_up_rate_count"] += 1
        if item.get("max_down_rate") is not None:
            group["max_down_rate_sum"] += float(item["max_down_rate"])
            group["max_down_rate_count"] += 1

    @staticmethod
    def _finalize_forward_stats(
        groups: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result = []
        for group in groups.values():
            up_count = int(group.pop("max_up_rate_count"))
            up_sum = float(group.pop("max_up_rate_sum"))
            down_count = int(group.pop("max_down_rate_count"))
            down_sum = float(group.pop("max_down_rate_sum"))
            group["average_max_up_rate"] = up_sum / up_count if up_count else None
            group["average_max_down_rate"] = (
                down_sum / down_count if down_count else None
            )
            result.append(group)
        result.sort(
            key=lambda item: (
                int(item["missed_rally_after_block"]),
                int(item["entered_then_dropped"]),
                int(item["count"]),
            ),
            reverse=True,
        )
        return result

    @staticmethod
    def _loads_json(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _representative_replay_outcome(
        outcomes: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not outcomes:
            return None
        priority = {
            "missed_rally_after_block": 60,
            "entered_then_dropped": 50,
            "entered_then_rallied": 40,
            "block_was_helpful": 30,
            "block_neutral": 20,
            "entered_flat": 10,
        }
        return max(
            outcomes,
            key=lambda item: (
                priority.get(str(item.get("actual_outcome") or ""), 0),
                int(item.get("horizon_minutes") or 0),
            ),
        )

    def recompute_decision_forward_outcomes(
        self,
        *,
        target_date: Optional[str] = None,
        horizons: tuple[int, ...] = (30, 60, 120),
        limit: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        if not horizons:
            return {
                "target_date": target_date,
                "horizons": [],
                "representative_decisions": 0,
                "represented_occurrences": 0,
                "saved": 0,
                "pending": 0,
                "unavailable": 0,
            }
        current = now or datetime.now()
        if current.tzinfo is not None:
            current = current.replace(tzinfo=None)
        timestamp = current.isoformat(timespec="seconds")
        saved = 0
        pending = 0
        unavailable = 0
        with self.connect() as connection:
            filters = []
            parameters: list[Any] = []
            if target_date is not None:
                filters.append("substr(rs.evaluated_at, 1, 10) = ?")
                parameters.append(target_date)
            where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
            limit_sql = "LIMIT ?" if limit is not None else ""
            if limit is not None:
                parameters.append(limit)
            decisions = connection.execute(
                f"""
                SELECT rs.decision_id, rs.stock_code, rs.evaluated_at,
                       rs.action, rs.score, d.occurrence_count,
                       (
                           SELECT r.status
                           FROM order_requests r
                           WHERE r.decision_id = rs.decision_id
                           ORDER BY r.id DESC
                           LIMIT 1
                       ) AS order_status,
                       d.outcome AS decision_outcome
                FROM strategy_replay_snapshots rs
                JOIN strategy_decisions d ON d.id = rs.decision_id
                {where_sql}
                ORDER BY rs.evaluated_at DESC, rs.id DESC
                {limit_sql}
                """,
                parameters,
            ).fetchall()
            represented_occurrences = sum(
                int(decision["occurrence_count"] or 1)
                for decision in decisions
            )
            for decision in decisions:
                entry = connection.execute(
                    """
                    SELECT close_price
                    FROM decision_candle_snapshots
                    WHERE decision_id = ?
                      AND candle_type = 'minute'
                      AND close_price IS NOT NULL
                    ORDER BY candle_time DESC
                    LIMIT 1
                    """,
                    (decision["decision_id"],),
                ).fetchone()
                if entry is None or entry["close_price"] in (None, 0):
                    unavailable += len(horizons)
                    continue
                entry_price = int(entry["close_price"])
                evaluated_at = str(decision["evaluated_at"])
                max_end_at = (
                    datetime.fromisoformat(evaluated_at)
                    + timedelta(minutes=max(horizons))
                ).isoformat(timespec="seconds")
                after_rows = connection.execute(
                    """
                    SELECT candle_time, open_price, high_price, low_price,
                           close_price, volume, interval_minutes
                    FROM candles
                    WHERE stock_code = ?
                      AND candle_type = 'minute'
                      AND candle_time > ?
                      AND candle_time <= ?
                      AND close_price IS NOT NULL
                    ORDER BY candle_time
                    """,
                    (decision["stock_code"], evaluated_at, max_end_at),
                ).fetchall()
                connection.execute(
                    """
                    DELETE FROM decision_candle_snapshots
                    WHERE decision_id = ?
                      AND candle_type = 'minute'
                      AND relative_position = 'after'
                    """,
                    (decision["decision_id"],),
                )
                connection.executemany(
                    """
                    INSERT INTO decision_candle_snapshots(
                        decision_id, stock_code, candle_type, interval_minutes,
                        candle_time, open_price, high_price, low_price,
                        close_price, volume, relative_position, created_at
                    )
                    VALUES (?, ?, 'minute', ?, ?, ?, ?, ?, ?, ?, 'after', ?)
                    """,
                    [
                        (
                            decision["decision_id"],
                            decision["stock_code"],
                            int(row["interval_minutes"] or 0),
                            row["candle_time"],
                            row["open_price"],
                            row["high_price"],
                            row["low_price"],
                            row["close_price"],
                            row["volume"],
                            timestamp,
                        )
                        for row in after_rows
                    ],
                )
                for horizon in horizons:
                    horizon_end = datetime.fromisoformat(evaluated_at) + timedelta(
                        minutes=horizon
                    )
                    if current < horizon_end:
                        pending += 1
                        continue
                    end_at = horizon_end.isoformat(timespec="seconds")
                    rows = connection.execute(
                        """
                        SELECT high_price, low_price, close_price, candle_time,
                               interval_minutes
                        FROM candles
                        WHERE stock_code = ?
                          AND candle_type = 'minute'
                          AND candle_time > ?
                          AND candle_time <= ?
                          AND close_price IS NOT NULL
                        ORDER BY candle_time
                        """,
                        (decision["stock_code"], evaluated_at, end_at),
                    ).fetchall()
                    if not rows:
                        unavailable += 1
                        continue
                    interval_minutes = max(
                        1,
                        min(
                            int(row["interval_minutes"] or 5)
                            for row in rows
                        ),
                    )
                    coverage_cutoff = horizon_end - timedelta(
                        minutes=interval_minutes
                    )
                    last_candle_time = datetime.fromisoformat(
                        str(rows[-1]["candle_time"])
                    )
                    if last_candle_time < coverage_cutoff:
                        unavailable += 1
                        continue
                    highs = [
                        int(row["high_price"])
                        for row in rows
                        if row["high_price"] is not None
                    ]
                    lows = [
                        int(row["low_price"])
                        for row in rows
                        if row["low_price"] is not None
                    ]
                    if not highs or not lows:
                        unavailable += 1
                        continue
                    close_price = int(rows[-1]["close_price"])
                    max_up_rate = (max(highs) - entry_price) / entry_price
                    max_down_rate = (min(lows) - entry_price) / entry_price
                    close_rate = (close_price - entry_price) / entry_price
                    actual = self._classify_forward_outcome(
                        action=str(decision["action"]),
                        max_up_rate=max_up_rate,
                        max_down_rate=max_down_rate,
                        order_status=decision["order_status"],
                    )
                    connection.execute(
                        """
                        INSERT INTO decision_forward_outcomes(
                            decision_id, stock_code, horizon_minutes,
                            max_up_rate, max_down_rate, close_rate,
                            actual_order_status, actual_outcome,
                            details_json, evaluated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(decision_id, horizon_minutes) DO UPDATE SET
                            stock_code = excluded.stock_code,
                            horizon_minutes = excluded.horizon_minutes,
                            max_up_rate = excluded.max_up_rate,
                            max_down_rate = excluded.max_down_rate,
                            close_rate = excluded.close_rate,
                            actual_order_status = excluded.actual_order_status,
                            actual_outcome = excluded.actual_outcome,
                            details_json = excluded.details_json,
                            evaluated_at = excluded.evaluated_at
                        """,
                        (
                            decision["decision_id"],
                            decision["stock_code"],
                            horizon,
                            max_up_rate,
                            max_down_rate,
                            close_rate,
                            decision["order_status"],
                            actual,
                            json.dumps(
                                {
                                    "entry_price": entry_price,
                                    "bar_count": len(rows),
                                    "coverage_complete": True,
                                    "interval_minutes": interval_minutes,
                                    "last_candle_time": rows[-1]["candle_time"],
                                    "decision_outcome": decision["decision_outcome"],
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            timestamp,
                        ),
                    )
                    saved += 1
        return {
            "target_date": target_date,
            "horizons": list(horizons),
            "representative_decisions": len(decisions),
            "represented_occurrences": represented_occurrences,
            "saved": saved,
            "pending": pending,
            "unavailable": unavailable,
        }

    def update_decision_forward_outcomes(
        self,
        *,
        horizons: tuple[int, ...] = (30, 60, 120),
        limit: int = 200,
        now: Optional[datetime] = None,
    ) -> int:
        result = self.recompute_decision_forward_outcomes(
            horizons=horizons,
            limit=limit,
            now=now,
        )
        return int(result["saved"])

    @staticmethod
    def _classify_forward_outcome(
        *,
        action: str,
        max_up_rate: float,
        max_down_rate: float,
        order_status: Optional[str],
    ) -> str:
        if action.lower() == "buy" or order_status == "filled":
            if max_up_rate >= 0.03:
                return "entered_then_rallied"
            if max_down_rate <= -0.02:
                return "entered_then_dropped"
            return "entered_flat"
        if max_up_rate >= 0.03:
            return "missed_rally_after_block"
        if max_down_rate <= -0.02:
            return "block_was_helpful"
        return "block_neutral"

    def upsert_theme_journal(
        self,
        *,
        theme_date: str,
        theme_name: str,
        strength: str,
        source: str = "manual",
        notes: Optional[str] = None,
        members: Optional[list[Dict[str, Any]]] = None,
    ) -> int:
        if strength not in {"strong", "medium", "weak", "none"}:
            raise ValueError("테마 강도는 strong, medium, weak, none 중 하나여야 합니다.")
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO theme_journals(
                    theme_date, theme_name, strength, source, notes,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_date, theme_name) DO UPDATE SET
                    strength = excluded.strength,
                    source = excluded.source,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    theme_date,
                    theme_name,
                    strength,
                    source,
                    notes,
                    timestamp,
                    timestamp,
                ),
            )
            journal_id = int(cursor.lastrowid or connection.execute(
                """
                SELECT id FROM theme_journals
                WHERE theme_date = ? AND theme_name = ?
                """,
                (theme_date, theme_name),
            ).fetchone()["id"])
            for member in members or []:
                stock_code = member.get("stock_code")
                if not stock_code:
                    continue
                role = member.get("role") or "watch"
                if role not in {"leader", "second", "follower", "watch", "blocked"}:
                    raise ValueError("테마 구성 역할이 올바르지 않습니다.")
                connection.execute(
                    """
                    INSERT INTO theme_journal_members(
                        journal_id, stock_code, stock_name, role, notes,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(journal_id, stock_code) DO UPDATE SET
                        stock_name = excluded.stock_name,
                        role = excluded.role,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        journal_id,
                        stock_code,
                        member.get("stock_name"),
                        role,
                        member.get("notes"),
                        timestamp,
                        timestamp,
                    ),
                )
            return journal_id

    def list_theme_journals(self, *, days: int = 30) -> list[sqlite3.Row]:
        if days < 1:
            raise ValueError("조회 기간은 1일 이상이어야 합니다.")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT j.*, COUNT(m.stock_code) AS member_count
                    FROM theme_journals j
                    LEFT JOIN theme_journal_members m ON m.journal_id = j.id
                    WHERE date(j.theme_date) >= date('now', ?)
                    GROUP BY j.id
                    ORDER BY j.theme_date DESC, j.strength, j.theme_name
                    """,
                    (f"-{days - 1} days",),
                ).fetchall()
            )

    def list_theme_trends(
        self,
        *,
        days: int = 30,
        limit: int = 30,
    ) -> list[Dict[str, Any]]:
        if days < 1:
            raise ValueError("조회 기간은 1일 이상이어야 합니다.")
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            journals = connection.execute(
                """
                SELECT j.theme_date, j.theme_name, j.strength,
                       COUNT(m.stock_code) AS member_count,
                       SUM(CASE WHEN m.role = 'leader' THEN 1 ELSE 0 END) AS manual_leaders,
                       SUM(CASE WHEN m.role = 'second' THEN 1 ELSE 0 END) AS manual_seconds
                FROM theme_journals j
                LEFT JOIN theme_journal_members m ON m.journal_id = j.id
                WHERE date(j.theme_date) >= date('now', ?)
                GROUP BY j.id
                """,
                (f"-{days - 1} days",),
            ).fetchall()
            leader_scores = connection.execute(
                f"""
                {self.leader_stock_scores_latest_cte()}
                SELECT trade_date, theme_name, role, total_score, blocked
                FROM latest_leader_stock_scores
                WHERE theme_name IS NOT NULL
                  AND theme_name != ''
                  AND date(trade_date) >= date('now', ?)
                """,
                (f"-{days - 1} days",),
            ).fetchall()
        trends: dict[str, dict[str, Any]] = {}

        def ensure(theme_name: str) -> dict[str, Any]:
            item = trends.get(theme_name)
            if item is None:
                item = {
                    "theme_name": theme_name,
                    "journal_dates": set(),
                    "score_dates": set(),
                    "strong_days": 0,
                    "medium_days": 0,
                    "weak_days": 0,
                    "member_count": 0,
                    "manual_leaders": 0,
                    "manual_seconds": 0,
                    "leader_count": 0,
                    "second_count": 0,
                    "follower_count": 0,
                    "blocked_count": 0,
                    "leader_score_sum": 0.0,
                    "leader_score_count": 0,
                    "latest_date": None,
                }
                trends[theme_name] = item
            return item

        for row in journals:
            theme_name = str(row["theme_name"] or "").strip()
            if not theme_name:
                continue
            item = ensure(theme_name)
            theme_date = str(row["theme_date"])
            item["journal_dates"].add(theme_date)
            item["latest_date"] = max(
                item["latest_date"] or theme_date,
                theme_date,
            )
            strength = row["strength"]
            if strength == "strong":
                item["strong_days"] += 1
            elif strength == "medium":
                item["medium_days"] += 1
            elif strength == "weak":
                item["weak_days"] += 1
            item["member_count"] += int(row["member_count"] or 0)
            item["manual_leaders"] += int(row["manual_leaders"] or 0)
            item["manual_seconds"] += int(row["manual_seconds"] or 0)

        for row in leader_scores:
            theme_name = str(row["theme_name"] or "").strip()
            if not theme_name:
                continue
            item = ensure(theme_name)
            trade_date = str(row["trade_date"])
            item["score_dates"].add(trade_date)
            item["latest_date"] = max(
                item["latest_date"] or trade_date,
                trade_date,
            )
            role = row["role"]
            if role == "leader":
                item["leader_count"] += 1
            elif role == "second":
                item["second_count"] += 1
            elif role == "follower":
                item["follower_count"] += 1
            elif role == "blocked" or row["blocked"]:
                item["blocked_count"] += 1
            item["leader_score_sum"] += float(row["total_score"] or 0)
            item["leader_score_count"] += 1

        result = []
        for item in trends.values():
            dates = set(item["journal_dates"]) | set(item["score_dates"])
            appearance_days = len(dates)
            average_score = (
                item["leader_score_sum"] / item["leader_score_count"]
                if item["leader_score_count"]
                else 0.0
            )
            trust_score = min(
                100.0,
                appearance_days * 8.0
                + item["strong_days"] * 10.0
                + item["medium_days"] * 6.0
                + item["leader_count"] * 5.0
                + item["second_count"] * 3.0
                + average_score * 0.12,
            )
            if trust_score >= 70:
                trust_level = "high"
            elif trust_score >= 45:
                trust_level = "medium"
            else:
                trust_level = "low"
            result.append(
                {
                    "theme_name": item["theme_name"],
                    "appearance_days": appearance_days,
                    "journal_days": len(item["journal_dates"]),
                    "score_days": len(item["score_dates"]),
                    "strong_days": item["strong_days"],
                    "medium_days": item["medium_days"],
                    "weak_days": item["weak_days"],
                    "member_count": item["member_count"],
                    "manual_leaders": item["manual_leaders"],
                    "manual_seconds": item["manual_seconds"],
                    "leader_count": item["leader_count"],
                    "second_count": item["second_count"],
                    "follower_count": item["follower_count"],
                    "blocked_count": item["blocked_count"],
                    "average_leader_score": round(average_score, 4),
                    "trust_score": round(trust_score, 4),
                    "trust_level": trust_level,
                    "latest_date": item["latest_date"],
                }
            )
        result.sort(
            key=lambda item: (
                -float(item["trust_score"]),
                -int(item["appearance_days"]),
                str(item["theme_name"]),
            )
        )
        return result[:limit]

    def save_leader_stock_scores(
        self,
        scores: list[Dict[str, Any]],
        *,
        scored_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (scored_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO leader_stock_scores(
                    scored_at, trade_date, stock_code, stock_name,
                    theme_code, theme_name, role, total_score, rank_score,
                    trade_amount_score, theme_score, persistence_score,
                    chart_score, risk_score, blocked, blocked_reason,
                    reasons_json, components_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        score.get("scored_at") or timestamp,
                        score["trade_date"],
                        score["stock_code"],
                        score.get("stock_name"),
                        score.get("theme_code"),
                        score.get("theme_name"),
                        score["role"],
                        score["total_score"],
                        score["rank_score"],
                        score["trade_amount_score"],
                        score["theme_score"],
                        score["persistence_score"],
                        score["chart_score"],
                        score["risk_score"],
                        int(bool(score.get("blocked"))),
                        score.get("blocked_reason"),
                        json.dumps(
                            score.get("reasons") or [],
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            score.get("components") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        timestamp,
                    )
                    for score in scores
                    if score.get("stock_code")
                ],
            )
            return connection.total_changes - before

    def latest_leader_stock_scores(
        self,
        *,
        trade_date: Optional[str] = None,
        limit: int = 20,
        include_components: bool = True,
    ) -> list[sqlite3.Row]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            selected_date = trade_date
            if selected_date is None:
                row = connection.execute(
                    f"""
                    {self.leader_stock_scores_latest_cte()}
                    SELECT MAX(trade_date) AS trade_date
                    FROM latest_leader_stock_scores
                    """
                ).fetchone()
                selected_date = row["trade_date"] if row else None
            if selected_date is None:
                return []
            columns = "*" if include_components else (
                "id, scored_at, trade_date, stock_code, stock_name, "
                "theme_code, theme_name, role, total_score, rank_score, "
                "trade_amount_score, theme_score, persistence_score, "
                "chart_score, risk_score, blocked, blocked_reason, "
                "reasons_json, created_at"
            )
            return list(
                connection.execute(
                    f"""
                    {self.leader_stock_scores_latest_cte()}
                    SELECT {columns}
                    FROM latest_leader_stock_scores
                    WHERE trade_date = ?
                    ORDER BY blocked, total_score DESC, trade_amount_score DESC,
                             rank_score DESC, stock_code
                    LIMIT ?
                    """,
                    (selected_date, limit),
                ).fetchall()
            )

    @staticmethod
    def leader_stock_scores_latest_cte() -> str:
        return """
        WITH latest_leader_stock_scores AS (
            SELECT scores.*
            FROM leader_stock_scores scores
            WHERE scores.id = (
                SELECT latest.id
                FROM leader_stock_scores latest
                WHERE latest.trade_date = scores.trade_date
                  AND latest.stock_code = scores.stock_code
                ORDER BY latest.scored_at DESC, latest.id DESC
                LIMIT 1
            )
        )
        """

    def save_leader_candidate_outcomes(
        self,
        outcomes: list[Dict[str, Any]],
        *,
        evaluated_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (evaluated_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO leader_candidate_outcomes(
                    evaluated_at, trade_date, stock_code, stock_name,
                    candidate_rank, role_at_pick, score_at_pick,
                    theme_code, theme_name, components_json,
                    same_day_reference_price, same_day_open_price,
                    same_day_high_price, same_day_low_price,
                    same_day_close_price, same_day_volume,
                    same_day_max_up_rate, same_day_close_rate,
                    next_day_date, next_day_open_price, next_day_high_price,
                    next_day_low_price, next_day_close_price, next_day_volume,
                    next_day_max_up_rate, next_day_close_rate,
                    actual_leader, actual_leader_rank, actual_leader_reason,
                    outcome_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, stock_code, theme_name) DO UPDATE SET
                    evaluated_at = excluded.evaluated_at,
                    stock_name = excluded.stock_name,
                    candidate_rank = excluded.candidate_rank,
                    role_at_pick = excluded.role_at_pick,
                    score_at_pick = excluded.score_at_pick,
                    theme_code = excluded.theme_code,
                    components_json = excluded.components_json,
                    same_day_reference_price = excluded.same_day_reference_price,
                    same_day_open_price = excluded.same_day_open_price,
                    same_day_high_price = excluded.same_day_high_price,
                    same_day_low_price = excluded.same_day_low_price,
                    same_day_close_price = excluded.same_day_close_price,
                    same_day_volume = excluded.same_day_volume,
                    same_day_max_up_rate = excluded.same_day_max_up_rate,
                    same_day_close_rate = excluded.same_day_close_rate,
                    next_day_date = excluded.next_day_date,
                    next_day_open_price = excluded.next_day_open_price,
                    next_day_high_price = excluded.next_day_high_price,
                    next_day_low_price = excluded.next_day_low_price,
                    next_day_close_price = excluded.next_day_close_price,
                    next_day_volume = excluded.next_day_volume,
                    next_day_max_up_rate = excluded.next_day_max_up_rate,
                    next_day_close_rate = excluded.next_day_close_rate,
                    actual_leader = excluded.actual_leader,
                    actual_leader_rank = excluded.actual_leader_rank,
                    actual_leader_reason = excluded.actual_leader_reason,
                    outcome_json = excluded.outcome_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        outcome.get("evaluated_at") or timestamp,
                        outcome["trade_date"],
                        outcome["stock_code"],
                        outcome.get("stock_name"),
                        int(outcome["candidate_rank"]),
                        outcome["role_at_pick"],
                        float(outcome["score_at_pick"]),
                        outcome.get("theme_code"),
                        outcome.get("theme_name") or "",
                        json.dumps(
                            outcome.get("components") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        outcome.get("same_day_reference_price"),
                        outcome.get("same_day_open_price"),
                        outcome.get("same_day_high_price"),
                        outcome.get("same_day_low_price"),
                        outcome.get("same_day_close_price"),
                        outcome.get("same_day_volume"),
                        outcome.get("same_day_max_up_rate"),
                        outcome.get("same_day_close_rate"),
                        outcome.get("next_day_date"),
                        outcome.get("next_day_open_price"),
                        outcome.get("next_day_high_price"),
                        outcome.get("next_day_low_price"),
                        outcome.get("next_day_close_price"),
                        outcome.get("next_day_volume"),
                        outcome.get("next_day_max_up_rate"),
                        outcome.get("next_day_close_rate"),
                        int(bool(outcome.get("actual_leader"))),
                        outcome.get("actual_leader_rank"),
                        outcome["actual_leader_reason"],
                        json.dumps(
                            outcome.get("outcome") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        timestamp,
                        timestamp,
                    )
                    for outcome in outcomes
                    if outcome.get("stock_code")
                ],
            )
            return connection.total_changes - before

    def latest_leader_candidate_outcomes(
        self,
        *,
        trade_date: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            selected_date = trade_date
            if selected_date is None:
                row = connection.execute(
                    """
                    SELECT MAX(trade_date) AS trade_date
                    FROM leader_candidate_outcomes
                    """
                ).fetchone()
                selected_date = row["trade_date"] if row else None
            if selected_date is None:
                return []
            rows = connection.execute(
                """
                SELECT *
                FROM leader_candidate_outcomes
                WHERE trade_date = ?
                ORDER BY candidate_rank, score_at_pick DESC, stock_code
                LIMIT ?
                """,
                (selected_date, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for source, target, fallback in (
                ("components_json", "components", {}),
                ("outcome_json", "outcome", {}),
            ):
                try:
                    item[target] = json.loads(item.pop(source) or json.dumps(fallback))
                except (TypeError, json.JSONDecodeError):
                    item[target] = fallback
            item["actual_leader"] = bool(item.get("actual_leader"))
            result.append(item)
        return result

    def save_chart_trade_decision(
        self,
        *,
        decision_id: int,
        chart: Dict[str, Any],
    ) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        stock_code = str(chart["stock_code"])
        evaluated_at = str(chart["evaluated_at"])
        daily_details = (chart.get("details") or {}).get("daily") or {}
        trigger_details = (chart.get("details") or {}).get("trigger") or {}
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chart_setups(
                    decision_id, stock_code, evaluated_at, setup_name,
                    setup_score, reasons_json, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    stock_code = excluded.stock_code,
                    evaluated_at = excluded.evaluated_at,
                    setup_name = excluded.setup_name,
                    setup_score = excluded.setup_score,
                    reasons_json = excluded.reasons_json,
                    details_json = excluded.details_json
                """,
                (
                    decision_id,
                    stock_code,
                    evaluated_at,
                    chart["daily_setup_name"],
                    float(chart["daily_setup_score"]),
                    json.dumps(chart.get("reasons") or [], ensure_ascii=False),
                    json.dumps(daily_details, ensure_ascii=False, sort_keys=True),
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO intraday_trade_patterns(
                    decision_id, stock_code, evaluated_at, trigger_name,
                    trigger_state, trigger_score, reasons_json, details_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    stock_code = excluded.stock_code,
                    evaluated_at = excluded.evaluated_at,
                    trigger_name = excluded.trigger_name,
                    trigger_state = excluded.trigger_state,
                    trigger_score = excluded.trigger_score,
                    reasons_json = excluded.reasons_json,
                    details_json = excluded.details_json
                """,
                (
                    decision_id,
                    stock_code,
                    evaluated_at,
                    chart["intraday_trigger_name"],
                    chart["intraday_trigger_state"],
                    float(chart["intraday_trigger_score"]),
                    json.dumps(chart.get("reasons") or [], ensure_ascii=False),
                    json.dumps(trigger_details, ensure_ascii=False, sort_keys=True),
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO entry_plans(
                    decision_id, stock_code, entry_type, entry_price,
                    invalidation_price, first_take_profit_price,
                    second_take_profit_price, force_entry, total_chart_score,
                    details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    stock_code = excluded.stock_code,
                    entry_type = excluded.entry_type,
                    entry_price = excluded.entry_price,
                    invalidation_price = excluded.invalidation_price,
                    first_take_profit_price = excluded.first_take_profit_price,
                    second_take_profit_price = excluded.second_take_profit_price,
                    force_entry = excluded.force_entry,
                    total_chart_score = excluded.total_chart_score,
                    details_json = excluded.details_json
                """,
                (
                    decision_id,
                    stock_code,
                    chart["entry_type"],
                    chart["entry_price"],
                    chart.get("invalidation_price"),
                    chart.get("first_take_profit_price"),
                    chart.get("second_take_profit_price"),
                    int(bool(chart.get("force_entry"))),
                    float(chart["total_score"]),
                    json.dumps(chart.get("details") or {}, ensure_ascii=False),
                    timestamp,
                ),
            )
            if chart.get("blocked"):
                connection.execute(
                    """
                    INSERT INTO pattern_block_events(
                        decision_id, stock_code, evaluated_at, reason,
                        risk_score, events_json, details_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        stock_code,
                        evaluated_at,
                        chart.get("block_reason") or "차트 위험 패턴 차단",
                        float(chart["risk_score"]),
                        json.dumps(chart.get("risk_events") or [], ensure_ascii=False),
                        json.dumps(chart.get("details") or {}, ensure_ascii=False),
                        timestamp,
                    ),
                )

    def latest_chart_entry_plans(self, *, limit: int = 20) -> list[sqlite3.Row]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT ep.*, cs.setup_name, cs.setup_score,
                           itp.trigger_name, itp.trigger_state,
                           itp.trigger_score, pbe.reason AS block_reason
                    FROM entry_plans ep
                    LEFT JOIN chart_setups cs ON cs.decision_id = ep.decision_id
                    LEFT JOIN intraday_trade_patterns itp
                      ON itp.decision_id = ep.decision_id
                    LEFT JOIN pattern_block_events pbe
                      ON pbe.decision_id = ep.decision_id
                    ORDER BY ep.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def save_real_account_snapshot(
        self,
        balance_payload: Dict[str, Any],
        holdings_payload: Dict[str, Any],
        positions: list[Dict[str, Any]],
        summary: Dict[str, Any],
        *,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            balance_raw_id = self._insert_api_snapshot(
                connection, "kt00005", None, timestamp, balance_payload
            )
            holdings_raw_id = self._insert_api_snapshot(
                connection, "kt00018", None, timestamp, holdings_payload
            )
            cursor = connection.execute(
                """
                INSERT INTO real_account_snapshots(
                    collected_at, deposit, withdrawable_amount, orderable_cash,
                    total_purchase_amount, total_evaluation_amount,
                    total_profit_loss, total_profit_rate, estimated_assets,
                    raw_balance_snapshot_id, raw_holdings_snapshot_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    summary.get("deposit"),
                    summary.get("withdrawable_amount"),
                    summary.get("orderable_cash"),
                    summary.get("total_purchase_amount"),
                    summary.get("total_evaluation_amount"),
                    summary.get("total_profit_loss"),
                    summary.get("total_profit_rate"),
                    summary.get("estimated_assets"),
                    balance_raw_id,
                    holdings_raw_id,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO real_positions(
                    account_snapshot_id, stock_code, stock_name, quantity,
                    tradable_quantity, current_price, purchase_price,
                    purchase_amount, evaluation_amount,
                    evaluation_profit_loss, profit_rate
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        position.get("stock_code"),
                        position.get("stock_name"),
                        position.get("quantity"),
                        position.get("tradable_quantity"),
                        position.get("current_price"),
                        position.get("purchase_price"),
                        position.get("purchase_amount"),
                        position.get("evaluation_amount"),
                        position.get("evaluation_profit_loss"),
                        position.get("profit_rate"),
                    )
                    for position in positions
                ],
            )
            return snapshot_id

    def save_real_order_executions(
        self,
        payload: Dict[str, Any],
        orders: list[Dict[str, Any]],
        *,
        order_date: Optional[str],
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            raw_id = self._insert_api_snapshot(
                connection, "kt00007", None, timestamp, payload
            )
            connection.executemany(
                """
                INSERT INTO real_order_execution_snapshots(
                    collected_at, order_date, order_number, stock_code,
                    stock_name, trade_type, order_type_name, order_quantity,
                    order_price, executed_quantity, executed_price,
                    remaining_quantity, order_time, status,
                    original_order_number, exchange_type, raw_snapshot_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        timestamp,
                        order_date,
                        order.get("order_number"),
                        order.get("stock_code"),
                        order.get("stock_name"),
                        order.get("trade_type"),
                        order.get("order_type_name"),
                        order.get("order_quantity"),
                        order.get("order_price"),
                        order.get("executed_quantity"),
                        order.get("executed_price"),
                        order.get("remaining_quantity"),
                        order.get("order_time"),
                        order.get("status"),
                        order.get("original_order_number"),
                        order.get("exchange_type"),
                        raw_id,
                    )
                    for order in orders
                ],
            )
            return len(orders)

    def prune_sensitive_account_data(self, retention_days: int) -> Dict[str, int]:
        if retention_days < 1:
            raise ValueError("보존 기간은 1일 이상이어야 합니다.")
        cutoff = f"-{retention_days} days"
        with self.connect() as connection:
            account_deleted = connection.execute(
                """
                DELETE FROM real_account_snapshots
                WHERE datetime(collected_at) < datetime('now', ?)
                """,
                (cutoff,),
            ).rowcount
            orders_deleted = connection.execute(
                """
                DELETE FROM real_order_execution_snapshots
                WHERE datetime(collected_at) < datetime('now', ?)
                """,
                (cutoff,),
            ).rowcount
            raw_deleted = connection.execute(
                """
                DELETE FROM api_snapshots
                WHERE api_id IN ('kt00005', 'kt00018', 'kt00007')
                  AND datetime(collected_at) < datetime('now', ?)
                """,
                (cutoff,),
            ).rowcount
        return {
            "account_snapshots": account_deleted,
            "order_executions": orders_deleted,
            "raw_snapshots": raw_deleted,
        }

    def get_latest_price(self, stock_code: str) -> Optional[sqlite3.Row]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT current_price AS price, collected_at AS price_time,
                       'price_snapshot' AS price_source
                FROM price_snapshots
                WHERE stock_code = ? AND current_price IS NOT NULL
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
            if row is not None:
                return row
            return connection.execute(
                """
                SELECT close_price AS price, candle_time AS price_time,
                       'candle' AS price_source
                FROM candles
                WHERE stock_code = ? AND close_price IS NOT NULL
                ORDER BY candle_time DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()

    def add_watchlist_stock(
        self,
        stock_code: str,
        stock_name: Optional[str] = None,
        *,
        updated_at: Optional[datetime] = None,
    ) -> None:
        timestamp = (updated_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO watchlist(
                    stock_code, stock_name, enabled, created_at, updated_at
                )
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(stock_code) DO UPDATE SET
                    stock_name = COALESCE(excluded.stock_name, watchlist.stock_name),
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (stock_code, stock_name, timestamp, timestamp),
            )

    def disable_watchlist_stock(
        self,
        stock_code: str,
        *,
        updated_at: Optional[datetime] = None,
    ) -> bool:
        timestamp = (updated_at or datetime.now()).isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE watchlist
                SET enabled = 0, updated_at = ?
                WHERE stock_code = ? AND enabled = 1
                """,
                (timestamp, stock_code),
            )
            return cursor.rowcount > 0

    def list_watchlist(self, *, include_disabled: bool = False) -> list[sqlite3.Row]:
        query = """
            SELECT stock_code, stock_name, enabled, created_at, updated_at
            FROM watchlist
        """
        parameters: tuple[Any, ...] = ()
        if not include_disabled:
            query += " WHERE enabled = ?"
            parameters = (1,)
        query += " ORDER BY stock_code"
        with self.connect() as connection:
            return list(connection.execute(query, parameters).fetchall())

    def save_price_snapshot(
        self,
        api_id: str,
        stock_code: str,
        payload: Dict[str, Any],
        *,
        current_price: Optional[int],
        open_price: Optional[int],
        high_price: Optional[int],
        low_price: Optional[int],
        volume: Optional[int],
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO api_snapshots(api_id, stock_code, collected_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (api_id, stock_code, timestamp, serialized),
            )
            raw_snapshot_id = int(cursor.lastrowid)
            cursor = connection.execute(
                """
                INSERT INTO price_snapshots(
                    stock_code, collected_at, current_price, open_price,
                    high_price, low_price, volume, raw_snapshot_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stock_code,
                    timestamp,
                    current_price,
                    open_price,
                    high_price,
                    low_price,
                    volume,
                    raw_snapshot_id,
                ),
            )
            return int(cursor.lastrowid)

    def save_candles(
        self,
        api_id: str,
        stock_code: str,
        payload: Dict[str, Any],
        candles: list[Dict[str, Any]],
        *,
        candle_type: str,
        interval_minutes: int = 0,
        collected_at: Optional[datetime] = None,
    ) -> int:
        timestamp = (collected_at or datetime.now()).isoformat(timespec="seconds")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO api_snapshots(api_id, stock_code, collected_at, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (api_id, stock_code, timestamp, serialized),
            )
            raw_snapshot_id = int(cursor.lastrowid)
            before_changes = connection.total_changes
            connection.executemany(
                """
                INSERT INTO candles(
                    stock_code, candle_type, interval_minutes, candle_time,
                    open_price, high_price, low_price, close_price, volume,
                    accumulated_volume, trade_amount, raw_snapshot_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    stock_code, candle_type, interval_minutes, candle_time
                ) DO UPDATE SET
                    open_price = excluded.open_price,
                    high_price = excluded.high_price,
                    low_price = excluded.low_price,
                    close_price = excluded.close_price,
                    volume = excluded.volume,
                    accumulated_volume = excluded.accumulated_volume,
                    trade_amount = excluded.trade_amount,
                    raw_snapshot_id = excluded.raw_snapshot_id,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        stock_code,
                        candle_type,
                        interval_minutes,
                        candle["candle_time"],
                        candle.get("open_price"),
                        candle.get("high_price"),
                        candle.get("low_price"),
                        candle.get("close_price"),
                        candle.get("volume"),
                        candle.get("accumulated_volume"),
                        candle.get("trade_amount"),
                        raw_snapshot_id,
                        timestamp,
                    )
                    for candle in candles
                ],
            )
            return connection.total_changes - before_changes

    def list_candles(
        self,
        stock_codes: list[str],
        *,
        candle_type: str,
        interval_minutes: int = 0,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> list[sqlite3.Row]:
        if not stock_codes:
            return []
        if candle_type not in {"daily", "minute"}:
            raise ValueError("candle_type은 daily 또는 minute이어야 합니다.")
        placeholders = ", ".join("?" for _ in stock_codes)
        query = f"""
            SELECT stock_code, candle_type, interval_minutes, candle_time,
                   open_price, high_price, low_price, close_price, volume,
                   accumulated_volume, trade_amount
            FROM candles
            WHERE stock_code IN ({placeholders})
              AND candle_type = ?
              AND interval_minutes = ?
              AND open_price IS NOT NULL
              AND high_price IS NOT NULL
              AND low_price IS NOT NULL
              AND close_price IS NOT NULL
        """
        parameters: list[Any] = [
            *stock_codes,
            candle_type,
            interval_minutes,
        ]
        if start_time is not None:
            query += " AND candle_time >= ?"
            parameters.append(start_time)
        if end_time is not None:
            query += " AND candle_time <= ?"
            parameters.append(end_time)
        query += " ORDER BY candle_time, stock_code, id"
        with self.connect() as connection:
            return list(connection.execute(query, parameters).fetchall())
