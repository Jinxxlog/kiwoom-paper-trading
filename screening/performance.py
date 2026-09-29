from __future__ import annotations

from datetime import date, datetime
from typing import Any

from db.database import Database
from screening.features import ALL_PATTERNS, DailyBar, calculate_features


class PatternPerformanceService:
    def __init__(self, database: Database, *, minimum_history: int = 60) -> None:
        self.database = database
        self.minimum_history = minimum_history

    def run(
        self,
        *,
        start_date: date,
        end_date: date,
        horizon_days: int = 5,
    ) -> dict[str, Any]:
        if start_date > end_date:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        if horizon_days < 1:
            raise ValueError("성과 평가 기간은 1일 이상이어야 합니다.")
        by_stock = self._load_bars()
        occurrences = {name: [] for name in ALL_PATTERNS}
        baseline = []
        for bars in by_stock.values():
            for index in range(self.minimum_history - 1, len(bars) - horizon_days):
                current_date = date.fromisoformat(bars[index].candle_time[:10])
                if not start_date <= current_date <= end_date:
                    continue
                current = bars[index].close
                future = bars[index + horizon_days].close
                if current <= 0:
                    continue
                forward_return = future / current - 1
                baseline.append(forward_return)
                patterns = calculate_features(bars[: index + 1]).patterns
                for pattern in patterns:
                    occurrences[pattern].append(forward_return)
        baseline_average = _average(baseline)
        results = []
        for pattern in ALL_PATTERNS:
            values = occurrences[pattern]
            results.append(
                {
                    "pattern_name": pattern,
                    "occurrence_count": len(values),
                    "win_rate": (
                        sum(value > 0 for value in values) / len(values)
                        if values
                        else None
                    ),
                    "average_forward_return": _average(values) if values else None,
                    "contribution": (
                        _average(values) - baseline_average if values else None
                    ),
                }
            )
        run_id = self._save(
            start_date,
            end_date,
            horizon_days,
            len(by_stock),
            results,
        )
        return {
            "run_id": run_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "horizon_days": horizon_days,
            "stock_count": len(by_stock),
            "baseline_average_return": baseline_average,
            "results": results,
        }

    def _load_bars(self) -> dict[str, list[DailyBar]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT stock_code, candle_time, open_price, high_price,
                       low_price, close_price, volume, trade_amount
                FROM candles
                WHERE candle_type = 'daily'
                  AND interval_minutes = 0
                  AND open_price IS NOT NULL
                  AND high_price IS NOT NULL
                  AND low_price IS NOT NULL
                  AND close_price IS NOT NULL
                ORDER BY stock_code, candle_time, id
                """
            ).fetchall()
        result: dict[str, list[DailyBar]] = {}
        for row in rows:
            result.setdefault(row["stock_code"], []).append(
                DailyBar(
                    candle_time=row["candle_time"],
                    open=int(row["open_price"]),
                    high=int(row["high_price"]),
                    low=int(row["low_price"]),
                    close=int(row["close_price"]),
                    volume=int(row["volume"] or 0),
                    trade_amount=(
                        int(row["trade_amount"])
                        if row["trade_amount"] is not None
                        else None
                    ),
                )
            )
        return result

    def _save(
        self,
        start_date: date,
        end_date: date,
        horizon_days: int,
        stock_count: int,
        results: list[dict[str, Any]],
    ) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            run_id = int(
                connection.execute(
                    """
                    INSERT INTO pattern_performance_runs(
                        start_date, end_date, horizon_days, stock_count, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        start_date.isoformat(),
                        end_date.isoformat(),
                        horizon_days,
                        stock_count,
                        timestamp,
                    ),
                ).lastrowid
            )
            connection.executemany(
                """
                INSERT INTO pattern_performance_results(
                    run_id, pattern_name, occurrence_count, win_rate,
                    average_forward_return, contribution
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        row["pattern_name"],
                        row["occurrence_count"],
                        row["win_rate"],
                        row["average_forward_return"],
                        row["contribution"],
                    )
                    for row in results
                ],
            )
        return run_id


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
