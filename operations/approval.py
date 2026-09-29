import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from data.market_calendar import MarketCalendar, SEOUL_TIMEZONE
from db.database import Database
from operations.daily_check import DailyOperationsChecker


@dataclass(frozen=True)
class ApprovalCheck:
    code: str
    status: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class ApprovalResult:
    as_of_date: date
    decision: str
    reviewed_dates: list[date]
    checks: list[ApprovalCheck]
    manual_checklist: list[str]


MANUAL_CHECKLIST = [
    "실전 계좌번호와 모의·실전 서버 선택을 재확인한다.",
    "수수료·세금·호가단위와 주문 가능 시간을 증권사 기준으로 확인한다.",
    "첫 실전 운전의 총자금·종목당 한도·일일 손실 한도를 문서로 확정한다.",
    "프로그램 중지, 미체결 주문 취소, 포지션 수동 청산 절차를 준비한다.",
    "장중 경보를 확인할 담당 채널과 장 마감 리포트 검토 절차를 정한다.",
    "실제 주문 기능은 별도 코드 리뷰와 소액 단계 승인을 거쳐 활성화한다.",
]


class PaperOperationApprovalService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calendar = MarketCalendar(database)
        self.daily_checker = DailyOperationsChecker(database)

    def evaluate(
        self,
        *,
        required_days: int = 5,
        interval_seconds: Optional[int] = None,
        minimum_round_trips: int = 1,
        now: Optional[datetime] = None,
    ) -> ApprovalResult:
        if required_days < 1:
            raise ValueError("필수 검증 거래일은 1일 이상이어야 합니다.")
        if interval_seconds is not None and interval_seconds < 1:
            raise ValueError("수집 간격은 1초 이상이어야 합니다.")
        if minimum_round_trips < 0:
            raise ValueError("최소 왕복 거래 횟수는 0 이상이어야 합니다.")
        current = self._seoul_now(now)
        end_date = current.date()
        if current.time().replace(tzinfo=None) < time(15, 30):
            end_date -= timedelta(days=1)
        reviewed_dates = self._recent_trading_days(end_date, required_days)
        daily_results = [
            self.daily_checker.check(
                target_date,
                now=datetime.combine(
                    current.date(),
                    time(16, 0),
                    tzinfo=SEOUL_TIMEZONE,
                ),
                expected_interval_seconds=interval_seconds,
            )
            for target_date in reviewed_dates
        ]
        start_day = reviewed_dates[0].isoformat()
        end_day = reviewed_dates[-1].isoformat()
        metrics = self._metrics(start_day, end_day)
        checks = [
            self._daily_checks(reviewed_dates, daily_results),
            self._auto_strategy_check(metrics),
            self._active_alert_check(metrics),
            self._error_event_check(metrics),
            self._automation_failure_check(metrics),
            self._round_trip_check(metrics, minimum_round_trips),
            self._open_position_check(metrics),
        ]
        technical_passed = all(check.status == "pass" for check in checks)
        return ApprovalResult(
            as_of_date=current.date(),
            decision=(
                "ready_for_manual_approval"
                if technical_passed
                else "not_ready"
            ),
            reviewed_dates=reviewed_dates,
            checks=checks,
            manual_checklist=list(MANUAL_CHECKLIST),
        )

    def export_json(
        self,
        result: ApprovalResult,
        output: Optional[Path] = None,
    ) -> Path:
        path = output or Path("reports") / (
            f"paper-approval-{result.as_of_date.isoformat()}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "as_of_date": result.as_of_date.isoformat(),
            "decision": result.decision,
            "reviewed_dates": [
                value.isoformat() for value in result.reviewed_dates
            ],
            "checks": [
                {
                    "code": check.code,
                    "status": check.status,
                    "message": check.message,
                    "details": check.details,
                }
                for check in result.checks
            ],
            "manual_checklist": result.manual_checklist,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _recent_trading_days(
        self,
        end_date: date,
        count: int,
    ) -> list[date]:
        days = []
        cursor = end_date
        search_limit = count * 4 + 30
        for _ in range(search_limit):
            if self.calendar.is_trading_day(cursor):
                days.append(cursor)
                if len(days) == count:
                    return list(reversed(days))
            cursor -= timedelta(days=1)
        raise ValueError("필요한 거래일 범위를 계산할 수 없습니다.")

    def _metrics(self, start_day: str, end_day: str) -> dict[str, int]:
        with self.database.connect() as connection:
            run_row = connection.execute(
                """
                SELECT COUNT(*) AS run_count,
                       SUM(
                           CASE
                               WHEN json_extract(metadata_json, '$.auto_strategy')
                                    IN (1, 'true')
                               THEN 1 ELSE 0
                           END
                       ) AS auto_strategy_runs
                FROM operation_runs
                WHERE run_type = 'periodic_collect'
                  AND substr(started_at, 1, 10) BETWEEN ? AND ?
                """,
                (start_day, end_day),
            ).fetchone()
            active_alerts = connection.execute(
                """
                SELECT COUNT(*)
                FROM operation_alerts
                WHERE status = 'active'
                """
            ).fetchone()[0]
            error_events = connection.execute(
                """
                SELECT COUNT(*)
                FROM operation_events
                WHERE severity = 'error'
                  AND substr(occurred_at, 1, 10) BETWEEN ? AND ?
                """,
                (start_day, end_day),
            ).fetchone()[0]
            strategy_failures = connection.execute(
                """
                SELECT COUNT(*)
                FROM strategy_signals
                WHERE action = 'failed'
                  AND substr(signal_time, 1, 10) BETWEEN ? AND ?
                """,
                (start_day, end_day),
            ).fetchone()[0]
            risk_failures = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_risk_events
                WHERE status = 'failed'
                  AND substr(occurred_at, 1, 10) BETWEEN ? AND ?
                """,
                (start_day, end_day),
            ).fetchone()[0]
            buy_fills = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_fills
                WHERE side = 'buy'
                  AND substr(filled_at, 1, 10) BETWEEN ? AND ?
                """,
                (start_day, end_day),
            ).fetchone()[0]
            sell_fills = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_fills
                WHERE side = 'sell'
                  AND substr(filled_at, 1, 10) BETWEEN ? AND ?
                """,
                (start_day, end_day),
            ).fetchone()[0]
            executed_signals = connection.execute(
                """
                SELECT COUNT(*)
                FROM strategy_signals
                WHERE action = 'executed'
                  AND substr(signal_time, 1, 10) BETWEEN ? AND ?
                """,
                (start_day, end_day),
            ).fetchone()[0]
            open_positions = connection.execute(
                "SELECT COUNT(*) FROM paper_positions"
            ).fetchone()[0]
        return {
            "run_count": int(run_row["run_count"] or 0),
            "auto_strategy_runs": int(run_row["auto_strategy_runs"] or 0),
            "active_alerts": int(active_alerts),
            "error_events": int(error_events),
            "strategy_failures": int(strategy_failures),
            "risk_failures": int(risk_failures),
            "buy_fills": int(buy_fills),
            "sell_fills": int(sell_fills),
            "executed_signals": int(executed_signals),
            "open_positions": int(open_positions),
        }

    @staticmethod
    def _daily_checks(
        dates: list[date],
        results: list[Any],
    ) -> ApprovalCheck:
        failed = [
            target_date.isoformat()
            for target_date, result in zip(dates, results)
            if result.status != "passed"
        ]
        return ApprovalCheck(
            "daily_operations",
            "pass" if not failed else "fail",
            (
                f"최근 {len(dates)}거래일 일일 검증이 모두 통과했습니다."
                if not failed
                else f"일일 검증 미통과 날짜: {', '.join(failed)}"
            ),
            {
                "reviewed_dates": [value.isoformat() for value in dates],
                "failed_dates": failed,
            },
        )

    @staticmethod
    def _auto_strategy_check(metrics: dict[str, int]) -> ApprovalCheck:
        total = metrics["run_count"]
        automated = metrics["auto_strategy_runs"]
        passed = total > 0 and automated == total
        return ApprovalCheck(
            "auto_strategy_coverage",
            "pass" if passed else "fail",
            (
                f"주기 수집 {total}회 모두 자동전략으로 운전했습니다."
                if passed
                else f"자동전략 운전 비율이 부족합니다: {automated}/{total}회"
            ),
            {"run_count": total, "auto_strategy_runs": automated},
        )

    @staticmethod
    def _active_alert_check(metrics: dict[str, int]) -> ApprovalCheck:
        count = metrics["active_alerts"]
        return ApprovalCheck(
            "active_alerts",
            "pass" if count == 0 else "fail",
            "활성 운영 경보가 없습니다."
            if count == 0
            else f"활성 운영 경보가 {count}건 있습니다.",
            {"active_alerts": count},
        )

    @staticmethod
    def _error_event_check(metrics: dict[str, int]) -> ApprovalCheck:
        count = metrics["error_events"]
        return ApprovalCheck(
            "error_events",
            "pass" if count == 0 else "fail",
            "검증 기간에 오류 이벤트가 없습니다."
            if count == 0
            else f"검증 기간에 오류 이벤트가 {count}건 있습니다.",
            {"error_events": count},
        )

    @staticmethod
    def _automation_failure_check(metrics: dict[str, int]) -> ApprovalCheck:
        strategy = metrics["strategy_failures"]
        risk = metrics["risk_failures"]
        passed = strategy == 0 and risk == 0
        return ApprovalCheck(
            "automation_failures",
            "pass" if passed else "fail",
            (
                "전략과 리스크 자동화 실패가 없습니다."
                if passed
                else f"전략 실패={strategy}건, 리스크 실패={risk}건"
            ),
            {
                "strategy_failures": strategy,
                "risk_failures": risk,
            },
        )

    @staticmethod
    def _round_trip_check(
        metrics: dict[str, int],
        minimum_round_trips: int,
    ) -> ApprovalCheck:
        completed = min(metrics["buy_fills"], metrics["sell_fills"])
        passed = (
            completed >= minimum_round_trips
            and metrics["executed_signals"] >= minimum_round_trips
        )
        return ApprovalCheck(
            "trade_lifecycle",
            "pass" if passed else "fail",
            (
                f"자동전략 매수부터 매도까지 {completed}회 검증했습니다."
                if passed
                else (
                    f"매수·매도 전체 경로 검증이 부족합니다: "
                    f"완료={completed}회, 자동전략 실행={metrics['executed_signals']}회, "
                    f"필요={minimum_round_trips}회"
                )
            ),
            {
                "buy_fills": metrics["buy_fills"],
                "sell_fills": metrics["sell_fills"],
                "executed_signals": metrics["executed_signals"],
                "minimum_round_trips": minimum_round_trips,
            },
        )

    @staticmethod
    def _open_position_check(metrics: dict[str, int]) -> ApprovalCheck:
        count = metrics["open_positions"]
        return ApprovalCheck(
            "open_positions",
            "pass" if count == 0 else "fail",
            "현재 남아 있는 모의 포지션이 없습니다."
            if count == 0
            else f"현재 모의 포지션이 {count}개 남아 있습니다.",
            {"open_positions": count},
        )

    @staticmethod
    def _seoul_now(now: Optional[datetime]) -> datetime:
        current = now or datetime.now(SEOUL_TIMEZONE)
        if current.tzinfo is None:
            return current.replace(tzinfo=SEOUL_TIMEZONE)
        return current.astimezone(SEOUL_TIMEZONE)
