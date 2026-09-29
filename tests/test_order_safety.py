import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from db.database import Database
from domain.models import DecisionAction, OrderRequest, OrderSide, StrategyDecision
from execution.safety import OrderSafetyGuard


class OrderSafetyGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.guard = OrderSafetyGuard(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _decision(self, *, stock_code: str = "A005930") -> StrategyDecision:
        return StrategyDecision(
            strategy_name="breakout",
            stock_code=stock_code,
            decided_at=datetime(2026, 6, 24, 10, 0),
            action=DecisionAction.BUY,
            score=Decimal("90"),
            confidence=Decimal("1"),
            reason="live safety test",
        )

    def _request(
        self,
        *,
        stock_code: str = "A005930",
        quantity: int = 1,
        client_order_id: str = "safety:live:test",
    ) -> OrderRequest:
        decision = self._decision(stock_code=stock_code)
        return OrderRequest(
            account_name="real",
            stock_code=stock_code,
            side=OrderSide.BUY,
            quantity=quantity,
            decision=decision,
            decision_id=None,
            reason=decision.reason,
            client_order_id=client_order_id,
        )

    def _seed_decision_id(self, *, stock_code: str = "A005930") -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            version_id = connection.execute(
                """
                INSERT INTO strategy_versions(
                    strategy_name, version, description, active,
                    created_at, updated_at
                )
                VALUES ('breakout', 'test', 'test', 1, ?, ?)
                ON CONFLICT(strategy_name, version) DO UPDATE SET
                    updated_at = excluded.updated_at
                RETURNING id
                """,
                (timestamp, timestamp),
            ).fetchone()["id"]
            return int(
                connection.execute(
                    """
                    INSERT INTO strategy_decisions(
                        account_id, strategy_version_id, strategy_config_id,
                        stock_code, decided_at, source_data_time, action,
                        score, confidence, reason, outcome, message,
                        created_at, updated_at
                    )
                    VALUES (NULL, ?, NULL, ?, ?, ?, 'buy',
                            90, 1, 'test', 'evaluated', NULL, ?, ?)
                    """,
                    (
                        version_id,
                        stock_code,
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                ).lastrowid
            )

    def _live_pass_kwargs(self) -> dict:
        return {
            "execution_mode": "live",
            "current_price": 6000,
            "expected_price": 6000,
            "order_book_pass": True,
            "leader_mode": {"entry_allowed": True, "name": "verified_leader"},
            "supply_score": {"blocked": False},
            "chart_blocked": False,
            "market_mode_allowed": True,
            "quote_collected_at": datetime.now(),
            "freshness_checks": {
                "leader": True,
                "supply": True,
                "chart": True,
                "market_mode": True,
            },
        }

    def test_pre_order_recheck_failure_is_saved(self) -> None:
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code="A005930",
            decided_at=datetime(2026, 6, 10, 10, 40),
            action=DecisionAction.BUY,
            score=Decimal("80"),
            confidence=Decimal("1"),
            reason="테스트 매수",
        )
        request = OrderRequest(
            account_name="default",
            stock_code="A005930",
            side=OrderSide.BUY,
            quantity=1,
            decision=decision,
            decision_id=None,
            reason="테스트 매수",
            client_order_id="safety:test",
        )

        result = self.guard.verify_before_submit(
            request,
            execution_mode="paper",
            current_price=110,
            expected_price=100,
            order_book_pass=False,
            leader_mode={"entry_allowed": True, "name": "verified_leader"},
            supply_score={"blocked": False},
            chart_blocked=False,
            market_mode_allowed=True,
        )
        events = self.database.latest_order_safety_events(limit=1)

        self.assertFalse(result.passed)
        self.assertIn("가격 괴리", result.reason_text)
        self.assertIn("호가·체결 조건 미충족", result.reason_text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "pre_order_recheck_failed")
        self.assertFalse(events[0]["passed"])
        self.assertEqual(events[0]["stock_code"], "A005930")
        self.assertEqual(events[0]["context"]["price_drift_rate"], 0.1)

    def test_live_initial_buy_is_limited_to_one_share(self) -> None:
        request = self._request(quantity=2)

        result = self.guard.verify_before_submit(
            request,
            execution_mode="live",
            current_price=1000,
            expected_price=1000,
            order_book_pass=True,
            leader_mode={"entry_allowed": True, "name": "verified_leader"},
            supply_score={"blocked": False},
            chart_blocked=False,
            market_mode_allowed=True,
        )
        events = self.database.latest_order_safety_events(limit=1)

        self.assertFalse(result.passed)
        self.assertIn("limited to 1 share", result.reason_text)
        self.assertTrue(events[0]["context"]["live_minimum_size_mode"])

    def test_live_one_share_order_passes_when_all_rechecks_are_fresh(self) -> None:
        request = self._request(client_order_id="safety:live:pass")

        result = self.guard.verify_before_submit(
            request,
            **self._live_pass_kwargs(),
        )

        self.assertTrue(result.passed, result.reason_text)
        self.assertEqual(self.database.latest_order_safety_events(limit=1), [])

    def test_live_buy_requires_minimum_order_amount_and_fresh_inputs(self) -> None:
        request = self._request(client_order_id="safety:live:stale")

        result = self.guard.verify_before_submit(
            request,
            execution_mode="live",
            current_price=1000,
            expected_price=1000,
            order_book_pass=True,
            leader_mode={"entry_allowed": True},
            supply_score={"blocked": False},
            chart_blocked=False,
            market_mode_allowed=True,
            quote_collected_at=datetime.now() - timedelta(seconds=300),
            max_quote_age_seconds=180,
            freshness_checks={"leader": True},
        )

        self.assertFalse(result.passed)
        self.assertIn("minimum order amount", result.reason_text)
        self.assertIn("현재가 오래됨", result.reason_text)
        self.assertIn("supply 최신성 미확인", result.reason_text)
        self.assertIn("chart 최신성 미확인", result.reason_text)
        self.assertIn("market_mode 최신성 미확인", result.reason_text)

    def test_live_duplicate_client_order_id_is_blocked_before_submit(self) -> None:
        decision_id = self._seed_decision_id()
        existing = self._request(client_order_id="safety:live:duplicate")
        self.database.save_order_request(
            existing,
            decision_id=decision_id,
            execution_mode="live",
        )
        request = self._request(client_order_id="safety:live:duplicate")

        result = self.guard.verify_before_submit(
            request,
            **self._live_pass_kwargs(),
        )

        self.assertFalse(result.passed)
        self.assertIn("duplicate client_order_id", result.reason_text)
        self.assertIn(
            "same-stock pending order exists",
            result.reason_text,
        )
        self.assertIn("same-stock reentry limit", result.reason_text)

    def test_live_daily_buy_and_consecutive_failure_limits_are_blocked(self) -> None:
        decision_id = self._seed_decision_id()
        for index in range(3):
            self.database.save_order_request(
                self._request(
                    stock_code=f"A{index:06d}",
                    client_order_id=f"safety:live:daily:{index}",
                ),
                decision_id=decision_id,
                execution_mode="live",
            )
        for index in range(3):
            self.database.save_order_safety_event(
                account_name="real",
                execution_mode="live",
                stock_code=f"AFAIL{index}",
                event_type="pre_order_recheck_failed",
                reason="seed failure",
                passed=False,
            )
        request = self._request(
            stock_code="A123456",
            client_order_id="safety:live:limited",
        )

        result = self.guard.verify_before_submit(
            request,
            **self._live_pass_kwargs(),
        )

        self.assertFalse(result.passed)
        self.assertIn("daily buy order limit", result.reason_text)
        self.assertIn("consecutive safety failure limit", result.reason_text)

    def test_live_broker_open_order_snapshot_blocks_new_buy(self) -> None:
        with self.database.connect() as connection:
            raw_id = connection.execute(
                """
                INSERT INTO api_snapshots(
                    api_id, stock_code, collected_at, payload_json
                )
                VALUES ('real-orders', 'A005930', ?, '{}')
                """,
                (datetime.now().isoformat(timespec="seconds"),),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO real_order_execution_snapshots(
                    collected_at, order_date, order_number, stock_code,
                    stock_name, trade_type, order_type_name, order_quantity,
                    order_price, executed_quantity, executed_price,
                    remaining_quantity, order_time, status,
                    original_order_number, exchange_type, raw_snapshot_id
                )
                VALUES (?, ?, '10001', 'A005930', '삼성전자', '매수',
                        '보통', 1, 6000, 0, NULL, 1, ?, '접수',
                        NULL, NULL, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    datetime.now().date().isoformat(),
                    datetime.now().strftime("%Y%m%d%H%M%S"),
                    raw_id,
                ),
            )
        request = self._request(client_order_id="safety:live:open-order")

        result = self.guard.verify_before_submit(
            request,
            **self._live_pass_kwargs(),
        )

        self.assertFalse(result.passed)
        self.assertIn("open/unfilled order exists", result.reason_text)
