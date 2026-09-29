import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from db.database import Database
from screening.stock_category import STOCK_CATEGORY_LABELS, stock_category_payload


@dataclass(frozen=True)
class LeaderOutcomeLearningResult:
    trade_date: str
    evaluated_count: int
    saved_count: int
    actual_leader_count: int


class LeaderOutcomeLearningEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def evaluate(
        self,
        *,
        trade_date: date | None = None,
        limit: int = 20,
    ) -> LeaderOutcomeLearningResult:
        if limit < 1:
            raise ValueError("대장주 성과 학습 후보 수는 1 이상이어야 합니다.")
        selected_date = trade_date.isoformat() if trade_date else None
        rows = self.database.latest_leader_stock_scores(
            trade_date=selected_date,
            limit=limit,
        )
        if not rows:
            return LeaderOutcomeLearningResult(
                trade_date=selected_date or date.today().isoformat(),
                evaluated_count=0,
                saved_count=0,
                actual_leader_count=0,
            )
        trade_date_text = str(rows[0]["trade_date"])
        candidates = [
            self._candidate_from_row(index, row, trade_date_text)
            for index, row in enumerate(rows, start=1)
        ]
        self._mark_actual_leaders(candidates)
        saved = self.database.save_leader_candidate_outcomes(candidates)
        return LeaderOutcomeLearningResult(
            trade_date=trade_date_text,
            evaluated_count=len(candidates),
            saved_count=saved,
            actual_leader_count=sum(
                1 for candidate in candidates if candidate["actual_leader"]
            ),
        )

    def _candidate_from_row(
        self,
        candidate_rank: int,
        row: Any,
        trade_date: str,
    ) -> dict[str, Any]:
        components = self._json_dict(row["components_json"])
        same_day = self._daily_candle(str(row["stock_code"]), trade_date)
        next_day = self._next_daily_candle(str(row["stock_code"]), trade_date)
        reference_price = self._reference_price(components, same_day)
        outcome = {
            "performance_score": None,
            "same_day_available": same_day is not None,
            "next_day_available": next_day is not None,
            "reference_source": (
                "component_price"
                if self._component_reference_price(components) is not None
                else "same_day_open"
            ),
        }
        same_day_max_up_rate = None
        same_day_close_rate = None
        if same_day is not None and reference_price:
            same_day_max_up_rate = _rate(same_day.get("high_price"), reference_price)
            same_day_close_rate = _rate(
                same_day.get("close_price"),
                reference_price,
            )
        next_day_max_up_rate = None
        next_day_close_rate = None
        if next_day is not None and reference_price:
            next_day_max_up_rate = _rate(next_day.get("high_price"), reference_price)
            next_day_close_rate = _rate(next_day.get("close_price"), reference_price)
        performance_values = [
            value
            for value in (same_day_max_up_rate, next_day_max_up_rate)
            if value is not None
        ]
        if performance_values:
            outcome["performance_score"] = max(performance_values)
        return {
            "trade_date": trade_date,
            "stock_code": str(row["stock_code"]),
            "stock_name": row["stock_name"],
            "candidate_rank": candidate_rank,
            "role_at_pick": str(row["role"]),
            "score_at_pick": float(row["total_score"] or 0),
            "theme_code": row["theme_code"],
            "theme_name": row["theme_name"] or "",
            "components": {
                "leader_components": components,
                "rank_score": float(row["rank_score"] or 0),
                "trade_amount_score": float(row["trade_amount_score"] or 0),
                "theme_score": float(row["theme_score"] or 0),
                "persistence_score": float(row["persistence_score"] or 0),
                "chart_score": float(row["chart_score"] or 0),
                "risk_score": float(row["risk_score"] or 0),
                "blocked": bool(row["blocked"]),
                "blocked_reason": row["blocked_reason"],
            },
            "same_day_reference_price": reference_price,
            "same_day_open_price": _int_field(same_day, "open_price"),
            "same_day_high_price": _int_field(same_day, "high_price"),
            "same_day_low_price": _int_field(same_day, "low_price"),
            "same_day_close_price": _int_field(same_day, "close_price"),
            "same_day_volume": _int_field(same_day, "volume"),
            "same_day_max_up_rate": same_day_max_up_rate,
            "same_day_close_rate": same_day_close_rate,
            "next_day_date": _date_text(next_day),
            "next_day_open_price": _int_field(next_day, "open_price"),
            "next_day_high_price": _int_field(next_day, "high_price"),
            "next_day_low_price": _int_field(next_day, "low_price"),
            "next_day_close_price": _int_field(next_day, "close_price"),
            "next_day_volume": _int_field(next_day, "volume"),
            "next_day_max_up_rate": next_day_max_up_rate,
            "next_day_close_rate": next_day_close_rate,
            "actual_leader": False,
            "actual_leader_rank": None,
            "actual_leader_reason": "성과 순위 계산 전",
            "outcome": outcome,
        }

    def _mark_actual_leaders(self, candidates: list[dict[str, Any]]) -> None:
        ranked = sorted(
            candidates,
            key=lambda item: (
                item["outcome"].get("performance_score")
                if item["outcome"].get("performance_score") is not None
                else -999.0,
                item.get("same_day_close_rate")
                if item.get("same_day_close_rate") is not None
                else -999.0,
            ),
            reverse=True,
        )
        for actual_rank, candidate in enumerate(ranked, start=1):
            performance = candidate["outcome"].get("performance_score")
            candidate["actual_leader_rank"] = actual_rank if performance is not None else None
            same_close = candidate.get("same_day_close_rate")
            next_high = candidate.get("next_day_max_up_rate")
            next_close = candidate.get("next_day_close_rate")
            if performance is None:
                candidate["actual_leader_reason"] = "성과 계산 데이터 부족"
                continue
            if actual_rank <= 3 and performance >= 0.03:
                candidate["actual_leader"] = True
                candidate["actual_leader_reason"] = f"TOP20 성과 {actual_rank}위"
            elif same_close is not None and same_close >= 0.04:
                candidate["actual_leader"] = True
                candidate["actual_leader_reason"] = "당일 종가 4% 이상 강세"
            elif (
                next_high is not None
                and next_high >= 0.05
                and next_close is not None
                and next_close >= 0
            ):
                candidate["actual_leader"] = True
                candidate["actual_leader_reason"] = "익일 5% 이상 상승 후 플러스 마감"
            else:
                candidate["actual_leader_reason"] = "성과 기준 미충족"
            candidate["outcome"]["actual_leader_rank"] = candidate[
                "actual_leader_rank"
            ]

    def _daily_candle(self, stock_code: str, trade_date: str) -> Optional[dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'daily'
                  AND substr(candle_time, 1, 10) = ?
                ORDER BY candle_time DESC
                LIMIT 1
                """,
                (stock_code, trade_date),
            ).fetchone()
        return dict(row) if row is not None else None

    def _next_daily_candle(
        self,
        stock_code: str,
        trade_date: str,
    ) -> Optional[dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'daily'
                  AND substr(candle_time, 1, 10) > ?
                ORDER BY candle_time
                LIMIT 1
                """,
                (stock_code, trade_date),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _reference_price(
        self,
        components: dict[str, Any],
        same_day: Optional[dict[str, Any]],
    ) -> Optional[int]:
        component_price = self._component_reference_price(components)
        if component_price is not None:
            return component_price
        return _int_field(same_day, "open_price") or _int_field(
            same_day,
            "close_price",
        )

    @staticmethod
    def _component_reference_price(components: dict[str, Any]) -> Optional[int]:
        fields = (
            "current_price",
            "price",
            "close_price",
            "last_price",
            "now_price",
        )

        def walk(value: Any) -> Optional[int]:
            if isinstance(value, dict):
                for field in fields:
                    parsed = _positive_int(value.get(field))
                    if parsed is not None:
                        return parsed
                for child in value.values():
                    parsed = walk(child)
                    if parsed is not None:
                        return parsed
            elif isinstance(value, list):
                for child in value:
                    parsed = walk(child)
                    if parsed is not None:
                        return parsed
            return None

        return walk(components)


class LeaderOutcomeReportService:
    COMPONENTS = (
        ("trade_amount_score", "거래대금"),
        ("rank_score", "조회/순위"),
        ("theme_score", "테마"),
        ("persistence_score", "지속성"),
        ("chart_score", "차트"),
        ("risk_score", "리스크"),
    )

    def __init__(self, database: Database) -> None:
        self.database = database

    def report(
        self,
        *,
        trade_date: date | None = None,
        days: int = 30,
        limit: int = 10,
    ) -> dict[str, Any]:
        if days < 1:
            raise ValueError("대장주 학습 리포트 기간은 1일 이상이어야 합니다.")
        if limit < 1:
            raise ValueError("대장주 학습 리포트 조회 건수는 1 이상이어야 합니다.")
        rows = self._load_outcomes(trade_date=trade_date, days=days)
        return {
            "summary": self._summary(rows),
            "by_stock_category": self._by_stock_category(rows),
            "by_theme": self._by_theme(rows, limit=limit),
            "component_performance": self._component_performance(rows),
            "blocked_review": self._blocked_review(rows, limit=limit),
            "rows": rows[:limit],
        }

    def _load_outcomes(
        self,
        *,
        trade_date: date | None,
        days: int,
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            if trade_date is not None:
                fetched = connection.execute(
                    """
                    SELECT *
                    FROM leader_candidate_outcomes
                    WHERE trade_date = ?
                    ORDER BY trade_date DESC, candidate_rank, stock_code
                    """,
                    (trade_date.isoformat(),),
                ).fetchall()
            else:
                fetched = connection.execute(
                    """
                    WITH latest AS (
                        SELECT MAX(trade_date) AS max_trade_date
                        FROM leader_candidate_outcomes
                    )
                    SELECT o.*
                    FROM leader_candidate_outcomes o, latest
                    WHERE latest.max_trade_date IS NOT NULL
                      AND date(o.trade_date) >= date(
                          latest.max_trade_date,
                          ?
                      )
                    ORDER BY o.trade_date DESC, o.candidate_rank, o.stock_code
                    """,
                    (f"-{days - 1} days",),
                ).fetchall()
        rows = []
        for row in fetched:
            item = dict(row)
            for source, target, fallback in (
                ("components_json", "components", {}),
                ("outcome_json", "outcome", {}),
            ):
                try:
                    parsed = json.loads(item.pop(source) or json.dumps(fallback))
                except (TypeError, json.JSONDecodeError):
                    parsed = fallback
                item[target] = parsed if isinstance(parsed, dict) else fallback
            item["actual_leader"] = bool(item.get("actual_leader"))
            item["blocked"] = self._is_blocked(item)
            item["performance_score"] = self._performance_score(item)
            item["blocked_outcome"] = self._blocked_outcome(item)
            item["stock_category"] = stock_category_payload(
                item.get("stock_code"),
                item.get("stock_name"),
            )
            rows.append(item)
        return rows

    def _summary(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "candidate_count": len(rows),
            "actual_leader_count": self._count_actual(rows),
            "hit_rate": self._hit_rate(rows),
            "average_score_at_pick": _average(
                row.get("score_at_pick") for row in rows
            ),
            "average_same_day_max_up_rate": _average(
                row.get("same_day_max_up_rate") for row in rows
            ),
            "average_next_day_max_up_rate": _average(
                row.get("next_day_max_up_rate") for row in rows
            ),
            "blocked_count": sum(1 for row in rows if row["blocked"]),
            "blocked_helpful_count": sum(
                1 for row in rows if row["blocked_outcome"] == "blocked_helpful"
            ),
            "blocked_missed_rally_count": sum(
                1 for row in rows if row["blocked_outcome"] == "blocked_missed_rally"
            ),
        }

    def _by_stock_category(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {
            key: [] for key in STOCK_CATEGORY_LABELS
        }
        for row in rows:
            category = row.get("stock_category") or stock_category_payload(
                row.get("stock_code"),
                row.get("stock_name"),
            )
            groups.setdefault(category["key"], []).append(row)
        result = []
        for key, label in STOCK_CATEGORY_LABELS.items():
            items = groups.get(key, [])
            result.append(
                {
                    "category": key,
                    "label": label,
                    "candidate_count": len(items),
                    "actual_leader_count": self._count_actual(items),
                    "hit_rate": self._hit_rate(items),
                    "average_same_day_max_up_rate": _average(
                        item.get("same_day_max_up_rate") for item in items
                    ),
                    "average_next_day_max_up_rate": _average(
                        item.get("next_day_max_up_rate") for item in items
                    ),
                    "average_performance_score": _average(
                        item.get("performance_score") for item in items
                    ),
                    "blocked_count": sum(1 for item in items if item["blocked"]),
                    "blocked_missed_rally_count": sum(
                        1
                        for item in items
                        if item["blocked_outcome"] == "blocked_missed_rally"
                    ),
                }
            )
        return result

    def _by_theme(
        self,
        rows: list[dict[str, Any]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            theme = str(row.get("theme_name") or "미지정")
            groups.setdefault(theme, []).append(row)
        result = []
        for theme, items in groups.items():
            result.append(
                {
                    "theme_name": theme,
                    "candidate_count": len(items),
                    "actual_leader_count": self._count_actual(items),
                    "hit_rate": self._hit_rate(items),
                    "average_score_at_pick": _average(
                        item.get("score_at_pick") for item in items
                    ),
                    "average_same_day_max_up_rate": _average(
                        item.get("same_day_max_up_rate") for item in items
                    ),
                    "average_next_day_max_up_rate": _average(
                        item.get("next_day_max_up_rate") for item in items
                    ),
                    "blocked_count": sum(1 for item in items if item["blocked"]),
                    "missed_rally_count": sum(
                        1
                        for item in items
                        if item["blocked_outcome"] == "blocked_missed_rally"
                    ),
                }
            )
        result.sort(
            key=lambda item: (
                float(item["hit_rate"] or 0),
                int(item["actual_leader_count"]),
                int(item["candidate_count"]),
            ),
            reverse=True,
        )
        return result[:limit]

    def _component_performance(
        self,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result = []
        for key, label in self.COMPONENTS:
            items = [
                row
                for row in rows
                if self._component_score(row, key) is not None
                and (
                    self._component_score(row, key) > 0
                    if key != "risk_score"
                    else self._component_score(row, key) != 0
                )
            ]
            result.append(
                {
                    "component": key,
                    "label": label,
                    "candidate_count": len(items),
                    "average_component_score": _average(
                        self._component_score(item, key) for item in items
                    ),
                    "actual_leader_count": self._count_actual(items),
                    "hit_rate": self._hit_rate(items),
                    "average_same_day_max_up_rate": _average(
                        item.get("same_day_max_up_rate") for item in items
                    ),
                    "average_next_day_max_up_rate": _average(
                        item.get("next_day_max_up_rate") for item in items
                    ),
                    "average_performance_score": _average(
                        item.get("performance_score") for item in items
                    ),
                }
            )
        result.sort(
            key=lambda item: (
                float(item["average_performance_score"] or -999),
                float(item["hit_rate"] or 0),
            ),
            reverse=True,
        )
        return result

    def _blocked_review(
        self,
        rows: list[dict[str, Any]],
        *,
        limit: int,
    ) -> dict[str, Any]:
        blocked = [row for row in rows if row["blocked"]]
        helpful = [
            row for row in blocked if row["blocked_outcome"] == "blocked_helpful"
        ]
        missed = [
            row for row in blocked if row["blocked_outcome"] == "blocked_missed_rally"
        ]
        neutral = [
            row for row in blocked if row["blocked_outcome"] == "blocked_neutral"
        ]
        by_reason: dict[str, list[dict[str, Any]]] = {}
        for row in blocked:
            reason = (
                str((row.get("components") or {}).get("blocked_reason") or "").strip()
                or str(row.get("actual_leader_reason") or "").strip()
                or "차단 사유 없음"
            )
            by_reason.setdefault(reason, []).append(row)
        reasons = []
        for reason, items in by_reason.items():
            reasons.append(
                {
                    "reason": reason,
                    "candidate_count": len(items),
                    "helpful_count": sum(
                        1
                        for item in items
                        if item["blocked_outcome"] == "blocked_helpful"
                    ),
                    "missed_rally_count": sum(
                        1
                        for item in items
                        if item["blocked_outcome"] == "blocked_missed_rally"
                    ),
                    "average_performance_score": _average(
                        item.get("performance_score") for item in items
                    ),
                }
            )
        reasons.sort(
            key=lambda item: (
                int(item["missed_rally_count"]),
                int(item["candidate_count"]),
            ),
            reverse=True,
        )
        return {
            "candidate_count": len(blocked),
            "helpful_count": len(helpful),
            "missed_rally_count": len(missed),
            "neutral_count": len(neutral),
            "helpful_rate": len(helpful) / len(blocked) if blocked else None,
            "missed_rally_rate": len(missed) / len(blocked) if blocked else None,
            "by_reason": reasons[:limit],
        }

    @staticmethod
    def _count_actual(rows: list[dict[str, Any]]) -> int:
        return sum(1 for row in rows if row.get("actual_leader"))

    def _hit_rate(self, rows: list[dict[str, Any]]) -> Optional[float]:
        return self._count_actual(rows) / len(rows) if rows else None

    @staticmethod
    def _component_score(row: dict[str, Any], key: str) -> Optional[float]:
        components = row.get("components") or {}
        try:
            return float(components.get(key))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _performance_score(row: dict[str, Any]) -> Optional[float]:
        outcome = row.get("outcome") or {}
        try:
            return float(outcome["performance_score"])
        except (KeyError, TypeError, ValueError):
            values = [
                value
                for value in (
                    row.get("same_day_max_up_rate"),
                    row.get("next_day_max_up_rate"),
                )
                if value is not None
            ]
            return max(float(value) for value in values) if values else None

    @staticmethod
    def _is_blocked(row: dict[str, Any]) -> bool:
        components = row.get("components") or {}
        return bool(components.get("blocked")) or row.get("role_at_pick") == "blocked"

    @staticmethod
    def _blocked_outcome(row: dict[str, Any]) -> Optional[str]:
        if not LeaderOutcomeReportService._is_blocked(row):
            return None
        performance = LeaderOutcomeReportService._performance_score(row)
        if row.get("actual_leader") or (performance is not None and performance >= 0.05):
            return "blocked_missed_rally"
        if performance is not None and performance < 0.03:
            return "blocked_helpful"
        return "blocked_neutral"


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _int_field(row: Optional[dict[str, Any]], field: str) -> Optional[int]:
    if row is None:
        return None
    return _positive_int(row.get(field))


def _rate(price: Any, reference_price: int) -> Optional[float]:
    parsed = _positive_int(price)
    if parsed is None or reference_price <= 0:
        return None
    return (parsed - reference_price) / reference_price


def _date_text(row: Optional[dict[str, Any]]) -> Optional[str]:
    if row is None:
        return None
    candle_time = str(row.get("candle_time") or "")
    return candle_time[:10] if candle_time else None


def _average(values: Any) -> Optional[float]:
    collected = [float(value) for value in values if value is not None]
    if not collected:
        return None
    return sum(collected) / len(collected)
