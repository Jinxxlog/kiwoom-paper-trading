import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from config.settings import (
    MarketModeGateSettings,
    PaperTradingSettings,
    RiskSettings,
    StrategySettings,
)
from db.database import Database
from domain.models import DecisionAction, StrategyDecision, TradePlan
from execution.paper import PaperOrderExecutor
from paper.engine import PaperTradingEngine
from paper.engine import PaperTradingError
from strategy.breakout import BreakoutStrategy


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


class BreakoutStrategyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.paper_settings = PaperTradingSettings(
            initial_cash=1_000_000,
            commission_rate=Decimal("0"),
            sell_tax_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
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
        self.strategy_settings = StrategySettings(
            volume_multiple=Decimal("2"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=20,
            minute_interval=5,
        )
        self.paper = PaperTradingEngine(
            self.database,
            self.paper_settings,
            self.risk_settings,
        )
        self.paper.create_account()
        self.notifier = FakeNotifier()
        self.strategy = BreakoutStrategy(
            self.database,
            self.paper,
            self.strategy_settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
        )
        self._save_signal_data()
        self._save_default_leader_score()
        self._save_default_supply_score()

    def _use_strategy_settings(self, **overrides) -> None:
        self.strategy_settings = replace(self.strategy_settings, **overrides)
        self.strategy.strategy_settings = self.strategy_settings

    def test_runtime_freshness_blocks_stale_daily_before_order(self):
        from data.candle_freshness import CandleFreshness
        self.strategy.candle_freshness = CandleFreshness(self.database)
        with self.database.connect() as connection:
            connection.execute("UPDATE candles SET candle_time='2026-06-08T00:00:00' WHERE candle_type='daily'")
        result = self.strategy.run('default',['A005930'],execute=True,now=datetime(2026,6,10,10,40))[0]
        self.assertEqual(result['action'],'failed')
        self.assertIn('캔들 최신성 부족',result['message'])
        self.assertIsNone(result['order_id'])
        self.assertEqual(self.paper.list_fills('default'),[])

    def test_runtime_freshness_blocks_stale_minute_before_order(self):
        from data.candle_freshness import CandleFreshness
        self.strategy.candle_freshness = CandleFreshness(self.database)
        with self.database.connect() as connection:
            connection.execute("DELETE FROM candles WHERE candle_type='minute' AND candle_time>'2026-06-10T10:10:00'")
        result = self.strategy.run('default',['A005930'],execute=True,now=datetime(2026,6,10,10,40))[0]
        self.assertEqual(result['action'],'failed')
        self.assertIsNone(result['order_id'])

    def test_runtime_evaluation_excludes_future_minute(self):
        from data.candle_freshness import CandleFreshness
        self.strategy.candle_freshness = CandleFreshness(self.database)
        baseline = self.strategy.evaluate('A005930')
        self.database.save_candles('test','A005930',{},[{
            'candle_time':'2026-06-10T12:00:00','open_price':999,'high_price':999,
            'low_price':999,'close_price':999,'volume':999999}],candle_type='minute',interval_minutes=5)
        evaluated = self.strategy.evaluate('A005930')
        self.assertEqual(evaluated.moving_average,baseline.moving_average)
        self.assertEqual(evaluated.vwap,baseline.vwap)

    def _use_risk_settings(self, **overrides) -> None:
        self.risk_settings = replace(self.risk_settings, **overrides)
        self.strategy.risk_settings = self.risk_settings

    def _assert_shadow_only_router(self, result) -> dict:
        self.assertEqual(result["action"], "observe")
        self.assertIsNotNone(result["trade_plan"])
        policy = result["trade_plan"].setup["router_execution_policy"]
        self.assertIn(
            policy["status"],
            {
                "shadow_only_non_core_playbook",
                "shadow_only_legacy_entry_route",
                "shadow_only_unqualified_money_flow_probe",
                "shadow_only_invalid_structural_stop",
                "shadow_only_waiting_position_confirmation",
                "shadow_only_stage_target_already_reached",
                "shadow_only_poor_data_confidence",
                "shadow_only_market_data_unavailable",
                "shadow_only_regime_playbook_mismatch",
            },
        )
        self.assertEqual(policy["order_effect"], "shadow_only")
        self.assertTrue(policy["shadow_result_saved"])
        return policy

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save_signal_data(self) -> None:
        self.database.save_candles(
            "test",
            "A005930",
            {"daily": True},
            [
                {
                    "candle_time": "2026-06-09T00:00:00",
                    "open_price": 94,
                    "high_price": 100,
                    "low_price": 93,
                    "close_price": 95,
                    "volume": 50_000_000,
                }
            ],
            candle_type="daily",
        )
        candles = []
        for index in range(20):
            candles.append(
                {
                    "candle_time": f"2026-06-10T{9 + index // 12:02d}:{(index % 12) * 5:02d}:00",
                    "open_price": 99,
                    "high_price": 100,
                    "low_price": 98,
                    "close_price": 99,
                    "volume": 100,
                }
            )
        self.database.save_candles(
            "test",
            "A005930",
            {"minute": True},
            candles,
            candle_type="minute",
            interval_minutes=5,
        )
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 105},
            current_price=105,
            open_price=96,
            high_price=105,
            low_price=95,
            volume=130_000_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

    def _save_default_leader_score(self) -> None:
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": "2026-06-10",
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "theme_code": "101",
                    "theme_name": "semiconductor",
                    "role": "leader",
                    "total_score": 82.0,
                    "rank_score": 85.0,
                    "trade_amount_score": 80.0,
                    "theme_score": 75.0,
                    "persistence_score": 70.0,
                    "chart_score": 70.0,
                    "risk_score": 60.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["test default leader"],
                    "components": {},
                }
            ]
        )

    def _save_default_supply_score(self) -> None:
        self.database.save_supply_score(
            {
                "stock_code": "A005930",
                "stock_name": "?쇱꽦?꾩옄",
                "evaluated_at": "2026-06-10T10:39:00",
                "total_score": 50.0,
                "broker_score": 0.0,
                "foreign_broker_score": 0.0,
                "investor_score": 0.0,
                "program_score": 0.0,
                "blocked": False,
                "blocked_reason": None,
                "reasons": ["test default supply"],
                "components": {},
            }
        )

    def _delete_supply_scores(self, stock_code: str = "A005930") -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM supply_scores WHERE stock_code = ?",
                (stock_code,),
            )

    def _save_candidate_score(
        self,
        *,
        stock_code: str = "A005930",
        information_blocked: bool = False,
        information_score: float = 0,
        pattern_score: float = 0,
        patterns: tuple[str, ...] = (),
        features: dict | None = None,
        evaluated_at: str = "2026-06-10T10:39:00",
    ) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO candidate_runs(
                    as_of, universe_count, eligible_count, selected_count,
                    config_json, created_at
                )
                VALUES (?, 1, 1, 1, '{}', ?)
                """,
                (evaluated_at, evaluated_at),
            )
            connection.execute(
                """
                INSERT INTO candidate_scores(
                    run_id, stock_code, rank, selected, excluded_reason,
                    total_score, technical_score, pattern_score,
                    relative_strength_score, liquidity_score,
                    information_score, information_blocked,
                    close_price, rsi, atr, relative_strength, watched,
                    patterns_json, features_json, reasons_json, evaluated_at
                )
                VALUES (?, ?, 1, 1, NULL, 75, 20, ?, 10, 12, ?, ?,
                        105, NULL, NULL, NULL, 1, ?, ?, '[]', ?)
                """,
                (
                    cursor.lastrowid,
                    stock_code,
                    pattern_score,
                    information_score,
                    int(information_blocked),
                    json.dumps(patterns),
                    json.dumps(features or {}),
                    evaluated_at,
                ),
            )

    def _account_id(self) -> int:
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    "SELECT id FROM paper_accounts WHERE name = 'default'"
                ).fetchone()["id"]
            )

    def _insert_filled_buy(
        self,
        *,
        stock_code: str = "A005930",
        at: str = "2026-06-10T10:05:00",
    ) -> int:
        account_id = self._account_id()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, ?, ?, 'buy', 'market', 1, 'filled',
                        'seed buy', NULL, ?, ?)
                """,
                (
                    account_id,
                    f"seed-buy-{stock_code}-{at}",
                    stock_code,
                    at,
                    at,
                ),
            )
            return int(cursor.lastrowid)

    def _strong_a_grade_signal_plan(self):
        now = datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9)))
        signal = self.strategy.evaluate("A005930")
        self.assertIsNotNone(signal.chart)
        chart = replace(
            signal.chart,
            daily_setup_name="daily_new_high",
            total_score=Decimal("86"),
            entry_type="chart_breakout_hold",
            force_entry=True,
            blocked=False,
            block_reason=None,
        )
        signal = replace(
            signal,
            chart=chart,
            previous_high=100,
            current_price=105,
            vwap=Decimal("101"),
            vwap_pass=True,
        )
        decision = self.strategy.decide(signal)
        signal = replace(signal, supply={"total_score": 70.0, "blocked": False})
        leader_score = {
            "role": "leader",
            "total_score": 88.0,
            "trade_amount_score": 85.0,
            "rank_score": 82.0,
            "blocked": False,
        }
        session_profile = self.strategy._session_profile(now)
        strategy_mode = self.strategy._strategy_mode(
            now,
            session_profile,
            leader_score=leader_score,
            supply_score=signal.supply,
        )
        plan = self.strategy._build_trade_plan(
            signal=signal,
            decision=decision,
            decision_id=1,
            planned_quantity=100,
            paper_probe=False,
            session_profile=session_profile,
            strategy_mode=strategy_mode,
        )
        return now, signal, replace(plan, entry_type="chart_breakout_hold"), leader_score, decision

    def _insert_realized_loss(self, amount: int, *, at: str) -> None:
        account_id = self._account_id()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, ?, 'A999999', 'sell', 'market', 1, 'filled',
                        'seed loss', NULL, ?, ?)
                """,
                (account_id, f"loss-{at}", at, at),
            )
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, 'A999999', 'sell', 1, 1000, 1000, 1000,
                        0, 0, 0, ?, ?, ?)
                """,
                (cursor.lastrowid, amount, at, at),
            )

    def _insert_stop_losses(
        self,
        count: int,
        *,
        at: str = "2026-06-10T10:00:00",
    ) -> None:
        account_id = self._account_id()
        with self.database.connect() as connection:
            for index in range(count):
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
                    (account_id, f"A{index:06d}", at),
                )

    def _insert_risk_exit(
        self,
        *,
        stock_code: str = "A005930",
        event_type: str = "partial_take_profit",
        realized_pnl: int = 1_000,
        at: str = "2026-06-10T10:31:00",
        exit_class: str | None = None,
        exit_reason_class: str | None = None,
        setup_invalidated: bool = False,
    ) -> None:
        account_id = self._account_id()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, ?, ?, 'sell', 'market', 1, 'filled',
                        'seed risk exit', NULL, ?, ?)
                """,
                (account_id, f"risk-exit-{stock_code}-{at}", stock_code, at, at),
            )
            order_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, ?, 'sell', 1, 1000, 1000, 1000,
                        0, 0, 0, ?, ?, ?)
                """,
                (order_id, stock_code, realized_pnl, at, at),
            )
            connection.execute(
                """
                INSERT INTO paper_risk_events(
                    account_id, stock_code, event_type, trigger_price,
                    threshold_price, highest_price, order_id, status,
                    exit_class, exit_reason_class, setup_invalidated,
                    classification_json, message, occurred_at
                )
                VALUES (?, ?, ?, 1000, 1000, 1100, ?, 'executed',
                        ?, ?, ?, '{}', 'seed risk exit', ?)
                """,
                (
                    account_id,
                    stock_code,
                    event_type,
                    order_id,
                    exit_class,
                    exit_reason_class,
                    int(setup_invalidated),
                    at,
                ),
            )

    def _insert_failed_level_trade(
        self,
        *,
        stock_code: str = "A005930",
        at: str,
        breakout_level: int = 100,
        vwap: Decimal = Decimal("101"),
        previous_high: int = 100,
        intraday_high: int = 106,
        exit_class: str = "loss_stop",
        exit_reason_class: str = "structure_loss_stop",
    ) -> None:
        decided_at = datetime.fromisoformat(at) - timedelta(minutes=5)
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code=stock_code,
            decided_at=decided_at,
            action=DecisionAction.BUY,
            score=Decimal("82"),
            confidence=Decimal("1"),
            reason="seed failed level trade",
        )
        decision_id = self.database.save_strategy_decision(
            decision,
            account_name="default",
            strategy_version_id=self.strategy.strategy_version_id,
            strategy_config_id=self.strategy.strategy_config_id,
            source_data_time=decided_at.isoformat(timespec="seconds"),
            outcome="executed",
        )
        self.database.save_trade_plan(
            TradePlan(
                decision_id=decision_id,
                stock_code=stock_code,
                entry_type="chart_breakout_hold",
                entry_tags=("chart_breakout_hold",),
                entry_price=105,
                planned_quantity=1,
                stop_loss_price=98,
                stop_loss_reason="seed stop",
                first_take_profit_price=110,
                first_take_profit_reason="seed target",
                partial_exit_rate=Decimal("0.5"),
                breakeven_price=105,
                breakeven_trigger_price=110,
                trailing_rule="seed trailing",
                invalidation_reason="seed invalidation",
                reward_risk_ratio=Decimal("1.5"),
                setup={
                    "breakout_level": breakout_level,
                    "vwap": str(vwap),
                    "previous_high": previous_high,
                    "intraday_high": intraday_high,
                },
            )
        )
        self._insert_risk_exit(
            stock_code=stock_code,
            event_type="stop_loss",
            realized_pnl=-1_000,
            at=at,
            exit_class=exit_class,
            exit_reason_class=exit_reason_class,
            setup_invalidated=True,
        )

    def _reentry_trade_plan(
        self,
        *,
        stock_code: str = "A089030",
        entry_price: int = 106,
        stop_loss_price: int = 100,
        first_take_profit_price: int = 118,
        reward_risk_ratio: Decimal = Decimal("2"),
    ) -> TradePlan:
        return TradePlan(
            decision_id=1,
            stock_code=stock_code,
            entry_type="chart_breakout_hold",
            entry_tags=("chart_breakout_hold",),
            entry_price=entry_price,
            planned_quantity=1,
            stop_loss_price=stop_loss_price,
            stop_loss_reason="test reentry stop",
            first_take_profit_price=first_take_profit_price,
            first_take_profit_reason="test reentry target",
            partial_exit_rate=Decimal("0.5"),
            breakeven_price=entry_price,
            breakeven_trigger_price=first_take_profit_price,
            trailing_rule="test trailing",
            invalidation_reason="test invalidation",
            reward_risk_ratio=reward_risk_ratio,
            setup={"entry_grade": "A"},
        )

    def _save_reentry_confirmation_bars(
        self,
        stock_code: str,
        *,
        closes: tuple[int, ...] = (99, 100, 104, 106),
        volumes: tuple[int, ...] = (100, 110, 140, 300),
    ) -> None:
        candles = []
        for index, (close, volume) in enumerate(zip(closes, volumes)):
            candles.append(
                {
                    "candle_time": f"2026-06-10T10:{20 + index * 5:02d}:00",
                    "open_price": close,
                    "high_price": close + 1,
                    "low_price": close - 1,
                    "close_price": close,
                    "volume": volume,
                }
            )
        self.database.save_candles(
            "test",
            stock_code,
            {"minute": True},
            candles,
            candle_type="minute",
            interval_minutes=5,
        )

    def test_all_conditions_produce_buy_signal(self) -> None:
        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)

        self.assertTrue(signal.passed)
        self.assertEqual(signal.volume_ratio, Decimal("2.6"))
        self.assertGreater(signal.projected_volume_ratio, Decimal("2"))
        self.assertGreaterEqual(signal.projected_trade_amount, 50_000_000_000)
        self.assertEqual(signal.moving_average, Decimal("99"))
        self.assertEqual(signal.vwap, Decimal("99"))
        self.assertEqual(signal.intraday_high, 100)
        self.assertEqual(decision.action, DecisionAction.BUY)
        self.assertEqual(decision.score, Decimal("90.800"))
        self.assertEqual(len(decision.components), 14)
        self.assertIn("chart_trade", {component.name for component in decision.components})
        self.assertIn("supply_flow", {component.name for component in decision.components})

    def test_reentry_lock_blocks_breakout_entry(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2026-06-10T15:10:00",
            reason="partial_take_profit_reentry_block",
            created_at="2026-06-10T10:30:00",
        )
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": "2026-06-10",
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "theme_code": "101",
                    "theme_name": "semiconductor",
                    "role": "follower",
                    "total_score": 55.0,
                    "rank_score": 30.0,
                    "trade_amount_score": 40.0,
                    "theme_score": 20.0,
                    "persistence_score": 10.0,
                    "chart_score": 20.0,
                    "risk_score": 0.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["test follower"],
                    "components": {},
                }
            ]
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertEqual(result["action"], "skipped")
        self.assertTrue(result["message"])
        self.assertIsNone(result["trade_plan"])

    def test_reentry_lock_is_released_when_new_high_and_leader_hold(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2026-06-10T15:10:00",
            reason="partial_take_profit_reentry_block",
            created_at="2026-06-10T10:30:00",
        )
        self._insert_risk_exit(
            stock_code="A005930",
            event_type="partial_take_profit",
            realized_pnl=1_500,
            at="2026-06-10T10:31:00",
        )
        self._save_reentry_confirmation_bars("A005930")
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": "2026-06-10",
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "theme_code": "101",
                    "theme_name": "semiconductor",
                    "role": "leader",
                    "total_score": 88.0,
                    "rank_score": 90.0,
                    "trade_amount_score": 85.0,
                    "theme_score": 80.0,
                    "persistence_score": 75.0,
                    "chart_score": 70.0,
                    "risk_score": 60.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["test"],
                    "components": {},
                }
            ],
            scored_at=datetime(2026, 6, 10, 10, 35),
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertEqual(result["action"], "executed")
        self.assertEqual(
            result["trade_plan"].setup["reentry_policy"]["status"],
            "allowed_after_partial_exit_reassessment",
        )
        reentry_policy = result["trade_plan"].setup["reentry_policy"]
        self.assertTrue(reentry_policy["reentry_quality_passed"])
        self.assertGreaterEqual(
            reentry_policy["reentry_quality_score"],
            reentry_policy["reentry_quality_threshold"],
        )
        self.assertIsNotNone(reentry_policy["reentry_reward_risk_ratio"])
        self.assertEqual(
            {
                item["name"]
                for item in reentry_policy["reentry_quality_components"]
            },
            {
                "vwap_recovery",
                "new_high_rebreakout",
                "volume_recovery",
                "supply",
                "entry_grade",
                "previous_exit",
                "reward_risk",
            },
        )
        self.assertTrue(
            reentry_policy["volume_recovered"]
        )
        self.assertTrue(
            reentry_policy["previous_exit_was_profit_protect"]
        )
        self.assertEqual(
            reentry_policy["previous_exit"]["exit_class"],
            "partial_take_profit",
        )
        engine_policy = result["trade_plan"].setup["reentry_engine_policy"]
        self.assertEqual(engine_policy["version"], "v0.12.96")
        self.assertEqual(engine_policy["status"], "allowed_new_setup")
        self.assertEqual(engine_policy["order_effect"], "allow_minimum_probe")
        self.assertEqual(
            engine_policy["decision_basis"],
            "new_setup_same_level_reward_risk",
        )
        self.assertEqual(engine_policy["time_gate_role"], "supporting_only")
        self.assertTrue(engine_policy["criteria"]["new_setup"]["passed"])
        self.assertTrue(engine_policy["criteria"]["quality"]["passed"])
        self.assertTrue(engine_policy["criteria"]["same_level_failure"]["passed"])
        self.assertTrue(engine_policy["criteria"]["reward_risk"]["passed"])
        self.assertTrue(engine_policy["strict_setup_passed"])
        self.assertIsNotNone(engine_policy["approved_reentry_lock_id"])
        self.assertEqual(
            result["trade_plan"].setup["entry_guard_policy"][
                "reentry_quantity_policy"
            ]["quantity_multiplier"],
            "0.25",
        )
        self.assertLess(result["trade_plan"].planned_quantity, 1000)
        self.assertIsNone(
            self.database.active_reentry_lock(
                "A005930",
                as_of="2026-06-10T10:40:00",
            )
        )
        self.assertIsNone(
            self.database.pending_reentry_lock(
                "A005930",
                as_of="2026-06-10T10:40:00",
            )
        )
        with self.database.connect() as connection:
            released_lock = connection.execute(
                "SELECT status, release_reason FROM reentry_locks WHERE id = ?",
                (engine_policy["approved_reentry_lock_id"],),
            ).fetchone()
        self.assertEqual(released_lock["status"], "released")
        self.assertIn("buy_executed", released_lock["release_reason"])

    def test_partial_reentry_lock_stays_blocked_after_loss_exit(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2026-06-10T15:10:00",
            reason="partial_take_profit_reentry_block",
            created_at="2026-06-10T10:30:00",
        )
        self._insert_risk_exit(
            stock_code="A005930",
            event_type="stop_loss",
            realized_pnl=-1_000,
            at="2026-06-10T10:31:00",
        )
        self._save_reentry_confirmation_bars("A005930")

        signal = self.strategy.evaluate("A005930")
        blocked, _message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked")
        self.assertFalse(policy["previous_exit_was_profit_protect"])
        self.assertEqual(policy["previous_exit"]["exit_class"], "loss_stop")
        self.assertIsNotNone(
            self.database.active_reentry_lock(
                "A005930",
                as_of="2026-06-10T10:40:00",
            )
        )

    def test_partial_reentry_lock_uses_stored_structure_exit_class(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2026-06-10T15:10:00",
            reason="partial_take_profit_reentry_block",
            created_at="2026-06-10T10:30:00",
        )
        self._insert_risk_exit(
            stock_code="A005930",
            event_type="stop_loss",
            realized_pnl=500,
            exit_class="profit_protect_stop",
            exit_reason_class="structure_loss_stop",
            setup_invalidated=True,
            at="2026-06-10T10:31:00",
        )
        self._save_reentry_confirmation_bars("A005930")

        signal = self.strategy.evaluate("A005930")
        blocked, _message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["previous_exit"]["exit_class"], "profit_protect_stop")
        self.assertEqual(
            policy["previous_exit"]["exit_reason_class"],
            "structure_loss_stop",
        )
        self.assertTrue(policy["previous_exit"]["setup_invalidated"])
        self.assertFalse(policy["previous_exit_was_profit_protect"])

    def test_partial_reentry_lock_requires_supply_maintained(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2026-06-10T15:10:00",
            reason="partial_take_profit_reentry_block",
            created_at="2026-06-10T10:30:00",
        )
        self._insert_risk_exit(
            stock_code="A005930",
            event_type="partial_take_profit",
            realized_pnl=1_500,
            at="2026-06-10T10:31:00",
        )
        self._save_reentry_confirmation_bars("A005930")
        signal = replace(
            self.strategy.evaluate("A005930"),
            supply={
                "total_score": 20.0,
                "blocked": True,
                "blocked_reason": "수급 약화",
            },
        )

        blocked, _message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked")
        self.assertFalse(policy["supply_maintained"])
        self.assertTrue(policy["previous_exit_was_profit_protect"])

    def test_simple_vwap_rebound_does_not_release_reentry_lock(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A089030",
            lock_until="2026-06-10T15:10:00",
            reason="vwap_actual_exit_reentry_block",
            created_at="2026-06-10T10:00:00",
        )
        self._save_reentry_confirmation_bars(
            "A089030",
            closes=(99, 100, 100, 102),
            volumes=(100, 110, 100, 105),
        )
        signal = replace(
            self.strategy.evaluate("A005930"),
            stock_code="A089030",
            current_price=103,
            previous_high=100,
            vwap=Decimal("101"),
            vwap_pass=True,
        )

        blocked, _message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked")
        self.assertFalse(policy["vwap_reanchored"])
        self.assertFalse(policy["volume_recovered"])
        self.assertFalse(policy["reentry_quality_passed"])
        self.assertEqual(
            policy["reassessment_reason"],
            policy["reassessment_reason"],
        )

    def test_reentry_quality_passes_but_cooldown_still_blocks(self) -> None:
        self._use_risk_settings(stop_loss_cooldown_minutes=60)
        self.database.save_reentry_lock(
            stock_code="A089030",
            lock_until="2026-06-10T15:10:00",
            reason="vwap_actual_exit_reentry_block",
            created_at="2026-06-10T10:30:00",
        )
        self._save_reentry_confirmation_bars("A089030")
        signal = replace(
            self.strategy.evaluate("A005930"),
            stock_code="A089030",
            current_price=106,
            previous_high=103,
            vwap=Decimal("101"),
            vwap_pass=True,
        )

        blocked, _message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )

        self.assertTrue(blocked)
        self.assertTrue(policy["reentry_quality_passed"])
        self.assertFalse(policy["minimum_wait_elapsed"])
        self.assertEqual(policy["status"], "blocked")

    def test_a089030_strong_rebreakout_releases_vwap_exit_lock(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A089030",
            lock_until="2026-06-10T15:10:00",
            reason="vwap_actual_exit_reentry_block",
            created_at="2026-06-10T10:00:00",
        )
        self._save_reentry_confirmation_bars("A089030")
        signal = replace(
            self.strategy.evaluate("A005930"),
            stock_code="A089030",
            current_price=106,
            previous_high=103,
            vwap=Decimal("101"),
            vwap_pass=True,
        )
        trade_plan = self._reentry_trade_plan()

        blocked, message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
            trade_plan=trade_plan,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["status"], "allowed_after_strong_reassessment")
        self.assertTrue(policy["reentry_reward_risk_passed"])
        self.assertTrue(policy["prior_vwap_failure_observed"])
        self.assertTrue(policy["vwap_reanchored"])
        self.assertTrue(policy["new_high_breakout"])
        self.assertTrue(policy["volume_recovered"])
        self.assertTrue(policy["leader_maintained"])
        self.assertIsNotNone(
            self.database.pending_reentry_lock(
                "A089030",
                as_of="2026-06-10T10:40:00",
            )
        )

    def test_elapsed_lock_time_alone_does_not_allow_reentry(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A089030",
            lock_until="2026-06-10T10:31:00",
            reason="exit_reentry_block:noise_stop",
            created_at="2026-06-10T10:30:00",
        )
        signal = replace(
            self.strategy.evaluate("A005930"),
            stock_code="A089030",
            current_price=103,
            previous_high=100,
            vwap=Decimal("101"),
            vwap_pass=True,
        )

        blocked, _message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
            trade_plan=self._reentry_trade_plan(),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["setup_version"], "v0.12.96")
        self.assertEqual(
            policy["reentry_engine_policy"]["status"],
            "blocked_reentry_quality",
        )
        self.assertIsNone(
            self.database.active_reentry_lock(
                "A089030",
                as_of="2026-06-10T10:40:00",
            )
        )
        self.assertIsNotNone(
            self.database.pending_reentry_lock(
                "A089030",
                as_of="2026-06-10T10:40:00",
            )
        )

    def test_reentry_is_blocked_when_reward_risk_is_too_weak(self) -> None:
        self.database.save_reentry_lock(
            stock_code="A089030",
            lock_until="2026-06-10T15:10:00",
            reason="vwap_actual_exit_reentry_block",
            created_at="2026-06-10T10:00:00",
        )
        self._save_reentry_confirmation_bars("A089030")
        signal = replace(
            self.strategy.evaluate("A005930"),
            stock_code="A089030",
            current_price=106,
            previous_high=103,
            vwap=Decimal("101"),
            vwap_pass=True,
        )
        trade_plan = self._reentry_trade_plan(
            stop_loss_price=100,
            first_take_profit_price=108,
            reward_risk_ratio=Decimal("2"),
        )

        blocked, _message, policy = self.strategy._reentry_policy(
            signal=signal,
            leader_score={"role": "leader", "total_score": 90, "blocked": False},
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
            trade_plan=trade_plan,
        )

        reward_risk_policy = policy["reentry_reward_risk_policy"]
        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_reentry_reward_risk")
        self.assertTrue(policy["reentry_quality_passed"])
        self.assertFalse(policy["reentry_reward_risk_passed"])
        self.assertEqual(reward_risk_policy["risk_per_share"], 6)
        self.assertEqual(reward_risk_policy["expected_upside"], 2)
        self.assertEqual(
            reward_risk_policy["status"],
            "blocked_reentry_reward_risk",
        )
        engine_policy = policy["reentry_engine_policy"]
        self.assertEqual(engine_policy["status"], "blocked_reward_risk")
        self.assertEqual(engine_policy["order_effect"], "block")
        self.assertTrue(engine_policy["criteria"]["new_setup"]["passed"])
        self.assertTrue(engine_policy["criteria"]["quality"]["passed"])
        self.assertFalse(engine_policy["criteria"]["reward_risk"]["passed"])
        self.assertEqual(engine_policy["time_gate_role"], "supporting_only")
        self.assertIsNotNone(
            self.database.active_reentry_lock(
                "A089030",
                as_of="2026-06-10T10:40:00",
            )
        )

    def test_rank_proxy_freshness_accepts_timezone_aware_now(self) -> None:
        result = BreakoutStrategy._rank_proxy_row(
            row={
                "observed_at": "2026-06-10T09:15:00",
                "big_data_rank": 3,
            },
            rank_key="big_data_rank",
            now=datetime(2026, 6, 10, 9, 20, tzinfo=timezone(timedelta(hours=9))),
            max_age_minutes=15,
            rank_limit=20,
        )

        self.assertTrue(result["fresh"])
        self.assertTrue(result["usable"])
        self.assertEqual(result["rank"], 3)

    def test_supply_flow_component_is_signed_and_weakly_weighted(self) -> None:
        missing = self.strategy._supply_component(SimpleNamespace(supply=None))
        good = self.strategy._supply_component(
            SimpleNamespace(
                supply={
                    "total_score": 82.0,
                    "broker_score": 30.0,
                    "foreign_broker_score": 20.0,
                    "investor_score": 25.0,
                    "blocked": False,
                }
            )
        )
        weak = self.strategy._supply_component(
            SimpleNamespace(
                supply={
                    "total_score": 32.0,
                    "broker_score": -25.0,
                    "foreign_broker_score": -20.0,
                    "investor_score": -30.0,
                    "blocked": False,
                }
            )
        )

        self.assertEqual(missing.weight, Decimal("0"))
        self.assertGreater(good.weighted_score, 0)
        self.assertLess(weak.weighted_score, 0)
        self.assertLessEqual(good.weighted_score, Decimal("4"))
        self.assertGreaterEqual(weak.weighted_score, Decimal("-4"))

    def test_supply_block_reduces_data_confidence_and_saves_replay(self) -> None:
        self.database.save_supply_score(
            {
                "stock_code": "A005930",
                "stock_name": "?쇱꽦?꾩옄",
                "evaluated_at": "2026-06-10T10:39:00",
                "total_score": 12.0,
                "broker_score": -55.0,
                "foreign_broker_score": -45.0,
                "investor_score": -50.0,
                "program_score": 0.0,
                "blocked": True,
                "blocked_reason": "supply blocked",
                "reasons": ["supply blocked"],
                "components": {"trend": {"consecutive_net_sell_count": 3}},
            }
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)
        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=False,
            now=datetime(2026, 6, 10, 10, 40),
        )[0]

        self.assertEqual(decision.action, DecisionAction.BUY)
        self.assertTrue(decision.reason)
        supply_component = {
            component.name: component for component in decision.components
        }["supply_flow"]
        self.assertLess(supply_component.score, 0)
        self.assertEqual(result["action"], "observe")
        confidence = result["trade_plan"].setup["data_confidence_policy"]
        self.assertFalse(confidence["individual_hard_blocks"])
        self.assertEqual(confidence["band"], "medium")
        self.assertEqual(confidence["order_effect"], "reduce_quantity")
        replays = self.database.latest_strategy_replays(limit=1)
        self.assertEqual(len(replays), 1)
        self.assertEqual(replays[0]["stock_code"], "A005930")
        self.assertTrue(replays[0]["supply_score"]["blocked"])
        self.assertGreater(len(replays[0]["candles"]), 0)

    def test_strong_leader_chart_relaxes_supply_hard_block_with_reduced_size(
        self,
    ) -> None:
        now, signal, plan, leader_score, _decision = self._strong_a_grade_signal_plan()
        blocked_supply = {
            "total_score": 18.0,
            "broker_score": -55.0,
            "foreign_broker_score": -45.0,
            "investor_score": -50.0,
            "program_score": -40.0,
            "blocked": True,
            "blocked_reason": "프로그램 당일 매도 집중",
            "components": {
                "program": {
                    "policy_state": "block",
                    "hard_block": True,
                    "consecutive_net_sell_count": 3,
                }
            },
        }
        signal = replace(signal, supply=blocked_supply)
        decision = self.strategy.decide(signal)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            decision_score=decision.score,
        )

        self.assertEqual(decision.action, DecisionAction.BUY)
        self.assertFalse(blocked, message)
        self.assertEqual(policy["status"], "allowed_supply_hard_block_reduced_probe")
        self.assertEqual(policy["quantity_multiplier"], "0.25")
        self.assertEqual(
            self.strategy._apply_entry_guard_quantity(100, policy),
            25,
        )
        self.assertEqual(policy["stop_policy"], "supply_hard_block_fast_exit_watch")
        self.assertTrue(policy["supply_block_policy"]["confirmed_net_sell"])
        self.assertTrue(policy["supply_block_policy"]["fast_profit_lock"])

    def test_supply_hard_block_blocks_followup_after_probe(self) -> None:
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(
            signal,
            supply={
                "total_score": 18.0,
                "blocked": True,
                "blocked_reason": "프로그램 당일 매도 집중",
                "components": {
                    "program": {
                        "policy_state": "block",
                        "hard_block": True,
                        "consecutive_net_sell_count": 3,
                    }
                },
            },
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, 'supply-probe-seed', 'A005930', 'buy',
                        'market', 1, 'filled', 'seed', NULL, ?, ?)
                """,
                (
                    self._account_id(),
                    "2026-06-10T10:10:00",
                    "2026-06-10T10:10:00",
                ),
            )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_supply_confirmed_followup")
        self.assertTrue(policy["supply_block_policy"]["confirmed_net_sell"])
        self.assertIn("후속 진입 차단", message)

    def test_trade_plan_setup_saves_supply_flow_details(self) -> None:
        self.database.save_supply_score(
            {
                "stock_code": "A005930",
                "stock_name": "?쇱꽦?꾩옄",
                "evaluated_at": "2026-06-10T10:39:00",
                "total_score": 82.0,
                "broker_score": 35.0,
                "foreign_broker_score": 25.0,
                "investor_score": 30.0,
                "program_score": 0.0,
                "blocked": False,
                "blocked_reason": None,
                "reasons": ["?곸쐞 嫄곕옒??留ㅼ닔 ?곗쐞"],
                "components": {"program": {"status": "not_collected"}},
            }
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=False,
            now=datetime(2026, 6, 10, 10, 40),
        )[0]

        self.assertIsNotNone(result["trade_plan"])
        setup = result["trade_plan"].setup
        self.assertEqual(setup["supply_score"], 82.0)
        self.assertFalse(setup["supply_blocked"])
        self.assertIsNone(setup["supply_block_reason"])
        self.assertEqual(setup["supply_flow"]["components"]["program"]["status"], "not_collected")

    def test_forward_outcome_tracks_rally_after_confidence_routed_buy(self) -> None:
        self.database.save_supply_score(
            {
                "stock_code": "A005930",
                "stock_name": "?쇱꽦?꾩옄",
                "evaluated_at": "2026-06-10T10:39:00",
                "total_score": 12.0,
                "broker_score": -55.0,
                "foreign_broker_score": -45.0,
                "investor_score": -50.0,
                "program_score": 0.0,
                "blocked": True,
                "blocked_reason": "?곸쐞 嫄곕옒??留ㅻ룄 ?뺣젰",
                "reasons": ["?곸쐞 嫄곕옒??留ㅻ룄 ?곗쐞"],
                "components": {},
            }
        )
        self.strategy.run(
            "default",
            ["A005930"],
            execute=False,
            now=datetime(2026, 6, 10, 10, 40),
        )
        future_candles = []
        for minute, high, low, close in (
            (45, 103, 99, 102),
            (50, 104, 101, 103),
            (55, 105, 102, 104),
            (0, 106, 103, 105),
            (5, 106, 104, 105),
            (10, 107, 104, 106),
        ):
            hour = 10 if minute >= 45 else 11
            future_candles.append(
                {
                    "candle_time": (
                        f"2026-06-10T{hour:02d}:{minute:02d}:00"
                    ),
                    "open_price": 100,
                    "high_price": high,
                    "low_price": low,
                    "close_price": close,
                    "volume": 1_000,
                }
            )
        self.database.save_candles(
            "test",
            "A005930",
            {"future": True},
            future_candles,
            candle_type="minute",
            interval_minutes=5,
        )

        saved = self.database.update_decision_forward_outcomes(
            horizons=(30,),
            limit=10,
        )
        replays = self.database.latest_strategy_replays(limit=1)

        self.assertEqual(saved, 1)
        self.assertEqual(
            replays[0]["forward_outcomes"][0]["actual_outcome"],
            "entered_then_rallied",
        )
        self.assertGreater(replays[0]["forward_outcomes"][0]["max_up_rate"], 0.03)
        self.assertTrue(
            any(
                candle["relative_position"] == "after"
                for candle in replays[0]["candles"]
            )
        )

    def test_forward_outcome_classifies_lowercase_buy_action(self) -> None:
        self.assertEqual(
            self.database._classify_forward_outcome(
                action="buy",
                max_up_rate=0.04,
                max_down_rate=-0.005,
                order_status=None,
            ),
            "entered_then_rallied",
        )

    def test_forward_outcome_waits_until_horizon_has_elapsed(self) -> None:
        self.strategy.run(
            "default",
            ["A005930"],
            execute=False,
            now=datetime(2026, 6, 10, 10, 40),
        )
        self.database.save_candles(
            "test",
            "A005930",
            {"future": True},
            [
                {
                    "candle_time": "2026-06-10T10:45:00",
                    "open_price": 100,
                    "high_price": 105,
                    "low_price": 99,
                    "close_price": 104,
                    "volume": 1_000,
                }
            ],
            candle_type="minute",
            interval_minutes=5,
        )

        saved = self.database.update_decision_forward_outcomes(
            horizons=(30,),
            limit=10,
            now=datetime(2026, 6, 10, 10, 50),
        )

        self.assertEqual(saved, 0)

    def test_breakout_decision_score_is_clamped_to_domain_range(self) -> None:
        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=False,
            now=datetime(2026, 6, 10, 10, 40),
        )[0]

        self.assertLessEqual(result["decision"].score, Decimal("100"))
        self.assertGreaterEqual(result["decision"].score, Decimal("-100"))

    def test_chart_pattern_can_force_entry_when_base_volume_fails(self) -> None:
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 103},
            current_price=103,
            open_price=96,
            high_price=103,
            low_price=95,
            volume=1_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )
        self._save_candidate_score(
            features={
                "new_60d_high": True,
                "relative_trade_amount20": 2.5,
            }
        )
        self.database.save_order_book(
            "A005930",
            {"book": True},
            [],
            quote_time="104000",
            best_ask_price=104,
            best_ask_quantity=1_000,
            best_bid_price=103,
            best_bid_quantity=2_000,
            total_ask_quantity=10_000,
            total_bid_quantity=20_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )
        self.database.save_trade_strength(
            "A005930",
            {"strength": True},
            [
                {
                    "trade_time": "2026-06-10T10:40:00",
                    "current_price": 103,
                    "trade_quantity": 100,
                    "accumulated_volume": 1_000,
                    "accumulated_amount": 103_000,
                    "strength": 180,
                    "ask_price": 104,
                    "bid_price": 103,
                    "exchange_type": "1",
                }
            ],
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)

        self.assertFalse(signal.volume_pass)
        self.assertTrue(signal.chart.force_entry)
        self.assertEqual(decision.action, DecisionAction.BUY)
        self.assertTrue(decision.reason)

    def test_chart_force_entry_bypasses_session_score_and_type_limits(self) -> None:
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 103},
            current_price=103,
            open_price=96,
            high_price=103,
            low_price=95,
            volume=1_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )
        self._save_candidate_score(
            features={
                "new_60d_high": True,
                "relative_trade_amount20": 2.5,
            }
        )
        self.database.save_order_book(
            "A005930",
            {"book": True},
            [],
            quote_time="104000",
            best_ask_price=104,
            best_ask_quantity=1_000,
            best_bid_price=103,
            best_bid_quantity=2_000,
            total_ask_quantity=10_000,
            total_bid_quantity=20_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )
        self.database.save_trade_strength(
            "A005930",
            {"strength": True},
            [
                {
                    "trade_time": "2026-06-10T10:40:00",
                    "current_price": 103,
                    "trade_quantity": 100,
                    "accumulated_volume": 1_000,
                    "accumulated_amount": 103_000,
                    "strength": 180,
                    "ask_price": 104,
                    "bid_price": 103,
                    "exchange_type": "1",
                }
            ],
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertEqual(result["action"], "executed")
        router = result["trade_plan"].setup["router_execution_policy"]
        self.assertEqual(
            router["status"],
            "allowed_a_leader_money_flow_minimum_probe",
        )
        self.assertEqual(router["order_effect"], "allow_minimum_probe")
        self.assertEqual(result["trade_plan"].planned_quantity, 1)
        self.assertTrue(result["signal"].chart.force_entry)
        self.assertTrue(result["trade_plan"].entry_type.startswith("chart_"))
        self.assertNotIn("吏꾩엯 ?좏삎 ?쒗븳", result["message"])

    def test_chart_risk_blocks_even_when_base_conditions_pass(self) -> None:
        candles = []
        for minute, price in enumerate((102, 103, 104, 105), start=25):
            candles.append(
                {
                    "candle_time": f"2026-06-10T10:{minute:02d}:00",
                    "open_price": price - 1,
                    "high_price": price + 1,
                    "low_price": price - 2,
                    "close_price": price,
                    "volume": 1_000,
                }
            )
        self.database.save_candles(
            "test",
            "A005930",
            {"risk": True},
            candles,
            candle_type="minute",
            interval_minutes=5,
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)

        self.assertTrue(signal.passed)
        self.assertTrue(signal.chart.blocked)
        self.assertEqual(decision.action, DecisionAction.HOLD)
        self.assertTrue(decision.reason)

    def test_strategy_executes_sized_buy_and_prevents_duplicate(self) -> None:
        seoul = timezone(timedelta(hours=9))
        first = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=seoul),
        )[0]
        second = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 41, tzinfo=seoul),
        )[0]

        self._assert_shadow_only_router(first)
        self.assertEqual(first["decision"].action, DecisionAction.BUY)
        self.assertEqual(second["action"], "observe")
        summary = self.paper.account_summary("default")
        self.assertEqual(len(summary["positions"]), 0)
        self.assertEqual(len(self.paper.list_orders("default")), 0)
        with self.database.connect() as connection:
            decision = connection.execute(
                "SELECT * FROM strategy_decisions"
            ).fetchone()
            request = connection.execute(
                "SELECT * FROM order_requests"
            ).fetchone()
            execution = connection.execute(
                "SELECT * FROM order_executions"
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM trade_plans"
            ).fetchone()
            components = connection.execute(
                "SELECT COUNT(*) FROM score_components"
            ).fetchone()[0]
            chart_setup = connection.execute(
                "SELECT * FROM chart_setups"
            ).fetchone()
            entry_plan = connection.execute(
                "SELECT * FROM entry_plans"
            ).fetchone()
        self.assertEqual(decision["outcome"], "observe")
        self.assertIsNotNone(decision["legacy_signal_id"])
        self.assertIsNone(request)
        self.assertIsNone(execution)
        self.assertEqual(plan["decision_id"], decision["id"])
        self.assertEqual(plan["entry_type"], "volume_breakout")
        self.assertEqual(plan["entry_price"], 105)
        self.assertEqual(plan["stop_loss_price"], 97)
        self.assertEqual(plan["first_take_profit_price"], 113)
        self.assertEqual(plan["breakeven_price"], 105)
        self.assertEqual(plan["partial_exit_rate"], 0.5)
        self.assertEqual(components, 14)
        self.assertIsNotNone(chart_setup)
        self.assertIsNotNone(entry_plan)
        setup = json.loads(plan["setup_json"])
        self.assertEqual(
            setup["router_execution_policy"]["status"],
            "shadow_only_legacy_entry_route",
        )
        self.assertEqual(
            setup["legacy_entry_policy"]["legacy_route"],
            "simple_volume_breakout",
        )
        self.assertTrue(setup["legacy_entry_policy"]["old_logic_would_buy"])
        self.assertTrue(
            setup["legacy_entry_policy"]["removed_from_order_influence"]
        )
        self.assertEqual(len(self.notifier.messages), 0)

    def test_market_mode_gate_shadows_mismatched_entry_when_risk_off(self) -> None:
        seoul = timezone(timedelta(hours=9))
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            self.strategy_settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
            market_mode_settings=MarketModeGateSettings(
                enabled=True,
                manual_mode="risk_off",
            ),
        )

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=seoul),
        )[0]

        self.assertEqual(result["decision"].action, DecisionAction.BUY)
        self.assertEqual(result["action"], "observe")
        self.assertTrue(result["message"])
        self.assertIsNotNone(result["trade_plan"])
        regime = result["trade_plan"].setup["market_regime_playbook_policy"]
        router = result["trade_plan"].setup["router_execution_policy"]
        self.assertEqual(regime["status"], "defer_non_core_to_strategy_router")
        self.assertEqual(router["order_effect"], "shadow_only")
        self.assertEqual(len(self.paper.list_orders("default")), 0)
        replays = self.database.latest_strategy_replays(limit=1)
        self.assertEqual(replays[0]["action"], "buy")
        self.assertEqual(replays[0]["details"]["strategy_action"], "buy")
        self.assertEqual(replays[0]["details"]["runtime_action"], "buy")

    def test_morning_entry_guard_blocks_after_limit(self) -> None:
        self._use_strategy_settings(
            morning_entry_guard_enabled=True,
            morning_max_new_buys=5,
        )
        seoul = timezone(timedelta(hours=9))
        with self.database.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = 'default'"
            ).fetchone()["id"]
            for index in range(5):
                connection.execute(
                    """
                    INSERT INTO paper_orders(
                        account_id, client_order_id, stock_code, side,
                        order_type, quantity, status, reason,
                        rejection_reason, requested_at, completed_at
                    )
                    VALUES (?, ?, ?, 'buy', 'market', 1, 'filled',
                            'seed', NULL, ?, ?)
                    """,
                    (
                        account_id,
                        f"morning-seed-{index}",
                        f"A{index:06d}",
                        "2026-06-10T09:12:00",
                        "2026-06-10T09:12:00",
                    ),
                )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 9, 15, tzinfo=seoul),
        )[0]

        self.assertEqual(result["decision"].action, DecisionAction.BUY)
        self.assertEqual(result["action"], "skipped")
        self.assertTrue(result["message"])
        self.assertIsNone(result["trade_plan"])
        self.assertEqual(len(self.paper.list_orders("default")), 5)

    def test_entry_bucket_guard_blocks_excessive_ten_minute_orders(self) -> None:
        self._use_strategy_settings(max_new_buys_per_bucket=5)
        seoul = timezone(timedelta(hours=9))
        with self.database.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = 'default'"
            ).fetchone()["id"]
            for index in range(5):
                connection.execute(
                    """
                    INSERT INTO paper_orders(
                        account_id, client_order_id, stock_code, side,
                        order_type, quantity, status, reason,
                        rejection_reason, requested_at, completed_at
                    )
                    VALUES (?, ?, ?, 'buy', 'market', 1, 'filled',
                            'seed', NULL, ?, ?)
                    """,
                    (
                        account_id,
                        f"bucket-seed-{index}",
                        f"A{index:06d}",
                        "2026-06-10T10:41:00",
                        "2026-06-10T10:41:00",
                    ),
                )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 45, tzinfo=seoul),
        )[0]

        self.assertEqual(result["action"], "skipped")
        self.assertTrue(result["message"])
        self.assertIsNone(result["trade_plan"])
        self.assertEqual(len(self.paper.list_orders("default")), 5)

    def test_same_stock_repeat_buy_is_shadowed_when_supply_data_is_missing(self) -> None:
        seoul = timezone(timedelta(hours=9))
        self._delete_supply_scores()
        with self.database.connect() as connection:
            account_id = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = 'default'"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, 'same-stock-seed', 'A005930', 'buy',
                        'market', 1, 'filled', 'seed', NULL, ?, ?)
                """,
                (
                    account_id,
                    "2026-06-10T10:10:00",
                    "2026-06-10T10:10:00",
                ),
            )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=seoul),
        )[0]

        self.assertEqual(result["action"], "observe")
        self.assertTrue(result["message"])
        self.assertIsNotNone(result["trade_plan"])
        confidence = result["trade_plan"].setup["data_confidence_policy"]
        self.assertIn(confidence["band"], {"medium", "low", "poor"})
        self.assertEqual(len(self.paper.list_orders("default")), 1)

    def test_a_grade_entry_survives_daily_loss_limit_with_reduced_size(self) -> None:
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        self._insert_realized_loss(-50_000, at="2026-06-10T10:20:00")

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "A")
        self.assertEqual(policy["risk_mode"], "emergency")
        self.assertEqual(policy["status"], "allowed_a_grade_after_daily_loss_limit")
        self.assertEqual(policy["quantity_multiplier"], "0.40")
        self.assertTrue(policy["risk_override"])
        self.assertEqual(policy["add_buy_policy"], "allowed")
        self.assertEqual(policy["stop_policy"], "shakeout_watch")

    def test_daily_loss_override_does_not_expand_data_gap_probe(self) -> None:
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(signal, supply=None)
        self._insert_realized_loss(-50_000, at="2026-06-10T10:20:00")

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["risk_mode"], "emergency")
        self.assertEqual(policy["quantity_multiplier"], "0.20")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertEqual(
            self.strategy._apply_entry_guard_quantity(100, policy),
            1,
        )

    def test_cooldown_allows_only_a_grade_breakout_or_opening_range(self) -> None:
        self._use_risk_settings(max_daily_stop_losses=3)
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        self._insert_stop_losses(3)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["risk_mode"], "cooldown")
        self.assertEqual(policy["status"], "allowed_a_grade_cooldown_probe")
        self.assertEqual(policy["quantity_multiplier"], "0.35")
        self.assertTrue(policy["risk_override"])

        weak_plan = replace(plan, entry_type="chart_order_block_reentry")
        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=weak_plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_daily_stop_loss_cooldown")
        self.assertIn("cooldown", message)

    def test_a_grade_same_stock_repeat_is_allowed(self) -> None:
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, 'same-stock-a-seed', 'A005930', 'buy',
                        'market', 1, 'filled', 'seed', NULL, ?, ?)
                """,
                (
                    self._account_id(),
                    "2026-06-10T10:10:00",
                    "2026-06-10T10:10:00",
                ),
            )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=True,
            leader_score=leader_score,
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "A")
        self.assertTrue(policy["same_stock"]["repeat_allowed"])
        self.assertEqual(policy["add_buy_policy"], "allowed")

    def test_missing_supply_and_leader_blocks_force_entry_without_rank_proxy(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(signal, supply=None)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=None,
            actual_leader_score=None,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_missing_supply_leader")
        self.assertEqual(policy["data_availability"]["supply_status"], "missing")
        self.assertEqual(policy["data_availability"]["leader_status"], "missing")
        self.assertTrue(message)

    def test_missing_leader_uses_fresh_rank_proxy_for_minimum_probe(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(signal, supply=None)
        self.database.save_market_rank_snapshot(
            rank_type="trade_amount",
            api_id="ka10032",
            market_type="ALL",
            query_params={},
            payload={"test": True},
            items=[
                {
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "rank": 3,
                    "trade_amount": 100_000_000_000,
                }
            ],
            observed_at="2026-06-10T10:39:00",
            collected_at=datetime(2026, 6, 10, 10, 39),
        )
        proxy = self.strategy._market_rank_proxy("A005930", now)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=self.strategy._proxy_leader_score(
                stock_code="A005930",
                market_rank_proxy=proxy,
            ),
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["status"], "allowed_minimum_probe_data_gap")
        self.assertEqual(
            policy["data_availability"]["status"],
            "minimum_probe_missing_supply_leader_with_rank_proxy",
        )
        self.assertTrue(policy["data_availability"]["minimum_probe"])
        self.assertEqual(policy["data_availability"]["leader_status"], "market_rank_proxy")
        self.assertEqual(policy["quantity_multiplier"], "0.20")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertEqual(policy["add_buy_policy"], "blocked")
        self.assertEqual(policy["reentry_policy"], "blocked_until_data_collected")

    def test_missing_supply_strong_b_grade_breakout_gets_morning_probe(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        now = now.replace(hour=10, minute=20)
        chart = replace(
            signal.chart,
            total_score=Decimal("75"),
            daily_setup_name="daily_pullback",
            entry_type="chart_breakout_hold",
            force_entry=True,
            blocked=False,
        )
        signal = replace(
            signal,
            chart=chart,
            supply=None,
            previous_high=None,
            breakout_hold_pass=True,
        )
        self.database.save_market_rank_snapshot(
            rank_type="trade_amount",
            api_id="ka10032",
            market_type="ALL",
            query_params={},
            payload={"test": True},
            items=[
                {
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "rank": 2,
                    "trade_amount": 100_000_000_000,
                }
            ],
            observed_at="2026-06-10T10:39:00",
            collected_at=datetime(2026, 6, 10, 10, 39),
        )
        proxy = self.strategy._market_rank_proxy("A005930", now)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=self.strategy._proxy_leader_score(
                stock_code="A005930",
                market_rank_proxy=proxy,
            ),
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=Decimal("76"),
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(policy["status"], "allowed_minimum_probe_data_gap")
        self.assertEqual(
            policy["data_availability"]["status"],
            "minimum_probe_missing_supply_strong_b_grade_breakout",
        )
        self.assertEqual(policy["quantity_multiplier"], "0.20")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertEqual(policy["add_buy_policy"], "blocked")
        self.assertEqual(policy["reentry_policy"], "blocked_until_data_collected")

    def test_missing_supply_strong_b_grade_probe_is_blocked_in_risk_off(self) -> None:
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            self.strategy_settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
            market_mode_settings=MarketModeGateSettings(
                enabled=True,
                manual_mode="risk_off",
            ),
        )
        now, signal, plan, _leader_score, _decision = self._strong_a_grade_signal_plan()
        chart = replace(
            signal.chart,
            total_score=Decimal("75"),
            daily_setup_name="daily_pullback",
            entry_type="chart_breakout_hold",
            force_entry=True,
            blocked=False,
        )
        signal = replace(
            signal,
            chart=chart,
            supply=None,
            previous_high=None,
            breakout_hold_pass=True,
        )
        self.database.save_market_rank_snapshot(
            rank_type="trade_amount",
            api_id="ka10032",
            market_type="ALL",
            query_params={},
            payload={"test": True},
            items=[
                {
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "rank": 2,
                    "trade_amount": 100_000_000_000,
                }
            ],
            observed_at="2026-06-10T10:39:00",
            collected_at=datetime(2026, 6, 10, 10, 39),
        )
        proxy = strategy._market_rank_proxy("A005930", now)

        blocked, message, policy = strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=strategy._proxy_leader_score(
                stock_code="A005930",
                market_rank_proxy=proxy,
            ),
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=Decimal("76"),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(policy["status"], "blocked_missing_supply_non_a_grade")
        self.assertTrue(message)

    def test_data_gap_minimum_probe_blocks_same_stock_additional_buy(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(signal, supply=None)
        self.database.save_market_rank_snapshot(
            rank_type="trade_amount",
            api_id="ka10032",
            market_type="ALL",
            query_params={},
            payload={"test": True},
            items=[
                {
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "rank": 3,
                    "trade_amount": 100_000_000_000,
                }
            ],
            observed_at="2026-06-10T10:39:00",
            collected_at=datetime(2026, 6, 10, 10, 39),
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, 'data-gap-probe-seed', 'A005930', 'buy',
                        'market', 1, 'filled', 'minimum probe', NULL, ?, ?)
                """,
                (
                    self._account_id(),
                    "2026-06-10T10:10:00",
                    "2026-06-10T10:10:00",
                ),
            )
        proxy = self.strategy._market_rank_proxy("A005930", now)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=True,
            leader_score=self.strategy._proxy_leader_score(
                stock_code="A005930",
                market_rank_proxy=proxy,
            ),
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_data_gap_add_buy_policy")
        self.assertEqual(policy["add_buy_policy"], "blocked")
        self.assertEqual(policy["reentry_policy"], "blocked_until_data_collected")
        self.assertEqual(policy["same_stock"]["buy_count"], 1)
        self.assertTrue(message)

    def test_chart_breakout_hold_data_gap_probe_is_once_per_day(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(signal, supply=None)
        self.database.save_market_rank_snapshot(
            rank_type="trade_amount",
            api_id="ka10032",
            market_type="ALL",
            query_params={},
            payload={"test": True},
            items=[
                {
                    "stock_code": "A005930",
                    "stock_name": "??깃쉐?袁⑹쁽",
                    "rank": 3,
                    "trade_amount": 100_000_000_000,
                }
            ],
            observed_at="2026-06-10T10:39:00",
            collected_at=datetime(2026, 6, 10, 10, 39),
        )
        with self.database.connect() as connection:
            decision_id = connection.execute(
                """
                INSERT INTO strategy_decisions(
                    account_id, strategy_version_id, stock_code,
                    decided_at, source_data_time, action, score,
                    confidence, reason, outcome, message,
                    created_at, updated_at
                )
                VALUES (?, 1, 'A005930',
                        '2026-06-10T10:20:00',
                        '2026-06-10T10:20:00',
                        'buy', 80, 0.8, 'seed probe', 'failed',
                        'seed failed probe',
                        '2026-06-10T10:20:00',
                        '2026-06-10T10:20:00')
                """,
                (self._account_id(),),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO trade_plans(
                    decision_id, stock_code, entry_type, entry_tags_json,
                    entry_price, planned_quantity, stop_loss_price,
                    stop_loss_reason, first_take_profit_price,
                    first_take_profit_reason, partial_exit_rate,
                    breakeven_price, breakeven_trigger_price,
                    trailing_rule, invalidation_reason, reward_risk_ratio,
                    setup_json, created_at, updated_at
                )
                VALUES (?, 'A005930', 'chart_breakout_hold', '[]',
                        105, 1, 100, 'seed stop', 112,
                        'seed take', 0.5, 105, 108,
                        'seed trailing', 'seed invalidation', 2.0,
                        ?, '2026-06-10T10:20:00',
                        '2026-06-10T10:20:00')
                """,
                (
                    decision_id,
                    json.dumps(
                        {
                            "entry_guard_policy": {
                                "status": "allowed_minimum_probe_data_gap",
                                "data_availability": {
                                    "minimum_probe": True,
                                    "status": (
                                        "minimum_probe_missing_supply_"
                                        "leader_with_rank_proxy"
                                    ),
                                },
                            },
                            "data_availability_policy": {
                                "minimum_probe": True,
                                "status": (
                                    "minimum_probe_missing_supply_"
                                    "leader_with_rank_proxy"
                                ),
                            },
                        }
                    ),
                ),
            )
        proxy = self.strategy._market_rank_proxy("A005930", now)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=self.strategy._proxy_leader_score(
                stock_code="A005930",
                market_rank_proxy=proxy,
            ),
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(
            policy["status"],
            "blocked_chart_breakout_hold_data_gap_probe_once",
        )
        self.assertEqual(policy["same_stock"]["buy_count"], 0)
        self.assertEqual(policy["same_stock"]["data_gap_probe_attempt_count"], 1)
        self.assertTrue(message)

    def test_missing_supply_blocks_b_or_c_grade_force_entry(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(signal, supply=None)
        weak_plan = replace(plan, entry_type="chart_vwap_reclaim")
        follower = {"role": "follower", "total_score": 50.0, "blocked": False}

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=weak_plan,
            now=now,
            has_position=False,
            leader_score=follower,
            actual_leader_score=follower,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_missing_supply_non_a_grade")
        self.assertIn(policy["entry_grade"], {"B", "C"})
        self.assertTrue(message)

    def test_missing_supply_b_grade_is_blocked_even_with_rank_proxy(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(
            signal,
            supply=None,
            chart=replace(signal.chart, total_score=Decimal("70")),
        )
        proxy = {
            "usable": True,
            "source": "trade_amount",
            "rank": 3,
        }

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=self.strategy._proxy_leader_score(
                stock_code="A005930",
                market_rank_proxy=proxy,
            ),
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(policy["status"], "blocked_missing_supply_non_a_grade")
        self.assertTrue(message)

    def test_midday_b_grade_chase_without_support_is_one_share_probe(self) -> None:
        now, signal, plan, leader_score, _decision = self._strong_a_grade_signal_plan()
        chart = replace(
            signal.chart,
            total_score=Decimal("75"),
            daily_setup_name="daily_pullback",
            entry_type="chart_breakout_hold",
            force_entry=True,
            blocked=False,
        )
        signal = replace(
            signal,
            chart=chart,
            previous_high=None,
            current_price=105,
            intraday_high=106,
            vwap_pass=False,
            pullback_pass=False,
            moving_average_pass=False,
            breakout_hold_pass=True,
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now.replace(hour=10, minute=40),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=Decimal("76"),
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(policy["status"], "minimum_probe_midday_b_grade_chase")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertFalse(policy["entry_type_policy"]["midday_b_chase"]["support_confirmed"])

    def test_midday_b_grade_chase_without_leader_or_rank_is_blocked(self) -> None:
        now, signal, plan, _leader_score, _decision = self._strong_a_grade_signal_plan()
        chart = replace(
            signal.chart,
            total_score=Decimal("75"),
            daily_setup_name="daily_pullback",
            entry_type="chart_breakout_hold",
            force_entry=True,
            blocked=False,
        )
        signal = replace(
            signal,
            chart=chart,
            previous_high=None,
            current_price=105,
            intraday_high=106,
            vwap_pass=False,
            pullback_pass=False,
            moving_average_pass=False,
            breakout_hold_pass=True,
        )
        follower = {"role": "follower", "total_score": 55.0, "blocked": False}

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now.replace(hour=10, minute=40),
            has_position=False,
            leader_score=follower,
            actual_leader_score=follower,
            market_rank_proxy={"usable": False},
            decision_score=Decimal("76"),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(
            policy["status"],
            "blocked_midday_b_grade_chase_without_support",
        )
        self.assertTrue(message)

    def test_b_grade_repeat_high_chase_after_midday_is_blocked(self) -> None:
        self._insert_filled_buy()
        now, signal, plan, leader_score, _decision = self._strong_a_grade_signal_plan()
        chart = replace(
            signal.chart,
            total_score=Decimal("75"),
            daily_setup_name="daily_pullback",
            entry_type="chart_breakout_hold",
            force_entry=True,
            blocked=False,
        )
        signal = replace(
            signal,
            chart=chart,
            previous_high=None,
            current_price=105,
            intraday_high=106,
            vwap_pass=False,
            pullback_pass=False,
            moving_average_pass=False,
            breakout_hold_pass=True,
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now.replace(hour=10, minute=40),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=Decimal("76"),
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(
            policy["status"],
            "blocked_b_grade_midday_high_chase_reentry",
        )
        self.assertEqual(
            policy["entry_type_policy"]["b_grade_reentry_limit"][
                "same_stock_buy_count"
            ],
            1,
        )
        engine_policy = policy["reentry_engine_policy"]
        self.assertEqual(engine_policy["status"], "blocked_b_grade_reentry")
        self.assertEqual(engine_policy["order_effect"], "block")
        self.assertTrue(
            engine_policy["criteria"]["b_grade_repeat_limit"]["checked"]
        )
        self.assertFalse(
            engine_policy["criteria"]["b_grade_repeat_limit"]["passed"]
        )
        self.assertTrue(message)

    def test_b_grade_repeat_after_midday_is_one_share_probe(self) -> None:
        self._insert_filled_buy()
        now, signal, plan, leader_score, _decision = self._strong_a_grade_signal_plan()
        chart = replace(
            signal.chart,
            total_score=Decimal("75"),
            daily_setup_name="daily_pullback",
            entry_type="chart_breakout_hold",
            force_entry=True,
            blocked=False,
        )
        signal = replace(
            signal,
            chart=chart,
            previous_high=None,
            current_price=105,
            intraday_high=106,
            vwap_pass=True,
            pullback_pass=False,
            moving_average_pass=False,
            breakout_hold_pass=True,
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now.replace(hour=10, minute=40),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=Decimal("76"),
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(policy["status"], "minimum_probe_b_grade_reentry")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertEqual(
            policy["entry_type_policy"]["b_grade_reentry_limit"][
                "same_stock_buy_count"
            ],
            1,
        )
        engine_policy = policy["reentry_engine_policy"]
        self.assertEqual(engine_policy["status"], "minimum_probe_b_grade_reentry")
        self.assertEqual(engine_policy["order_effect"], "allow_minimum_probe")
        self.assertTrue(
            engine_policy["criteria"]["b_grade_repeat_limit"]["minimum_probe"]
        )

    def test_same_level_two_failures_block_until_new_base_or_high(self) -> None:
        self._insert_failed_level_trade(at="2026-06-10T10:10:00")
        self._insert_failed_level_trade(at="2026-06-10T10:20:00")
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(
            signal,
            breakout_level=100,
            previous_high=100,
            intraday_high=106,
            current_price=105,
            vwap=Decimal("101"),
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        failure_policy = policy["same_level_failure_policy"]
        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_same_level_failure_reentry")
        self.assertEqual(failure_policy["failure_count"], 2)
        self.assertEqual(len(failure_policy["matched_failures"]), 2)
        engine_policy = policy["reentry_engine_policy"]
        self.assertEqual(engine_policy["status"], "blocked_same_failure_level")
        self.assertEqual(engine_policy["order_effect"], "block")
        self.assertTrue(engine_policy["criteria"]["same_level_failure"]["checked"])
        self.assertFalse(engine_policy["criteria"]["same_level_failure"]["passed"])
        self.assertIn("same level failed twice", message)

    def test_same_level_failures_are_not_unlocked_by_new_high_alone(self) -> None:
        self._insert_failed_level_trade(at="2026-06-10T10:10:00")
        self._insert_failed_level_trade(at="2026-06-10T10:20:00")
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(
            signal,
            breakout_level=100,
            previous_high=108,
            intraday_high=109,
            current_price=108,
            vwap=Decimal("101"),
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        failure_policy = policy["same_level_failure_policy"]
        self.assertTrue(blocked, message)
        self.assertEqual(failure_policy["failure_count"], 2)
        self.assertFalse(failure_policy["unlocked_by_new_high"])
        self.assertEqual(
            failure_policy["status"],
            "blocked_same_level_failure_reentry",
        )

    def test_same_level_failures_are_unlocked_by_new_base(self) -> None:
        self._insert_failed_level_trade(at="2026-06-10T10:10:00")
        self._insert_failed_level_trade(at="2026-06-10T10:20:00")
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        signal = replace(
            signal,
            breakout_level=110,
            opening_range_high=110,
            previous_high=108,
            intraday_high=112,
            current_price=111,
            vwap=Decimal("101"),
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        failure_policy = policy["same_level_failure_policy"]
        self.assertFalse(blocked, message)
        self.assertTrue(failure_policy["unlocked_by_new_base"])
        self.assertEqual(
            failure_policy["status"],
            "allowed_new_base_after_same_level_failures",
        )

    def test_data_gap_probe_quantity_is_capped_at_one_share(self) -> None:
        policy = {
            "quantity_multiplier": "0.20",
            "max_quantity": 1,
        }

        self.assertEqual(
            self.strategy._apply_entry_guard_quantity(100, policy),
            1,
        )
        self.assertEqual(
            self.strategy._apply_entry_guard_quantity(3, policy),
            1,
        )
        self.assertEqual(
            self.strategy._apply_entry_guard_quantity(0, policy),
            0,
        )

    def test_order_block_reentry_requires_a_grade_and_uses_minimum_size(self) -> None:
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        order_block_plan = replace(plan, entry_type="chart_order_block_reentry")

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=order_block_plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "A")
        self.assertEqual(
            policy["entry_type_policy"]["status"],
            "minimum_probe_order_block_reentry_a_grade",
        )
        self.assertEqual(policy["quantity_multiplier"], "0.25")

        follower = {"role": "follower", "total_score": 50.0, "blocked": False}
        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=order_block_plan,
            now=now,
            has_position=False,
            leader_score=follower,
            actual_leader_score=follower,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_order_block_reentry_not_a_grade")

    def test_vwap_reclaim_a_grade_requires_reclaim_and_uses_minimum_size(self) -> None:
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        reclaim_plan = replace(plan, entry_type="chart_vwap_reclaim")

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=reclaim_plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(
            policy["entry_type_policy"]["status"],
            "minimum_probe_vwap_reclaim_a_grade",
        )
        self.assertEqual(policy["quantity_multiplier"], "0.25")

        unreclaimed = replace(signal, vwap=Decimal("110"), vwap_pass=False)
        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=unreclaimed,
            trade_plan=reclaim_plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(
            policy["status"],
            "blocked_vwap_reclaim_without_confirmation",
        )

    def test_opening_range_repeat_requires_vwap_reclaim(self) -> None:
        now, signal, plan, leader_score, decision = self._strong_a_grade_signal_plan()
        orb_plan = replace(plan, entry_type="opening_range_breakout")
        signal = replace(signal, vwap=Decimal("110"), vwap_pass=False)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    rejection_reason, requested_at, completed_at
                )
                VALUES (?, 'orb-seed', 'A005930', 'buy',
                        'market', 1, 'filled', 'seed', NULL, ?, ?)
                """,
                (
                    self._account_id(),
                    "2026-06-10T09:15:00",
                    "2026-06-10T09:15:00",
                ),
            )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=orb_plan,
            now=now,
            has_position=True,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_orb_reentry_without_vwap_reclaim")

    def test_low_score_chart_pattern_entry_is_c_grade_blocked(self) -> None:
        now, signal, plan, _leader_score, decision = self._strong_a_grade_signal_plan()
        weak_chart = replace(
            signal.chart,
            daily_setup_name="daily_unconfirmed",
            total_score=Decimal("58"),
            entry_type="chart_pattern_entry",
            force_entry=True,
        )
        signal = replace(
            signal,
            chart=weak_chart,
            supply={"total_score": 60.0, "blocked": False},
        )
        pattern_plan = replace(plan, entry_type="chart_pattern_entry")
        follower = {"role": "follower", "total_score": 52.0, "blocked": False}

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            signal=signal,
            trade_plan=pattern_plan,
            now=now,
            has_position=False,
            leader_score=follower,
            actual_leader_score=follower,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["entry_grade"], "C")
        self.assertEqual(policy["status"], "blocked_low_score_chart_pattern_entry")

    def test_legacy_entry_policy_marks_unconfirmed_chart_pattern_shadow(
        self,
    ) -> None:
        _now, signal, plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        pattern_plan = replace(plan, entry_type="chart_pattern_entry")
        signal = replace(signal, vwap_pass=False)
        playbook = {"name": "standard_breakout"}
        legacy = self.strategy._legacy_entry_policy(
            signal=signal,
            trade_plan=pattern_plan,
            entry_guard_policy={
                "status": "allowed",
                "entry_grade": "B",
                "money_flow_policy": {},
                "entry_grade_details": {"leader_role": "follower"},
            },
            strategy_playbook=playbook,
        )
        router = self.strategy._router_execution_policy(
            trade_plan=pattern_plan,
            strategy_playbook=playbook,
            legacy_entry_policy=legacy,
        )

        self.assertTrue(legacy["legacy_candidate"])
        self.assertTrue(legacy["old_logic_would_buy"])
        self.assertIn(
            "broad_chart_pattern_entry",
            legacy["legacy_reasons"],
        )
        self.assertIn(
            "unconfirmed_pattern_entry",
            legacy["legacy_reasons"],
        )
        self.assertEqual(router["status"], "shadow_only_legacy_entry_route")
        self.assertEqual(router["order_effect"], "shadow_only")
        self.assertTrue(router["would_order_without_router"])

    def test_market_mode_auto_unavailable_fails_closed_to_shadow(self) -> None:
        seoul = timezone(timedelta(hours=9))
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            self.strategy_settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
            market_mode_settings=MarketModeGateSettings(
                enabled=True,
                manual_mode="risk_off",
                source="auto_unavailable",
                requested_mode="auto",
                reason="?먮룞 ?쒖옣 紐⑤뱶 ?곗씠?곌? ?꾩쭅 ?곌껐?섏? ?딆븘 fallback 紐⑤뱶 ?곸슜",
            ),
        )

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=seoul),
        )[0]

        self.assertEqual(result["decision"].action, DecisionAction.BUY)
        self.assertEqual(result["action"], "observe")
        self.assertIsNotNone(result["trade_plan"])
        regime = result["trade_plan"].setup["market_regime_playbook_policy"]
        self.assertEqual(regime["status"], "shadow_only_market_data_unavailable")
        self.assertEqual(regime["order_effect"], "shadow_only")
        self.assertEqual(len(self.paper.list_orders("default")), 0)

    def test_opening_leader_core_allows_entry_before_opening_range_is_built(self) -> None:
        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(
                2026,
                6,
                10,
                9,
                5,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )[0]

        self.assertEqual(result["session_profile"].name, "opening_leader_core")
        self.assertEqual(result["strategy_mode"].name, "opening_leader_core")
        replays = self.database.latest_strategy_replays(limit=1)
        self.assertEqual(replays[0]["action"], "buy")
        self.assertEqual(replays[0]["details"]["strategy_action"], "buy")
        self.assertEqual(replays[0]["details"]["runtime_action"], "buy")
        self._assert_shadow_only_router(result)
        self.assertIsNotNone(result["trade_plan"])
        compare = result["trade_plan"].setup["mode_shadow_compare"]
        self.assertEqual(compare["actual_mode"], "normal")
        self.assertTrue(compare["shadow_only"])
        self.assertTrue(compare["normal"]["would_enter"])
        self.assertTrue(compare["small_account_aggressive"]["would_enter"])
        self.assertNotEqual(compare["diff"]["quantity_diff"], 0)
        ab_compare = result["trade_plan"].setup["strategy_ab_compare"]
        self.assertEqual(ab_compare["version"], "v0.12.89")
        self.assertEqual(ab_compare["actual_mode"], "remake")
        self.assertEqual(ab_compare["shadow_mode"], "normal")
        self.assertTrue(ab_compare["actual_orders_follow_selected_mode_only"])
        self.assertTrue(ab_compare["normal"]["would_enter"])
        self.assertFalse(ab_compare["remake"]["would_enter"])
        self.assertEqual(ab_compare["remake"]["order_effect"], "shadow_only")
        self.assertTrue(ab_compare["diff"]["normal_only_entry"])
        opening_policy = result["trade_plan"].setup["opening_leader_mode"]
        self.assertTrue(opening_policy["active"])
        self.assertEqual(opening_policy["window"], "09:00~09:20")
        self.assertEqual(
            opening_policy["loss_control"],
            "account_10pct_kill_switch_and_structure_break",
        )
        self.assertFalse(opening_policy["new_entry_limit_control"])
        self.assertEqual(len(self.paper.list_orders("default")), 0)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM trade_plans").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM partial_exit_plans"
                ).fetchone()[0],
                1,
            )
        replay_compare = replays[0]["details"]["mode_shadow_compare"]
        self.assertEqual(replay_compare["actual_mode"], "normal")
        self.assertNotEqual(replay_compare["diff"]["quantity_diff"], 0)
        replay_ab = replays[0]["details"]["strategy_ab_compare"]
        self.assertEqual(replay_ab["actual_mode"], "remake")
        self.assertTrue(replay_ab["diff"]["normal_only_entry"])

    def test_small_account_aggressive_marks_opening_a_plus_target(self) -> None:
        self._use_strategy_settings(small_account_aggressive_enabled=True)
        _base_now, signal, plan, leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=signal,
            trade_plan=plan,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "A+")
        self.assertEqual(policy["status"], "allowed_small_account_a_plus")
        self.assertEqual(policy["target_position_rate"], "0.60")
        self.assertEqual(policy["per_trade_risk_rate"], "0.030")
        self.assertEqual(policy["minimum_quantity_if_affordable"], 1)
        money_flow = policy["money_flow_policy"]
        self.assertEqual(money_flow["version"], "v0.12.82")
        self.assertTrue(money_flow["a_plus_qualified"])
        self.assertTrue(money_flow["money_flow_top"])
        self.assertTrue(money_flow["realtime_rank_top"])
        self.assertTrue(money_flow["vwap_hold"])
        self.assertTrue(money_flow["new_high"])
        self.assertTrue(money_flow["normal_stock"])
        self.assertFalse(money_flow["supply_missing"])
        self.assertFalse(money_flow["confirmed_net_sell"])
        self.assertEqual(
            policy["small_account_aggressive"]["profile"],
            "small_account_aggressive_1m",
        )

    def test_strategy_playbook_marks_a_plus_trend_day_runner(self) -> None:
        self._use_strategy_settings(small_account_aggressive_enabled=True)
        _base_now, signal, plan, leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )
        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=signal,
            trade_plan=plan,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        playbook = self.strategy._strategy_playbook_policy(
            signal=signal,
            trade_plan=plan,
            entry_guard_policy=policy,
            reentry_policy={"status": "allowed"},
        )

        self.assertFalse(blocked, message)
        self.assertEqual(playbook["name"], "a_plus_trend_day_runner")
        self.assertEqual(playbook["priority"], 1)
        self.assertEqual(playbook["fast_partial_profit_rate"], "0.02")
        self.assertEqual(playbook["residual_exit_rule"], "structure_break_runner")
        self.assertFalse(playbook["percent_exit_as_primary"])

    def test_staged_position_build_advances_only_on_price_confirmation(self) -> None:
        _base_now, signal, _plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        playbook = {"name": "a_plus_trend_day_runner"}
        guard = {"target_position_rate": "0.60"}

        scout = self.strategy._position_build_policy(
            signal=signal,
            strategy_playbook=playbook,
            entry_guard_policy=guard,
            has_position=False,
            position_value=0,
            total_equity=1_000_000,
        )
        confirmation = self.strategy._position_build_policy(
            signal=replace(
                signal,
                breakout_hold_pass=True,
                volume_pass=True,
                intraday_high=104,
                higher_low_pass=False,
            ),
            strategy_playbook=playbook,
            entry_guard_policy=guard,
            has_position=True,
            position_value=250_000,
            total_equity=1_000_000,
        )
        trend = self.strategy._position_build_policy(
            signal=replace(
                signal,
                higher_low_pass=True,
                vwap_pass=True,
                moving_average_pass=True,
            ),
            strategy_playbook=playbook,
            entry_guard_policy=guard,
            has_position=True,
            position_value=450_000,
            total_equity=1_000_000,
        )
        premature_trend = self.strategy._position_build_policy(
            signal=replace(
                signal,
                higher_low_pass=True,
                vwap_pass=True,
                moving_average_pass=True,
            ),
            strategy_playbook=playbook,
            entry_guard_policy=guard,
            has_position=True,
            position_value=250_000,
            total_equity=1_000_000,
        )
        waiting = self.strategy._position_build_policy(
            signal=replace(
                signal,
                breakout_hold_pass=False,
                opening_range_pass=False,
                intraday_high=110,
                higher_low_pass=False,
            ),
            strategy_playbook=playbook,
            entry_guard_policy=guard,
            has_position=True,
            position_value=250_000,
            total_equity=1_000_000,
        )

        self.assertEqual(scout["stage"], "scout")
        self.assertEqual(scout["target_position_rate"], "0.25")
        self.assertEqual(confirmation["stage"], "breakout_confirmed")
        self.assertEqual(confirmation["target_position_rate"], "0.45")
        self.assertEqual(premature_trend["stage"], "breakout_confirmed")
        self.assertEqual(premature_trend["target_position_rate"], "0.45")
        self.assertEqual(trend["stage"], "trend_confirmed")
        self.assertEqual(trend["target_position_rate"], "0.60")
        self.assertEqual(waiting["order_effect"], "shadow_only")

    def test_rebreakout_position_build_stays_reduced_probe(self) -> None:
        _base_now, signal, _plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        policy = self.strategy._position_build_policy(
            signal=signal,
            strategy_playbook={"name": "new_high_rebreakout_reentry_probe"},
            entry_guard_policy={"target_position_rate": "0.60"},
            has_position=False,
            position_value=0,
            total_equity=1_000_000,
        )

        self.assertEqual(policy["stage"], "reentry_probe")
        self.assertEqual(policy["target_position_rate"], "0.20")

    def test_structural_stop_inside_normal_noise_is_shadow_only(self) -> None:
        _base_now, signal, plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        noisy_signal = replace(
            signal,
            minute_atr=Decimal("10"),
            recent_candle_range=Decimal("8"),
        )
        stop_policy = self.strategy._structural_stop_policy(
            signal=noisy_signal,
            stop_loss_price=104,
            stop_loss_reason="차트 무효화 가격 이탈",
        )
        plan = replace(
            plan,
            setup={**plan.setup, "structural_stop_policy": stop_policy},
        )
        router = self.strategy._router_execution_policy(
            trade_plan=plan,
            strategy_playbook={"name": "a_plus_trend_day_runner"},
        )

        self.assertFalse(stop_policy["valid"])
        self.assertEqual(stop_policy["status"], "invalid_inside_normal_noise")
        self.assertEqual(router["status"], "shadow_only_invalid_structural_stop")
        self.assertEqual(router["order_effect"], "shadow_only")

    def test_strategy_playbook_marks_vwap_pullback_reentry(self) -> None:
        _base_now, signal, plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        pullback_signal = replace(
            signal,
            vwap_pass=True,
            pullback_pass=True,
            pullback_support_pass=True,
        )
        pullback_plan = replace(
            plan,
            entry_type="vwap_pullback",
            setup={**plan.setup, "residual_exit_rule": "hold_to_second_target"},
        )

        playbook = self.strategy._strategy_playbook_policy(
            signal=pullback_signal,
            trade_plan=pullback_plan,
            entry_guard_policy={"entry_grade": "A"},
            reentry_policy={"status": "allowed"},
        )

        self.assertEqual(playbook["name"], "vwap_pullback_reentry")
        self.assertEqual(playbook["priority"], 2)
        self.assertFalse(playbook["chase_after_spike"])

    def test_strategy_playbook_marks_new_high_rebreakout_reentry_probe(self) -> None:
        _base_now, signal, plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )

        playbook = self.strategy._strategy_playbook_policy(
            signal=signal,
            trade_plan=plan,
            entry_guard_policy={"entry_grade": "A"},
            reentry_policy={
                "status": "allowed_after_partial_exit_reassessment",
                "new_high_breakout": True,
                "vwap_reanchored": True,
                "volume_recovered": True,
                "quantity_multiplier": "0.25",
                "previous_exit_was_profit_protect": True,
            },
        )

        self.assertEqual(playbook["name"], "new_high_rebreakout_reentry_probe")
        self.assertEqual(playbook["priority"], 3)
        self.assertEqual(playbook["reentry_quantity_multiplier"], "0.25")

    def test_market_regime_selects_core_playbook_and_reacceleration(self) -> None:
        signal = self.strategy.evaluate("A005930")
        guard = {
            "entry_grade": "A+",
            "money_flow_policy": {
                "components": {"theme_score": "75"},
                "confirmed_net_sell": False,
            },
            "data_availability": {"status": "available"},
            "supply_block_policy": {"status": "clear"},
        }

        def policy_for(mode: str, playbook: str) -> dict:
            strategy = BreakoutStrategy(
                self.database,
                self.paper,
                self.strategy_settings,
                self.paper_settings,
                self.risk_settings,
                order_executor=PaperOrderExecutor(self.paper, self.notifier),
                market_mode_settings=MarketModeGateSettings(
                    enabled=True,
                    manual_mode=mode,
                    order_effect="paper_apply",
                ),
            )
            confidence = strategy._data_confidence_policy(
                signal=signal,
                entry_guard_policy=guard,
            )
            return strategy._market_regime_playbook_policy(
                strategy_playbook={"name": playbook},
                entry_guard_policy=guard,
                data_confidence_policy=confidence,
            )

        trend = policy_for("risk_on", "a_plus_trend_day_runner")
        pullback = policy_for("neutral", "vwap_pullback_reentry")
        reacceleration = policy_for(
            "neutral",
            "new_high_rebreakout_reentry_probe",
        )
        mismatch = policy_for("risk_on", "vwap_pullback_reentry")
        risk_off = policy_for("risk_off", "a_plus_trend_day_runner")

        self.assertEqual(trend["status"], "allowed_risk_on_preferred_playbook")
        self.assertEqual(pullback["status"], "allowed_neutral_preferred_playbook")
        self.assertEqual(
            reacceleration["status"],
            "allowed_strong_stock_reacceleration",
        )
        self.assertEqual(mismatch["order_effect"], "shadow_only")
        self.assertEqual(risk_off["order_effect"], "allow_reduced")
        self.assertEqual(risk_off["quantity_multiplier"], "0.20")
        self.assertEqual(risk_off["max_quantity"], 1)

    def test_risk_off_a_plus_core_executes_only_one_share_reduced_probe(self) -> None:
        _base_now, signal, _plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        signal = replace(signal, signal_time="2026-06-10T09:11:00")
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            self.strategy_settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
            market_mode_settings=MarketModeGateSettings(
                enabled=True,
                manual_mode="risk_off",
                order_effect="paper_apply",
            ),
        )
        strategy.evaluate = lambda _stock_code: signal

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )[0]

        self.assertEqual(result["action"], "executed")
        self.assertEqual(result["trade_plan"].planned_quantity, 1)
        regime = result["trade_plan"].setup["market_regime_playbook_policy"]
        self.assertEqual(regime["status"], "allowed_risk_off_reduced_probe")
        self.assertEqual(regime["order_effect"], "allow_reduced")
        self.assertEqual(self.paper.list_orders("default")[0]["quantity"], 1)

    def test_data_confidence_combines_supply_news_theme_and_availability(self) -> None:
        signal = self.strategy.evaluate("A005930")
        information_risk = self.strategy._data_confidence_policy(
            signal=replace(
                signal,
                information_blocked=True,
                information_score=Decimal("-20"),
            ),
            entry_guard_policy={
                "money_flow_policy": {
                    "components": {"theme_score": "75"},
                    "confirmed_net_sell": False,
                },
                "data_availability": {"status": "available"},
                "supply_block_policy": {"status": "clear"},
            },
        )
        poor = self.strategy._data_confidence_policy(
            signal=replace(
                signal,
                information_blocked=True,
                supply={
                    **(signal.supply or {}),
                    "blocked": True,
                    "blocked_reason": "confirmed net selling",
                },
            ),
            entry_guard_policy={
                "money_flow_policy": {
                    "components": {"theme_score": None},
                    "confirmed_net_sell": True,
                },
                "data_availability": {
                    "status": "blocked_data_gap",
                    "blocked": True,
                },
                "supply_block_policy": {
                    "status": "blocked_supply",
                    "confirmed_net_sell": True,
                },
            },
        )

        self.assertFalse(information_risk["individual_hard_blocks"])
        self.assertEqual(information_risk["band"], "medium")
        self.assertEqual(information_risk["order_effect"], "reduce_quantity")
        self.assertEqual(poor["band"], "poor")
        self.assertEqual(poor["order_effect"], "shadow_only")
        self.assertEqual(
            {item["source"] for item in poor["deductions"]},
            {"supply", "information", "theme", "availability"},
        )

    def test_small_account_aggressive_requires_money_flow_for_a_plus(self) -> None:
        self._use_strategy_settings(small_account_aggressive_enabled=True)
        _base_now, signal, plan, leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )
        signal = replace(signal, projected_trade_amount=1_000_000_000)
        leader_score = {
            **leader_score,
            "total_score": 95.0,
            "trade_amount_score": 20.0,
            "rank_score": 20.0,
        }

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=signal,
            trade_plan=plan,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "A")
        self.assertEqual(policy["status"], "allowed_small_account_a")
        self.assertFalse(
            policy["small_account_aggressive"]["a_plus_conditions"][
                "money_flow_top"
            ]
        )
        self.assertFalse(policy["money_flow_policy"]["a_plus_qualified"])

    def test_money_flow_policy_requires_common_stock_and_clear_supply(self) -> None:
        _base_now, signal, plan, leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        now = datetime(
            2026,
            6,
            10,
            9,
            11,
            tzinfo=timezone(timedelta(hours=9)),
        )
        grade_details = {
            "grade": "A",
            "daily_breakout_ok": True,
            "trade_amount_top": True,
            "market_rank_top": True,
            "chart_score": "86",
        }

        special_policy = self.strategy._money_flow_policy(
            signal=replace(signal, stock_code="A0039P0"),
            trade_plan=plan,
            grade_details=grade_details,
            leader_score={**leader_score, "stock_name": "삼성전자우"},
            market_rank_proxy={"usable": True},
            now=now,
        )

        self.assertFalse(special_policy["normal_stock"])
        self.assertFalse(special_policy["a_plus_qualified"])
        self.assertEqual(
            special_policy["stock_category"]["key"],
            "preferred_or_special",
        )

        blocked_supply = {
            "total_score": 12.0,
            "blocked": True,
            "blocked_reason": "프로그램 당일 매도 집중",
            "components": {
                "program": {
                    "hard_block": True,
                    "consecutive_net_sell_count": 3,
                }
            },
        }
        supply_policy = self.strategy._money_flow_policy(
            signal=replace(signal, supply=blocked_supply),
            trade_plan=plan,
            grade_details=grade_details,
            leader_score=leader_score,
            market_rank_proxy={"usable": True},
            now=now,
        )

        self.assertTrue(supply_policy["supply_blocked"])
        self.assertTrue(supply_policy["confirmed_net_sell"])
        self.assertFalse(supply_policy["a_plus_qualified"])

    def test_b_grade_strong_money_flow_becomes_one_share_probe(self) -> None:
        self._use_strategy_settings(small_account_aggressive_enabled=True)
        _base_now, signal, plan, leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )
        probe_plan = replace(plan, entry_type="breakout_probe")
        proxy = {
            "usable": True,
            "trade_amount": {"usable": True, "rank": 5},
            "realtime": {"usable": True, "rank": 8},
        }

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=replace(
                signal,
                projected_volume_ratio=Decimal("3.5"),
                volume_ratio=Decimal("3.5"),
            ),
            trade_plan=probe_plan,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy=proxy,
            decision_score=Decimal("82"),
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "B")
        self.assertEqual(policy["status"], "allowed_small_account_b_money_flow_probe")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertTrue(policy["money_flow_policy"]["strong_probe_qualified"])

        playbook = self.strategy._strategy_playbook_policy(
            signal=signal,
            trade_plan=probe_plan,
            entry_guard_policy=policy,
            reentry_policy={"status": "allowed"},
        )
        legacy = self.strategy._legacy_entry_policy(
            signal=signal,
            trade_plan=probe_plan,
            entry_guard_policy=policy,
            strategy_playbook=playbook,
        )
        router = self.strategy._router_execution_policy(
            trade_plan=probe_plan,
            strategy_playbook=playbook,
            legacy_entry_policy=legacy,
        )

        self.assertEqual(playbook["name"], "money_flow_strong_probe")
        self.assertFalse(legacy["legacy_candidate"])
        self.assertEqual(
            router["status"],
            "shadow_only_unqualified_money_flow_probe",
        )
        self.assertEqual(router["order_effect"], "shadow_only")

    def test_a_grade_leader_money_flow_probe_can_order_one_share(self) -> None:
        _base_now, signal, plan, leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        now = datetime(
            2026,
            6,
            10,
            10,
            0,
            tzinfo=timezone(timedelta(hours=9)),
        )
        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=signal,
            trade_plan=plan,
            now=now,
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=Decimal("82"),
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "A")
        playbook = self.strategy._strategy_playbook_policy(
            signal=signal,
            trade_plan=plan,
            entry_guard_policy=policy,
            reentry_policy={"status": "allowed"},
        )
        router = self.strategy._router_execution_policy(
            trade_plan=plan,
            strategy_playbook=playbook,
            entry_candle_quality_policy={"blocked": False},
        )

        self.assertEqual(playbook["name"], "money_flow_strong_probe")
        self.assertTrue(playbook["real_order_qualified"])
        self.assertEqual(
            router["status"],
            "allowed_a_leader_money_flow_minimum_probe",
        )
        self.assertEqual(router["max_quantity"], 1)

    def test_a_plus_candidate_stays_armed_until_new_high_trigger(self) -> None:
        _base_now, signal, plan, leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )
        waiting_signal = replace(
            signal,
            previous_high=110,
            intraday_high=108,
            breakout_pass=False,
            breakout_hold_pass=False,
            opening_range_pass=False,
            pullback_pass=False,
        )
        arm_time = datetime(
            2026,
            6,
            10,
            9,
            10,
            tzinfo=timezone(timedelta(hours=9)),
        )
        grade = self.strategy._entry_grade(
            signal=waiting_signal,
            trade_plan=plan,
            leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )
        money_flow = self.strategy._money_flow_policy(
            signal=waiting_signal,
            trade_plan=plan,
            grade_details=grade,
            leader_score=leader_score,
            market_rank_proxy={"usable": False},
            now=arm_time.replace(tzinfo=None),
        )
        armed = self.strategy._a_plus_armed_policy(
            account_id=self._account_id(),
            signal=waiting_signal,
            trade_plan=plan,
            grade_details=grade,
            money_flow_policy=money_flow,
            leader_score=leader_score,
            now=arm_time,
        )

        self.assertTrue(armed["active"])
        self.assertFalse(armed["trigger_ready"])
        self.assertEqual(armed["status"], "newly_armed")

        trigger_signal = replace(
            waiting_signal,
            previous_high=100,
            intraday_high=104,
            breakout_pass=True,
            breakout_hold_pass=True,
        )
        trigger_time = arm_time + timedelta(minutes=15)
        trigger_money_flow = self.strategy._money_flow_policy(
            signal=trigger_signal,
            trade_plan=plan,
            grade_details=grade,
            leader_score=leader_score,
            market_rank_proxy={"usable": False},
            now=trigger_time.replace(tzinfo=None),
        )
        triggered = self.strategy._a_plus_armed_policy(
            account_id=self._account_id(),
            signal=trigger_signal,
            trade_plan=plan,
            grade_details=grade,
            money_flow_policy=trigger_money_flow,
            leader_score=leader_score,
            now=trigger_time,
        )

        self.assertTrue(triggered["active"])
        self.assertTrue(triggered["trigger_ready"])
        self.assertEqual(triggered["trigger_type"], "new_high_rebreakout")
        row = self.database.armed_candidate(
            account_id=self._account_id(),
            stock_code="A005930",
            trading_day="2026-06-10",
        )
        self.assertEqual(row["status"], "armed")
        self.assertEqual(row["trigger_type"], "new_high_rebreakout")

    def test_strong_money_flow_data_gap_allows_minimum_probe(self) -> None:
        self._use_strategy_settings(small_account_aggressive_enabled=True)
        _base_now, signal, plan, _leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )
        signal = replace(
            signal,
            supply=None,
            projected_volume_ratio=Decimal("3.5"),
            volume_ratio=Decimal("3.5"),
        )
        proxy = {
            "usable": True,
            "trade_amount": {"usable": True, "rank": 3},
            "realtime": {"usable": True, "rank": 7},
        }

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=signal,
            trade_plan=plan,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            has_position=False,
            leader_score=None,
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(
            policy["data_availability"]["status"],
            "minimum_probe_money_flow_data_gap",
        )
        self.assertEqual(policy["quantity_multiplier"], "0.20")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertTrue(policy["money_flow_policy"]["supply_missing"])
        self.assertTrue(policy["money_flow_policy"]["strong_probe_qualified"])

        playbook = self.strategy._strategy_playbook_policy(
            signal=signal,
            trade_plan=plan,
            entry_guard_policy=policy,
            reentry_policy={"status": "allowed"},
        )
        self.assertEqual(playbook["name"], "money_flow_strong_probe")

    def test_small_account_aggressive_blocks_supply_gap_probe(self) -> None:
        self._use_strategy_settings(small_account_aggressive_enabled=True)
        _base_now, signal, plan, leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )
        signal = replace(signal, supply=None)

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=signal,
            trade_plan=plan,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            has_position=False,
            leader_score=leader_score,
            actual_leader_score=leader_score,
            market_rank_proxy={"usable": False},
            decision_score=decision.score,
        )

        self.assertTrue(blocked)
        self.assertEqual(policy["status"], "blocked_small_account_data_gap")
        self.assertIn("데이터 공백", message)
        self.assertTrue(policy["small_account_aggressive"]["blocked"])

    def test_small_account_a_plus_allows_minimum_probe_when_only_leader_score_missing(
        self,
    ) -> None:
        self._use_strategy_settings(small_account_aggressive_enabled=True)
        _base_now, signal, plan, _leader_score, decision = (
            self._strong_a_grade_signal_plan()
        )
        proxy = {
            "usable": True,
            "source": "trade_amount",
            "rank": 3,
            "trade_amount": {"usable": True, "rank": 3},
        }
        proxy_leader = self.strategy._proxy_leader_score(
            stock_code="A005930",
            market_rank_proxy=proxy,
        )

        blocked, message, policy = self.strategy._entry_guard_policy(
            account_name="default",
            account_id=self._account_id(),
            account_total_equity=1_000_000,
            signal=signal,
            trade_plan=plan,
            now=datetime(
                2026,
                6,
                10,
                9,
                11,
                tzinfo=timezone(timedelta(hours=9)),
            ),
            has_position=False,
            leader_score=proxy_leader,
            actual_leader_score=None,
            market_rank_proxy=proxy,
            decision_score=decision.score,
        )

        self.assertFalse(blocked, message)
        self.assertEqual(policy["entry_grade"], "A+")
        self.assertEqual(
            policy["status"],
            "allowed_small_account_a_plus_data_gap_probe",
        )
        self.assertEqual(
            policy["data_availability"]["status"],
            "minimum_probe_missing_leader_with_rank_proxy",
        )
        self.assertEqual(policy["quantity_multiplier"], "0.20")
        self.assertEqual(policy["max_quantity"], 1)
        self.assertTrue(policy["small_account_aggressive"]["data_gap_probe"])

    def test_session_profile_reduces_midday_position_size(self) -> None:
        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(
                2026,
                6,
                10,
                10,
                40,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )[0]

        self.assertEqual(result["session_profile"].name, "midday_conservative")
        self._assert_shadow_only_router(result)
        self.assertEqual(result["trade_plan"].planned_quantity, 476)
        self.assertEqual(
            result["trade_plan"].setup["session_quantity_multiplier"],
            "0.50",
        )
        self.assertEqual(result["trade_plan"].setup["strategy_mode"], "verified_leader")
        self.assertTrue(result["trade_plan"].setup["leader_mode_pass"])
        self.assertTrue(result["trade_plan"].setup["supply_mode_pass"])
        self.assertTrue(result["trade_plan"].setup["market_mode_pass"])
        self.assertEqual(
            result["trade_plan"].setup["intraday_strategy_mode"]["label"],
            "검증 대장주",
        )
        self.assertLess(result["trade_plan"].planned_quantity, 952)

    def test_market_mode_quantity_multiplier_and_blocking_are_applied_to_strategy_mode(self) -> None:
        now = datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9)))
        leader_score = {"role": "leader", "total_score": 88.0, "blocked": False}
        supply_score = {"total_score": 70.0, "blocked": False}
        cases = [
            ("risk_on", Decimal("1.0"), Decimal("0.500"), True),
            ("neutral", Decimal("0.5"), Decimal("0.250"), True),
            ("risk_off", Decimal("0.2"), Decimal("0.100"), False),
        ]
        for mode, multiplier, expected_quantity, expected_allowed in cases:
            with self.subTest(mode=mode):
                strategy = BreakoutStrategy(
                    self.database,
                    self.paper,
                    self.strategy_settings,
                    self.paper_settings,
                    self.risk_settings,
                    order_executor=PaperOrderExecutor(self.paper, self.notifier),
                    market_mode_settings=MarketModeGateSettings(
                        enabled=True,
                        manual_mode=mode,
                        source="auto_kosdaq",
                        requested_mode="auto",
                        order_effect="paper_apply",
                        quantity_multiplier=multiplier,
                    ),
                )
                session_profile = strategy._session_profile(now)

                mode_result = strategy._strategy_mode(
                    now,
                    session_profile,
                    leader_score=leader_score,
                    supply_score=supply_score,
                )

                self.assertEqual(
                    mode_result.quantity_multiplier,
                    expected_quantity,
                )
                self.assertEqual(
                    mode_result.details["market_mode_quantity_multiplier"],
                    str(multiplier),
                )
                self.assertEqual(mode_result.entry_allowed, expected_allowed)
                self.assertEqual(mode_result.market_mode_pass, expected_allowed)

    def test_intraday_strategy_mode_blocks_non_leader_when_leader_data_exists(self) -> None:
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": "2026-06-10",
                    "stock_code": "A005930",
                    "stock_name": "?쇱꽦?꾩옄",
                    "theme_code": "101",
                    "theme_name": "semiconductor",
                    "role": "follower",
                    "total_score": 55.0,
                    "rank_score": 10.0,
                    "trade_amount_score": 35.0,
                    "theme_score": 5.0,
                    "persistence_score": 0.0,
                    "chart_score": 5.0,
                    "risk_score": 0.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["test"],
                    "components": {},
                }
            ]
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(
                2026,
                6,
                10,
                10,
                40,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )[0]

        self.assertEqual(result["strategy_mode"].name, "verified_leader")
        self.assertFalse(result["strategy_mode"].leader_pass)
        self.assertEqual(result["action"], "skipped")
        self.assertTrue(result["message"])
        self.assertEqual(len(self.paper.list_orders("default")), 0)

    def test_session_profile_can_be_disabled(self) -> None:
        settings = StrategySettings(
            volume_multiple=Decimal("2"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=20,
            minute_interval=5,
            session_profile_enabled=False,
        )
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
        )

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(
                2026,
                6,
                10,
                9,
                5,
                tzinfo=timezone(timedelta(hours=9)),
            ),
        )[0]

        self.assertEqual(result["session_profile"].name, "all_day_active")
        self._assert_shadow_only_router(result)
        self.assertGreater(result["trade_plan"].planned_quantity, 0)

    def test_strategy_does_not_resubmit_existing_failed_signal(self) -> None:
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = 'default'"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO strategy_signals(
                    account_id, stock_code, signal_time, current_price,
                    volume_pass, breakout_pass, moving_average_pass,
                    chase_pass, signal_pass, action, message, created_at
                )
                VALUES (?, 'A005930', '2026-06-10T10:40:00', 105,
                        1, 1, 1, 1, 1, 'failed',
                        '湲곗〈 二쇰Ц ?붿껌 ?ㅽ뙣', '2026-06-10T10:40:01')
                """,
                (account["id"],),
            )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 41, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertEqual(result["action"], "failed")
        self.assertTrue(result["message"])
        self.assertEqual(len(self.paper.list_orders("default")), 0)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM order_requests").fetchone()[0],
                0,
            )

    def test_information_block_reduces_confidence_without_hard_block(self) -> None:
        self._save_candidate_score(
            information_blocked=True,
            information_score=-20,
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)

        self.assertTrue(signal.passed)
        self.assertTrue(signal.information_blocked)
        self.assertEqual(decision.action, DecisionAction.BUY)
        self.assertTrue(decision.reason)

    def test_pattern_score_is_connected_to_breakout_decision(self) -> None:
        neutral = self.strategy.decide(self.strategy.evaluate("A005930"))
        self._save_candidate_score(
            pattern_score=30,
            patterns=("cup_handle", "double_bottom"),
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)

        self.assertEqual(signal.patterns, ("cup_handle", "double_bottom"))
        self.assertGreater(decision.score, neutral.score)
        self.assertIn(
            "cup_handle",
            next(c.reason for c in decision.components if c.name == "pattern"),
        )

    def test_stale_candidate_score_is_not_used_for_order_decision(self) -> None:
        self._save_candidate_score(
            information_blocked=True,
            information_score=-20,
            pattern_score=30,
            patterns=("cup_handle",),
            evaluated_at="2026-06-10T08:00:00",
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)

        self.assertTrue(signal.passed)
        self.assertFalse(signal.information_blocked)
        self.assertEqual(signal.patterns, ())
        self.assertEqual(decision.action, DecisionAction.BUY)

    def test_missing_price_does_not_stop_remaining_strategy_run(self) -> None:
        seoul = timezone(timedelta(hours=9))
        results = self.strategy.run(
            "default",
            ["A000660", "A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=seoul),
        )

        self.assertEqual(results[0]["action"], "failed")
        self.assertTrue(results[0]["message"])
        self._assert_shadow_only_router(results[1])

    def test_existing_position_can_be_added_up_to_score_target_rate(self) -> None:
        risk_settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=0,
            max_position_rate=Decimal("0.25"),
            max_positions=10,
            max_entry_gain_rate=Decimal("0.15"),
        )
        paper = PaperTradingEngine(
            self.database,
            self.paper_settings,
            risk_settings,
        )
        strategy = BreakoutStrategy(
            self.database,
            paper,
            self.strategy_settings,
            self.paper_settings,
            risk_settings,
        )
        paper.place_market_order(
            "default",
            "A005930",
            "buy",
            1,
            client_order_id="manual-seed-position",
        )
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 105},
            current_price=105,
            open_price=96,
            high_price=105,
            low_price=95,
            volume=130_000_000,
            collected_at=datetime(2026, 6, 10, 10, 45),
        )

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 45, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self._assert_shadow_only_router(result)
        self.assertTrue(result["message"])
        self.assertEqual(len(paper.list_orders("default")), 1)

    def test_score_position_rate_scales_up_to_twenty_five_percent(self) -> None:
        self.assertEqual(
            BreakoutStrategy._score_position_rate(Decimal("50")),
            Decimal("0.10"),
        )
        self.assertEqual(
            BreakoutStrategy._score_position_rate(Decimal("60")),
            Decimal("0.15"),
        )
        self.assertEqual(
            BreakoutStrategy._score_position_rate(Decimal("70")),
            Decimal("0.20"),
        )
        self.assertEqual(
            BreakoutStrategy._score_position_rate(Decimal("80")),
            Decimal("0.25"),
        )

    def test_quantity_is_capped_by_available_cash(self) -> None:
        quantity = self.strategy._calculate_quantity(
            total_equity=1_000_000,
            cash_balance=1_000,
            market_price=105,
            score=Decimal("80"),
        )

        self.assertEqual(quantity, 9)

    def test_quantity_is_capped_by_per_trade_risk(self) -> None:
        quantity = self.strategy._calculate_quantity(
            total_equity=1_000_000,
            cash_balance=1_000_000,
            market_price=1_000,
            score=Decimal("80"),
            stop_loss_price=800,
        )

        self.assertEqual(quantity, 25)

    def test_small_account_minimum_quantity_allows_one_affordable_share(self) -> None:
        quantity = self.strategy._calculate_quantity(
            total_equity=1_000_000,
            cash_balance=1_000_000,
            market_price=310_000,
            score=Decimal("77"),
            stop_loss_price=295_000,
            target_position_rate=Decimal("0.08"),
            per_trade_risk_rate=Decimal("0.006"),
            minimum_quantity_if_affordable=1,
        )

        self.assertEqual(quantity, 1)

    def test_small_account_minimum_quantity_respects_cash_and_daily_loss(self) -> None:
        no_cash = self.strategy._calculate_quantity(
            total_equity=1_000_000,
            cash_balance=1_000_000,
            market_price=1_520_000,
            score=Decimal("85"),
            stop_loss_price=1_450_000,
            target_position_rate=Decimal("0.30"),
            per_trade_risk_rate=Decimal("0.018"),
            minimum_quantity_if_affordable=1,
        )
        too_much_stop_risk = self.strategy._calculate_quantity(
            total_equity=1_000_000,
            cash_balance=1_000_000,
            market_price=900_000,
            score=Decimal("85"),
            stop_loss_price=750_000,
            target_position_rate=Decimal("0.30"),
            per_trade_risk_rate=Decimal("0.018"),
            minimum_quantity_if_affordable=1,
        )

        self.assertEqual(no_cash, 0)
        self.assertEqual(too_much_stop_risk, 0)

    def test_minimum_buy_diagnostics_identifies_verified_high_price_stock(self) -> None:
        self.database.save_price_snapshot(
            "test",
            "A402340",
            {"price": 1_594_000},
            current_price=1_594_000,
            open_price=1_500_000,
            high_price=1_603_000,
            low_price=1_407_000,
            volume=100,
            collected_at=datetime(2026, 7, 3, 10, 0),
        )
        self.database.save_candles(
            "test",
            "A402340",
            {"minute": True},
            [
                {
                    "candle_time": "2026-07-03T10:00:00",
                    "open_price": 1_500_000,
                    "high_price": 1_603_000,
                    "low_price": 1_407_000,
                    "close_price": 1_589_000,
                    "volume": 100,
                }
            ],
            candle_type="minute",
            interval_minutes=5,
        )

        diagnostics = self.strategy._minimum_buy_diagnostics(
            account={"cash_balance": 1_000_000, "total_equity": 1_000_000},
            signal=SimpleNamespace(stock_code="A402340", current_price=1_594_000),
            trade_plan=SimpleNamespace(stop_loss_price=1_570_000),
        )

        self.assertEqual(diagnostics["reason"], "verified_high_price_unaffordable")
        self.assertLessEqual(diagnostics["price_reference_ratio"], 1.05)

    def test_minimum_buy_diagnostics_flags_price_scale_mismatch(self) -> None:
        self.database.save_price_snapshot(
            "test",
            "A402340",
            {"price": 1_594_000},
            current_price=1_594_000,
            open_price=1_500_000,
            high_price=1_603_000,
            low_price=1_407_000,
            volume=100,
            collected_at=datetime(2026, 7, 3, 10, 0),
        )
        self.database.save_candles(
            "test",
            "A402340",
            {"minute": True},
            [
                {
                    "candle_time": "2026-07-03T10:00:00",
                    "open_price": 159_000,
                    "high_price": 160_300,
                    "low_price": 140_700,
                    "close_price": 159_400,
                    "volume": 100,
                }
            ],
            candle_type="minute",
            interval_minutes=5,
        )

        diagnostics = self.strategy._minimum_buy_diagnostics(
            account={"cash_balance": 1_000_000, "total_equity": 1_000_000},
            signal=SimpleNamespace(stock_code="A402340", current_price=1_594_000),
            trade_plan=SimpleNamespace(stop_loss_price=1_590_000),
        )

        self.assertEqual(diagnostics["reason"], "price_scale_mismatch")
        self.assertGreaterEqual(diagnostics["price_reference_ratio"], 5)

    def test_trade_plan_fallback_stop_uses_strategy_rate(self) -> None:
        stop_price, reason = self.strategy._fallback_stop(10_000)

        self.assertEqual(stop_price, 9_650)
        self.assertTrue(reason)

    def test_projected_volume_can_pass_before_full_day_volume(self) -> None:
        settings = StrategySettings(
            volume_multiple=Decimal("1.3"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=20,
            minute_interval=5,
            minimum_projected_trade_amount=0,
        )
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            settings,
            self.paper_settings,
            self.risk_settings,
        )
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 105},
            current_price=105,
            open_price=96,
            high_price=105,
            low_price=95,
            volume=20_000_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        signal = strategy.evaluate("A005930")

        self.assertEqual(signal.volume_ratio, Decimal("0.4"))
        self.assertGreater(signal.projected_volume_ratio, Decimal("1.3"))
        self.assertTrue(signal.volume_pass)
        self.assertTrue(signal.passed)

    def test_paper_probe_executes_when_base_volume_filter_fails(self) -> None:
        settings = StrategySettings(
            volume_multiple=Decimal("20"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=20,
            minute_interval=5,
            paper_probe_enabled=True,
            paper_probe_min_score=Decimal("60"),
        )
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
        )

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertFalse(result["signal"].passed)
        self.assertEqual(result["decision"].action, DecisionAction.BUY)
        self._assert_shadow_only_router(result)
        self.assertTrue(result["message"])
        self.assertEqual(result["trade_plan"].entry_type, "breakout_probe")
        self.assertEqual(len(self.paper.list_orders("default")), 0)

    def test_paper_probe_executes_on_pullback_support(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE candles
                SET high_price = 110
                WHERE stock_code = 'A005930'
                  AND candle_type = 'daily'
                """
            )
            connection.execute(
                """
                UPDATE candles
                SET high_price = 107
                WHERE stock_code = 'A005930'
                  AND candle_type = 'minute'
                  AND candle_time = '2026-06-10T10:30:00'
                """
            )
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 104},
            current_price=104,
            open_price=96,
            high_price=107,
            low_price=95,
            volume=130_000_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertFalse(result["signal"].breakout_pass)
        self.assertTrue(result["signal"].pullback_pass)
        self.assertTrue(result["signal"].pullback_support_pass)
        self.assertFalse(result["signal"].deep_pullback_blocked)
        self.assertIsNotNone(result["signal"].intraday_midline)
        self.assertFalse(result["signal"].passed)
        self.assertEqual(result["decision"].action, DecisionAction.BUY)
        self.assertEqual(result["action"], "executed")
        self.assertTrue(result["message"])
        self.assertEqual(result["trade_plan"].entry_type, "vwap_pullback")
        self.assertIn("support_recovery", result["trade_plan"].entry_tags)
        self.assertIn("midline_support", result["trade_plan"].entry_tags)
        self.assertEqual(result["trade_plan"].first_take_profit_price, 107)
        self.assertTrue(result["trade_plan"].setup["pullback_support_pass"])
        self.assertIsNotNone(result["trade_plan"].setup["intraday_midline"])
        self.assertIsNotNone(result["trade_plan"].setup["pullback_support_level"])

    def test_breakout_without_hold_buffer_does_not_pass(self) -> None:
        settings = StrategySettings(
            volume_multiple=Decimal("1.3"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=20,
            minute_interval=5,
            intraday_breakout_hold_buffer_rate=Decimal("0.02"),
        )
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            settings,
            self.paper_settings,
            self.risk_settings,
        )
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 101},
            current_price=101,
            open_price=96,
            high_price=101,
            low_price=95,
            volume=130_000_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        signal = strategy.evaluate("A005930")
        decision = strategy.decide(signal)
        breakout_component = next(
            component for component in decision.components
            if component.name == "breakout"
        )

        self.assertTrue(signal.breakout_pass)
        self.assertFalse(signal.breakout_hold_pass)
        self.assertFalse(signal.passed)
        self.assertEqual(decision.action, DecisionAction.HOLD)
        self.assertEqual(breakout_component.score, Decimal("60"))
        self.assertTrue(breakout_component.reason)

    def test_bad_breakout_candle_quality_is_saved_in_trade_plan(self) -> None:
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 105, "quality": "upper_wick"},
            current_price=105,
            open_price=96,
            high_price=120,
            low_price=95,
            volume=130_000_000,
            collected_at=datetime(2026, 6, 10, 10, 41),
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 41, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertIsNotNone(result["trade_plan"])
        quality = result["trade_plan"].setup["entry_candle_quality_policy"]
        self.assertTrue(quality["applies_to_entry"])
        self.assertTrue(quality["blocked"])
        self.assertEqual(quality["status"], "blocked_bad_entry_candle_quality")
        self.assertIn("excessive_upper_wick", quality["block_reasons"])
        self.assertIn("breakout_close_not_near_high", quality["block_reasons"])

    def test_bad_breakout_candle_quality_demotes_core_router_to_shadow(
        self,
    ) -> None:
        _now, signal, plan, _leader_score, _decision = (
            self._strong_a_grade_signal_plan()
        )
        signal = replace(
            signal,
            entry_candle_quality={
                "version": "v0.12.85",
                "evaluated": True,
                "status": "blocked_bad_entry_candle_quality",
                "blocked": True,
                "score": "35",
                "threshold": "60",
                "block_reasons": [
                    "breakout_close_not_near_high",
                    "excessive_upper_wick",
                ],
                "components": [],
            },
        )
        playbook = {"name": "a_plus_trend_day_runner"}
        quality = self.strategy._entry_candle_quality_policy(
            signal=signal,
            trade_plan=plan,
            strategy_playbook=playbook,
        )
        router = self.strategy._router_execution_policy(
            trade_plan=plan,
            strategy_playbook=playbook,
            entry_candle_quality_policy=quality,
        )

        self.assertEqual(router["status"], "shadow_only_bad_entry_candle_quality")
        self.assertEqual(router["order_effect"], "shadow_only")
        self.assertTrue(router["shadow_result_saved"])
        self.assertEqual(router["entry_candle_quality_score"], "35")

    def test_opening_range_breakout_executes_as_paper_probe(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE candles
                SET high_price = 110
                WHERE stock_code = 'A005930'
                  AND candle_type = 'daily'
                """
            )
            connection.execute(
                """
                DELETE FROM candles
                WHERE stock_code = 'A005930'
                  AND candle_type = 'minute'
                """
            )
            connection.execute(
                """
                DELETE FROM price_snapshots
                WHERE stock_code = 'A005930'
                """
            )
        self.database.save_candles(
            "test",
            "A005930",
            {"minute": True},
            [
                {
                    "candle_time": "2026-06-10T09:00:00",
                    "open_price": 96,
                    "high_price": 101,
                    "low_price": 95,
                    "close_price": 100,
                    "volume": 1_000_000,
                },
                {
                    "candle_time": "2026-06-10T09:05:00",
                    "open_price": 100,
                    "high_price": 102,
                    "low_price": 99,
                    "close_price": 101,
                    "volume": 1_200_000,
                },
            ],
            candle_type="minute",
            interval_minutes=5,
        )
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 104},
            current_price=104,
            open_price=96,
            high_price=104,
            low_price=95,
            volume=20_000_000,
            collected_at=datetime(2026, 6, 10, 9, 12),
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 9, 12, tzinfo=timezone(timedelta(hours=9))),
        )[0]
        opening_range = next(
            component for component in result["decision"].components
            if component.name == "opening_range"
        )

        self.assertFalse(result["signal"].passed)
        self.assertTrue(result["signal"].opening_range_pass)
        self.assertEqual(result["signal"].opening_range_high, 102)
        self.assertEqual(result["signal"].opening_range_low, 95)
        self.assertFalse(result["signal"].moving_average_pass)
        self.assertEqual(result["decision"].action, DecisionAction.BUY)
        self.assertEqual(result["action"], "executed")
        self.assertEqual(self.paper.list_orders("default")[0]["quantity"], 1)
        confidence = result["trade_plan"].setup["data_confidence_policy"]
        self.assertEqual(confidence["band"], "medium")
        self.assertEqual(confidence["order_effect"], "reduce_quantity")
        self.assertEqual(result["trade_plan"].entry_type, "opening_range_breakout")
        self.assertIn("opening_range_breakout", result["trade_plan"].entry_tags)
        self.assertEqual(result["trade_plan"].stop_loss_price, 94)
        structural_stop = result["trade_plan"].setup["structural_stop_policy"]
        self.assertTrue(structural_stop["valid"])
        self.assertEqual(structural_stop["status"], "valid_structural_stop")
        self.assertTrue(result["trade_plan"].setup["opening_range_pass"])
        self.assertEqual(result["trade_plan"].setup["opening_range_high"], 102)
        self.assertEqual(opening_range.score, Decimal("100"))
        self.assertTrue(opening_range.reason)

    def test_deep_pullback_is_blocked_before_probe_entry(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE candles
                SET high_price = 120
                WHERE stock_code = 'A005930'
                  AND candle_type = 'daily'
                """
            )
            connection.execute(
                """
                UPDATE candles
                SET high_price = 107
                WHERE stock_code = 'A005930'
                  AND candle_type = 'minute'
                  AND candle_time = '2026-06-10T10:30:00'
                """
            )
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 99},
            current_price=99,
            open_price=96,
            high_price=107,
            low_price=95,
            volume=130_000_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]
        pullback_component = next(
            component for component in result["decision"].components
            if component.name == "pullback"
        )

        self.assertTrue(result["signal"].deep_pullback_blocked)
        self.assertFalse(result["signal"].pullback_pass)
        self.assertEqual(result["decision"].action, DecisionAction.HOLD)
        self.assertEqual(result["action"], "observe")
        self.assertTrue(pullback_component.reason)

    def test_order_flow_component_uses_hoga_and_trade_strength(self) -> None:
        collected_at = datetime(2026, 6, 10, 10, 39)
        self.database.save_order_book(
            "A005930",
            {"hoga": True},
            [],
            quote_time="2026-06-10T10:39:00",
            best_ask_price=106,
            best_ask_quantity=100,
            best_bid_price=105,
            best_bid_quantity=300,
            total_ask_quantity=1_000,
            total_bid_quantity=2_400,
            collected_at=collected_at,
        )
        self.database.save_trade_strength(
            "A005930",
            {"strength": True},
            [
                {
                    "trade_time": "2026-06-10T10:39:00",
                    "current_price": 105,
                    "trade_quantity": 1_000,
                    "accumulated_volume": 130_000_000,
                    "accumulated_amount": 13_650_000_000,
                    "strength": 160.0,
                    "ask_price": 106,
                    "bid_price": 105,
                    "exchange_type": "mock",
                }
            ],
            collected_at=collected_at,
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)
        order_flow = next(c for c in decision.components if c.name == "order_flow")

        self.assertTrue(signal.order_flow_pass)
        self.assertGreater(signal.order_book_bid_ask_ratio, Decimal("2"))
        self.assertEqual(signal.trade_strength, Decimal("160"))
        self.assertEqual(signal.order_flow_score, Decimal("100"))
        self.assertEqual(order_flow.score, Decimal("100"))
        self.assertIn("160.0", order_flow.reason)
        self.assertTrue(order_flow.reason)

    def test_ask_wall_absorption_boosts_order_flow_score(self) -> None:
        self.database.save_order_book(
            "A005930",
            {"hoga": True},
            [],
            quote_time="2026-06-10T10:37:00",
            best_ask_price=106,
            best_ask_quantity=500,
            best_bid_price=105,
            best_bid_quantity=200,
            total_ask_quantity=5_000,
            total_bid_quantity=2_000,
            collected_at=datetime(2026, 6, 10, 10, 37),
        )
        self.database.save_order_book(
            "A005930",
            {"hoga": True},
            [],
            quote_time="2026-06-10T10:39:00",
            best_ask_price=106,
            best_ask_quantity=300,
            best_bid_price=105,
            best_bid_quantity=220,
            total_ask_quantity=3_000,
            total_bid_quantity=2_200,
            collected_at=datetime(2026, 6, 10, 10, 39),
        )
        self.database.save_trade_strength(
            "A005930",
            {"strength": True},
            [
                {
                    "trade_time": "2026-06-10T10:39:00",
                    "current_price": 105,
                    "trade_quantity": 1_000,
                    "accumulated_volume": 130_000_000,
                    "accumulated_amount": 13_650_000_000,
                    "strength": 160.0,
                    "ask_price": 106,
                    "bid_price": 105,
                    "exchange_type": "mock",
                }
            ],
            collected_at=datetime(2026, 6, 10, 10, 39),
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)
        order_flow = next(c for c in decision.components if c.name == "order_flow")

        self.assertTrue(signal.ask_wall_absorption)
        self.assertTrue(signal.order_flow_pass)
        self.assertEqual(signal.ask_quantity_change_rate, Decimal("-0.4"))
        self.assertGreaterEqual(signal.order_flow_score, Decimal("90"))
        self.assertTrue(order_flow.reason)

    def test_bid_wall_risk_suppresses_paper_probe(self) -> None:
        settings = StrategySettings(
            volume_multiple=Decimal("20"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=20,
            minute_interval=5,
            paper_probe_enabled=True,
            paper_probe_min_score=Decimal("50"),
        )
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            settings,
            self.paper_settings,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, self.notifier),
        )
        self.database.save_order_book(
            "A005930",
            {"hoga": True},
            [],
            quote_time="2026-06-10T10:39:00",
            best_ask_price=106,
            best_ask_quantity=100,
            best_bid_price=105,
            best_bid_quantity=300,
            total_ask_quantity=1_000,
            total_bid_quantity=3_000,
            collected_at=datetime(2026, 6, 10, 10, 39),
        )
        self.database.save_trade_strength(
            "A005930",
            {"strength": True},
            [
                {
                    "trade_time": "2026-06-10T10:39:00",
                    "current_price": 105,
                    "trade_quantity": 1_000,
                    "accumulated_volume": 130_000_000,
                    "accumulated_amount": 13_650_000_000,
                    "strength": 90.0,
                    "ask_price": 106,
                    "bid_price": 105,
                    "exchange_type": "mock",
                }
            ],
            collected_at=datetime(2026, 6, 10, 10, 39),
        )

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]
        order_flow = next(
            component for component in result["decision"].components
            if component.name == "order_flow"
        )

        self.assertTrue(result["signal"].bid_wall_risk)
        self.assertFalse(result["signal"].order_flow_pass)
        self.assertEqual(result["decision"].action, DecisionAction.HOLD)
        self.assertEqual(result["action"], "observe")
        self.assertTrue(order_flow.reason)

    def test_paper_probe_can_be_disabled(self) -> None:
        settings = StrategySettings(
            volume_multiple=Decimal("20"),
            max_day_gain_rate=Decimal("0.15"),
            moving_average_period=20,
            minute_interval=5,
            paper_probe_enabled=False,
        )
        strategy = BreakoutStrategy(
            self.database,
            self.paper,
            settings,
            self.paper_settings,
            self.risk_settings,
        )

        result = strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 10, 40, tzinfo=timezone(timedelta(hours=9))),
        )[0]

        self.assertFalse(result["signal"].passed)
        self.assertEqual(result["decision"].action, DecisionAction.HOLD)
        self.assertEqual(result["action"], "observe")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_price_below_vwap_fails_signal(self) -> None:
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 98},
            current_price=98,
            open_price=96,
            high_price=105,
            low_price=95,
            volume=130_000_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        signal = self.strategy.evaluate("A005930")

        self.assertFalse(signal.vwap_pass)
        self.assertFalse(signal.passed)

    def test_low_projected_trade_amount_blocks_entry(self) -> None:
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 105},
            current_price=105,
            open_price=96,
            high_price=105,
            low_price=95,
            volume=27_000_000,
            collected_at=datetime(2026, 6, 10, 10, 40),
        )

        signal = self.strategy.evaluate("A005930")
        decision = self.strategy.decide(signal)

        self.assertTrue(signal.volume_pass)
        self.assertFalse(signal.liquidity_pass)
        self.assertFalse(signal.passed)
        self.assertEqual(decision.action, DecisionAction.HOLD)

    def test_strategy_does_not_enter_after_market_close_cutoff(self) -> None:
        seoul = timezone(timedelta(hours=9))
        result = self.strategy.run(
            "default",
            ["A005930"],
            execute=True,
            now=datetime(2026, 6, 10, 15, 20, tzinfo=seoul),
        )[0]

        self.assertEqual(result["action"], "skipped")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_missing_moving_average_bars_fails_signal(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                DELETE FROM candles
                WHERE candle_type = 'minute'
                  AND id NOT IN (
                      SELECT id FROM candles
                      WHERE candle_type = 'minute'
                      ORDER BY candle_time DESC
                      LIMIT 19
                  )
                """
            )

        signal = self.strategy.evaluate("A005930")

        self.assertFalse(signal.moving_average_pass)
        self.assertFalse(signal.passed)

    def test_stale_current_price_blocks_strategy_evaluation(self) -> None:
        paper = PaperTradingEngine(
            self.database,
            self.paper_settings,
            self.risk_settings,
            max_price_age_seconds=60,
        )
        strategy = BreakoutStrategy(
            self.database,
            paper,
            self.strategy_settings,
            self.paper_settings,
            self.risk_settings,
        )

        with self.assertRaises(PaperTradingError):
            strategy.evaluate("A005930")


if __name__ == "__main__":
    unittest.main()
