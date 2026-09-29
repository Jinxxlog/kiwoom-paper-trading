import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from db.database import Database
from screening.market_product import is_market_product_item, is_market_product_name


RANK_TYPES = (
    "trade_amount",
    "realtime_inquiry",
    "expected_change",
    "change_rate",
)


@dataclass(frozen=True)
class LeaderScoringResult:
    trade_date: str
    scored_count: int
    saved_count: int


class LeaderStockScoringEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def score(
        self,
        *,
        trade_date: date | None = None,
        limit: int = 50,
    ) -> LeaderScoringResult:
        if limit < 1:
            raise ValueError("대장주 점수 저장 개수는 1 이상이어야 합니다.")
        selected_date = trade_date or date.today()
        selected_date_text = selected_date.isoformat()
        market_items = self._latest_market_items()
        themes = self._latest_theme_contexts()
        manual = self._manual_theme_members(selected_date)
        chart = self._latest_chart_features()
        supply = self._latest_supply_scores()
        candidates = sorted(
            set(market_items)
            | set(themes)
            | set(manual),
        )
        scores = []
        for stock_code in candidates:
            score = self._score_stock(
                selected_date_text,
                stock_code,
                market_items.get(stock_code, {}),
                themes.get(stock_code),
                manual.get(stock_code),
                chart.get(stock_code, {}),
                supply.get(stock_code),
            )
            scores.append(score)
        scores.sort(
            key=lambda item: (
                item["blocked"],
                -item["total_score"],
                -item["trade_amount_score"],
                -item["rank_score"],
                item["stock_code"],
            )
        )
        selected = scores[:limit]
        saved = self.database.save_leader_stock_scores(selected)
        return LeaderScoringResult(
            trade_date=selected_date_text,
            scored_count=len(scores),
            saved_count=saved,
        )

    def _score_stock(
        self,
        trade_date: str,
        stock_code: str,
        ranks: dict[str, dict[str, Any]],
        theme: dict[str, Any] | None,
        manual: dict[str, Any] | None,
        chart: dict[str, Any],
        supply: dict[str, Any] | None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        stock_name = (
            _first(
                [
                    ranks.get("trade_amount", {}).get("stock_name"),
                    ranks.get("realtime_inquiry", {}).get("stock_name"),
                    ranks.get("expected_change", {}).get("stock_name"),
                    ranks.get("change_rate", {}).get("stock_name"),
                    (theme or {}).get("stock_name"),
                    (manual or {}).get("stock_name"),
                    (supply or {}).get("stock_name"),
                ]
            )
            or stock_code
        )
        product_excluded = is_market_product_name(stock_name)
        trade_rank = _int_or_none(ranks.get("trade_amount", {}).get("rank"))
        trade_amount = _int_or_none(
            ranks.get("trade_amount", {}).get("trade_amount")
        )
        trade_amount_score = 0.0
        trade_amount_qualified = False
        if trade_rank is not None:
            if trade_rank <= 10:
                trade_amount_score += 35
                trade_amount_qualified = True
                reasons.append(f"거래대금 {trade_rank}위")
            elif trade_rank <= 30:
                trade_amount_score += 25
                trade_amount_qualified = True
                reasons.append(f"거래대금 {trade_rank}위")
            elif trade_rank <= 60:
                trade_amount_score += 12
                reasons.append(f"거래대금 {trade_rank}위")
        if trade_amount is not None and trade_amount >= 50_000_000_000:
            trade_amount_score = max(trade_amount_score, 28)
            trade_amount_qualified = True
            reasons.append(f"거래대금 {trade_amount // 100_000_000:,}억원")

        rank_score = 0.0
        repeated = 0
        for rank_type, label, top_score in (
            ("realtime_inquiry", "조회", 16),
            ("expected_change", "예상체결", 14),
            ("change_rate", "등락률", 8),
        ):
            rank = _int_or_none(ranks.get(rank_type, {}).get("rank"))
            if rank is None:
                continue
            repeated += 1
            if rank <= 10:
                rank_score += top_score
                reasons.append(f"{label} {rank}위")
            elif rank <= 30:
                rank_score += top_score * 0.65
                reasons.append(f"{label} {rank}위")
            elif rank <= 60:
                rank_score += top_score * 0.35
        if repeated >= 3:
            rank_score += 12
            reasons.append("시장 순위 3개 이상 반복 등장")
        elif repeated >= 2:
            rank_score += 7
            reasons.append("시장 순위 2개 반복 등장")

        theme_score = 0.0
        theme_name = None
        theme_code = None
        if theme:
            theme_name = theme.get("theme_name")
            theme_code = theme.get("theme_code")
            theme_rank = _int_or_none(theme.get("theme_rank"))
            if theme_rank is not None and theme_rank <= 5:
                theme_score += 18
            elif theme_rank is not None and theme_rank <= 10:
                theme_score += 12
            elif theme_rank is not None:
                theme_score += 6
            if _float_or_zero(theme.get("theme_change_rate")) >= 3:
                theme_score += 8
            reasons.append(f"키움 테마 {theme_name or '-'}")
        if manual:
            theme_name = manual.get("theme_name") or theme_name
            strength = manual.get("strength")
            role = manual.get("role")
            if strength == "strong":
                theme_score += 22
            elif strength == "medium":
                theme_score += 14
            elif strength == "weak":
                theme_score += 6
            if role == "leader":
                theme_score += 12
            elif role == "second":
                theme_score += 7
            elif role == "follower":
                theme_score += 3
            reasons.append(f"수동 테마 {theme_name or '-'}:{strength}")

        persistence_score = self._persistence_score(theme_name, stock_code, reasons)
        chart_score = self._chart_score(chart, reasons)
        supply_score = self._supply_score(supply, reasons)
        risk_score = 0.0
        blocked = False
        blocked_reason = None
        if product_excluded:
            blocked = True
            blocked_reason = "ETF/ETN/leveraged product excluded"
            risk_score -= 80
            reasons.append("ETF/ETN/leveraged product excluded")
        change_rate = max(
            _float_or_zero(item.get("change_rate"))
            for item in ranks.values()
        ) if ranks else 0.0
        features = chart.get("features") or {}
        if not product_excluded and (
            features.get("gap_overheated") or features.get("day_gain_overheated")
        ):
            blocked = True
            blocked_reason = "일봉 과열"
            risk_score -= 35
            reasons.append("일봉 과열 차단")
        elif (
            not product_excluded
            and change_rate >= 25
            and trade_rank is not None
            and trade_rank <= 10
        ):
            blocked = True
            blocked_reason = "고점 클라이맥스 의심"
            risk_score -= 30
            reasons.append("고점 클라이맥스 의심")
        if supply and supply.get("blocked") and trade_amount_qualified:
            blocked = True
            blocked_reason = supply.get("blocked_reason") or "수급 이탈"
            risk_score -= 20
            reasons.append(f"수급 차단: {blocked_reason}")

        if not trade_amount_qualified:
            if blocked_reason is None:
                blocked_reason = "거래대금 상위권 아님"
            reasons.append("거래대금 상위권 미충족")

        total = (
            trade_amount_score
            + rank_score
            + theme_score
            + persistence_score
            + chart_score
            + supply_score
            + risk_score
        )
        if blocked:
            role = "blocked"
        elif trade_amount_qualified and total >= 85:
            role = "leader"
        elif trade_amount_qualified and total >= 65:
            role = "second"
        elif total >= 40:
            role = "follower"
        else:
            role = "watch"
        if role == "leader" and not trade_amount_qualified:
            role = "follower"

        return {
            "trade_date": trade_date,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "theme_code": theme_code,
            "theme_name": theme_name,
            "role": role,
            "total_score": round(total, 4),
            "rank_score": round(rank_score, 4),
            "trade_amount_score": round(trade_amount_score, 4),
            "theme_score": round(theme_score, 4),
            "persistence_score": round(persistence_score, 4),
            "chart_score": round(chart_score, 4),
            "risk_score": round(risk_score, 4),
            "blocked": blocked,
            "blocked_reason": blocked_reason,
            "reasons": reasons,
            "components": {
                "ranks": ranks,
                "theme": theme,
                "manual_theme": manual,
                "chart": chart,
                "supply": supply,
                "supply_score": round(supply_score, 4),
                "trade_amount_qualified": trade_amount_qualified,
            },
        }

    def _latest_market_items(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self.database.connect() as connection:
            snapshots = connection.execute(
                """
                SELECT rank_type, MAX(id) AS snapshot_id
                FROM market_rank_snapshots
                WHERE rank_type IN ('trade_amount', 'realtime_inquiry',
                                    'expected_change', 'change_rate')
                GROUP BY rank_type
                """
            ).fetchall()
            if not snapshots:
                return {}
            snapshot_ids = [int(row["snapshot_id"]) for row in snapshots]
            placeholders = ",".join("?" for _ in snapshot_ids)
            rows = connection.execute(
                f"""
                SELECT *
                FROM market_rank_items
                WHERE snapshot_id IN ({placeholders})
                """,
                snapshot_ids,
            ).fetchall()
        items: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["metadata"] = {}
            if is_market_product_item(item):
                continue
            items.setdefault(item["stock_code"], {})[item["rank_type"]] = item
        return items

    def _latest_theme_contexts(self) -> dict[str, dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT tc.stock_code, tc.stock_name, tc.current_price,
                       tc.change_rate AS component_change_rate,
                       tc.accumulated_volume, tc.collected_at,
                       tg.theme_code, tg.theme_name, tg.stock_count,
                       tg.change_rate AS theme_change_rate,
                       ROW_NUMBER() OVER (
                           ORDER BY COALESCE(tg.change_rate, 0) DESC,
                                    COALESCE(tg.day_profit_rate, 0) DESC,
                                    tg.theme_code
                       ) AS theme_rank
                FROM theme_components tc
                JOIN theme_groups tg ON tg.theme_code = tc.theme_code
                WHERE tc.collected_at = (
                    SELECT MAX(collected_at) FROM theme_components
                )
                ORDER BY tc.stock_code, theme_rank
                """
            ).fetchall()
        contexts: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            if item["stock_code"] not in contexts:
                contexts[item["stock_code"]] = item
        return contexts

    def _manual_theme_members(
        self,
        selected_date: date,
    ) -> dict[str, dict[str, Any]]:
        start = (selected_date - timedelta(days=30)).isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.theme_date, j.theme_name, j.strength,
                       m.stock_code, m.stock_name, m.role, m.notes
                FROM theme_journals j
                JOIN theme_journal_members m ON m.journal_id = j.id
                WHERE j.theme_date >= ?
                ORDER BY j.theme_date DESC
                """,
                (start,),
            ).fetchall()
        members: dict[str, dict[str, Any]] = {}
        strength_rank = {"strong": 3, "medium": 2, "weak": 1, "none": 0}
        role_rank = {"leader": 4, "second": 3, "follower": 2, "watch": 1, "blocked": 0}
        for row in rows:
            item = dict(row)
            current = members.get(item["stock_code"])
            if current is None or (
                strength_rank.get(item["strength"], 0),
                role_rank.get(item["role"], 0),
                item["theme_date"],
            ) > (
                strength_rank.get(current["strength"], 0),
                role_rank.get(current["role"], 0),
                current["theme_date"],
            ):
                members[item["stock_code"]] = item
        return members

    def _latest_chart_features(self) -> dict[str, dict[str, Any]]:
        with self.database.connect() as connection:
            run = connection.execute(
                "SELECT MAX(id) AS id FROM candidate_runs"
            ).fetchone()
            if run is None or run["id"] is None:
                return {}
            rows = connection.execute(
                """
                SELECT stock_code, features_json, patterns_json,
                       total_score, technical_score, pattern_score
                FROM candidate_scores
                WHERE run_id = ?
                """,
                (run["id"],),
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            try:
                features = json.loads(item.pop("features_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                features = {}
            try:
                patterns = json.loads(item.pop("patterns_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                patterns = []
            features.pop("leader_stock_score", None)
            features.pop("supply_score", None)
            item["features"] = features
            item["patterns"] = patterns
            result[item["stock_code"]] = item
        return result

    def _latest_supply_scores(self) -> dict[str, dict[str, Any]]:
        with self.database.connect() as connection:
            latest = connection.execute(
                "SELECT MAX(evaluated_at) AS evaluated_at FROM supply_scores"
            ).fetchone()
            if latest is None or latest["evaluated_at"] is None:
                return {}
            rows = connection.execute(
                """
                SELECT *
                FROM supply_scores
                WHERE evaluated_at = ?
                """,
                (latest["evaluated_at"],),
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            for source, target, fallback in (
                ("reasons_json", "reasons", []),
            ):
                try:
                    item[target] = json.loads(item.pop(source) or json.dumps(fallback))
                except (TypeError, json.JSONDecodeError):
                    item[target] = fallback
            item.pop("components_json", None)
            item["blocked"] = bool(item.get("blocked"))
            result[item["stock_code"]] = item
        return result

    def _persistence_score(
        self,
        theme_name: str | None,
        stock_code: str,
        reasons: list[str],
    ) -> float:
        with self.database.connect() as connection:
            theme_count = 0
            if theme_name:
                theme_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM theme_journals
                        WHERE theme_name = ?
                          AND date(theme_date) >= date('now', '-30 days')
                          AND strength IN ('strong', 'medium')
                        """,
                        (theme_name,),
                    ).fetchone()[0]
                )
            stock_count = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT j.theme_date)
                    FROM theme_journals j
                    JOIN theme_journal_members m ON m.journal_id = j.id
                    WHERE m.stock_code = ?
                      AND date(j.theme_date) >= date('now', '-30 days')
                    """,
                    (stock_code,),
                ).fetchone()[0]
            )
        score = min(18.0, theme_count * 4.0 + stock_count * 3.0)
        if theme_count:
            reasons.append(f"최근 30일 테마 반복 {theme_count}회")
        if stock_count:
            reasons.append(f"최근 30일 테마 구성 반복 {stock_count}회")
        return score

    @staticmethod
    def _chart_score(chart: dict[str, Any], reasons: list[str]) -> float:
        features = chart.get("features") or {}
        score = 0.0
        if features.get("new_60d_high"):
            score += 16
            reasons.append("60일 신고가")
        if features.get("recent5_high_breakout"):
            score += 12
            reasons.append("5일 고점 돌파")
        elif features.get("recent5_high_near"):
            score += 6
            reasons.append("5일 고점 근접")
        if features.get("resistance_breakout"):
            score += 12
            reasons.append("저항 돌파")
        elif features.get("near_resistance"):
            score += 5
            reasons.append("저항 근처")
        for key, label in (
            ("box_breakout_state", "박스"),
            ("cup_handle_state", "컵앤핸들"),
            ("ascending_triangle_state", "상승삼각형"),
        ):
            state = features.get(key)
            if state == "confirmed":
                score += 10
                reasons.append(f"{label} 확정")
            elif state in {"ready", "breakout_pending"}:
                score += 5
                reasons.append(f"{label} 대기")
        return min(35.0, score)

    @staticmethod
    def _supply_score(supply: dict[str, Any] | None, reasons: list[str]) -> float:
        if not supply:
            return 0.0
        total = _float_or_zero(supply.get("total_score"))
        if supply.get("blocked"):
            reasons.append(f"수급 이탈: {supply.get('blocked_reason') or '-'}")
            return -10.0
        if total >= 70:
            reasons.append(f"수급 우호 {total:.1f}점")
            return min(10.0, (total - 50.0) * 0.25)
        if total <= 35:
            reasons.append(f"수급 약세 {total:.1f}점")
            return max(-10.0, (total - 50.0) * 0.35)
        return 0.0


def _first(values: list[Any]) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
