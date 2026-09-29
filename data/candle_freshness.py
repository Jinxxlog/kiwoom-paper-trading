"""Trading-calendar based candle readiness and date-based refresh.

Historical bootstrap completion is deliberately independent of freshness.
Minute timestamps are treated conservatively as bar starts: a full interval
must have elapsed before a bar is used as confirmed input.
"""

from datetime import datetime, time, timedelta

from data.market_calendar import MarketCalendar, SEOUL_TIMEZONE
from paper.engine import PaperTradingError


class CandleFreshness:
    def __init__(self, database):
        self.database = database
        self.calendar = MarketCalendar(database)

    @staticmethod
    def local(now):
        if now.tzinfo is not None:
            return now.astimezone(SEOUL_TIMEZONE).replace(tzinfo=None)
        return now

    def previous_session(self, day):
        day -= timedelta(days=1)
        while not self.calendar.is_trading_day(day):
            day -= timedelta(days=1)
        return day

    def daily_target(self, now, *, for_entry=False):
        now = self.local(now)
        # Avoid persisting an in-progress daily bar as a completed session.
        if not for_entry and now.time() >= time(16) and self.calendar.is_trading_day(now.date()):
            return now.date()
        return self.previous_session(now.date())

    def latest(self, code, kind, interval=0, *, before=None):
        query = """SELECT candle_time,open_price,high_price,low_price,close_price,
                          volume,updated_at FROM candles
                   WHERE stock_code=? AND candle_type=? AND interval_minutes=?"""
        args = [code, kind, interval]
        if before is not None:
            query += " AND candle_time<=?"
            args.append(before.isoformat(timespec="seconds"))
        query += " ORDER BY candle_time DESC LIMIT 1"
        with self.database.connect() as connection:
            row = connection.execute(query, args).fetchone()
        return dict(row) if row else None

    @staticmethod
    def valid_bar(row):
        if not row:
            return False
        prices = [row.get(k) for k in ("open_price", "high_price", "low_price", "close_price")]
        return bool(all(p is not None and p > 0 for p in prices)
                    and row["low_price"] <= min(row["open_price"], row["close_price"])
                    and row["high_price"] >= max(row["open_price"], row["close_price"])
                    and row.get("volume") is not None and row["volume"] >= 0)

    def daily_status(self, code, now, *, for_entry=False):
        target = self.daily_target(now, for_entry=for_entry)
        bar = self.latest(code, "daily", before=datetime.combine(target, time(23, 59, 59)))
        ready = bool(self.valid_bar(bar) and bar["candle_time"][:10] == target.isoformat())
        return {"stock_code": code, "kind": "daily", "ready": ready,
                "expected_date": target.isoformat(),
                "latest_time": bar["candle_time"] if bar else None,
                "reason": "ready" if ready else "missing_or_stale_daily"}

    def minute_status(self, code, now, interval=5):
        if interval not in (1, 3, 5, 10, 15, 30, 45, 60):
            raise ValueError("Unsupported minute interval")
        now = self.local(now)
        day = now.date()
        if not self.calendar.is_trading_day(day) or now.time() < time(9):
            day = self.previous_session(day)
            cutoff = datetime.combine(day, time(15, 30))
        else:
            opening = datetime.combine(day, time(9))
            elapsed = int((now - opening).total_seconds() // 60)
            cutoff = min(opening + timedelta(minutes=(elapsed // interval - 1) * interval),
                         datetime.combine(day, time(15, 30)))
        # Closing auction does not supply continuous regular five-minute bars.
        if time(15, 20) < cutoff.time() < time(15, 30):
            cutoff = datetime.combine(day, time(15, 20))
        bar = self.latest(code, "minute", interval, before=cutoff)
        age = (cutoff - datetime.fromisoformat(bar["candle_time"])).total_seconds() if bar else None
        ready = bool(cutoff.time() >= time(9) and self.valid_bar(bar)
                     and bar["candle_time"][:10] == day.isoformat()
                     and age is not None and 0 <= age <= interval * 60)
        return {"stock_code": code, "kind": "minute", "ready": ready,
                "interval": interval, "confirmed_cutoff": cutoff.isoformat(),
                "latest_time": bar["candle_time"] if bar else None,
                "lag_from_cutoff_seconds": age,
                "reason": "ready" if ready else "missing_or_stale_confirmed_minute"}

    def require_entry_data(self, code, now, interval=5):
        results = [self.daily_status(code, now, for_entry=True), self.minute_status(code, now, interval)]
        missing = [r for r in results if not r["ready"]]
        if missing:
            details = "; ".join(f"{r['kind']}={r['latest_time']} ({r['reason']})" for r in missing)
            raise PaperTradingError(f"캔들 최신성 부족: {code}: {details}")
        return results


class CandleRefreshService:
    def __init__(self, database, collector):
        self.freshness = CandleFreshness(database)
        self.collector = collector

    def daily(self, code, now, *, max_pages=1):
        before = self.freshness.daily_status(code, now)
        if before["ready"]:
            return {**before, "action": "already_current"}
        result = self.collector.collect_daily_candles(
            code, before["expected_date"].replace("-", ""), max_pages=max_pages)
        return {**self.freshness.daily_status(code, now), "action": "refreshed",
                "previous_time": before["latest_time"], "saved_count": result.saved_count}

    def minute(self, code, now, *, interval=5, max_pages=1):
        result = self.collector.collect_minute_candles(
            code, self.freshness.local(now).strftime("%Y%m%d"), interval, max_pages=max_pages)
        return {**self.freshness.minute_status(code, now, interval), "action": "refreshed",
                "saved_count": result.saved_count, "has_more_history": result.has_more}
