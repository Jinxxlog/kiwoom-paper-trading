import json
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from db.database import Database
from domain.models import DecisionAction, StrategyDecision, TradePlan
from reports.trading_review import TradingReviewReport


class TradingReviewReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.version_id, self.config_id = self.database.register_strategy(
            "breakout",
            "test",
            {"test": True},
        )
        with self.database.connect() as connection:
            self.account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, realized_pnl,
                    created_at, updated_at
                )
                VALUES ('default', 1000000, 999500, -500, ?, ?)
                """,
                ("2026-06-22T09:00:00", "2026-06-22T10:00:00"),
            ).lastrowid
        self.service = TradingReviewReport(self.database)
        self._seed_trades()
        self._seed_blocked_forward_outcome()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_review_summarizes_loss_drivers(self) -> None:
        report = self.service.build(date(2026, 6, 22))

        self.assertEqual(report["summary"]["buy_count"], 2)
        self.assertEqual(report["summary"]["sell_count"], 2)
        self.assertEqual(report["summary"]["realized_pnl"], -500)
        self.assertEqual(report["summary"]["stop_loss_count"], 2)
        self.assertEqual(report["summary"]["loss_stop_count"], 1)
        self.assertEqual(report["summary"]["profit_protect_stop_count"], 1)
        self.assertEqual(report["summary"]["loss_stop_realized_pnl"], -1000)
        self.assertEqual(
            report["summary"]["profit_protect_stop_realized_pnl"],
            500,
        )
        self.assertEqual(report["risk_exit_breakdown"]["stop_loss_count"], 2)
        self.assertEqual(report["risk_exit_breakdown"]["loss_stop_count"], 1)
        self.assertEqual(
            report["risk_exit_breakdown"]["profit_protect_stop_count"],
            1,
        )
        self.assertEqual(
            report["missing_supply_performance"]["realized_pnl"],
            -1000,
        )
        self.assertEqual(report["force_entry_performance"]["realized_pnl"], -1000)
        self.assertEqual(report["buys_after_stop_loss"]["count"], 1)
        self.assertEqual(
            report["preventable_loss_estimate"]["total_preventable_loss"],
            1000,
        )

        by_type = {
            item["entry_type"]: item
            for item in report["entry_type_performance"]
        }
        self.assertEqual(by_type["chart_vwap_reclaim"]["realized_pnl"], -1000)
        self.assertEqual(by_type["opening_range_breakout"]["realized_pnl"], 500)
        self.assertEqual(by_type["chart_vwap_reclaim"]["loss_stop_count"], 1)
        self.assertEqual(
            by_type["opening_range_breakout"]["profit_protect_stop_count"],
            1,
        )
        self.assertEqual(report["stock_repeat_buys"][0]["stock_code"], "A005930")
        self.assertEqual(report["stock_repeat_buys"][0]["buy_count"], 2)
        self.assertEqual(
            report["blocked_forward_review"]["missed_spike_count"],
            1,
        )

    def test_strategy_router_diagnostics_groups_routes_and_legacy(self) -> None:
        report = self.service.build(date(2026, 6, 22))
        diagnostics = report["strategy_router_diagnostics"]

        by_entry = {
            row["entry_type"]: row
            for row in diagnostics["entry_routes"]
        }
        self.assertEqual(
            by_entry["opening_range_breakout"]["leader_strategy"],
            "trend_day_runner",
        )
        self.assertTrue(
            by_entry["opening_range_breakout"]["three_strategy_mapped"]
        )
        self.assertEqual(
            by_entry["opening_range_breakout"]["order_impact"],
            "filled_order",
        )
        self.assertEqual(
            by_entry["opening_range_breakout"]["realized_pnl"],
            500,
        )
        self.assertTrue(by_entry["chart_vwap_reclaim"]["legacy_candidate"])
        self.assertIn(
            "mean_reversion_rebound",
            by_entry["chart_vwap_reclaim"]["legacy_reasons"],
        )
        self.assertEqual(
            by_entry["chart_vwap_reclaim"]["old_logic_buy_count"],
            1,
        )
        self.assertEqual(
            by_entry["chart_vwap_reclaim"]["legacy_shadow_count"],
            1,
        )

        by_strategy = {
            row["name"]: row
            for row in diagnostics["leader_strategy_mapping"]
        }
        self.assertEqual(by_strategy["trend_day_runner"]["buy_count"], 1)
        self.assertEqual(by_strategy["trend_day_runner"]["realized_pnl"], 500)
        self.assertGreaterEqual(by_strategy["unmapped"]["buy_count"], 1)
        self.assertEqual(
            diagnostics["legacy_candidates"][0]["entry_type"],
            "chart_vwap_reclaim",
        )
        legacy_compare = diagnostics["legacy_shadow_comparison"]
        self.assertEqual(legacy_compare["old_logic_buy_count"], 1)
        self.assertEqual(legacy_compare["new_logic_shadow_count"], 1)
        self.assertEqual(legacy_compare["removed_from_order_influence_count"], 1)
        self.assertEqual(
            legacy_compare["by_legacy_route"][0]["legacy_route"],
            "mean_reversion_rebound",
        )
        self.assertTrue(diagnostics["guard_status_counts"])

    def test_strategy_dashboard_review_groups_remake_sections(self) -> None:
        missed_a_plus_id = self._save_decision(
            "A000660",
            "2026-06-22T09:25:00",
            action=DecisionAction.BUY,
            score=91,
            outcome="skipped",
        )
        self._save_plan(
            missed_a_plus_id,
            "opening_range_breakout",
            {
                "entry_guard_policy": {
                    "status": "blocked_bad_entry_candle_quality",
                    "entry_grade": "A+",
                },
                "money_flow_policy": {"a_plus_qualified": True},
                "strategy_playbook": {"name": "a_plus_trend_day_runner"},
                "router_execution_policy": {
                    "status": "shadow_only_bad_entry_candle_quality",
                    "order_effect": "shadow_only",
                },
                "strategy_ab_compare": {
                    "version": "v0.12.89",
                    "actual_mode": "remake",
                    "shadow_mode": "normal",
                    "normal": {
                        "would_enter": True,
                        "planned_quantity": 10,
                    },
                    "remake": {
                        "would_enter": False,
                        "planned_quantity": 0,
                    },
                    "diff": {
                        "action_diff": True,
                        "quantity_diff": -10,
                    },
                },
            },
        )
        self._save_forward_outcome(
            missed_a_plus_id,
            "A000660",
            max_up_rate=0.07,
            actual_order_status="not_ordered",
            actual_outcome="missed_rally_after_block",
        )
        missed_reentry_id = self._save_decision(
            "A089030",
            "2026-06-22T10:10:00",
            action=DecisionAction.BUY,
            score=88,
            outcome="skipped",
        )
        self._save_plan(
            missed_reentry_id,
            "chart_breakout_hold",
            {
                "entry_guard_policy": {
                    "status": "blocked_reentry_reward_risk",
                    "entry_grade": "A",
                },
                "reentry_engine_policy": {
                    "status": "blocked_reward_risk",
                    "new_setup_detected": True,
                },
                "strategy_playbook": {
                    "name": "new_high_rebreakout_reentry_probe"
                },
                "router_execution_policy": {
                    "status": "allowed_core_playbook",
                    "order_effect": "allow_order",
                },
                "strategy_ab_compare": {
                    "version": "v0.12.89",
                    "actual_mode": "remake",
                    "shadow_mode": "normal",
                    "normal": {
                        "would_enter": True,
                        "planned_quantity": 5,
                    },
                    "remake": {
                        "would_enter": False,
                        "planned_quantity": 0,
                    },
                    "diff": {
                        "action_diff": True,
                        "quantity_diff": -5,
                    },
                },
            },
        )
        self._save_forward_outcome(
            missed_reentry_id,
            "A089030",
            max_up_rate=0.05,
            actual_order_status="not_ordered",
            actual_outcome="missed_rally_after_block",
        )
        self._save_forward_outcome(
            2,
            "A005930",
            max_up_rate=0.08,
            actual_order_status="filled",
            actual_outcome="entered_then_rallied",
        )

        report = self.service.build(date(2026, 6, 22))
        review = report["strategy_dashboard_review"]

        self.assertEqual(review["version"], "v0.12.92")
        self.assertIn("a_plus_armed_count", review["summary"])
        self.assertIn("a_plus_armed_candidates", review)
        self.assertGreaterEqual(review["summary"]["a_plus_candidate_count"], 2)
        self.assertGreaterEqual(review["summary"]["a_plus_entered_count"], 1)
        self.assertGreaterEqual(review["summary"]["a_plus_not_entered_count"], 1)
        self.assertTrue(
            {
                row["playbook"]
                for row in review["playbook_performance"]
            }.issuperset(
                {
                    "a_plus_trend_day_runner",
                    "vwap_pullback_reentry",
                    "new_high_rebreakout_reentry_probe",
                }
            )
        )
        self.assertEqual(
            review["legacy_vs_new_router"]["summary"]["old_logic_buy_count"],
            1,
        )
        self.assertTrue(review["top_block_reasons"])
        self.assertIn(
            "A000660",
            {row["stock_code"] for row in review["missed_leaders"]},
        )
        self.assertIn(
            "A005930",
            {row["stock_code"] for row in review["sold_too_early"]},
        )
        self.assertIn(
            "A089030",
            {row["stock_code"] for row in review["missed_reentries"]},
        )
        ab = review["ab_test"]
        self.assertEqual(ab["version"], "v0.12.89")
        self.assertEqual(ab["actual_orders"], "selected_mode_only")
        by_mode = {row["mode"]: row for row in ab["modes"]}
        self.assertGreaterEqual(by_mode["normal"]["would_enter_count"], 3)
        self.assertGreaterEqual(by_mode["remake"]["actual_entry_count"], 1)
        self.assertGreaterEqual(by_mode["remake"]["missed_rally_count"], 2)
        self.assertGreaterEqual(ab["summary"]["action_diff_count"], 2)
        self.assertEqual(
            review["legacy_retirement_review"]["version"],
            "v0.12.90",
        )

    def test_legacy_retirement_review_scores_remove_and_shadow_candidates(self) -> None:
        for index in range(3):
            decision_id = self._save_decision(
                f"A7770{index}",
                f"2026-06-22T10:2{index}:00",
                action=DecisionAction.BUY,
                score=72,
                outcome="skipped",
            )
            self._save_plan(
                decision_id,
                "volume_breakout",
                {
                    "entry_guard_policy": {
                        "status": "blocked_data_gap",
                        "message": "데이터 공백/수급 공백으로 차단",
                    },
                    "strategy_playbook": {"name": "standard_breakout"},
                    "router_execution_policy": {
                        "status": "shadow_only_legacy_entry_route",
                        "order_effect": "shadow_only",
                    },
                    "legacy_entry_policy": {
                        "version": "v0.12.84",
                        "legacy_candidate": True,
                        "legacy_route": "simple_volume_breakout",
                        "legacy_reasons": ["simple_volume_breakout"],
                        "old_logic_would_buy": True,
                        "new_logic_would_buy": False,
                        "removed_from_order_influence": True,
                    },
                },
            )
            self._save_forward_outcome(
                decision_id,
                f"A7770{index}",
                max_up_rate=0.01,
                max_down_rate=-0.03,
                actual_order_status="not_ordered",
                actual_outcome="avoided_loss_after_block",
            )

        report = self.service.build(date(2026, 6, 22))
        retirement = report["legacy_retirement_review"]

        self.assertEqual(retirement["version"], "v0.12.90")
        by_route = {row["legacy_route"]: row for row in retirement["routes"]}
        self.assertEqual(
            by_route["simple_volume_breakout"]["recommendation"],
            "remove",
        )
        self.assertIn(
            "data_or_supply_gap_false_entry",
            by_route["simple_volume_breakout"]["evidence"],
        )
        self.assertIn(
            "unnecessary_entry_risk",
            by_route["simple_volume_breakout"]["evidence"],
        )
        self.assertEqual(
            by_route["mean_reversion_rebound"]["recommendation"],
            "permanent_shadow",
        )
        self.assertIn(
            "forward_outcome_not_ready",
            by_route["mean_reversion_rebound"]["warnings"],
        )

    def test_review_is_exported_as_json(self) -> None:
        output = Path(self.temp_dir.name) / "review.json"

        path = self.service.export_json(date(2026, 6, 22), output)

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["report_date"], "2026-06-22")
        self.assertEqual(payload["summary"]["realized_pnl"], -500)

    def test_blocked_reviews_split_common_special_and_products(self) -> None:
        self._upsert_universe("A122630", "KODEX 레버리지")
        special_id = self._seed_blocked_forward_outcome_for(
            "A0039P0",
            "2026-06-22T09:40:00",
            max_up_rate=0.08,
        )
        product_id = self._seed_blocked_forward_outcome_for(
            "A122630",
            "2026-06-22T09:50:00",
            max_up_rate=0.10,
        )
        self._save_replay_snapshot(special_id, "A0039P0", "2026-06-22T09:40:00")
        self._save_replay_snapshot(product_id, "A122630", "2026-06-22T09:50:00")

        report = self.service.build(date(2026, 6, 22))
        replay_review = self.database.strategy_replay_review(limit=10)

        blocked_by_category = {
            row["category"]: row
            for row in report["blocked_forward_review"]["by_stock_category"]
        }
        self.assertEqual(blocked_by_category["common_stock"]["missed_spike_count"], 1)
        self.assertEqual(
            blocked_by_category["preferred_or_special"]["missed_spike_count"],
            1,
        )
        self.assertEqual(blocked_by_category["market_product"]["missed_spike_count"], 1)
        self.assertEqual(
            next(
                row
                for row in report["blocked_forward_review"]["notable"]
                if row["stock_code"] == "A0039P0"
            )["stock_category"]["label"],
            "우선주/특수코드",
        )

        replay_by_category = {
            row["category"]: row for row in replay_review["by_stock_category"]
        }
        self.assertEqual(
            replay_by_category["preferred_or_special"]["missed_rally_after_block"],
            1,
        )
        self.assertEqual(
            replay_by_category["market_product"]["missed_rally_after_block"],
            1,
        )
        self.assertTrue(
            all(
                row["stock_category"]["key"] != "common_stock"
                for row in replay_review["rows"]
            )
        )

    def _seed_trades(self) -> None:
        decision1 = self._save_decision(
            "A005930",
            "2026-06-22T09:10:00",
            score=68,
            outcome="executed",
        )
        self._save_plan(
            decision1,
            "chart_vwap_reclaim",
            {
                "chart_trade": {"force_entry": True, "entry_grade": "C"},
                "supply_score": None,
                "entry_guard_policy": {
                    "status": "blocked_vwap_reclaim_without_confirmation",
                    "entry_grade": "C",
                },
                "entry_type_policy": {"entry_grade": "C"},
                "reentry_policy": {"status": "blocked"},
                "strategy_playbook": {"name": "standard_breakout"},
                "router_execution_policy": {
                    "status": "shadow_only_legacy_entry_route",
                    "order_effect": "shadow_only",
                },
                "legacy_entry_policy": {
                    "version": "v0.12.84",
                    "legacy_candidate": True,
                    "legacy_route": "mean_reversion_rebound",
                    "legacy_reasons": ["mean_reversion_rebound"],
                    "old_logic_would_buy": True,
                    "new_logic_would_buy": False,
                    "removed_from_order_influence": True,
                },
            },
        )
        order1 = self._save_paper_fill(
            "A005930",
            "buy",
            10,
            1000,
            "2026-06-22T09:10:05",
            decision_id=decision1,
        )
        self.assertGreater(order1, 0)
        sell1 = self._save_paper_fill(
            "A005930",
            "sell",
            10,
            900,
            "2026-06-22T09:15:00",
            realized_pnl=-1000,
        )
        self._save_stop_loss(sell1, "A005930", "2026-06-22T09:15:00")

        decision2 = self._save_decision(
            "A005930",
            "2026-06-22T09:20:00",
            score=86,
            outcome="executed",
        )
        self._save_plan(
            decision2,
            "opening_range_breakout",
            {
                "supply_score": 12,
                "supply_flow": {"total_score": 12},
                "entry_guard_policy": {
                    "status": "allowed_small_account_a_plus",
                    "entry_grade": "A+",
                },
                "entry_type_policy": {"entry_grade": "A+"},
                "reentry_policy": {"status": "allowed"},
                "strategy_playbook": {"name": "a_plus_trend_day_runner"},
                "strategy_ab_compare": {
                    "version": "v0.12.89",
                    "actual_mode": "remake",
                    "shadow_mode": "normal",
                    "normal": {
                        "would_enter": True,
                        "planned_quantity": 10,
                    },
                    "remake": {
                        "would_enter": True,
                        "planned_quantity": 10,
                    },
                    "diff": {"action_diff": False, "quantity_diff": 0},
                },
            },
        )
        self._save_paper_fill(
            "A005930",
            "buy",
            10,
            920,
            "2026-06-22T09:20:00",
            decision_id=decision2,
        )
        sell2 = self._save_paper_fill(
            "A005930",
            "sell",
            10,
            970,
            "2026-06-22T10:00:00",
            realized_pnl=500,
        )
        self._save_stop_loss(sell2, "A005930", "2026-06-22T10:00:00")

    def _seed_blocked_forward_outcome(self) -> None:
        self._seed_blocked_forward_outcome_for(
            "A000660",
            "2026-06-22T09:30:00",
            max_up_rate=0.04,
        )

    def _seed_blocked_forward_outcome_for(
        self,
        stock_code: str,
        decided_at: str,
        *,
        max_up_rate: float,
    ) -> int:
        decision_id = self._save_decision(
            stock_code,
            decided_at,
            action=DecisionAction.HOLD,
            score=55,
            outcome="skipped",
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO decision_forward_outcomes(
                    decision_id, stock_code, horizon_minutes,
                    max_up_rate, max_down_rate, close_rate,
                    actual_order_status, actual_outcome, details_json,
                    evaluated_at
                )
                VALUES (?, ?, 30, ?, -0.005, 0.03,
                        'not_ordered', 'missed_spike', '{}', ?)
                """,
                (decision_id, stock_code, max_up_rate, "2026-06-22T10:00:00"),
            )
        return decision_id

    def _save_forward_outcome(
        self,
        decision_id: int,
        stock_code: str,
        *,
        max_up_rate: float,
        actual_order_status: str,
        actual_outcome: str,
        max_down_rate: float = -0.004,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO decision_forward_outcomes(
                    decision_id, stock_code, horizon_minutes,
                    max_up_rate, max_down_rate, close_rate,
                    actual_order_status, actual_outcome, details_json,
                    evaluated_at
                )
                VALUES (?, ?, 60, ?, ?, 0.02, ?, ?, '{}',
                        '2026-06-22T11:00:00')
                """,
                (
                    decision_id,
                    stock_code,
                    max_up_rate,
                    max_down_rate,
                    actual_order_status,
                    actual_outcome,
                ),
            )

    def _save_replay_snapshot(
        self,
        decision_id: int,
        stock_code: str,
        evaluated_at: str,
    ) -> None:
        self.database.save_strategy_replay_snapshot(
            decision_id=decision_id,
            stock_code=stock_code,
            evaluated_at=evaluated_at,
            action="hold",
            score=55.0,
            reason="test block",
            supply_score={"blocked": True, "blocked_reason": "test block"},
            block_reasons=["test block"],
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE decision_forward_outcomes
                SET actual_outcome = 'missed_rally_after_block'
                WHERE decision_id = ?
                """,
                (decision_id,),
            )

    def _save_decision(
        self,
        stock_code: str,
        decided_at: str,
        *,
        action: DecisionAction = DecisionAction.BUY,
        score: int,
        outcome: str,
    ) -> int:
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code=stock_code,
            decided_at=datetime.fromisoformat(decided_at),
            action=action,
            score=Decimal(str(score)),
            confidence=Decimal("1"),
            reason="test decision",
        )
        return self.database.save_strategy_decision(
            decision,
            account_name="default",
            strategy_version_id=self.version_id,
            strategy_config_id=self.config_id,
            source_data_time=decided_at,
            outcome=outcome,
        )

    def _save_plan(
        self,
        decision_id: int,
        entry_type: str,
        setup: dict,
    ) -> None:
        self.database.save_trade_plan(
            TradePlan(
                decision_id=decision_id,
                stock_code="A005930",
                entry_type=entry_type,
                entry_tags=(entry_type,),
                entry_price=1000,
                planned_quantity=10,
                stop_loss_price=950,
                stop_loss_reason="test stop",
                first_take_profit_price=1100,
                first_take_profit_reason="test target",
                partial_exit_rate=Decimal("0.5"),
                breakeven_price=1000,
                breakeven_trigger_price=1100,
                trailing_rule="test trailing",
                invalidation_reason="test invalidation",
                reward_risk_ratio=Decimal("2"),
                setup=setup,
            )
        )

    def _save_paper_fill(
        self,
        stock_code: str,
        side: str,
        quantity: int,
        price: int,
        filled_at: str,
        *,
        decision_id: int | None = None,
        realized_pnl: int = 0,
    ) -> int:
        with self.database.connect() as connection:
            order_id = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    order_type, quantity, status, reason,
                    requested_at, completed_at
                )
                VALUES (?, ?, ?, ?, 'market', ?, 'filled', 'test',
                        ?, ?)
                """,
                (
                    self.account_id,
                    f"{side}-{stock_code}-{filled_at}",
                    stock_code,
                    side,
                    quantity,
                    filled_at,
                    filled_at,
                ),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                """,
                (
                    order_id,
                    stock_code,
                    side,
                    quantity,
                    price,
                    price,
                    quantity * price,
                    realized_pnl,
                    filled_at,
                    filled_at,
                ),
            )
            if decision_id is not None:
                request_id = connection.execute(
                    """
                    INSERT INTO order_requests(
                        decision_id, account_name, execution_mode, stock_code,
                        side, order_type, quantity, client_order_id, reason,
                        status, requested_at, updated_at
                    )
                    VALUES (?, 'default', 'paper', ?, ?, 'market', ?,
                            ?, 'test', 'filled', ?, ?)
                    """,
                    (
                        decision_id,
                        stock_code,
                        side,
                        quantity,
                        f"req-{side}-{stock_code}-{filled_at}",
                        filled_at,
                        filled_at,
                    ),
                ).lastrowid
                connection.execute(
                    """
                    INSERT INTO order_executions(
                        order_request_id, status, internal_order_id,
                        fill_price, quantity, message, executed_at
                    )
                    VALUES (?, 'filled', ?, ?, ?, 'test', ?)
                    """,
                    (request_id, order_id, price, quantity, filled_at),
                )
            return int(order_id)

    def _upsert_universe(self, stock_code: str, stock_name: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO stock_universe(stock_code, stock_name, updated_at)
                VALUES (?, ?, '2026-06-22T09:00:00')
                ON CONFLICT(stock_code) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    updated_at = excluded.updated_at
                """,
                (stock_code, stock_name),
            )

    def _save_stop_loss(
        self,
        order_id: int,
        stock_code: str,
        occurred_at: str,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_risk_events(
                    account_id, stock_code, event_type, trigger_price,
                    threshold_price, highest_price, order_id, status,
                    message, occurred_at
                )
                VALUES (?, ?, 'stop_loss', 900, 950, 1000, ?,
                        'executed', 'test stop', ?)
                """,
                (self.account_id, stock_code, order_id, occurred_at),
            )


if __name__ == "__main__":
    unittest.main()
