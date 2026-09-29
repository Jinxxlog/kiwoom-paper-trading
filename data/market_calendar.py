from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Optional

from db.database import Database


SEOUL_TIMEZONE = timezone(timedelta(hours=9), name="Asia/Seoul")
MARKET_OPEN = clock_time(9, 0)
MARKET_CLOSE = clock_time(15, 30)

# KRX publishes its closures annually. Keep the current operational year
# explicit, while allowing DB overrides for temporary closures or openings.
BUILTIN_CLOSURES: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 2, 16): "Lunar New Year holiday",
    date(2026, 2, 17): "Lunar New Year",
    date(2026, 2, 18): "Lunar New Year holiday",
    date(2026, 3, 2): "Substitute holiday for Independence Movement Day",
    date(2026, 5, 1): "Labor Day",
    date(2026, 5, 5): "Children's Day",
    date(2026, 5, 25): "Substitute holiday for Buddha's Birthday",
    date(2026, 6, 3): "Local election day",
    date(2026, 8, 17): "Substitute holiday for Liberation Day",
    date(2026, 9, 24): "Chuseok holiday",
    date(2026, 9, 25): "Chuseok",
    date(2026, 9, 28): "Substitute holiday for Chuseok",
    date(2026, 10, 5): "Substitute holiday for National Foundation Day",
    date(2026, 10, 9): "Hangul Day",
    date(2026, 12, 25): "Christmas Day",
    date(2026, 12, 31): "KRX year-end closure",
}


@dataclass(frozen=True)
class MarketDay:
    market_date: date
    status: str
    name: Optional[str]
    source: str


class MarketCalendar:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_day(self, market_date: date) -> MarketDay:
        override = self._get_override(market_date)
        if override is not None:
            return override
        if market_date.weekday() >= 5:
            return MarketDay(market_date, "closed", "Weekend", "weekday")
        closure_name = BUILTIN_CLOSURES.get(market_date)
        if closure_name is not None:
            return MarketDay(
                market_date,
                "closed",
                closure_name,
                "builtin-2026",
            )
        return MarketDay(market_date, "open", None, "weekday")

    def is_trading_day(self, market_date: date) -> bool:
        return self.get_day(market_date).status == "open"

    def is_market_open(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(SEOUL_TIMEZONE)
        if current.tzinfo is None:
            current = current.replace(tzinfo=SEOUL_TIMEZONE)
        else:
            current = current.astimezone(SEOUL_TIMEZONE)
        local_time = current.time().replace(tzinfo=None)
        return (
            self.is_trading_day(current.date())
            and MARKET_OPEN <= local_time <= MARKET_CLOSE
        )

    def set_day(
        self,
        market_date: date,
        status: str,
        *,
        name: Optional[str] = None,
        source: str = "manual",
    ) -> None:
        normalized_status = status.strip().lower()
        if normalized_status not in {"open", "closed"}:
            raise ValueError("거래일 상태는 open 또는 closed여야 합니다.")
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO market_calendar_days(
                    market_date, status, name, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(market_date) DO UPDATE SET
                    status = excluded.status,
                    name = excluded.name,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    market_date.isoformat(),
                    normalized_status,
                    name,
                    source.strip() or "manual",
                    timestamp,
                ),
            )

    def remove_day(self, market_date: date) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM market_calendar_days WHERE market_date = ?",
                (market_date.isoformat(),),
            )
            return cursor.rowcount > 0

    def list_exceptions(self, year: int) -> list[MarketDay]:
        days = {
            market_date: MarketDay(
                market_date,
                "closed",
                name,
                "builtin-2026",
            )
            for market_date, name in BUILTIN_CLOSURES.items()
            if market_date.year == year
        }
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT market_date, status, name, source
                FROM market_calendar_days
                WHERE market_date >= ? AND market_date < ?
                ORDER BY market_date
                """,
                (f"{year:04d}-01-01", f"{year + 1:04d}-01-01"),
            ).fetchall()
        for row in rows:
            market_date = date.fromisoformat(row["market_date"])
            days[market_date] = MarketDay(
                market_date,
                row["status"],
                row["name"],
                row["source"],
            )
        return [days[key] for key in sorted(days)]

    def _get_override(self, market_date: date) -> Optional[MarketDay]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT market_date, status, name, source
                FROM market_calendar_days
                WHERE market_date = ?
                """,
                (market_date.isoformat(),),
            ).fetchone()
        if row is None:
            return None
        return MarketDay(
            date.fromisoformat(row["market_date"]),
            row["status"],
            row["name"],
            row["source"],
        )
