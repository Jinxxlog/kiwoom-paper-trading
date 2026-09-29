from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from data.collector import MarketDataCollector
from db.database import Database
from screening.market_product import is_market_product_name


@dataclass(frozen=True)
class BulkCollectionResult:
    attempted_count: int
    completed_count: int
    failed_count: int
    skipped_count: int
    available_history_completed_count: int
    stopped: bool


class UniverseDailyCollector:
    def __init__(
        self,
        database: Database,
        collector: MarketDataCollector,
    ) -> None:
        self.database = database
        self.collector = collector

    def collect(
        self,
        *,
        base_date: str,
        target_bars: int = 200,
        max_pages: int = 1,
        limit: int | None = None,
        delay_seconds: float = 0.5,
        stop_requested: Callable[[], bool] | None = None,
    ) -> BulkCollectionResult:
        if target_bars < 60:
            raise ValueError("일봉 목표 개수는 60 이상이어야 합니다.")
        if max_pages < 1:
            raise ValueError("종목별 수집 페이지 수는 1 이상이어야 합니다.")
        if limit is not None and limit < 1:
            raise ValueError("수집 종목 제한은 1 이상이어야 합니다.")
        if delay_seconds < 0:
            raise ValueError("API 요청 간격은 0 이상이어야 합니다.")
        codes = self._pending_codes(target_bars, limit)
        completed = failed = skipped = attempted = available_completed = 0
        for code in codes:
            if stop_requested is not None and stop_requested():
                return BulkCollectionResult(
                    attempted,
                    completed,
                    failed,
                    skipped,
                    available_completed,
                    True,
                )
            current_count = self._daily_count(code)
            if current_count >= target_bars:
                self._save_status(code, "completed", target_bars)
                skipped += 1
                continue
            attempted += 1
            self._save_status(code, "running", target_bars, increment_attempt=True)
            try:
                collected = self.collector.collect_daily_candles(
                    code,
                    base_date,
                    max_pages=max_pages,
                )
                final_count = self._daily_count(code)
                source_exhausted = (
                    final_count > 0
                    and not bool(getattr(collected, "has_more", True))
                )
                status = (
                    "completed"
                    if final_count >= target_bars or source_exhausted
                    else "pending"
                )
                self._save_status(
                    code,
                    status,
                    target_bars,
                    error=(
                        "available_history_exhausted"
                        if source_exhausted and final_count < target_bars
                        else None
                    ),
                )
                if status == "completed":
                    completed += 1
                    if source_exhausted and final_count < target_bars:
                        available_completed += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                self._save_status(
                    code,
                    "failed",
                    target_bars,
                    error=str(exc),
                )
            if delay_seconds:
                time.sleep(delay_seconds)
        return BulkCollectionResult(
            attempted,
            completed,
            failed,
            skipped,
            available_completed,
            False,
        )

    def summary(self) -> dict[str, int | str | None]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total_count,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                           AS completed_count,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                           AS failed_count,
                       SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END)
                           AS running_count,
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
                           AS pending_count,
                       SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END)
                           AS skipped_count,
                       SUM(
                           CASE
                               WHEN status = 'completed'
                                AND daily_bar_count < target_bars
                               THEN 1 ELSE 0
                           END
                       ) AS available_history_completed_count,
                       MAX(updated_at) AS updated_at
                FROM universe_collection_status
                """
            ).fetchone()
        return {
            key: (int(row[key] or 0) if key.endswith("_count") else row[key])
            for key in row.keys()
        }

    def _pending_codes(
        self,
        target_bars: int,
        limit: int | None,
    ) -> list[str]:
        query = """
            SELECT u.stock_code, u.stock_name
            FROM stock_universe u
            LEFT JOIN universe_collection_status s
              ON s.stock_code = u.stock_code
            WHERE u.tradable = 1
              AND u.management = 0
              AND u.halted = 0
              AND u.overheated = 0
              AND u.market IN ('KOSPI', 'KOSDAQ')
              AND (
                  s.stock_code IS NULL
                  OR s.status IN ('pending', 'failed', 'running')
                  OR (
                      s.status = 'completed'
                      AND s.daily_bar_count < ?
                      AND COALESCE(s.last_error, '') != 'available_history_exhausted'
                  )
              )
            ORDER BY
                CASE COALESCE(s.status, 'pending')
                    WHEN 'failed' THEN 0
                    WHEN 'running' THEN 1
                    ELSE 2
                END,
                COALESCE(s.daily_bar_count, 0),
                u.stock_code
        """
        parameters: list[object] = [target_bars]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        codes = []
        for row in rows:
            if is_market_product_name(row["stock_name"]):
                self._save_status(
                    row["stock_code"],
                    "skipped",
                    target_bars,
                    error="market_product_excluded",
                )
                continue
            codes.append(row["stock_code"])
        return codes

    def _daily_count(self, stock_code: str) -> int:
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM candles
                    WHERE stock_code = ? AND candle_type = 'daily'
                    """,
                    (stock_code,),
                ).fetchone()[0]
            )

    def _save_status(
        self,
        stock_code: str,
        status: str,
        target_bars: int,
        *,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS daily_bar_count, MAX(candle_time) AS latest
                FROM candles
                WHERE stock_code = ? AND candle_type = 'daily'
                """,
                (stock_code,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO universe_collection_status(
                    stock_code, status, target_bars, daily_bar_count,
                    latest_candle_time, attempt_count, last_error,
                    started_at, completed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stock_code) DO UPDATE SET
                    status = excluded.status,
                    target_bars = excluded.target_bars,
                    daily_bar_count = excluded.daily_bar_count,
                    latest_candle_time = excluded.latest_candle_time,
                    attempt_count = universe_collection_status.attempt_count + ?,
                    last_error = excluded.last_error,
                    started_at = CASE
                        WHEN excluded.status = 'running' THEN excluded.started_at
                        ELSE universe_collection_status.started_at
                    END,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    stock_code,
                    status,
                    target_bars,
                    int(row["daily_bar_count"]),
                    row["latest"],
                    int(increment_attempt),
                    error,
                    timestamp if status == "running" else None,
                    timestamp if status == "completed" else None,
                    timestamp,
                    int(increment_attempt),
                ),
            )
