import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from data.market_calendar import MarketCalendar, SEOUL_TIMEZONE
from db.database import Database


OPENING_DEADLINE = time(9, 10)
CLOSING_COVERAGE = time(15, 20)
MARKET_CLOSE = time(15, 30)


@dataclass(frozen=True)
class DailyCheckItem:
    code: str
    status: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class DailyCheckResult:
    target_date: date
    status: str
    trading_day: bool
    full_day_expected: bool
    checks: list[DailyCheckItem]
    summary: dict[str, Any]


class DailyOperationsChecker:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calendar = MarketCalendar(database)

    def check(
        self,
        target_date: date,
        *,
        now: Optional[datetime] = None,
        expected_interval_seconds: Optional[int] = None,
    ) -> DailyCheckResult:
        if expected_interval_seconds is not None and expected_interval_seconds < 1:
            raise ValueError("예상 수집 간격은 1초 이상이어야 합니다.")
        current = self._seoul_now(now)
        trading_day = self.calendar.is_trading_day(target_date)
        full_day_expected = (
            target_date < current.date()
            or (
                target_date == current.date()
                and current.time().replace(tzinfo=None) >= MARKET_CLOSE
            )
        )
        if not trading_day:
            return DailyCheckResult(
                target_date,
                "not_applicable",
                False,
                full_day_expected,
                [
                    DailyCheckItem(
                        "trading_day",
                        "skip",
                        "거래일이 아니므로 일일 운전 검증 대상이 아닙니다.",
                        {},
                    )
                ],
                self._summary(target_date),
            )
        if target_date > current.date() or (
            target_date == current.date()
            and current.time().replace(tzinfo=None) < OPENING_DEADLINE
        ):
            return DailyCheckResult(
                target_date,
                "in_progress",
                True,
                False,
                [
                    DailyCheckItem(
                        "waiting_for_open",
                        "pending",
                        "아직 일일 운전 검증 시작 시각 전입니다.",
                        {},
                    )
                ],
                self._summary(target_date),
            )

        runs = self._runs(target_date)
        price_rows = self._prices(target_date)
        events = self._event_counts(target_date)
        summary = self._summary(target_date)
        interval_seconds = (
            expected_interval_seconds
            or self._infer_interval(runs)
            or 60
        )
        checks = [
            self._check_collection_presence(runs),
            self._check_opening_coverage(runs, full_day_expected),
            self._check_closing_coverage(runs, full_day_expected),
            self._check_run_failures(runs),
            self._check_run_gaps(runs, interval_seconds),
            self._check_price_coverage(
                price_rows,
                full_day_expected,
                interval_seconds,
            ),
            self._check_automation(runs, events),
            self._check_late_buys(summary),
            self._check_open_positions(
                target_date,
                current.date(),
                full_day_expected,
                summary,
            ),
        ]
        status = self._overall_status(checks, full_day_expected)
        return DailyCheckResult(
            target_date,
            status,
            True,
            full_day_expected,
            checks,
            {
                **summary,
                "run_count": len(runs),
                "expected_interval_seconds": interval_seconds,
            },
        )

    def _runs(self, target_date: date) -> list[dict[str, Any]]:
        day = target_date.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, status, started_at, completed_at,
                       success_count, failure_count, metadata_json
                FROM operation_runs
                WHERE run_type = 'periodic_collect'
                  AND substr(started_at, 1, 10) = ?
                ORDER BY started_at, id
                """,
                (day,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def _prices(self, target_date: date) -> dict[str, list[datetime]]:
        day = target_date.isoformat()
        with self.database.connect() as connection:
            watchlist = [
                row["stock_code"]
                for row in connection.execute(
                    """
                    SELECT stock_code
                    FROM watchlist
                    WHERE enabled = 1
                    ORDER BY stock_code
                    """
                ).fetchall()
            ]
            rows = connection.execute(
                """
                SELECT stock_code, collected_at
                FROM price_snapshots
                WHERE substr(collected_at, 1, 10) = ?
                ORDER BY stock_code, collected_at, id
                """,
                (day,),
            ).fetchall()
        result = {stock_code: [] for stock_code in watchlist}
        for row in rows:
            if row["stock_code"] in result:
                result[row["stock_code"]].append(
                    datetime.fromisoformat(row["collected_at"])
                )
        return result

    def _event_counts(self, target_date: date) -> dict[str, int]:
        day = target_date.isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT module, event_type, COUNT(*) AS count
                FROM operation_events
                WHERE substr(occurred_at, 1, 10) = ?
                GROUP BY module, event_type
                """,
                (day,),
            ).fetchall()
        return {
            f"{row['module']}/{row['event_type']}": int(row["count"])
            for row in rows
        }

    def _summary(self, target_date: date) -> dict[str, Any]:
        day = target_date.isoformat()
        with self.database.connect() as connection:
            order_row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) AS filled,
                       SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
                       SUM(
                           CASE
                               WHEN side = 'buy'
                                AND substr(requested_at, 12, 8) >= '15:20:00'
                                AND status = 'filled'
                               THEN 1 ELSE 0
                           END
                       ) AS late_buys
                FROM paper_orders
                WHERE substr(requested_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            fill_row = connection.execute(
                """
                SELECT COUNT(*) AS count,
                       COALESCE(SUM(realized_pnl), 0) AS realized_pnl
                FROM paper_fills
                WHERE substr(filled_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            risk_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_risk_events
                WHERE substr(occurred_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()[0]
            signal_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM strategy_signals
                WHERE substr(signal_time, 1, 10) = ?
                """,
                (day,),
            ).fetchone()[0]
            open_positions = connection.execute(
                "SELECT COUNT(*) FROM paper_positions"
            ).fetchone()[0]
        return {
            "orders": int(order_row["total"] or 0),
            "filled_orders": int(order_row["filled"] or 0),
            "rejected_orders": int(order_row["rejected"] or 0),
            "late_buys": int(order_row["late_buys"] or 0),
            "fills": int(fill_row["count"] or 0),
            "realized_pnl": int(fill_row["realized_pnl"] or 0),
            "risk_events": int(risk_count),
            "strategy_signals": int(signal_count),
            "open_positions": int(open_positions),
        }

    @staticmethod
    def _check_collection_presence(
        runs: list[dict[str, Any]],
    ) -> DailyCheckItem:
        if runs:
            return DailyCheckItem(
                "collection_presence",
                "pass",
                f"주기 수집 실행 {len(runs)}건이 기록되었습니다.",
                {"run_count": len(runs)},
            )
        return DailyCheckItem(
            "collection_presence",
            "fail",
            "주기 수집 실행 기록이 없습니다.",
            {"run_count": 0},
        )

    @staticmethod
    def _check_opening_coverage(
        runs: list[dict[str, Any]],
        full_day_expected: bool,
    ) -> DailyCheckItem:
        if not runs:
            return DailyCheckItem("opening_coverage", "fail", "장 시작 기록이 없습니다.", {})
        first = datetime.fromisoformat(runs[0]["started_at"])
        covered = first.time() <= OPENING_DEADLINE
        status = "pass" if covered else ("fail" if full_day_expected else "warning")
        return DailyCheckItem(
            "opening_coverage",
            status,
            (
                f"첫 수집 시각은 {first.time().isoformat(timespec='seconds')}입니다."
                if covered
                else f"첫 수집이 09:10보다 늦습니다: {first.time().isoformat(timespec='seconds')}"
            ),
            {"first_run_at": runs[0]["started_at"]},
        )

    @staticmethod
    def _check_closing_coverage(
        runs: list[dict[str, Any]],
        full_day_expected: bool,
    ) -> DailyCheckItem:
        if not full_day_expected:
            return DailyCheckItem(
                "closing_coverage",
                "pending",
                "아직 장 마감 전이므로 종료 구간은 판정하지 않습니다.",
                {},
            )
        if not runs:
            return DailyCheckItem("closing_coverage", "fail", "장 마감 기록이 없습니다.", {})
        last = datetime.fromisoformat(runs[-1]["started_at"])
        covered = last.time() >= CLOSING_COVERAGE
        return DailyCheckItem(
            "closing_coverage",
            "pass" if covered else "fail",
            (
                f"마지막 수집 시각은 {last.time().isoformat(timespec='seconds')}입니다."
                if covered
                else f"마지막 수집이 15:20보다 이릅니다: {last.time().isoformat(timespec='seconds')}"
            ),
            {"last_run_at": runs[-1]["started_at"]},
        )

    @staticmethod
    def _check_run_failures(runs: list[dict[str, Any]]) -> DailyCheckItem:
        failed = [
            run
            for run in runs
            if run["status"] in {"failed", "partial_failure", "running"}
            or run["failure_count"] > 0
        ]
        return DailyCheckItem(
            "run_failures",
            "pass" if not failed else "fail",
            (
                "실패하거나 완료되지 않은 수집 실행이 없습니다."
                if not failed
                else f"실패·부분 실패·미완료 실행이 {len(failed)}건 있습니다."
            ),
            {"run_ids": [run["id"] for run in failed]},
        )

    @staticmethod
    def _check_run_gaps(
        runs: list[dict[str, Any]],
        interval_seconds: int,
    ) -> DailyCheckItem:
        times = [datetime.fromisoformat(run["started_at"]) for run in runs]
        maximum_gap = max(
            (
                int((current - previous).total_seconds())
                for previous, current in zip(times, times[1:])
            ),
            default=0,
        )
        limit = max(interval_seconds * 3, interval_seconds + 30)
        return DailyCheckItem(
            "collection_gaps",
            "pass" if maximum_gap <= limit else "fail",
            (
                f"최대 수집 공백은 {maximum_gap}초입니다."
                if maximum_gap <= limit
                else f"최대 수집 공백 {maximum_gap}초가 허용 {limit}초를 초과했습니다."
            ),
            {
                "maximum_gap_seconds": maximum_gap,
                "allowed_gap_seconds": limit,
            },
        )

    @staticmethod
    def _check_price_coverage(
        prices: dict[str, list[datetime]],
        full_day_expected: bool,
        interval_seconds: int,
    ) -> DailyCheckItem:
        issues = []
        details: dict[str, Any] = {}
        gap_limit = max(interval_seconds * 3, interval_seconds + 30)
        for stock_code, times in prices.items():
            if not times:
                issues.append(f"{stock_code}: 가격 없음")
                details[stock_code] = {"count": 0}
                continue
            maximum_gap = max(
                (
                    int((current - previous).total_seconds())
                    for previous, current in zip(times, times[1:])
                ),
                default=0,
            )
            opening_ok = times[0].time() <= OPENING_DEADLINE
            closing_ok = not full_day_expected or times[-1].time() >= CLOSING_COVERAGE
            gap_ok = maximum_gap <= gap_limit
            if not opening_ok:
                issues.append(f"{stock_code}: 장 시작 가격 지연")
            if not closing_ok:
                issues.append(f"{stock_code}: 장 마감 가격 부족")
            if not gap_ok:
                issues.append(f"{stock_code}: 가격 공백 {maximum_gap}초")
            details[stock_code] = {
                "count": len(times),
                "first_at": times[0].isoformat(timespec="seconds"),
                "last_at": times[-1].isoformat(timespec="seconds"),
                "maximum_gap_seconds": maximum_gap,
            }
        if not prices:
            return DailyCheckItem(
                "price_coverage",
                "warning",
                "활성 관심 종목이 없어 가격 범위를 검사하지 않았습니다.",
                {},
            )
        return DailyCheckItem(
            "price_coverage",
            "pass" if not issues else "fail",
            (
                f"활성 관심 종목 {len(prices)}개의 가격 범위가 정상입니다."
                if not issues
                else "; ".join(issues)
            ),
            details,
        )

    @staticmethod
    def _check_automation(
        runs: list[dict[str, Any]],
        events: dict[str, int],
    ) -> DailyCheckItem:
        if not runs:
            return DailyCheckItem(
                "automation",
                "fail",
                "수집 실행이 없어 자동화 후처리를 확인할 수 없습니다.",
                {},
            )
        risk_count = events.get("risk/risk_evaluated", 0)
        strategy_expected = any(
            bool(run["metadata"].get("auto_strategy")) for run in runs
        )
        strategy_count = events.get("strategy/strategy_evaluated", 0)
        minimum = max(1, math.ceil(len(runs) * 0.9))
        issues = []
        if risk_count < minimum:
            issues.append(f"리스크 평가 {risk_count}/{len(runs)}건")
        if strategy_expected and strategy_count < minimum:
            issues.append(f"전략 평가 {strategy_count}/{len(runs)}건")
        return DailyCheckItem(
            "automation",
            "pass" if not issues else "fail",
            (
                "자동 리스크·전략 후처리 기록이 충분합니다."
                if not issues
                else ", ".join(issues)
            ),
            {
                "risk_evaluations": risk_count,
                "strategy_expected": strategy_expected,
                "strategy_evaluations": strategy_count,
                "minimum_expected": minimum,
            },
        )

    @staticmethod
    def _check_late_buys(summary: dict[str, Any]) -> DailyCheckItem:
        count = summary["late_buys"]
        return DailyCheckItem(
            "late_buys",
            "pass" if count == 0 else "fail",
            (
                "15:20 이후 체결된 신규 매수가 없습니다."
                if count == 0
                else f"15:20 이후 신규 매수 {count}건이 체결되었습니다."
            ),
            {"late_buys": count},
        )

    @staticmethod
    def _check_open_positions(
        target_date: date,
        current_date: date,
        full_day_expected: bool,
        summary: dict[str, Any],
    ) -> DailyCheckItem:
        if not full_day_expected:
            return DailyCheckItem(
                "market_close_positions",
                "pending",
                "장 마감 이후에 잔여 포지션을 판정합니다.",
                {},
            )
        if target_date != current_date:
            return DailyCheckItem(
                "market_close_positions",
                "skip",
                "과거 날짜는 이후 거래로 현재 포지션이 바뀔 수 있어 판정하지 않습니다.",
                {},
            )
        count = summary["open_positions"]
        return DailyCheckItem(
            "market_close_positions",
            "pass" if count == 0 else "fail",
            (
                "장 마감 후 남은 모의 포지션이 없습니다."
                if count == 0
                else f"장 마감 후 모의 포지션 {count}개가 남아 있습니다."
            ),
            {"open_positions": count},
        )

    @staticmethod
    def _infer_interval(runs: list[dict[str, Any]]) -> Optional[int]:
        values = [
            int(run["metadata"]["interval_seconds"])
            for run in runs
            if run["metadata"].get("interval_seconds")
        ]
        return min(values) if values else None

    @staticmethod
    def _overall_status(
        checks: list[DailyCheckItem],
        full_day_expected: bool,
    ) -> str:
        if any(check.status == "fail" for check in checks):
            return "failed"
        if not full_day_expected or any(
            check.status == "pending" for check in checks
        ):
            return "in_progress"
        if any(check.status == "warning" for check in checks):
            return "warning"
        return "passed"

    @staticmethod
    def _seoul_now(now: Optional[datetime]) -> datetime:
        current = now or datetime.now(SEOUL_TIMEZONE)
        if current.tzinfo is None:
            return current.replace(tzinfo=SEOUL_TIMEZONE)
        return current.astimezone(SEOUL_TIMEZONE)
