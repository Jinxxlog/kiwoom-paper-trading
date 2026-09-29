import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from config.settings import PaperTradingSettings, RiskSettings
from db.database import Database
from domain.models import DecisionAction, StrategyDecision, TradePlan
from execution.paper import PaperOrderExecutor
from paper.engine import PaperTradingEngine, PaperTradingError
from risk.manager import RiskManager


class FakeNotifier:
    def send(self, text: str) -> bool:
        return True


class RiskManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.paper_settings = PaperTradingSettings(
            initial_cash=2_000_000,
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
        )
        self.paper = PaperTradingEngine(
            self.database,
            self.paper_settings,
            self.risk_settings,
        )
        self.risk = RiskManager(
            self.database,
            self.paper,
            self.risk_settings,
            order_executor=PaperOrderExecutor(self.paper, FakeNotifier()),
        )
        self.paper.create_account()
        self._save_price("A005930", 10_000)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save_price(self, stock_code: str, price: int) -> None:
        self.database.save_price_snapshot(
            "test",
            stock_code,
            {"price": price},
            current_price=price,
            open_price=price,
            high_price=price,
            low_price=price,
            volume=100,
        )

    def _save_previous_daily(self, stock_code: str, close_price: int) -> None:
        self.database.save_candles(
            "test",
            stock_code,
            {"daily": True},
            [
                {
                    "candle_time": "2026-06-09T00:00:00",
                    "open_price": close_price,
                    "high_price": close_price,
                    "low_price": close_price,
                    "close_price": close_price,
                    "volume": 100,
                }
            ],
            candle_type="daily",
        )

    def _save_trade_plan(
        self,
        *,
        stock_code: str = "A005930",
        entry_type: str = "vwap_pullback",
        entry_price: int = 10_000,
        stop_loss_price: int = 9_650,
        first_take_profit_price: int = 10_500,
        second_take_profit_price: int = 11_000,
        breakeven_price: int = 10_000,
        strategy_mode: str = "verified_leader",
        setup_overrides: dict | None = None,
    ) -> int:
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code=stock_code,
            decided_at=datetime(2026, 6, 10, 10, 0),
            action=DecisionAction.BUY,
            score=Decimal("80"),
            confidence=Decimal("1"),
            reason="테스트 매수 계획",
        )
        decision_id = self.database.save_strategy_decision(
            decision,
            account_name="default",
            strategy_version_id=self.risk.strategy_version_id,
            strategy_config_id=self.risk.strategy_config_id,
            source_data_time="2026-06-10T10:00:00",
        )
        self.database.save_trade_plan(
            TradePlan(
                decision_id=decision_id,
                stock_code=stock_code,
                entry_type=entry_type,
                entry_tags=(entry_type,),
                entry_price=entry_price,
                planned_quantity=10,
                stop_loss_price=stop_loss_price,
                stop_loss_reason="VWAP 지지 이탈",
                first_take_profit_price=first_take_profit_price,
                first_take_profit_reason="직전 장중 고점에서 1차 반익절",
                partial_exit_rate=Decimal("0.5"),
                breakeven_price=breakeven_price,
                breakeven_trigger_price=first_take_profit_price,
                trailing_rule="1차 익절 후 본절 이동",
                invalidation_reason="VWAP 지지 이탈",
                reward_risk_ratio=Decimal("1.4"),
                setup={
                    "test": True,
                    "second_take_profit_price": second_take_profit_price,
                    "residual_exit_rule": "hold_to_second_target",
                    "strategy_mode": strategy_mode,
                    **(setup_overrides or {}),
                },
            )
        )
        return decision_id

    def _save_intraday_vwap(
        self,
        *,
        stock_code: str = "A005930",
        price: int = 10_900,
        vwap: int = 10_400,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO intraday_pattern_evaluations(
                    stock_code, session_date, interval_minutes,
                    source_data_time, evaluated_at, current_price,
                    vwap, vwap_distance_rate, reclaim_state, reclaim_score,
                    bounce_state, bounce_score, details_json, reasons_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stock_code,
                    "2026-06-10",
                    5,
                    "2026-06-10T10:15:00",
                    "2026-06-10T10:15:00",
                    price,
                    vwap,
                    (price - vwap) / vwap,
                    "confirmed",
                    80,
                    "confirmed",
                    75,
                    "{}",
                    "[]",
                ),
            )

    def _save_leader_score(
        self,
        *,
        stock_code: str = "A005930",
        role: str = "leader",
        total_score: float = 88.0,
        blocked: bool = False,
        blocked_reason: str | None = None,
    ) -> None:
        self.database.save_leader_stock_scores(
            [
                {
                    "scored_at": "2026-06-10T10:15:00",
                    "trade_date": "2026-06-10",
                    "stock_code": stock_code,
                    "stock_name": "삼성전자",
                    "theme_code": "T001",
                    "theme_name": "반도체",
                    "role": role,
                    "total_score": total_score,
                    "rank_score": 90,
                    "trade_amount_score": 85,
                    "theme_score": 80,
                    "persistence_score": 82,
                    "chart_score": 88,
                    "risk_score": 70,
                    "blocked": blocked,
                    "blocked_reason": blocked_reason,
                    "reasons": ["테스트 대장주 유지"],
                    "components": {"supply": {"total_score": 60}},
                }
            ],
            scored_at=datetime(2026, 6, 10, 10, 15),
        )

    def _save_supply_score(
        self,
        *,
        stock_code: str = "A005930",
        total_score: float = 62.0,
        blocked: bool = False,
        blocked_reason: str | None = None,
        components: dict | None = None,
    ) -> None:
        self.database.save_supply_score(
            {
                "stock_code": stock_code,
                "stock_name": "삼성전자",
                "evaluated_at": "2026-06-10T10:15:00",
                "total_score": total_score,
                "broker_score": 12,
                "foreign_broker_score": 15,
                "investor_score": 20,
                "program_score": 0,
                "blocked": blocked,
                "blocked_reason": blocked_reason or ("수급 차단" if blocked else None),
                "reasons": ["테스트 수급 양호"],
                "components": components or {"program": {"status": "not_collected"}},
            }
        )

    def _save_trade_strength(
        self,
        *,
        stock_code: str = "A005930",
        strength: float = 130.0,
    ) -> None:
        self.database.save_trade_strength(
            stock_code,
            {"test": True},
            [
                {
                    "trade_time": "2026-06-10T10:15:00",
                    "current_price": 10_900,
                    "trade_quantity": 100,
                    "accumulated_volume": 10_000,
                    "accumulated_amount": 109_000_000,
                    "strength": strength,
                    "ask_price": 10_910,
                    "bid_price": 10_890,
                    "exchange_type": "KRX",
                }
            ],
            collected_at=datetime(2026, 6, 10, 10, 15),
        )

    def _save_recent_minute_bars(
        self,
        *,
        stock_code: str = "A005930",
        closes: tuple[int, ...],
        volumes: tuple[int, ...],
        opens: tuple[int, ...] | None = None,
        highs: tuple[int, ...] | None = None,
        lows: tuple[int, ...] | None = None,
    ) -> None:
        candles = []
        for index, (close, volume) in enumerate(zip(closes, volumes)):
            open_price = opens[index] if opens is not None else close
            high_price = highs[index] if highs is not None else max(open_price, close) + 10
            low_price = lows[index] if lows is not None else min(open_price, close) - 10
            candles.append(
                {
                    "candle_time": f"2026-06-10T10:{index * 5:02d}:00",
                    "open_price": open_price,
                    "high_price": high_price,
                    "low_price": low_price,
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

    def test_stop_loss_closes_position_and_records_event(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_price("A005930", 9_800)

        result = self.risk.evaluate_account("default")[0]

        self.assertEqual(result.event_type, "stop_loss")
        self.assertIsNotNone(result.order_id)
        self.assertEqual(self.paper.account_summary("default")["positions"], [])
        events = self.risk.events("default")
        self.assertEqual(events[0]["event_type"], "stop_loss")
        self.assertEqual(events[0]["status"], "executed")
        self.assertEqual(events[0]["exit_class"], "loss_stop")
        self.assertEqual(events[0]["exit_reason_class"], "noise_stop")
        self.assertEqual(events[0]["setup_invalidated"], 0)
        classification = json.loads(events[0]["classification_json"])
        self.assertEqual(
            classification["reentry_setup"]["version"],
            "v0.12.96",
        )
        self.assertEqual(
            classification["reentry_setup"]["exit_reason_class"],
            "noise_stop",
        )
        lock = self.database.active_reentry_lock("A005930")
        self.assertIsNotNone(lock)
        self.assertEqual(
            lock["reason"],
            "exit_reentry_block:noise_stop",
        )
        pending_lock = self.database.pending_reentry_lock("A005930")
        self.assertEqual(pending_lock["setup_version"], "v0.12.96")
        self.assertEqual(pending_lock["setup"]["exit_reason_class"], "noise_stop")
        with self.database.connect() as connection:
            decision = connection.execute(
                """
                SELECT d.*, v.strategy_name
                FROM strategy_decisions d
                JOIN strategy_versions v ON v.id = d.strategy_version_id
                WHERE v.strategy_name = 'risk'
                """
            ).fetchone()
            request = connection.execute(
                "SELECT * FROM order_requests WHERE decision_id = ?",
                (decision["id"],),
            ).fetchone()
        self.assertEqual(decision["action"], "sell")
        self.assertEqual(decision["outcome"], "executed")
        self.assertEqual(request["status"], "filled")
        self.assertEqual(self.risk.evaluate_account("default"), [])

    def test_trade_plan_stop_is_used_by_actual_risk_evaluation(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan()
        self._save_price("A005930", 9_700)

        held = self.risk.evaluate_account("default")[0]

        self.assertIsNone(held.event_type)
        self.assertEqual(held.initial_stop_price, 9_650)
        self.assertEqual(held.current_stop_price, 9_650)

        self._save_price("A005930", 9_650)
        stopped = self.risk.evaluate_account("default")[0]

        self.assertEqual(stopped.event_type, "stop_loss")
        self.assertEqual(stopped.threshold_price, 9_650)

    def test_early_leader_stop_first_touch_waits_for_confirmation(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 15)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            strategy_mode="opening_leader_active",
            setup_overrides={
                "entry_type_policy": {"entry_grade": "A"},
                "opening_range_low": 9_600,
            },
        )
        self._save_leader_score(total_score=88)
        self._save_price("A005930", 9_650)

        held = self.risk.evaluate_account("default")[0]

        self.assertIsNone(held.event_type)
        self.assertEqual(held.expected_exit_reason, "stop_loss_confirmation")
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            10,
        )
        with self.database.connect() as connection:
            setup = json.loads(
                connection.execute(
                    "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
                ).fetchone()["setup_json"]
            )
            event_count = connection.execute(
                "SELECT COUNT(*) FROM paper_risk_events"
            ).fetchone()[0]
        self.assertEqual(
            setup["early_leader_stop_state"]["state"],
            "confirmation_pending",
        )
        self.assertEqual(setup["early_leader_stop_state"]["below_stop_count"], 1)
        self.assertEqual(event_count, 0)

        self._save_price("A005930", 9_600)
        stopped = self.risk.evaluate_account("default")[0]

        self.assertEqual(stopped.event_type, "stop_loss")
        self.assertEqual(stopped.threshold_price, 9_650)
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_wide_target_early_leader_locks_partial_profit_before_plan_target(
        self,
    ) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 40)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="chart_breakout_hold",
            first_take_profit_price=11_200,
            strategy_mode="opening_leader_active",
            setup_overrides={
                "entry_type_policy": {"entry_grade": "A"},
                "leader_mode_pass": True,
            },
        )
        self._save_leader_score(total_score=90)
        self._save_price("A005930", 10_450)

        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        self.assertEqual(partial.threshold_price, 10_450)
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            5,
        )
        with self.database.connect() as connection:
            setup = json.loads(
                connection.execute(
                    "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
                ).fetchone()["setup_json"]
            )
        self.assertEqual(
            setup["adaptive_partial_exit"]["reason"],
            "adaptive_leader_profit_lock",
        )
        self.assertEqual(
            setup["partial_exit_policy"]["planned_first_take_profit_price"],
            11_200,
        )
        self.assertEqual(
            setup["partial_exit_policy"]["actual_first_exit_trigger_price"],
            10_450,
        )

    def test_opening_leader_mode_locks_partial_profit_faster(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 12)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            first_take_profit_price=11_200,
            strategy_mode="opening_leader_core",
            setup_overrides={
                "entry_type_policy": {"entry_grade": "A"},
                "leader_mode_pass": True,
                "opening_leader_mode": {
                    "active": True,
                    "mode": "opening_leader_core",
                    "window": "09:00~09:20",
                    "profit_plan": "fast_partial_take_profit",
                    "fast_partial_profit_rate": "0.03",
                    "fast_partial_min_r": "0.35",
                    "residual_protection": "breakeven_then_vwap_or_5m20ma_break",
                    "loss_control": "account_10pct_kill_switch_and_structure_break",
                    "new_entry_limit_control": False,
                },
            },
        )
        self._save_leader_score(total_score=90)
        self._save_price("A005930", 10_300)

        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        self.assertEqual(partial.threshold_price, 10_300)
        with self.database.connect() as connection:
            setup = json.loads(
                connection.execute(
                    "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
                ).fetchone()["setup_json"]
            )
        self.assertEqual(
            setup["adaptive_partial_exit"]["reason"],
            "opening_leader_fast_partial_profit",
        )
        self.assertEqual(
            setup["partial_exit_policy"]["actual_first_exit_trigger_price"],
            10_300,
        )

    def test_aggressive_a_plus_mode_locks_partial_profit_fast(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 18)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            first_take_profit_price=11_200,
            strategy_mode="opening_leader_core",
            setup_overrides={
                "entry_type_policy": {"entry_grade": "A+"},
                "aggressive_exit_policy": {
                    "active": True,
                    "mode": "small_account_aggressive",
                    "entry_grade": "A+",
                    "fast_partial_profit_rate": "0.03",
                    "fast_partial_min_r": "0.35",
                    "structure_break_exit": True,
                },
            },
        )
        self._save_price("A005930", 10_300)

        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        self.assertEqual(partial.threshold_price, 10_300)
        with self.database.connect() as connection:
            setup = json.loads(
                connection.execute(
                    "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
                ).fetchone()["setup_json"]
            )
        self.assertEqual(
            setup["adaptive_partial_exit"]["reason"],
            "aggressive_a_plus_fast_partial_profit",
        )

    def test_a_plus_trend_day_runner_uses_one_r_partial_and_holds_residual(
        self,
    ) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 18)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            first_take_profit_price=11_200,
            second_take_profit_price=11_000,
            strategy_mode="opening_leader_core",
            setup_overrides={
                "entry_type_policy": {"entry_grade": "A+"},
                "residual_exit_rule": "structure_break_runner",
                "strategy_playbook": {
                    "name": "a_plus_trend_day_runner",
                    "priority": 1,
                    "residual_exit_rule": "structure_break_runner",
                    "fast_partial_profit_rate": "0.02",
                    "fast_partial_min_r": "0.30",
                },
                "aggressive_exit_policy": {
                    "active": True,
                    "mode": "small_account_aggressive",
                    "entry_grade": "A+",
                    "playbook": "a_plus_trend_day_runner",
                    "fast_partial_profit_rate": "0.02",
                    "fast_partial_min_r": "0.30",
                    "structure_break_exit": True,
                    "midday_time_or_tight_trailing": False,
                },
            },
        )
        self._save_price("A005930", 10_200)

        before_one_r = self.risk.evaluate_account("default")[0]

        self.assertIsNone(before_one_r.event_type)
        self.assertEqual(before_one_r.exit_state, "initial_risk")
        self.assertFalse(before_one_r.breakeven_active)
        self.assertFalse(before_one_r.trailing_active)
        self.assertEqual(before_one_r.current_stop_price, 9_650)
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            10,
        )

        self._save_price("A005930", 10_350)
        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        self.assertEqual(partial.threshold_price, 10_350)
        self.assertEqual(partial.exit_state, "trend_runner")
        self.assertFalse(partial.breakeven_active)
        self.assertFalse(partial.trailing_active)
        self.assertEqual(partial.current_stop_price, 9_650)
        with self.database.connect() as connection:
            plan = connection.execute(
                "SELECT * FROM trade_plans WHERE stock_code = 'A005930'"
            ).fetchone()
            breakeven_order_count = connection.execute(
                "SELECT COUNT(*) FROM breakeven_orders"
            ).fetchone()[0]
        self.assertEqual(plan["partial_exit_quantity"], 2)
        self.assertEqual(plan["breakeven_activated"], 0)
        self.assertEqual(breakeven_order_count, 0)
        state = self.database.position_exit_state(
            account_id=1,
            stock_code="A005930",
            trade_plan_id=int(plan["id"]),
        )
        self.assertEqual(state["state"], "trend_runner")
        self.assertEqual(state["entry_highest_price"], 10_350)

        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 20)
        self._save_intraday_vwap(price=11_000, vwap=10_500)
        self._save_recent_minute_bars(
            closes=(10_700, 10_820, 10_900, 10_960, 11_000),
            volumes=(1_000, 1_100, 1_150, 1_120, 1_130),
            lows=(10_650, 10_780, 10_860, 10_920, 10_960),
        )
        self._save_leader_score(total_score=92)
        self._save_supply_score(total_score=66)
        self._save_trade_strength(strength=135)
        self._save_price("A005930", 11_000)
        residual = self.risk.evaluate_account("default")[0]

        self.assertIsNone(residual.event_type)
        self.assertEqual(
            residual.expected_exit_reason,
            "trend_day_structure_runner",
        )
        self.assertEqual(residual.exit_state, "trend_runner")
        self.assertFalse(residual.breakeven_active)
        self.assertFalse(residual.trailing_active)
        self.assertEqual(residual.current_stop_price, 9_650)
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            8,
        )
        state = self.database.position_exit_state(
            account_id=1,
            stock_code="A005930",
            trade_plan_id=int(plan["id"]),
        )
        self.assertEqual(state["state"], "trend_runner")
        self.assertEqual(state["entry_highest_price"], 11_000)

    def test_trend_runner_can_partial_on_first_expansion_stall(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 18)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            first_take_profit_price=11_200,
            setup_overrides={
                "entry_type_policy": {"entry_grade": "A+"},
                "residual_exit_rule": "structure_break_runner",
                "strategy_playbook": {"name": "a_plus_trend_day_runner"},
            },
        )
        self._save_price("A005930", 10_280)
        self.assertIsNone(self.risk.evaluate_account("default")[0].event_type)
        self._save_recent_minute_bars(
            closes=(10_180, 10_240, 10_210),
            volumes=(1_000, 1_050, 1_000),
            highs=(10_200, 10_260, 10_230),
            lows=(10_150, 10_190, 10_180),
        )
        self._save_price("A005930", 10_210)

        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        self.assertEqual(partial.threshold_price, 10_210)
        self.assertEqual(partial.exit_state, "trend_runner")
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            8,
        )
        with self.database.connect() as connection:
            setup = json.loads(
                connection.execute(
                    "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
                ).fetchone()["setup_json"]
            )
        self.assertEqual(
            setup["adaptive_partial_exit"]["reason"],
            "first_expansion_stall_partial",
        )

    def test_trend_runner_raises_stop_only_to_recent_higher_low(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 20)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            setup_overrides={
                "residual_exit_rule": "structure_break_runner",
                "strategy_playbook": {"name": "a_plus_trend_day_runner"},
            },
        )
        self._save_price("A005930", 10_350)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )
        self._save_intraday_vwap(price=10_600, vwap=10_400)
        self._save_recent_minute_bars(
            closes=(10_250, 10_220, 10_300, 10_270, 10_350, 10_320, 10_600),
            volumes=(1_000, 950, 1_050, 980, 1_080, 1_000, 1_100),
            lows=(10_200, 10_180, 10_240, 10_210, 10_280, 10_230, 10_550),
        )
        self._save_price("A005930", 10_600)

        result = self.risk.evaluate_account("default")[0]

        self.assertIsNone(result.event_type)
        self.assertFalse(result.breakeven_active)
        self.assertEqual(result.current_stop_price, 10_230)
        with self.database.connect() as connection:
            plan = connection.execute(
                "SELECT id, breakeven_activated FROM trade_plans"
            ).fetchone()
        state = self.database.position_exit_state(
            account_id=1,
            stock_code="A005930",
            trade_plan_id=int(plan["id"]),
        )
        self.assertEqual(plan["breakeven_activated"], 0)
        self.assertEqual(state["recent_higher_low"], 10_230)
        self.assertEqual(state["protection_stop_price"], 10_230)

    def test_vwap_warning_requires_new_five_minute_failed_reclaim_to_exit(
        self,
    ) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 20)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            setup_overrides={
                "residual_exit_rule": "structure_break_runner",
                "strategy_playbook": {"name": "a_plus_trend_day_runner"},
            },
        )
        self._save_price("A005930", 10_350)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )
        self._save_intraday_vwap(price=10_390, vwap=10_400)
        self._save_recent_minute_bars(
            closes=(10_500, 10_480, 10_440, 10_410, 10_390),
            volumes=(1_000, 1_000, 1_000, 1_000, 1_000),
        )
        self._save_price("A005930", 10_390)

        warning = self.risk.evaluate_account("default")[0]

        self.assertIsNone(warning.event_type)
        self.assertEqual(warning.exit_state, "vwap_warning")
        with self.database.connect() as connection:
            plan_id = int(
                connection.execute("SELECT id FROM trade_plans").fetchone()["id"]
            )
        state = self.database.position_exit_state(
            account_id=1,
            stock_code="A005930",
            trade_plan_id=plan_id,
        )
        self.assertEqual(state["state"], "vwap_warning")
        self.assertEqual(state["vwap_warning_count"], 1)
        self.assertEqual(
            state["state_data"]["vwap_warning_bar_time"],
            "2026-06-10T10:20:00",
        )

        same_bar = self.risk.evaluate_account("default")[0]
        self.assertIsNone(same_bar.event_type)
        self.assertEqual(same_bar.exit_state, "vwap_warning")
        state = self.database.position_exit_state(
            account_id=1,
            stock_code="A005930",
            trade_plan_id=plan_id,
        )
        self.assertEqual(state["vwap_warning_count"], 1)

        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 25)
        self._save_recent_minute_bars(
            closes=(10_500, 10_480, 10_440, 10_410, 10_390, 10_380),
            volumes=(1_000, 1_000, 1_000, 1_000, 1_000, 1_000),
        )
        self._save_price("A005930", 10_380)

        exited = self.risk.evaluate_account("default")[0]

        self.assertEqual(exited.event_type, "stop_loss")
        self.assertEqual(exited.exit_state, "structure_break_exit")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])
        state = self.database.position_exit_state(
            account_id=1,
            stock_code="A005930",
            trade_plan_id=plan_id,
        )
        self.assertEqual(state["state"], "structure_break_exit")
        self.assertEqual(state["vwap_warning_count"], 1)
        with self.database.connect() as connection:
            transitions = connection.execute(
                """
                SELECT from_state, to_state
                FROM position_exit_state_events
                ORDER BY id
                """
            ).fetchall()
        self.assertEqual(
            [(row["from_state"], row["to_state"]) for row in transitions],
            [
                (None, "trend_runner"),
                ("trend_runner", "vwap_warning"),
                ("vwap_warning", "structure_break_exit"),
            ],
        )

    def test_volume_bearish_vwap_break_exits_runner_without_prior_warning(
        self,
    ) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 20)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            setup_overrides={
                "residual_exit_rule": "structure_break_runner",
                "strategy_playbook": {"name": "a_plus_trend_day_runner"},
            },
        )
        self._save_price("A005930", 10_350)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )
        self._save_intraday_vwap(price=10_380, vwap=10_400)
        self._save_recent_minute_bars(
            closes=(10_500, 10_480, 10_460, 10_430, 10_380),
            opens=(10_500, 10_480, 10_460, 10_430, 10_500),
            volumes=(1_000, 1_000, 1_000, 1_000, 2_000),
        )
        self._save_price("A005930", 10_380)

        exited = self.risk.evaluate_account("default")[0]

        self.assertEqual(exited.event_type, "stop_loss")
        self.assertEqual(exited.exit_state, "volume_selloff_exit")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_aggressive_a_plus_residual_exits_on_structure_break(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 18)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            entry_type="opening_range_breakout",
            first_take_profit_price=11_200,
            strategy_mode="opening_leader_core",
            setup_overrides={
                "entry_type_policy": {"entry_grade": "A+"},
                "opening_range_low": 10_150,
                "aggressive_exit_policy": {
                    "active": True,
                    "mode": "small_account_aggressive",
                    "entry_grade": "A+",
                    "fast_partial_profit_rate": "0.03",
                    "fast_partial_min_r": "0.35",
                    "structure_break_exit": True,
                },
            },
        )
        self._save_price("A005930", 10_300)
        self.risk.evaluate_account("default")

        self.risk.now_provider = lambda: datetime(2026, 6, 10, 9, 35)
        self._save_intraday_vwap(price=10_100, vwap=10_400)
        self._save_supply_score(total_score=15, blocked=True)
        self._save_price("A005930", 10_100)
        residual = self.risk.evaluate_account("default")[0]

        self.assertEqual(residual.event_type, "stop_loss")
        self.assertEqual(residual.threshold_price, 10_100)
        self.assertEqual(residual.exit_state, "structure_break_exit")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])
        with self.database.connect() as connection:
            setup = json.loads(
                connection.execute(
                    "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
                ).fetchone()["setup_json"]
            )
        evidence = setup["vwap_exit_state"]
        self.assertIn(
            setup["residual_target_adjustment"]["target"],
            (11_000, 11_200),
        )
        self.assertEqual(
            setup["partial_exit_policy"]["first_exit_executed"],
            True,
        )

    def test_supply_hard_block_relaxed_trade_locks_profit_early(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 30)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            first_take_profit_price=11_000,
            setup_overrides={
                "supply_hard_block_policy": {
                    "status": "relaxed",
                    "fast_profit_lock": True,
                    "confirmed_net_sell": True,
                }
            },
        )
        self._save_price("A005930", 10_260)

        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        with self.database.connect() as connection:
            setup = json.loads(
                connection.execute(
                    "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
                ).fetchone()["setup_json"]
            )
        self.assertEqual(
            setup["adaptive_partial_exit"]["reason"],
            "supply_hard_block_fast_profit_lock",
        )
        self.assertLess(
            setup["partial_exit_policy"]["actual_first_exit_trigger_price"],
            11_000,
        )

    def test_take_profit_closes_position(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_price("A005930", 10_400)

        result = self.risk.evaluate_account("default")[0]

        self.assertEqual(result.event_type, "take_profit")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_trailing_stop_activates_after_profit_then_closes_on_drawdown(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_price("A005930", 10_350)

        first = self.risk.evaluate_account("default")[0]
        self.assertIsNone(first.event_type)
        status = self.risk.status("default")[0]
        self.assertEqual(status["trailing_active"], 1)
        self.assertEqual(status["highest_price"], 10_350)

        self._save_price("A005930", 10_190)
        second = self.risk.evaluate_account("default")[0]

        self.assertEqual(second.event_type, "trailing_stop")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_trade_plan_partial_take_profit_then_breakeven_stop(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan()
        self._save_price("A005930", 10_500)

        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        self.assertEqual(partial.threshold_price, 10_500)
        self.assertEqual(partial.exit_state, "partial_profit_locked")
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            5,
        )
        with self.database.connect() as connection:
            plan = connection.execute(
                "SELECT * FROM trade_plans WHERE stock_code = 'A005930'"
            ).fetchone()
            profile = connection.execute(
                """
                SELECT current_stop_price, breakeven_active,
                       highest_price, trailing_active
                FROM paper_risk_profiles
                WHERE stock_code = 'A005930'
                """
            ).fetchone()
            partial_plan = connection.execute(
                """
                SELECT *
                FROM partial_exit_plans
                WHERE trade_plan_id = ?
                """,
                (plan["id"],),
            ).fetchone()
            partial_fill = connection.execute(
                """
                SELECT *
                FROM partial_exit_fills
                WHERE trade_plan_id = ?
                """,
                (plan["id"],),
            ).fetchone()
            breakeven_order = connection.execute(
                """
                SELECT *
                FROM breakeven_orders
                WHERE trade_plan_id = ?
                """,
                (plan["id"],),
            ).fetchone()
            trailing_state = connection.execute(
                """
                SELECT *
                FROM trailing_exit_state
                WHERE trade_plan_id = ?
                """,
                (plan["id"],),
            ).fetchone()
            sell_order = connection.execute(
                """
                SELECT *
                FROM paper_orders
                WHERE stock_code = 'A005930' AND side = 'sell'
                """
            ).fetchone()
            sell_fill = connection.execute(
                """
                SELECT *
                FROM paper_fills
                WHERE order_id = ?
                """,
                (partial.order_id,),
            ).fetchone()
            order_request = connection.execute(
                """
                SELECT *
                FROM order_requests
                WHERE stock_code = 'A005930' AND side = 'sell'
                """
            ).fetchone()
            order_execution = connection.execute(
                """
                SELECT *
                FROM order_executions
                WHERE order_request_id = ?
                """,
                (order_request["id"],),
            ).fetchone()
        self.assertEqual(plan["partial_exit_executed"], 1)
        self.assertEqual(plan["partial_exit_quantity"], 5)
        self.assertEqual(plan["partial_exit_order_id"], partial.order_id)
        self.assertEqual(plan["breakeven_activated"], 1)
        self.assertEqual(profile["current_stop_price"], 10_000)
        self.assertEqual(profile["breakeven_active"], 1)
        self.assertEqual(profile["highest_price"], 10_500)
        self.assertEqual(profile["trailing_active"], 1)
        self.assertEqual(partial_plan["status"], "partial_taken")
        self.assertEqual(partial_plan["first_exit_completed"], 1)
        self.assertEqual(partial_plan["breakeven_activated"], 1)
        self.assertEqual(partial_plan["residual_quantity"], 5)
        partial_state = json.loads(trailing_state["state_json"])
        self.assertEqual(
            partial_state["exit_state_machine"]["state"],
            "partial_profit_locked",
        )
        self.assertIn(
            "trend_runner",
            partial_state["exit_state_machine"]["expected_next_states"],
        )
        self.assertEqual(partial_fill["quantity"], 5)
        self.assertEqual(partial_fill["order_id"], partial.order_id)
        self.assertEqual(breakeven_order["status"], "activated_after_partial")
        self.assertEqual(trailing_state["active"], 1)
        self.assertEqual(trailing_state["current_price"], 10_500)
        self.assertEqual(sell_order["id"], partial.order_id)
        self.assertEqual(sell_order["status"], "filled")
        self.assertEqual(sell_order["quantity"], 5)
        self.assertEqual(sell_fill["side"], "sell")
        self.assertEqual(sell_fill["quantity"], 5)
        self.assertEqual(order_request["status"], "filled")
        self.assertEqual(order_request["quantity"], 5)
        self.assertTrue(str(order_request["client_order_id"]).startswith("plan-partial:"))
        self.assertEqual(order_execution["status"], "filled")
        self.assertEqual(order_execution["internal_order_id"], partial.order_id)

        repeated = self.risk.evaluate_account("default")[0]
        self.assertIsNone(repeated.event_type)
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            5,
        )
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_orders WHERE side = 'sell'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM partial_exit_fills"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM breakeven_orders"
                ).fetchone()[0],
                1,
            )

        self._save_price("A005930", 9_900)
        breakeven = self.risk.evaluate_account("default")[0]

        self.assertEqual(breakeven.event_type, "stop_loss")
        self.assertGreaterEqual(breakeven.threshold_price, 10_000)
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_residual_target_is_raised_after_first_profit_when_strength_holds(
        self,
    ) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 20)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan()
        self._save_intraday_vwap()
        self._save_leader_score()
        self._save_supply_score()
        self._save_trade_strength()
        self._save_price("A005930", 10_500)

        partial = self.risk.evaluate_account("default")[0]

        self.assertEqual(partial.event_type, "partial_take_profit")
        self._save_price("A005930", 10_900)
        residual = self.risk.evaluate_account("default")[0]

        self.assertIsNone(residual.event_type)
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            5,
        )
        adjustments = self.database.latest_partial_exit_adjustments()
        self.assertEqual(adjustments[0]["adjustment_type"], "raise_residual_target")
        self.assertEqual(adjustments[0]["old_target"], 11_000)
        self.assertGreater(adjustments[0]["new_target"], 11_000)
        evidence = adjustments[0]["evidence"]
        self.assertTrue(evidence["vwap_support"])
        self.assertTrue(evidence["leader_strength"])
        with self.database.connect() as connection:
            trade_plan = connection.execute(
                "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
            ).fetchone()
            partial_plan = connection.execute(
                """
                SELECT second_target_price
                FROM partial_exit_plans
                WHERE stock_code = 'A005930'
                """
            ).fetchone()
        setup = json.loads(trade_plan["setup_json"])
        self.assertEqual(
            setup["partial_exit_adjustment"]["adjustment_type"],
            "raise_residual_target",
        )
        self.assertGreater(setup["adjusted_second_take_profit_price"], 11_000)
        self.assertEqual(
            partial_plan["second_target_price"],
            setup["adjusted_second_take_profit_price"],
        )

    def test_second_take_profit_is_deferred_for_strong_leader_until_late_session(
        self,
    ) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 30)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            setup_overrides={"adjusted_second_take_profit_price": 11_000}
        )
        self._save_intraday_vwap(price=11_100)
        self._save_leader_score(total_score=92)
        self._save_supply_score(total_score=66)
        self._save_trade_strength(strength=145)
        self._save_price("A005930", 10_500)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )

        self._save_price("A005930", 11_100)
        deferred = self.risk.evaluate_account("default")[0]

        self.assertIsNone(deferred.event_type)
        self.assertEqual(deferred.expected_exit_reason, "trailing_stop")
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            5,
        )
        adjustments = self.database.latest_partial_exit_adjustments()
        self.assertEqual(adjustments[0]["adjustment_type"], "delay_second_take_profit")
        evidence = adjustments[0]["evidence"]
        self.assertFalse(evidence["near_close"])

        self.risk.now_provider = lambda: datetime(2026, 6, 10, 14, 40)
        released = self.risk.evaluate_account("default")[0]

        self.assertEqual(released.event_type, "take_profit")
        self.assertEqual(released.threshold_price, 11_000)
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_vwap_shakeout_after_partial_exit_keeps_residual_position(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 35)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan()
        self._save_price("A005930", 10_500)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )
        self._save_intraday_vwap(price=10_450, vwap=10_400)
        self._save_recent_minute_bars(
            closes=(10_550, 10_480, 10_390, 10_380, 10_450),
            volumes=(1_000, 950, 700, 650, 600),
        )
        self._save_leader_score(total_score=90)
        self._save_supply_score(total_score=62)
        self._save_trade_strength(strength=120)
        self._save_price("A005930", 10_450)

        result = self.risk.evaluate_account("default")[0]

        self.assertIsNone(result.event_type)
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            5,
        )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT setup_json FROM trade_plans WHERE stock_code = 'A005930'"
            ).fetchone()
            adjustment_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM partial_exit_adjustments
                WHERE adjustment_type = 'vwap_actual_exit'
                """
            ).fetchone()[0]
        setup = json.loads(row["setup_json"])
        self.assertEqual(
            setup["vwap_exit_state"]["state"],
            "shakeout_recovered",
        )
        self.assertEqual(adjustment_count, 0)

    def test_residual_trailing_stop_defers_when_structure_holds(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 35)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan()
        self._save_price("A005930", 10_500)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )
        self._save_intraday_vwap(price=10_300, vwap=10_200)
        self._save_recent_minute_bars(
            closes=(10_200, 10_240, 10_270, 10_290, 10_300),
            volumes=(1_000, 950, 900, 850, 820),
            lows=(10_180, 10_220, 10_250, 10_270, 10_280),
        )
        self._save_leader_score(total_score=88)
        self._save_supply_score(total_score=62)
        self._save_trade_strength(strength=125)
        self._save_price("A005930", 10_300)

        result = self.risk.evaluate_account("default")[0]

        self.assertIsNone(result.event_type)
        self.assertEqual(result.expected_exit_reason, "residual_structure_hold")
        self.assertEqual(
            self.paper.account_summary("default")["positions"][0]["quantity"],
            5,
        )

    def test_residual_trailing_stop_closes_on_confirmed_structure_break(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 40)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan()
        self._save_price("A005930", 10_500)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )
        self._save_intraday_vwap(price=10_050, vwap=10_200)
        self._save_recent_minute_bars(
            closes=(10_300, 10_280, 10_260, 10_240, 10_050),
            volumes=(1_000, 980, 950, 920, 1_600),
            opens=(10_300, 10_280, 10_260, 10_240, 10_230),
            lows=(10_280, 10_260, 10_240, 10_220, 10_030),
        )
        self._save_leader_score(total_score=88)
        self._save_supply_score(total_score=62)
        self._save_trade_strength(strength=82)
        self._save_price("A005930", 10_050)

        result = self.risk.evaluate_account("default")[0]

        self.assertEqual(result.event_type, "stop_loss")
        self.assertEqual(result.exit_class, "profit_protect_stop")
        self.assertEqual(result.exit_reason_class, "structure_loss_stop")
        self.assertTrue(result.setup_invalidated)
        self.assertEqual(self.paper.account_summary("default")["positions"], [])
        events = self.risk.events("default")
        self.assertEqual(events[0]["exit_class"], "profit_protect_stop")
        self.assertEqual(events[0]["exit_reason_class"], "structure_loss_stop")
        self.assertEqual(events[0]["setup_invalidated"], 1)
        classification = json.loads(events[0]["classification_json"])
        self.assertEqual(
            classification["reentry_setup"]["version"],
            "v0.12.96",
        )
        self.assertEqual(
            classification["reentry_setup"]["exit_class"],
            "profit_protect_stop",
        )
        pending_lock = self.database.pending_reentry_lock("A005930")
        self.assertEqual(pending_lock["setup_version"], "v0.12.96")
        self.assertEqual(
            pending_lock["setup"]["exit_reason_class"],
            "structure_loss_stop",
        )

    def test_vwap_actual_breakdown_after_partial_exit_closes_residual(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 45)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan()
        self._save_price("A005930", 10_500)
        self.assertEqual(
            self.risk.evaluate_account("default")[0].event_type,
            "partial_take_profit",
        )
        self._save_intraday_vwap(price=10_300, vwap=10_400)
        self._save_recent_minute_bars(
            closes=(10_500, 10_390, 10_350, 10_320, 10_300),
            volumes=(1_000, 1_050, 1_100, 1_200, 2_000),
        )
        self._save_leader_score(total_score=90)
        self._save_supply_score(total_score=20, blocked=True)
        self._save_trade_strength(strength=82)
        self._save_price("A005930", 10_300)

        result = self.risk.evaluate_account("default")[0]

        self.assertEqual(result.event_type, "stop_loss")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])
        adjustments = self.database.latest_partial_exit_adjustments()
        self.assertEqual(adjustments[0]["adjustment_type"], "vwap_actual_exit")
        self.assertEqual(
            adjustments[0]["evidence"]["vwap_exit_state"]["state"],
            "actual_breakdown",
        )
        lock = self.database.active_reentry_lock(
            "A005930",
            as_of="2026-06-10T10:45:00",
        )
        self.assertTrue(lock["reason"].startswith("exit_reentry_block:"))
        self.assertIn(":vwap_actual_exit", lock["reason"])

    def test_supply_confirmed_net_sell_fast_exits_relaxed_trade(self) -> None:
        self.risk.now_provider = lambda: datetime(2026, 6, 10, 10, 45)
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_trade_plan(
            first_take_profit_price=11_000,
            setup_overrides={
                "supply_hard_block_policy": {
                    "status": "relaxed",
                    "fast_profit_lock": True,
                    "exit_watch": "confirmed_net_sell_fast_exit",
                }
            },
        )
        self._save_intraday_vwap(price=9_950, vwap=10_050)
        self._save_supply_score(
            total_score=18,
            blocked=True,
            blocked_reason="프로그램 당일 매도 집중",
            components={
                "program": {
                    "policy_state": "block",
                    "hard_block": True,
                    "consecutive_net_sell_count": 3,
                }
            },
        )
        self._save_trade_strength(strength=82)
        self._save_price("A005930", 9_950)

        result = self.risk.evaluate_account("default")[0]

        self.assertEqual(result.event_type, "stop_loss")
        self.assertEqual(result.threshold_price, 9_950)
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_dynamic_exit_state_is_saved_in_profile_and_history(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self._save_price("A005930", 10_200)

        result = self.risk.evaluate_account("default")[0]

        self.assertTrue(result.breakeven_active)
        self.assertEqual(result.initial_stop_price, 9_800)
        self.assertEqual(result.current_stop_price, 10_000)
        with self.database.connect() as connection:
            profile = connection.execute(
                """
                SELECT initial_stop_price, current_stop_price,
                       breakeven_active, expected_exit_reason
                FROM paper_risk_profiles
                WHERE stock_code = 'A005930'
                """
            ).fetchone()
            state = connection.execute(
                """
                SELECT current_price, initial_stop_price, current_stop_price,
                       breakeven_active, rule_reason
                FROM paper_risk_state_events
                WHERE stock_code = 'A005930'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(profile["current_stop_price"], 10_000)
        self.assertEqual(profile["breakeven_active"], 1)
        self.assertEqual(state["current_price"], 10_200)
        self.assertEqual(state["initial_stop_price"], 9_800)
        self.assertEqual(state["current_stop_price"], 10_000)
        self.assertEqual(state["breakeven_active"], 1)
        self.assertIn("breakeven", state["rule_reason"])

    def test_custom_position_rates_are_applied(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        self.risk.configure_position(
            "default",
            "A005930",
            stop_loss_rate=Decimal("0.01"),
            take_profit_rate=Decimal("0.10"),
        )
        self._save_price("A005930", 9_900)

        result = self.risk.evaluate_account("default")[0]

        self.assertEqual(result.event_type, "stop_loss")

    def test_daily_loss_limit_blocks_new_buy(self) -> None:
        settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.01"),
            max_consecutive_losses=10,
            max_position_rate=Decimal("1"),
        )
        paper = PaperTradingEngine(self.database, self.paper_settings, settings)
        paper.place_market_order("default", "A005930", "buy", 50)
        self._save_price("A005930", 9_500)
        paper.place_market_order("default", "A005930", "sell", 50)
        self._save_price("A000660", 10_000)

        with self.assertRaisesRegex(PaperTradingError, "일일 최대 손실"):
            paper.place_market_order("default", "A000660", "buy", 1)

    def test_consecutive_losses_block_new_buy(self) -> None:
        settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.90"),
            max_consecutive_losses=2,
            max_position_rate=Decimal("1"),
        )
        paper = PaperTradingEngine(self.database, self.paper_settings, settings)
        for stock_code in ("A005930", "A000660"):
            self._save_price(stock_code, 10_000)
            paper.place_market_order("default", stock_code, "buy", 1)
            self._save_price(stock_code, 9_900)
            paper.place_market_order("default", stock_code, "sell", 1)
        self._save_price("A035420", 10_000)

        with self.assertRaisesRegex(PaperTradingError, "연속 손실 2회"):
            paper.place_market_order("default", "A035420", "buy", 1)

    def test_position_value_limit_blocks_oversized_buy(self) -> None:
        settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
            max_position_rate=Decimal("0.25"),
        )
        paper = PaperTradingEngine(self.database, self.paper_settings, settings)
        with self.assertRaisesRegex(PaperTradingError, "종목당 투자 한도"):
            paper.place_market_order("default", "A005930", "buy", 51)

    def test_max_position_count_blocks_new_stock(self) -> None:
        settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.90"),
            max_consecutive_losses=10,
            max_position_rate=Decimal("1"),
            max_positions=2,
        )
        paper = PaperTradingEngine(self.database, self.paper_settings, settings)
        for stock_code in ("A005930", "A000660", "A035420"):
            self._save_price(stock_code, 10_000)
        paper.place_market_order("default", "A005930", "buy", 1)
        paper.place_market_order("default", "A000660", "buy", 1)

        with self.assertRaisesRegex(PaperTradingError, "동시 보유 종목"):
            paper.place_market_order("default", "A035420", "buy", 1)

    def test_entry_gain_limit_blocks_chasing(self) -> None:
        paper = PaperTradingEngine(
            self.database,
            self.paper_settings,
            RiskSettings(
                stop_loss_rate=Decimal("0.02"),
                take_profit_rate=Decimal("0.04"),
                trailing_activation_rate=Decimal("0.03"),
                trailing_drawdown_rate=Decimal("0.015"),
                daily_max_loss_rate=Decimal("0.05"),
                max_consecutive_losses=3,
                max_entry_gain_rate=Decimal("0.15"),
            ),
        )
        self._save_previous_daily("A005930", 10_000)
        self._save_price("A005930", 11_600)

        with self.assertRaisesRegex(PaperTradingError, "급등주 추격"):
            paper.place_market_order("default", "A005930", "buy", 1)

    def test_market_close_liquidates_all_positions(self) -> None:
        self.paper.place_market_order("default", "A005930", "buy", 10)
        seoul = timezone(timedelta(hours=9))

        results = self.risk.liquidate_market_close(
            "default",
            now=datetime(2026, 6, 10, 15, 20, tzinfo=seoul),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].event_type, "market_close")
        self.assertEqual(self.paper.account_summary("default")["positions"], [])

    def test_stale_price_blocks_risk_liquidation(self) -> None:
        paper = PaperTradingEngine(
            self.database,
            self.paper_settings,
            self.risk_settings,
            max_price_age_seconds=60,
        )
        risk = RiskManager(
            self.database,
            paper,
            self.risk_settings,
            order_executor=PaperOrderExecutor(paper, FakeNotifier()),
        )
        paper.place_market_order("default", "A005930", "buy", 10)
        self._save_price("A005930", 9_000)
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
            risk.evaluate_account("default")

        self.assertEqual(
            paper.account_summary("default")["positions"][0]["quantity"],
            10,
        )


if __name__ == "__main__":
    unittest.main()
