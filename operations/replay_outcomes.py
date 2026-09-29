from datetime import date, datetime
from typing import Any, Optional

from data.market_calendar import SEOUL_TIMEZONE
from db.database import Database


class ReplayOutcomeService:
    HORIZONS = (30, 60, 120)

    def __init__(self, database: Database) -> None:
        self.database = database

    def recompute(
        self,
        target_date: date,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(SEOUL_TIMEZONE)
        if current.tzinfo is not None:
            current = current.astimezone(SEOUL_TIMEZONE).replace(tzinfo=None)
        result = self.database.recompute_decision_forward_outcomes(
            target_date=target_date.isoformat(),
            horizons=self.HORIZONS,
            now=current,
        )
        result["status"] = (
            "pending"
            if int(result["pending"]) > 0
            else (
                "partial"
                if int(result["unavailable"]) > 0
                else "complete"
            )
        )
        return result
