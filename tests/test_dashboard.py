import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from dashboard.server import HTML, DashboardService
from data.api_budget import ApiBudgetDecision, ApiCallPriority
from db.database import Database
from domain.models import (
    DecisionAction,
    OrderRequest,
    OrderSide,
    ScoreComponent,
    StrategyDecision,
    TradePlan,
)
from operations.runtime_control import CollectorRuntimeControl
from screening.engine import CandidateScreeningEngine
from config.settings import CandidateSettings


class DashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.runtime = CollectorRuntimeControl(self.database_path)
        self.service = DashboardService(self.database, self.runtime)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_status_reports_stopped_runtime_and_empty_account(self) -> None:
        status = self.service.status()

        self.assertFalse(status["runtime"]["running"])
        self.assertFalse(status["collection"]["fresh"])
        self.assertEqual(status["accounts"], [])
        self.assertEqual(status["risk_positions"], [])
        self.assertIsNone(status["candidates"]["run"])
        self.assertIsNone(status["real_account"]["snapshot"])
        self.assertEqual(status["real_account"]["positions"], [])
        self.assertEqual(status["real_account"]["orders"], [])
        self.assertIn("automation_flow", status)
        self.assertEqual(status["automation_flow"]["mode_label"], "정지")
        self.assertEqual(status["automation_flow"]["signal_counts"]["total"], 0)
        self.assertEqual(status["automation_flow"]["order_counts"]["total"], 0)
        self.assertEqual(status["automation_flow"]["recent_signals"], [])
        self.assertEqual(status["automation_flow"]["recent_orders"], [])
        self.assertGreaterEqual(len(status["strategy"]["versions"]), 2)
        self.assertFalse(status["strategy"]["runtime_enabled"])
        self.assertFalse(status["strategy"]["small_account_aggressive"])
        self.assertEqual(status["strategy"]["strategy_ab_active_mode"], "remake")
        breakout = next(
            item
            for item in status["strategy"]["versions"]
            if item["strategy_name"] == "breakout" and item["active"]
        )
        self.assertIn("VWAP", breakout["summary"])
        self.assertIn("details", breakout)
        self.assertIn("모의계좌 신규 매수", [x["value"] for x in breakout["details"]])
        self.assertEqual(
            status["strategy"]["risk_settings"]["stop_loss_rate"],
            0.02,
        )
        self.assertIn("market", status)
        self.assertIn("api", status)
        self.assertIn("operation_warning", status["strategy"])
        self.assertIn("new_buy", status["strategy"]["operation_warning"])
        self.assertIn("trading_review", status["strategy"])
        self.assertEqual(status["strategy"]["trading_review"]["summary"]["buy_count"], 0)
        self.assertIn("supply_pipeline", status["strategy"])
        self.assertEqual(
            status["strategy"]["supply_pipeline"]["counts"]["supply_scores"],
            0,
        )
        self.assertIn(
            "program_validation",
            status["strategy"]["supply_pipeline"],
        )
        self.assertEqual(
            status["strategy"]["supply_pipeline"]["program_validation"][
                "hard_block_count"
            ],
            0,
        )
        self.assertIn(
            "program_outcome_validation",
            status["strategy"]["supply_pipeline"],
        )
        self.assertIn("learning_report", status["leader_themes"])
        self.assertEqual(
            status["leader_themes"]["learning_report"]["summary"][
                "candidate_count"
            ],
            0,
        )

    def test_dashboard_has_separate_paper_and_live_tabs(self) -> None:
        self.assertIn("trading-review-summary", HTML)
        self.assertIn("trading-review-entry", HTML)
        self.assertIn("supply-pipeline-summary", HTML)
        self.assertIn("replay-filter-stock", HTML)
        self.assertIn("replay-filter-start", HTML)
        self.assertIn("replay-filter-end", HTML)
        self.assertIn("replay-filter-action", HTML)
        self.assertIn("replay-filter-reason", HTML)
        self.assertIn("applyReplayFilters", HTML)
        self.assertIn("resetReplayFilters", HTML)
        self.assertIn('id="paper-tab"', HTML)
        self.assertIn('id="performance-tab"', HTML)
        self.assertIn('id="theme-trends-tab"', HTML)
        self.assertIn('id="leader-tab"', HTML)
        self.assertIn('id="live-tab"', HTML)
        self.assertIn("오늘 손익 합계", HTML)
        self.assertIn("총 계좌 변동", HTML)
        self.assertNotIn("오늘 계좌 변동", HTML)
        self.assertIn("30일 주도테마", HTML)
        self.assertIn("최근 30일 주도 테마 신뢰도", HTML)
        self.assertIn("오늘 대장주 후보 TOP 20", HTML)
        self.assertIn("최근 청산 결과", HTML)
        self.assertIn("실전 주문 기능 없음", HTML)
        self.assertIn("조회 전용 화면", HTML)
        self.assertIn("최근 전략 판단", HTML)
        self.assertIn("위험관리 설정", HTML)
        self.assertIn("보유 종목 청산 상태", HTML)
        self.assertIn("기술·패턴 후보 종목", HTML)
        self.assertIn("종합판단", HTML)
        self.assertIn("AI Shadow 분석", HTML)
        self.assertIn("수급 Shadow 점수", HTML)
        self.assertIn("supply-shadow-scores", HTML)
        self.assertIn("자동 등록·수집 시작", HTML)
        self.assertIn("무인 모의투자 시작", HTML)
        self.assertIn('id="small-account-aggressive"', HTML)
        self.assertIn("small_account_aggressive", HTML)
        self.assertIn("small-account-mode", HTML)
        self.assertIn("strategy-ab-active-mode", HTML)
        self.assertIn("strategy_ab_active_mode", HTML)
        self.assertIn("strategy-ab-mode", HTML)
        self.assertIn("실시간 자동매매 흐름", HTML)
        self.assertIn("automation-summary", HTML)
        self.assertIn("automation-signals", HTML)
        self.assertIn("automation-orders", HTML)
        self.assertIn("operation-warning-badge", HTML)
        self.assertIn("operation-warning-summary", HTML)
        self.assertIn("renderOperationWarning", HTML)
        self.assertIn("전략 복기 요약", HTML)
        self.assertIn("최근 복기 상세", HTML)
        self.assertIn("replay-review-rows", HTML)
        self.assertIn("대장주 학습 리포트", HTML)
        self.assertIn("현재 장중 모드", HTML)
        self.assertIn("주문 안전장치 상태", HTML)
        self.assertIn("API 예산 상태", HTML)
        self.assertIn("현재 우선순위", HTML)
        self.assertIn("예산 부족 스킵", HTML)
        self.assertIn("backoff 차단", HTML)
        self.assertIn("dry-run 차단 사유", HTML)
        self.assertIn("strategy-summary", HTML)
        self.assertIn("패턴", HTML)
        self.assertIn("뉴스", HTML)
        self.assertNotIn("toggleStrategyDetail", HTML)

    def test_status_reports_intraday_mode_leader_learning_and_order_safety(self) -> None:
        self.database.save_leader_candidate_outcomes(
            [
                {
                    "trade_date": "2026-06-19",
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "candidate_rank": 1,
                    "role_at_pick": "leader",
                    "score_at_pick": 91.0,
                    "theme_code": "101",
                    "theme_name": "반도체",
                    "components": {
                        "trade_amount_score": 35.0,
                        "rank_score": 15.0,
                        "theme_score": 20.0,
                        "persistence_score": 5.0,
                        "chart_score": 10.0,
                        "risk_score": 0.0,
                        "blocked": False,
                    },
                    "same_day_reference_price": 100,
                    "same_day_open_price": 100,
                    "same_day_high_price": 106,
                    "same_day_low_price": 98,
                    "same_day_close_price": 104,
                    "same_day_volume": 1000,
                    "same_day_max_up_rate": 0.06,
                    "same_day_close_rate": 0.04,
                    "next_day_date": "2026-06-22",
                    "next_day_open_price": 104,
                    "next_day_high_price": 108,
                    "next_day_low_price": 103,
                    "next_day_close_price": 107,
                    "next_day_volume": 900,
                    "next_day_max_up_rate": 0.08,
                    "next_day_close_rate": 0.07,
                    "actual_leader": True,
                    "actual_leader_rank": 1,
                    "actual_leader_reason": "TOP20 성과 1위",
                    "outcome": {"performance_score": 0.08},
                }
            ]
        )
        self.database.save_order_safety_event(
            account_name="default",
            stock_code="A005930",
            execution_mode="dry_run",
            event_type="pre_order_recheck_failed",
            severity="warning",
            passed=False,
            reason="주문 직전 가격 괴리 3% 초과",
            context={"price_drift_rate": 0.05},
        )
        self.database.upsert_api_budget_window(
            api_group="order_book",
            api_id="ka10004",
            endpoint="/api/dostk/mrkcond",
            window_started_at="2026-06-22T09:10:00",
            window_seconds=60,
            limit_count=120,
            used_count=10,
            backoff_until="2026-06-22T09:11:00",
            updated_at="2026-06-22T09:10:30",
        )
        self.database.save_api_budget_event(
            decision=ApiBudgetDecision(
                allowed=True,
                api_group="order_book",
                api_id="ka10004",
                endpoint="/api/dostk/mrkcond",
                stock_code="A005930",
                priority=ApiCallPriority.HIGH,
                consumed=True,
                blocked=False,
                reason="http_429_backoff",
                limit_count=120,
                used_count=10,
                remaining_count=110,
                window_started_at="2026-06-22T09:10:00",
                backoff_seconds=30,
            ),
            status_code=429,
        )
        self.database.save_api_budget_event(
            decision=ApiBudgetDecision(
                allowed=False,
                api_group="order_book",
                api_id="ka10004",
                endpoint="/api/dostk/mrkcond",
                stock_code="A000660",
                priority=ApiCallPriority.LOW,
                consumed=False,
                blocked=True,
                reason="blocked_by_backoff",
                limit_count=120,
                used_count=10,
                remaining_count=110,
                window_started_at="2026-06-22T09:10:00",
                backoff_seconds=20,
            ),
            status_code=None,
        )
        self.database.save_api_budget_event(
            decision=ApiBudgetDecision(
                allowed=False,
                api_group="trade_strength",
                api_id="ka10003",
                endpoint="/api/dostk/stkinfo",
                stock_code="A035420",
                priority=ApiCallPriority.LOW,
                consumed=False,
                blocked=True,
                reason="skipped_by_budget",
                limit_count=120,
                used_count=120,
                remaining_count=0,
                window_started_at="2026-06-22T09:10:00",
                backoff_seconds=0,
            ),
            status_code=None,
        )

        status = self.service.status()

        self.assertIn("intraday_mode", status["strategy"])
        self.assertIn("entry_allowed", status["strategy"]["intraday_mode"])
        report = status["leader_themes"]["learning_report"]
        self.assertEqual(report["summary"]["candidate_count"], 1)
        self.assertEqual(report["summary"]["actual_leader_count"], 1)
        self.assertEqual(report["by_theme"][0]["theme_name"], "반도체")
        safety = status["strategy"]["order_safety"]
        self.assertEqual(safety["summary"]["total_events"], 1)
        self.assertEqual(safety["summary"]["recent_failures"], 1)
        self.assertEqual(safety["events"][0]["execution_mode"], "dry_run")
        self.assertIn("가격 괴리", safety["events"][0]["reason"])
        budget = status["strategy"]["api_budget"]
        self.assertEqual(budget["summary"]["window_count"], 1)
        self.assertEqual(budget["summary"]["backoff_groups"], 1)
        self.assertEqual(budget["summary"]["recent_blocked"], 2)
        self.assertEqual(budget["summary"]["recent_budget_skipped"], 1)
        self.assertEqual(budget["summary"]["recent_backoff_blocked"], 1)
        self.assertEqual(budget["summary"]["recent_429"], 1)
        self.assertIn("current_priority_mode", budget["summary"])
        self.assertIn("data_quality", budget["summary"])
        self.assertGreaterEqual(
            budget["summary"]["data_quality"]["warning_count"],
            1,
        )
        warning = status["strategy"]["operation_warning"]
        self.assertEqual(warning["api_budget"]["recent_429"], 1)
        self.assertEqual(warning["api_budget"]["backoff_groups"], 1)
        self.assertGreaterEqual(warning["data_quality"]["warning_count"], 1)
        self.assertEqual(warning["dry_run"]["count"], 1)
        self.assertIn(warning["level"], {"warn", "blocked"})
        self.assertEqual(budget["summary"]["backoff_apis"][0]["api_id"], "ka10004")
        self.assertEqual(budget["windows"][0]["api_group"], "order_book")

    def test_status_reports_operation_warning_for_daily_risk_state(self) -> None:
        today = datetime.now().date().isoformat()
        timestamp = f"{today}T09:20:00"
        with self.database.connect() as connection:
            account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, realized_pnl,
                    created_at, updated_at
                )
                VALUES ('default', 1000000, 850000, -150000, ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
            buy_order_id = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    quantity, status, reason, requested_at, completed_at
                )
                VALUES (?, 'buy-risk', 'A005930', 'buy', 10,
                        'filled', 'risk test', ?, ?)
                """,
                (account_id, timestamp, timestamp),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, 'A005930', 'buy', 10, 1000, 1000, 10000,
                        0, 0, 0, 0, ?, ?)
                """,
                (buy_order_id, timestamp, timestamp),
            )
            sell_order_id = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side,
                    quantity, status, reason, requested_at, completed_at
                )
                VALUES (?, 'sell-risk', 'A005930', 'sell', 10,
                        'filled', 'risk test', ?, ?)
                """,
                (account_id, f"{today}T09:25:00", f"{today}T09:25:00"),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, 'A005930', 'sell', 10, 850, 850, 8500,
                        0, 0, 0, -150000, ?, ?)
                """,
                (sell_order_id, f"{today}T09:25:00", f"{today}T09:25:00"),
            )
            for index in range(3):
                connection.execute(
                    """
                    INSERT INTO paper_risk_events(
                        account_id, stock_code, event_type, trigger_price,
                        threshold_price, highest_price, order_id, status,
                        message, occurred_at
                    )
                    VALUES (?, 'A005930', 'stop_loss', 850, 900, 1000,
                            ?, 'executed', 'risk test', ?)
                    """,
                    (
                        account_id,
                        sell_order_id,
                        f"{today}T09:2{index}:00",
                    ),
                )
        self.database.save_order_safety_event(
            account_name="default",
            stock_code="A005930",
            execution_mode="dry_run",
            event_type="pre_order_recheck_failed",
            severity="warning",
            passed=False,
            reason="test dry-run block",
            context={},
        )

        warning = self.service.status()["strategy"]["operation_warning"]

        self.assertEqual(warning["today_new_buys"]["count"], 1)
        self.assertEqual(warning["stop_losses"]["count"], 3)
        self.assertFalse(warning["stop_losses"]["blocked"])
        self.assertEqual(warning["daily_pnl"]["realized"], -150000)
        self.assertTrue(warning["daily_pnl"]["blocked"])
        self.assertEqual(warning["dry_run"]["count"], 1)
        self.assertIn(warning["level"], {"warn", "blocked"})
        self.assertIn(
            "daily_loss_limit",
            {reason["code"] for reason in warning["reasons"]},
        )

    def test_status_reports_supply_shadow_policy_and_scores(self) -> None:
        self.database.save_supply_score(
            {
                "stock_code": "A005930",
                "stock_name": "삼성전자",
                "evaluated_at": "2026-06-21T10:00:00",
                "total_score": 25.0,
                "broker_score": -50.0,
                "foreign_broker_score": -40.0,
                "investor_score": -35.0,
                "program_score": 0.0,
                "blocked": True,
                "blocked_reason": "상위 거래원 매도 압력",
                "reasons": ["상위 거래원 매도 우위"],
                "components": {
                    "program": {
                        "status": "not_collected",
                        "reason": "키움 REST 프로그램 매매 전용 TR 미확정",
                    }
                },
            }
        )

        supply = self.service.status()["strategy"]["supply_shadow"]

        self.assertEqual(supply["policy"]["label"], "Shadow")
        self.assertEqual(supply["policy"]["live_order_effect"], "실전 주문 영향 없음")
        self.assertEqual(
            supply["policy"]["paper_order_effect"],
            "SQLite 모의 신규 진입 차단 가능",
        )
        self.assertEqual(supply["rows"][0]["stock_code"], "A005930")
        self.assertTrue(supply["rows"][0]["blocked"])
        self.assertEqual(supply["rows"][0]["program_status"], "not_collected")

    def test_status_reports_thirty_day_theme_trends(self) -> None:
        today = datetime.now().date().isoformat()
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        self.database.upsert_theme_journal(
            theme_date=today,
            theme_name="반도체",
            strength="strong",
            members=[
                {
                    "stock_code": "A000660",
                    "stock_name": "SK하이닉스",
                    "role": "leader",
                }
            ],
        )
        self.database.upsert_theme_journal(
            theme_date=yesterday,
            theme_name="반도체",
            strength="medium",
            members=[
                {
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "role": "second",
                }
            ],
        )
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": today,
                    "stock_code": "A000660",
                    "stock_name": "SK하이닉스",
                    "theme_code": "101",
                    "theme_name": "반도체",
                    "role": "leader",
                    "total_score": 95.0,
                    "rank_score": 20.0,
                    "trade_amount_score": 35.0,
                    "theme_score": 20.0,
                    "persistence_score": 12.0,
                    "chart_score": 8.0,
                    "risk_score": 0.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["test"],
                    "components": {},
                }
            ],
            scored_at=datetime.now(),
        )

        trends = self.service.status()["theme_trends"]
        top = trends["items"][0]

        self.assertEqual(trends["days"], 30)
        self.assertEqual(top["theme_name"], "반도체")
        self.assertEqual(top["appearance_days"], 2)
        self.assertEqual(top["journal_days"], 2)
        self.assertEqual(top["score_days"], 1)
        self.assertEqual(top["strong_days"], 1)
        self.assertEqual(top["leader_count"], 1)
        self.assertGreater(top["trust_score"], 0)

    def test_status_reports_realtime_automation_flow(self) -> None:
        timestamp = f"{datetime.now().date().isoformat()}T09:10:00"
        with self.database.connect() as connection:
            account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, created_at, updated_at
                )
                VALUES ('default', 10000000, 9930000, ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO stock_universe(
                    stock_code, stock_name, source, updated_at
                )
                VALUES ('A005930', '삼성전자', 'test', ?)
                """,
                (timestamp,),
            )
            order_id = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side, quantity,
                    status, reason, requested_at, completed_at
                )
                VALUES (?, 'flow-test', 'A005930', 'buy', 1, 'filled',
                        '테스트 주문', ?, ?)
                """,
                (account_id, timestamp, timestamp),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, price_time, filled_at
                )
                VALUES (?, 'A005930', 'buy', 1, 70000, 70000, 70000,
                        0, 0, 0, ?, ?)
                """,
                (order_id, timestamp, timestamp),
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
                VALUES (?, 'A005930', ?, 70000, 69000, 68000, 2000,
                        1000, 2.0, 65000, 0.03, 1, 1, 1, 1, 1,
                        'executed', ?, '매수 조건 통과', ?)
                """,
                (account_id, timestamp, order_id, timestamp),
            )
            connection.execute(
                """
                INSERT INTO operation_runs(
                    run_type, status, started_at, completed_at,
                    success_count, failure_count, message
                )
                VALUES ('periodic_collect', 'success', ?, ?, 1, 0, 'ok')
                """,
                (timestamp, timestamp),
            )

        flow = self.service.status()["automation_flow"]

        self.assertEqual(flow["signal_counts"]["total"], 1)
        self.assertEqual(flow["signal_counts"]["passed"], 1)
        self.assertEqual(flow["order_counts"]["total"], 1)
        self.assertEqual(flow["order_counts"]["filled"], 1)
        self.assertEqual(flow["recent_signals"][0]["stock_name"], "삼성전자")
        self.assertEqual(flow["recent_orders"][0]["fill_price"], 70000)

    def test_status_reports_paper_performance(self) -> None:
        timestamp = f"{datetime.now().date().isoformat()}T09:30:00"
        with self.database.connect() as connection:
            account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, realized_pnl,
                    created_at, updated_at
                )
                VALUES ('default', 10000000, 9900000, 3000, ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO stock_universe(
                    stock_code, stock_name, source, updated_at
                )
                VALUES ('A005930', '삼성전자', 'test', ?),
                       ('A000660', 'SK하이닉스', 'test', ?)
                """,
                (timestamp, timestamp),
            )
            snapshot_id = connection.execute(
                """
                INSERT INTO api_snapshots(api_id, stock_code, collected_at, payload_json)
                VALUES ('test', 'A000660', ?, '{}')
                """,
                (timestamp,),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO price_snapshots(
                    stock_code, collected_at, current_price, raw_snapshot_id
                )
                VALUES ('A000660', ?, 10500, ?)
                """,
                (timestamp, snapshot_id),
            )
            connection.execute(
                """
                INSERT INTO paper_positions(
                    account_id, stock_code, quantity, cost_basis, updated_at
                )
                VALUES (?, 'A000660', 10, 100000, ?)
                """,
                (account_id, timestamp),
            )
            order_id = connection.execute(
                """
                INSERT INTO paper_orders(
                    account_id, client_order_id, stock_code, side, quantity,
                    status, reason, requested_at, completed_at
                )
                VALUES (?, 'sell-test', 'A005930', 'sell', 1, 'filled',
                        '익절', ?, ?)
                """,
                (account_id, timestamp, timestamp),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, stock_code, side, quantity, market_price,
                    fill_price, gross_amount, commission, tax,
                    slippage_amount, realized_pnl, price_time, filled_at
                )
                VALUES (?, 'A005930', 'sell', 1, 73000, 73000, 73000,
                        0, 0, 0, 3000, ?, ?)
                """,
                (order_id, timestamp, timestamp),
            )

        performance = self.service.status()["paper_performance"]

        self.assertEqual(performance["summary"]["realized_pnl"], 3000)
        self.assertEqual(performance["summary"]["unrealized_pnl"], 5000)
        self.assertEqual(performance["summary"]["total_pnl"], 8000)
        self.assertEqual(performance["summary"]["winning_trades"], 1)
        self.assertEqual(performance["last_closed_trade"]["stock_name"], "삼성전자")
        self.assertEqual(performance["positions"][0]["stock_name"], "SK하이닉스")
        self.assertEqual(performance["positions"][0]["return_rate"], 0.05)

    def test_status_reports_trade_type_performance(self) -> None:
        timestamp = f"{datetime.now().date().isoformat()}T09:00:00"
        with self.database.connect() as connection:
            account_id = connection.execute(
                """
                INSERT INTO paper_accounts(
                    name, initial_cash, cash_balance, realized_pnl,
                    created_at, updated_at
                )
                VALUES ('default', 10000000, 10000000, 0, ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
            version = connection.execute(
                """
                SELECT v.id, c.id AS config_id
                FROM strategy_versions v
                JOIN strategy_configs c ON c.strategy_version_id = v.id
                WHERE v.strategy_name = 'breakout' AND c.active = 1
                """
            ).fetchone()

        def save_plan(stock_code: str, entry_type: str, decided_at: str) -> int:
            decision = StrategyDecision(
                strategy_name="breakout",
                stock_code=stock_code,
                decided_at=datetime.fromisoformat(decided_at),
                action=DecisionAction.BUY,
                score=Decimal("70"),
                confidence=Decimal("1"),
                reason="type performance test",
            )
            decision_id = self.database.save_strategy_decision(
                decision,
                account_name="default",
                strategy_version_id=version["id"],
                strategy_config_id=version["config_id"],
                source_data_time=decided_at,
                outcome="executed",
            )
            self.database.save_trade_plan(
                TradePlan(
                    decision_id=decision_id,
                    stock_code=stock_code,
                    entry_type=entry_type,
                    entry_tags=(entry_type,),
                    entry_price=10_000,
                    planned_quantity=10,
                    stop_loss_price=9_500,
                    stop_loss_reason="support break",
                    first_take_profit_price=10_500,
                    first_take_profit_reason="1R",
                    partial_exit_rate=Decimal("0.5"),
                    breakeven_price=10_000,
                    breakeven_trigger_price=10_500,
                    trailing_rule="breakeven after partial",
                    invalidation_reason="setup invalidated",
                    reward_risk_ratio=Decimal("1"),
                    setup={},
                )
            )
            with self.database.connect() as connection:
                return int(
                    connection.execute(
                        "SELECT id FROM trade_plans WHERE decision_id = ?",
                        (decision_id,),
                    ).fetchone()["id"]
                )

        def insert_sell(
            stock_code: str,
            client_order_id: str,
            filled_at: str,
            quantity: int,
            fill_price: int,
            realized_pnl: int,
        ) -> int:
            with self.database.connect() as connection:
                order_id = connection.execute(
                    """
                    INSERT INTO paper_orders(
                        account_id, client_order_id, stock_code, side, quantity,
                        status, reason, requested_at, completed_at
                    )
                    VALUES (?, ?, ?, 'sell', ?, 'filled', 'test', ?, ?)
                    """,
                    (
                        account_id,
                        client_order_id,
                        stock_code,
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
                    VALUES (?, ?, 'sell', ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                    """,
                    (
                        order_id,
                        stock_code,
                        quantity,
                        fill_price,
                        fill_price,
                        fill_price * quantity,
                        realized_pnl,
                        filled_at,
                        filled_at,
                    ),
                )
                return int(order_id)

        partial_plan_id = save_plan("A005930", "vwap_pullback", f"{datetime.now().date().isoformat()}T09:00:00")
        save_plan("A000660", "vwap_pullback", f"{datetime.now().date().isoformat()}T09:30:00")
        partial_order_id = insert_sell(
            "A005930",
            "partial",
            f"{datetime.now().date().isoformat()}T09:10:00",
            5,
            10_500,
            2_500,
        )
        insert_sell(
            "A005930",
            "breakeven",
            f"{datetime.now().date().isoformat()}T09:20:00",
            5,
            10_000,
            0,
        )
        insert_sell(
            "A000660",
            "loss",
            f"{datetime.now().date().isoformat()}T09:40:00",
            10,
            9_900,
            -1_000,
        )
        self.database.mark_trade_plan_partial_exit(
            partial_plan_id,
            quantity=5,
            order_id=partial_order_id,
            fill_price=10_500,
            executed_at=f"{datetime.now().date().isoformat()}T09:10:00",
        )
        self.database.mark_trade_plan_breakeven(
            partial_plan_id,
            activated_at=f"{datetime.now().date().isoformat()}T09:10:00",
        )

        rows = self.service.status()["paper_performance"]["type_performance"]

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["entry_type"], "vwap_pullback")
        self.assertEqual(row["trade_count"], 2)
        self.assertEqual(row["closed_trades"], 2)
        self.assertEqual(row["winning_trades"], 1)
        self.assertEqual(row["losing_trades"], 1)
        self.assertEqual(row["win_rate"], 0.5)
        self.assertEqual(row["average_profit"], 2500)
        self.assertEqual(row["average_loss"], -1000)
        self.assertEqual(row["profit_loss_ratio"], 2.5)
        self.assertEqual(row["expectancy"], 750)
        self.assertEqual(row["one_r_hit_rate"], 0.5)
        self.assertEqual(row["partial_breakeven_count"], 1)
        self.assertEqual(row["partial_breakeven_success"], 1)
        self.assertEqual(row["partial_breakeven_success_rate"], 1.0)

    def test_status_reports_strategy_replay_review(self) -> None:
        timestamp = f"{datetime.now().date().isoformat()}T10:00:00"
        with self.database.connect() as connection:
            version = connection.execute(
                """
                SELECT v.id, c.id AS config_id
                FROM strategy_versions v
                JOIN strategy_configs c ON c.strategy_version_id = v.id
                WHERE v.strategy_name = 'breakout' AND c.active = 1
                """
            ).fetchone()
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code="A005930",
            decided_at=datetime.fromisoformat(timestamp),
            action=DecisionAction.HOLD,
            score=Decimal("45"),
            confidence=Decimal("0.8"),
            reason="수급 차단 테스트",
        )
        decision_id = self.database.save_strategy_decision(
            decision,
            account_name=None,
            strategy_version_id=version["id"],
            strategy_config_id=version["config_id"],
            source_data_time=timestamp,
            outcome="skipped",
        )
        self.database.save_strategy_replay_snapshot(
            decision_id=decision_id,
            stock_code="A005930",
            evaluated_at=timestamp,
            action="hold",
            score=45.0,
            reason="수급 차단 테스트",
            leader_score={"role": "leader", "total_score": 90.0},
            supply_score={
                "total_score": 20.0,
                "blocked": True,
                "blocked_reason": "기관 연속 순매도",
            },
            chart_setup={"daily_setup": "breakout"},
            intraday_trigger={"trigger": "pullback"},
            block_reasons=["기관 연속 순매도"],
            candles=[
                {
                    "stock_code": "A005930",
                    "candle_type": "minute",
                    "interval_minutes": 5,
                    "candle_time": timestamp,
                    "open_price": 10000,
                    "high_price": 10100,
                    "low_price": 9900,
                    "close_price": 10000,
                    "volume": 1000,
                    "relative_position": "entry",
                }
            ],
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO decision_forward_outcomes(
                    decision_id, stock_code, horizon_minutes,
                    max_up_rate, max_down_rate, close_rate,
                    actual_order_status, actual_outcome,
                    details_json, evaluated_at
                )
                VALUES (?, 'A005930', 30, 0.045, -0.005, 0.03,
                        NULL, 'missed_rally_after_block',
                        '{"entry_price": 10000}', ?)
                """,
                (decision_id, timestamp),
            )

        review = self.service.status()["strategy"]["replay_review"]

        self.assertEqual(review["summary"]["total"], 1)
        self.assertEqual(review["summary"]["blocked_decisions"], 1)
        self.assertEqual(review["summary"]["missed_rally_after_block"], 1)
        self.assertEqual(review["by_block_reason"][0]["reason"], "기관 연속 순매도")
        self.assertEqual(review["rows"][0]["primary_outcome"]["horizon_minutes"], 30)
        self.assertEqual(review["rows"][0]["leader_score"]["role"], "leader")
        self.assertEqual(
            review["long_term_stats"]["by_supply_block_reason"][0]["key"],
            review["by_block_reason"][0]["reason"],
        )
        self.assertIn("A005930", review["filters"]["stock_codes"])
        self.assertIn("hold", review["filters"]["actions"])
        self.assertIn("기관 연속 순매도", review["filters"]["block_reasons"])
        self.assertEqual(review["filters"]["min_date"], timestamp[:10])
        self.assertEqual(review["filters"]["max_date"], timestamp[:10])

        filtered = self.service.replay_review(
            {
                "stock_code": ["A005930"],
                "start_date": [timestamp[:10]],
                "end_date": [timestamp[:10]],
                "action": ["hold"],
                "block_reason": ["기관 연속 순매도"],
                "limit": ["50"],
            }
        )
        self.assertEqual(filtered["summary"]["total"], 1)
        self.assertEqual(filtered["rows"][0]["decision_id"], decision_id)
        self.assertEqual(
            filtered["applied_filters"]["block_reason"],
            "기관 연속 순매도",
        )

        empty = self.service.replay_review(
            {"stock_code": ["A000660"], "limit": ["50"]}
        )
        self.assertEqual(empty["summary"]["total"], 0)
        self.assertEqual(empty["rows"], [])

    def test_replay_review_rejects_invalid_filter_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "시작일"):
            self.service.replay_review(
                {
                    "start_date": ["2026-06-25"],
                    "end_date": ["2026-06-24"],
                }
            )

    def test_status_reports_latest_real_account_snapshot(self) -> None:
        self.database.save_real_account_snapshot(
            {"balance": True},
            {"holdings": True},
            [
                {
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "quantity": 2,
                    "tradable_quantity": 2,
                    "current_price": 70000,
                    "purchase_price": 68000,
                    "purchase_amount": 136000,
                    "evaluation_amount": 140000,
                    "evaluation_profit_loss": 4000,
                    "profit_rate": 2.94,
                }
            ],
            {
                "deposit": 1_000_000,
                "total_profit_loss": 4_000,
                "estimated_assets": 1_140_000,
            },
            collected_at=datetime(2026, 6, 14, 10, 0),
        )
        self.database.save_real_order_executions(
            {"orders": True},
            [
                {
                    "order_number": "1",
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "trade_type": "매수",
                    "order_quantity": 2,
                    "executed_quantity": 2,
                    "order_time": "095500",
                    "status": "체결",
                }
            ],
            order_date="20260614",
            collected_at=datetime(2026, 6, 14, 10, 1),
        )

        real = self.service.status()["real_account"]

        self.assertEqual(real["snapshot"]["deposit"], 1_000_000)
        self.assertEqual(real["positions"][0]["stock_code"], "A005930")
        self.assertEqual(real["orders"][0]["status"], "체결")

    def test_status_links_recent_decision_and_order_result(self) -> None:
        with self.database.connect() as connection:
            version = connection.execute(
                """
                SELECT v.id, c.id AS config_id
                FROM strategy_versions v
                JOIN strategy_configs c ON c.strategy_version_id = v.id
                WHERE v.strategy_name = 'breakout' AND c.active = 1
                """
            ).fetchone()
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code="A005930",
            decided_at=datetime(2026, 6, 14, 10, 0),
            action=DecisionAction.BUY,
            score=Decimal("75"),
            confidence=Decimal("1"),
            reason="테스트 매수 판단",
            components=(
                ScoreComponent(
                    name="pattern",
                    score=Decimal("80"),
                    weight=Decimal("0.08"),
                    reason="컵앤핸들 패턴 가점",
                ),
                ScoreComponent(
                    name="information",
                    score=Decimal("50"),
                    weight=Decimal("0.03"),
                    reason="뉴스·공시 중립",
                ),
            ),
        )
        decision_id = self.database.save_strategy_decision(
            decision,
            account_name=None,
            strategy_version_id=version["id"],
            strategy_config_id=version["config_id"],
            source_data_time="2026-06-14T10:00:00",
        )
        self.database.save_trade_plan(
            TradePlan(
                decision_id=decision_id,
                stock_code="A005930",
                entry_type="volume_breakout",
                entry_tags=("breakout", "vwap_support"),
                entry_price=70_000,
                planned_quantity=1,
                stop_loss_price=68_000,
                stop_loss_reason="돌파 기준선 재이탈",
                first_take_profit_price=72_000,
                first_take_profit_reason="1R 도달 시 1차 반익절",
                partial_exit_rate=Decimal("0.5"),
                breakeven_price=70_000,
                breakeven_trigger_price=72_000,
                trailing_rule="1차 익절 후 본절 이동",
                invalidation_reason="돌파 기준선 재이탈",
                reward_risk_ratio=Decimal("1"),
                setup={
                    "breakout_pass": True,
                    "data_availability_policy": {
                        "supply_status": "missing",
                        "leader_status": "market_rank_proxy",
                        "minimum_probe": True,
                        "add_buy_policy": "blocked",
                    },
                },
            )
        )
        request = OrderRequest(
            account_name="default",
            stock_code="A005930",
            side=OrderSide.BUY,
            quantity=1,
            decision=decision,
            decision_id=decision_id,
            client_order_id="dashboard-test",
        )
        request_id = self.database.save_order_request(
            request,
            decision_id=decision_id,
            execution_mode="paper",
        )
        self.database.save_order_execution(
            request_id,
            status="filled",
            quantity=1,
            internal_order_id=10,
            fill_price=70_000,
        )

        recent = self.service.status()["strategy"]["recent_decisions"][0]

        self.assertEqual(recent["id"], decision_id)
        self.assertEqual(recent["order_status"], "filled")
        self.assertEqual(recent["source_data_time"], "2026-06-14T10:00:00")
        self.assertIn("pattern=6.4", recent["component_summary"])
        self.assertEqual(recent["components"][0]["name"], "pattern")
        self.assertEqual(recent["components"][0]["reason"], "컵앤핸들 패턴 가점")
        self.assertEqual(recent["trade_plan"]["entry_type"], "volume_breakout")
        self.assertEqual(recent["trade_plan"]["stop_loss_price"], 68_000)
        self.assertEqual(recent["trade_plan"]["entry_tags"], ["breakout", "vwap_support"])
        self.assertEqual(
            recent["data_availability_policy"]["supply_status"],
            "missing",
        )
        self.assertTrue(recent["data_availability_policy"]["minimum_probe"])
        self.assertEqual(
            recent["data_availability_policy"]["add_buy_policy"],
            "blocked",
        )

    def test_status_reports_dynamic_exit_state_for_paper_position(self) -> None:
        from config.settings import PaperTradingSettings, RiskSettings
        from paper.engine import PaperTradingEngine
        from risk.manager import RiskManager

        paper_settings = PaperTradingSettings(
            initial_cash=2_000_000,
            commission_rate=Decimal("0"),
            sell_tax_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
        )
        risk_settings = RiskSettings(
            stop_loss_rate=Decimal("0.02"),
            take_profit_rate=Decimal("0.04"),
            trailing_activation_rate=Decimal("0.03"),
            trailing_drawdown_rate=Decimal("0.015"),
            daily_max_loss_rate=Decimal("0.05"),
            max_consecutive_losses=3,
        )
        paper = PaperTradingEngine(
            self.database,
            paper_settings,
            risk_settings,
        )
        risk = RiskManager(self.database, paper, risk_settings)
        paper.create_account()
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 10_000},
            current_price=10_000,
            open_price=10_000,
            high_price=10_000,
            low_price=10_000,
            volume=100,
        )
        paper.place_market_order("default", "A005930", "buy", 10)
        self.database.save_price_snapshot(
            "test",
            "A005930",
            {"price": 10_200},
            current_price=10_200,
            open_price=10_200,
            high_price=10_200,
            low_price=10_200,
            volume=100,
        )
        risk.evaluate_account("default")
        decision = StrategyDecision(
            strategy_name="breakout",
            stock_code="A005930",
            decided_at=datetime.now(),
            action=DecisionAction.BUY,
            score=Decimal("80"),
            confidence=Decimal("1"),
            reason="dashboard partial exit state",
        )
        decision_id = self.database.save_strategy_decision(
            decision,
            account_name="default",
            strategy_version_id=risk.strategy_version_id,
            strategy_config_id=risk.strategy_config_id,
            source_data_time=datetime.now().isoformat(timespec="seconds"),
        )
        plan_id = self.database.save_trade_plan(
            TradePlan(
                decision_id=decision_id,
                stock_code="A005930",
                entry_type="vwap_pullback",
                entry_tags=("vwap_pullback",),
                entry_price=10_000,
                planned_quantity=10,
                stop_loss_price=9_700,
                stop_loss_reason="VWAP 이탈",
                first_take_profit_price=10_500,
                first_take_profit_reason="1차익절",
                partial_exit_rate=Decimal("0.5"),
                breakeven_price=10_000,
                breakeven_trigger_price=10_500,
                trailing_rule="1차익절 후 트레일링",
                invalidation_reason="VWAP 이탈",
                reward_risk_ratio=Decimal("1.5"),
                setup={
                    "partial_exit_strategy": "vwap_pullback",
                    "residual_exit_rule": "hold_to_second_target",
                    "second_take_profit_price": 11_000,
                },
            )
        )
        self.database.update_trade_plan_setup(
            plan_id,
            {
                "partial_exit_adjustment": {
                    "adjustment_type": "raise_residual_target",
                    "new_target": 11_200,
                },
                "adjusted_second_take_profit_price": 11_200,
                "second_take_profit_deferred": True,
                "residual_target_adjustment": {
                    "raised_target": 11_200,
                    "defer_second_target": True,
                },
                "vwap_exit_state": {
                    "state": "shakeout_recovered",
                    "reason": "1~2개 봉 이탈 후 VWAP 재회복",
                },
                "reentry_policy": {
                    "status": "locked_after_partial_exit",
                    "reason": "1차익절 후 같은 종목 즉시 재진입 금지",
                },
            },
        )
        self.database.save_reentry_lock(
            stock_code="A005930",
            lock_until="2999-01-01T15:10:00",
            reason="partial_take_profit_reentry_block",
        )

        position = self.service.status()["risk_positions"][0]

        self.assertEqual(position["stock_code"], "A005930")
        self.assertEqual(position["average_price"], 10_000)
        self.assertAlmostEqual(position["return_rate"], 0.02)
        self.assertEqual(position["initial_stop_price"], 9_800)
        self.assertEqual(position["current_stop_price"], 10_000)
        self.assertEqual(position["breakeven_active"], 1)
        partial_state = position["partial_exit_dashboard"]
        self.assertTrue(partial_state["target_raised"])
        self.assertEqual(partial_state["raised_target"], 11_200)
        self.assertTrue(partial_state["second_deferred"])
        self.assertEqual(partial_state["vwap_label"], "흔들림")
        self.assertTrue(partial_state["reentry_locked"])
        self.assertEqual(
            partial_state["reentry_lock_reason"],
            "partial_take_profit_reentry_block",
        )

    def test_status_reports_latest_candidate_scores(self) -> None:
        candles = []
        start = datetime(2026, 1, 1)
        for index in range(70):
            close = 10_000 + index * 10
            candles.append(
                {
                    "candle_time": (
                        start + timedelta(days=index)
                    ).isoformat(timespec="seconds"),
                    "open_price": close,
                    "high_price": close + 20,
                    "low_price": close - 20,
                    "close_price": close,
                    "volume": 200_000,
                    "trade_amount": close * 200_000,
                }
            )
        self.database.save_candles(
            "test",
            "A000660",
            {"test": True},
            candles,
            candle_type="daily",
        )
        CandidateScreeningEngine(
            self.database,
            CandidateSettings(
                minimum_history_bars=60,
                minimum_price=100,
                minimum_average_volume=1,
                minimum_average_trade_amount=1,
                maximum_daily_volatility=1,
                selected_count=10,
            ),
        ).scan(as_of=datetime(2026, 3, 11))

        candidates = self.service.status()["candidates"]

        self.assertIsNotNone(candidates["run"])
        self.assertEqual(candidates["rows"][0]["stock_code"], "A000660")
        self.assertEqual(candidates["rows"][0]["watched"], 0)

    def test_pattern_candidate_outside_top_thirty_is_still_visible(self) -> None:
        timestamp = "2026-06-14T14:00:00"
        with self.database.connect() as connection:
            run_id = connection.execute(
                """
                INSERT INTO candidate_runs(
                    as_of, universe_count, eligible_count, selected_count,
                    config_json, created_at
                )
                VALUES (?, 31, 31, 20, '{}', ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
            for rank in range(1, 32):
                code = f"A{rank:06d}"
                connection.execute(
                    """
                    INSERT INTO stock_universe(
                        stock_code, stock_name, source, updated_at
                    )
                    VALUES (?, ?, 'test', ?)
                    """,
                    (code, f"테스트{rank}", timestamp),
                )
                features = (
                    '{"bull_flag_state":"breakout_pending",'
                    '"bull_flag_score":69.71}'
                    if rank == 31
                    else "{}"
                )
                connection.execute(
                    """
                    INSERT INTO candidate_scores(
                        run_id, stock_code, rank, selected, total_score,
                        technical_score, pattern_score,
                        relative_strength_score, liquidity_score,
                        watched, patterns_json, features_json, reasons_json,
                        evaluated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, 0, '[]', ?, '[]', ?)
                    """,
                    (
                        run_id,
                        code,
                        rank,
                        int(rank <= 20),
                        float(100 - rank),
                        features,
                        timestamp,
                    ),
                )

        rows = self.service.status()["candidates"]["rows"]

        self.assertEqual(len(rows), 13)
        self.assertTrue(
            any(
                row["stock_code"] == "A000031"
                and row["features"]["bull_flag_state"] == "breakout_pending"
                for row in rows
            )
        )

    def test_auto_strategy_requires_existing_paper_account(self) -> None:
        with self.assertRaisesRegex(ValueError, "모의계좌 'default'가 없습니다"):
            self.service.start(
                {
                    "interval": 60,
                    "account": "default",
                    "auto_strategy": True,
                }
            )

    def test_start_without_strategy_passes_options_to_runtime(self) -> None:
        expected = {"pid": 1234}
        with patch.object(self.runtime, "start", return_value=expected) as start:
            result = self.service.start(
                {
                    "interval": 30,
                    "account": "default",
                    "auto_strategy": False,
                    "small_account_aggressive": True,
                    "strategy_ab_active_mode": "normal",
                    "with_depth": True,
                    "with_trades": True,
                }
            )

        self.assertEqual(result, expected)
        start.assert_called_once_with(
            interval=30,
            account="default",
            auto_strategy=False,
            small_account_aggressive=True,
            strategy_ab_active_mode="normal",
            with_depth=True,
            with_trades=True,
        )

    def test_runtime_rejects_second_start_while_first_is_starting(self) -> None:
        with patch.object(
            self.runtime,
            "status",
            return_value={
                "running": False,
                "starting": True,
                "stale_lock": False,
                "metadata": {"pid": 1234},
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "자동매매 시작 처리 중입니다. PID=1234",
            ):
                self.runtime.start()

    def test_runtime_unattended_command_enables_preopen_scan(self) -> None:
        process = SimpleNamespace(pid=4321)
        with patch(
            "operations.runtime_control.subprocess.Popen",
            return_value=process,
        ) as popen:
            metadata = self.runtime.start(
                interval=60,
                account="default",
                unattended=True,
                candidate_limit=20,
                small_account_aggressive=True,
                strategy_ab_active_mode="normal",
            )

        command = metadata["command"]
        self.assertIn("--unattended", command)
        self.assertIn("--preopen-scan", command)
        self.assertTrue(metadata["preopen_scan"])
        self.assertTrue(metadata["small_account_aggressive"])
        self.assertEqual(metadata["strategy_ab_active_mode"], "normal")
        env = popen.call_args.kwargs["env"]
        self.assertEqual(
            env["STRATEGY_SMALL_ACCOUNT_AGGRESSIVE_ENABLED"],
            "true",
        )
        self.assertEqual(env["STRATEGY_AB_ACTIVE_MODE"], "normal")
        popen.assert_called_once()

    def test_unattended_start_scans_candidates_and_enables_safe_options(self) -> None:
        from config.settings import PaperTradingSettings, RiskSettings
        from paper.engine import PaperTradingEngine

        paper = PaperTradingEngine(
            self.database,
            PaperTradingSettings(
                initial_cash=10_000_000,
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
        paper.create_account()
        candles = []
        start = datetime(2026, 1, 1)
        for index in range(70):
            close = 10_000 + index * 10
            candles.append(
                {
                    "candle_time": (
                        start + timedelta(days=index)
                    ).isoformat(timespec="seconds"),
                    "open_price": close,
                    "high_price": close + 20,
                    "low_price": close - 20,
                    "close_price": close,
                    "volume": 200_000,
                    "trade_amount": close * 200_000,
                }
            )
        self.database.save_candles(
            "test",
            "A000660",
            {"test": True},
            candles,
            candle_type="daily",
        )
        expected = {"pid": 4321}
        settings = SimpleNamespace(
            app_key="key",
            secret_key="secret",
            use_mock=False,
            token_cache_path=None,
            request_timeout=10,
        )
        with (
            patch(
                "dashboard.server.load_settings",
                return_value=settings,
            ),
            patch.object(
                self.service,
                "_preflight_market_api",
                return_value={
                    "token": "bootstrap-token",
                    "expires_dt": "20260616090000",
                },
            ) as preflight,
            patch.object(self.runtime, "start", return_value=expected) as start_call,
        ):
            result = self.service.start_unattended(
                {
                    "interval": 60,
                    "account": "default",
                    "candidate_limit": 10,
                    "small_account_aggressive": True,
                    "strategy_ab_active_mode": "normal",
                }
            )

        self.assertEqual(result, expected)
        preflight.assert_called_once_with(settings, "A000660")
        start_call.assert_called_once_with(
            interval=60,
            account="default",
            auto_strategy=True,
            with_depth=True,
            with_trades=False,
            unattended=True,
            candidate_limit=10,
            preopen_scan=True,
            small_account_aggressive=True,
            strategy_ab_active_mode="normal",
            bootstrap_token="bootstrap-token",
            bootstrap_expires_dt="20260616090000",
        )

    def test_unattended_start_stops_when_api_preflight_fails(self) -> None:
        client = SimpleNamespace(
            get_intraday_prices=Mock(side_effect=RuntimeError("wrong server")),
            token=None,
            expires_dt=None,
        )
        with patch("dashboard.server.KiwoomRestClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "시작 전 API 점검 실패"):
                self.service._preflight_market_api(
                    SimpleNamespace(
                        app_key="wrong",
                        secret_key="wrong",
                        use_mock=True,
                        token_cache_path=None,
                        request_timeout=1,
                    ),
                    "A005930",
                )

    def test_stop_request_file_is_persistent(self) -> None:
        self.runtime.stop_path.write_text("requested", encoding="ascii")

        self.assertTrue(self.runtime.stop_requested())
        self.runtime.clear_stop_request()
        self.assertFalse(self.runtime.stop_requested())


if __name__ == "__main__":
    unittest.main()
