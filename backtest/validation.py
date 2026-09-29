from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from data.watchlist import normalize_stock_code
from db.database import Database


MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)


@dataclass(frozen=True)
class DataValidationIssue:
    severity: str
    code: str
    stock_code: str
    message: str
    candle_day: str | None = None


@dataclass(frozen=True)
class StockDataSummary:
    stock_code: str
    first_day: str | None
    last_day: str | None
    trading_days: int
    minute_bars: int
    daily_bars: int
    minimum_day_bars: int
    maximum_day_bars: int


@dataclass(frozen=True)
class DataValidationResult:
    start_date: date
    end_date: date
    interval_minutes: int
    summaries: tuple[StockDataSummary, ...]
    issues: tuple[DataValidationIssue, ...]

    @property
    def error_count(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    @property
    def is_ready(self) -> bool:
        return self.error_count == 0


class BacktestDataValidator:
    def __init__(self, database: Database) -> None:
        self.database = database

    def validate(
        self,
        stock_codes: list[str],
        *,
        start_date: date,
        end_date: date,
        interval_minutes: int,
        moving_average_period: int,
    ) -> DataValidationResult:
        if start_date > end_date:
            raise ValueError("검사 시작일은 종료일보다 늦을 수 없습니다.")
        if interval_minutes < 1:
            raise ValueError("검사 분봉 간격은 1 이상이어야 합니다.")
        if moving_average_period < 1:
            raise ValueError("이동평균 기간은 1 이상이어야 합니다.")
        codes = sorted({normalize_stock_code(code) for code in stock_codes})
        if not codes:
            raise ValueError("검사할 종목을 1개 이상 지정해야 합니다.")

        minute_rows = self.database.list_candles(
            codes,
            candle_type="minute",
            interval_minutes=interval_minutes,
            start_time=f"{start_date.isoformat()}T00:00:00",
            end_time=f"{end_date.isoformat()}T23:59:59",
        )
        daily_rows = self.database.list_candles(
            codes,
            candle_type="daily",
            end_time=f"{end_date.isoformat()}T23:59:59",
        )
        minute_by_stock_day: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in minute_rows:
            candle_day = row["candle_time"][:10]
            minute_by_stock_day[(row["stock_code"], candle_day)].append(dict(row))
        daily_by_stock: dict[str, list[dict]] = defaultdict(list)
        for row in daily_rows:
            daily_by_stock[row["stock_code"]].append(dict(row))

        issues: list[DataValidationIssue] = []
        summaries: list[StockDataSummary] = []
        observed_days: dict[str, set[str]] = {}
        for stock_code in codes:
            day_rows = {
                day: rows
                for (code, day), rows in minute_by_stock_day.items()
                if code == stock_code
            }
            observed_days[stock_code] = set(day_rows)
            daily_count = len(daily_by_stock.get(stock_code, []))
            if not day_rows:
                issues.append(
                    DataValidationIssue(
                        severity="error",
                        code="missing_minute_data",
                        stock_code=stock_code,
                        message="지정 기간에 분봉 데이터가 없습니다.",
                    )
                )
                summaries.append(
                    StockDataSummary(
                        stock_code=stock_code,
                        first_day=None,
                        last_day=None,
                        trading_days=0,
                        minute_bars=0,
                        daily_bars=daily_count,
                        minimum_day_bars=0,
                        maximum_day_bars=0,
                    )
                )
                continue

            counts = [len(rows) for rows in day_rows.values()]
            sorted_days = sorted(day_rows)
            summaries.append(
                StockDataSummary(
                    stock_code=stock_code,
                    first_day=sorted_days[0],
                    last_day=sorted_days[-1],
                    trading_days=len(sorted_days),
                    minute_bars=sum(counts),
                    daily_bars=daily_count,
                    minimum_day_bars=min(counts),
                    maximum_day_bars=max(counts),
                )
            )
            for candle_day, rows in sorted(day_rows.items()):
                self._validate_day(
                    stock_code,
                    candle_day,
                    rows,
                    interval_minutes,
                    moving_average_period,
                    daily_by_stock.get(stock_code, []),
                    issues,
                )

        non_empty_day_sets = [
            days for days in observed_days.values() if days
        ]
        if len(non_empty_day_sets) > 1:
            ranges = {
                (
                    min(days),
                    max(days),
                )
                for days in non_empty_day_sets
            }
            if len(ranges) > 1:
                issues.append(
                    DataValidationIssue(
                        severity="warning",
                        code="stock_range_mismatch",
                        stock_code="*",
                        message="종목별 데이터 시작일 또는 종료일이 다릅니다.",
                    )
                )
            common_days = set.intersection(*non_empty_day_sets)
            union_days = set.union(*non_empty_day_sets)
            if common_days != union_days:
                issues.append(
                    DataValidationIssue(
                        severity="warning",
                        code="stock_day_mismatch",
                        stock_code="*",
                        message=(
                            "종목별 보유 거래일이 다릅니다: "
                            f"공통={len(common_days)}일, 전체={len(union_days)}일"
                        ),
                    )
                )

        return DataValidationResult(
            start_date=start_date,
            end_date=end_date,
            interval_minutes=interval_minutes,
            summaries=tuple(summaries),
            issues=tuple(issues),
        )

    @staticmethod
    def _validate_day(
        stock_code: str,
        candle_day: str,
        rows: list[dict],
        interval_minutes: int,
        moving_average_period: int,
        daily_rows: list[dict],
        issues: list[DataValidationIssue],
    ) -> None:
        times = [
            datetime.fromisoformat(row["candle_time"])
            for row in rows
        ]
        if len(times) < moving_average_period:
            issues.append(
                DataValidationIssue(
                    severity="warning",
                    code="insufficient_minute_bars",
                    stock_code=stock_code,
                    candle_day=candle_day,
                    message=(
                        f"분봉 {len(times)}개로 이동평균 "
                        f"{moving_average_period}개 계산이 불가능합니다."
                    ),
                )
            )
        if times[0].time() > MARKET_OPEN:
            issues.append(
                DataValidationIssue(
                    severity="warning",
                    code="late_day_start",
                    stock_code=stock_code,
                    candle_day=candle_day,
                    message=f"첫 분봉이 {times[0].strftime('%H:%M')}에 시작합니다.",
                )
            )
        if times[-1].time() < MARKET_CLOSE:
            issues.append(
                DataValidationIssue(
                    severity="warning",
                    code="early_day_end",
                    stock_code=stock_code,
                    candle_day=candle_day,
                    message=f"마지막 분봉이 {times[-1].strftime('%H:%M')}입니다.",
                )
            )
        expected_gap = timedelta(minutes=interval_minutes)
        missing_intervals = []
        for previous, current in zip(times, times[1:]):
            gap = current - previous
            if (
                gap != expected_gap
                and not BacktestDataValidator._is_closing_auction_gap(
                    previous,
                    current,
                )
            ):
                missing_intervals.append(
                    f"{previous.strftime('%H:%M')}->{current.strftime('%H:%M')}"
                )
        if missing_intervals:
            issues.append(
                DataValidationIssue(
                    severity="warning",
                    code="minute_gap",
                    stock_code=stock_code,
                    candle_day=candle_day,
                    message=(
                        f"{interval_minutes}분 간격이 아닌 구간 "
                        f"{len(missing_intervals)}개: "
                        + ", ".join(missing_intervals[:5])
                    ),
                )
            )

        has_previous_daily = any(
            row["candle_time"][:10] < candle_day
            for row in daily_rows
        )
        if not has_previous_daily:
            issues.append(
                DataValidationIssue(
                    severity="warning",
                    code="missing_previous_daily",
                    stock_code=stock_code,
                    candle_day=candle_day,
                    message="전일 고가·종가·거래량 계산용 이전 일봉이 없습니다.",
                )
            )

    @staticmethod
    def _is_closing_auction_gap(
        previous: datetime,
        current: datetime,
    ) -> bool:
        return (
            previous.date() == current.date()
            and time(15, 10) <= previous.time() <= time(15, 20)
            and current.time() == MARKET_CLOSE
        )
