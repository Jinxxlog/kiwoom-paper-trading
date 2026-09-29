import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from db.database import Database


@dataclass(frozen=True)
class SupplyScoringResult:
    evaluated_at: str
    scored_count: int
    saved_count: int
    program_validation: dict[str, Any]


class SupplyScoringEngine:
    def __init__(self, database: Database) -> None:
        self.database = database

    def score(
        self,
        stock_codes: list[str] | None = None,
        *,
        evaluated_at: str | None = None,
    ) -> SupplyScoringResult:
        selected_codes = stock_codes or self._candidate_stock_codes()
        timestamp = evaluated_at or datetime.now().isoformat(timespec="seconds")
        saved = 0
        program_rows: list[dict[str, Any]] = []
        for stock_code in sorted(set(selected_codes)):
            score = self._score_stock(stock_code, timestamp)
            if score is None:
                continue
            self.database.save_supply_score(score)
            saved += 1
            program = (score.get("components") or {}).get("program") or {}
            program_rows.append(
                {
                    "stock_code": stock_code,
                    "state": program.get("policy_state", "not_collected"),
                    "hard_block": bool(program.get("hard_block")),
                    "legacy_block_candidate": bool(
                        program.get("legacy_block_candidate")
                    ),
                    "legacy_block_relaxed": bool(
                        program.get("legacy_block_relaxed")
                    ),
                    "pressure": program.get("pressure"),
                    "recent_pressure": program.get("recent_pressure"),
                    "consecutive_net_sell_count": program.get(
                        "consecutive_net_sell_count",
                        0,
                    ),
                }
            )
        return SupplyScoringResult(
            evaluated_at=timestamp,
            scored_count=len(set(selected_codes)),
            saved_count=saved,
            program_validation=self._program_validation_summary(program_rows),
        )

    def _candidate_stock_codes(self) -> list[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT stock_code
                FROM (
                    SELECT stock_code FROM broker_flow_snapshots
                    UNION ALL
                    SELECT stock_code FROM investor_flow_snapshots
                    UNION ALL
                    SELECT stock_code FROM broker_flow_items
                    UNION ALL
                    SELECT stock_code FROM investor_flow_items
                    UNION ALL
                    SELECT stock_code FROM program_trade_snapshots
                )
                WHERE stock_code IS NOT NULL AND stock_code != ''
                """
            ).fetchall()
        return [str(row["stock_code"]) for row in rows]

    def _score_stock(
        self,
        stock_code: str,
        evaluated_at: str,
    ) -> dict[str, Any] | None:
        main_broker = self._latest_main_broker(stock_code)
        investor_total = self._latest_investor_total(stock_code)
        investor_history = self._recent_investor_items(
            stock_code,
            "stock_investor_total",
            limit=5,
        )
        intraday = self._latest_intraday_investor(stock_code)
        intraday_history = self._recent_investor_items(
            stock_code,
            "intraday_investor_trend",
            limit=8,
        )
        foreign_rank = self._latest_foreign_rank(stock_code)
        program_history = self._recent_program_trade(stock_code, limit=5)
        if not any(
            (
                main_broker,
                investor_total,
                intraday,
                foreign_rank,
                program_history,
            )
        ):
            return None
        leader_context = self._latest_leader_context(stock_code)

        reasons: list[str] = []
        broker_score, broker_details = self._broker_score(main_broker, reasons)
        foreign_score, foreign_details = self._foreign_score(
            main_broker,
            foreign_rank,
            reasons,
        )
        investor_score, investor_details = self._investor_score(
            investor_total,
            intraday,
            investor_history,
            intraday_history,
            reasons,
        )
        program_score, program_details = self._program_score(
            program_history,
            reasons,
        )
        total = (
            50.0
            + broker_score * 0.30
            + foreign_score * 0.35
            + investor_score * 0.35
            + program_score * 0.15
        )
        total = max(0.0, min(100.0, total))
        leader_outflow = self._leader_supply_outflow(
            leader_context,
            total,
            broker_score,
            foreign_score,
            investor_score,
            investor_details,
        )
        blocked = False
        blocked_reason = None
        if broker_score <= -45:
            blocked = True
            blocked_reason = "상위 거래원 매도 압력"
        if foreign_score <= -45:
            blocked = True
            blocked_reason = blocked_reason or "외국계 창구 순매도"
        if investor_score <= -45:
            blocked = True
            blocked_reason = blocked_reason or "외국인·기관 동반 순매도"
        if investor_details.get("consecutive_net_sell_count", 0) >= 3:
            blocked = True
            blocked_reason = blocked_reason or "외국인·기관 연속 순매도"
        if program_details.get("hard_block"):
            blocked = True
            blocked_reason = blocked_reason or str(
                program_details.get("block_reason") or "프로그램 매도 집중"
            )
        if leader_outflow:
            blocked = True
            blocked_reason = blocked_reason or "대장주 후보 수급 이탈"
            reasons.append("대장주 후보 수급 이탈")
        stock_name = _first_text(
            [
                broker_details.get("stock_name"),
                investor_details.get("stock_name"),
                foreign_details.get("stock_name"),
                (leader_context or {}).get("stock_name"),
            ]
        )
        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "evaluated_at": evaluated_at,
            "total_score": round(total, 4),
            "broker_score": round(broker_score, 4),
            "foreign_broker_score": round(foreign_score, 4),
            "investor_score": round(investor_score, 4),
            "program_score": round(program_score, 4),
            "blocked": blocked,
            "blocked_reason": blocked_reason,
            "reasons": reasons or ["수급 데이터 부족, 중립 처리"],
            "components": {
                "broker": broker_details,
                "foreign_broker": foreign_details,
                "investor": investor_details,
                "program": program_details,
                "leader_context": leader_context or {"status": "missing"},
            },
        }

    def _latest_main_broker(self, stock_code: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            snapshot = connection.execute(
                """
                SELECT *
                FROM broker_flow_snapshots
                WHERE stock_code = ? AND flow_type = 'today_main_brokers'
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
            if snapshot is None:
                return None
            items = connection.execute(
                """
                SELECT *
                FROM broker_flow_items
                WHERE snapshot_id = ?
                ORDER BY side, rank
                """,
                (snapshot["id"],),
            ).fetchall()
        return {
            "snapshot": dict(snapshot),
            "items": [_row_with_json(row, "metadata_json", "metadata") for row in items],
        }

    def _latest_foreign_rank(self, stock_code: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT i.*, s.observed_at
                FROM broker_flow_items i
                JOIN broker_flow_snapshots s ON s.id = i.snapshot_id
                WHERE i.stock_code = ?
                  AND s.flow_type = 'foreign_broker_ranking'
                ORDER BY s.observed_at DESC, i.rank IS NULL, i.rank
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
        return _row_with_json(row, "metadata_json", "metadata") if row else None

    def _latest_investor_total(self, stock_code: str) -> dict[str, Any] | None:
        return self._latest_investor_item(stock_code, "stock_investor_total")

    def _latest_intraday_investor(self, stock_code: str) -> dict[str, Any] | None:
        return self._latest_investor_item(stock_code, "intraday_investor_trend")

    def _latest_investor_item(
        self,
        stock_code: str,
        flow_type: str,
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT i.*, s.observed_at
                FROM investor_flow_items i
                JOIN investor_flow_snapshots s ON s.id = i.snapshot_id
                WHERE COALESCE(i.stock_code, s.stock_code) = ?
                  AND s.flow_type = ?
                ORDER BY s.observed_at DESC, i.observed_key DESC, i.id DESC
                LIMIT 1
                """,
                (stock_code, flow_type),
            ).fetchone()
        return _row_with_json(row, "metadata_json", "metadata") if row else None

    def _recent_investor_items(
        self,
        stock_code: str,
        flow_type: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, s.observed_at
                FROM investor_flow_items i
                JOIN investor_flow_snapshots s ON s.id = i.snapshot_id
                WHERE COALESCE(i.stock_code, s.stock_code) = ?
                  AND s.flow_type = ?
                ORDER BY s.observed_at DESC, i.observed_key DESC, i.id DESC
                LIMIT ?
                """,
                (stock_code, flow_type, limit),
            ).fetchall()
        return [_row_with_json(row, "metadata_json", "metadata") for row in rows]

    def _latest_leader_context(self, stock_code: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                {Database.leader_stock_scores_latest_cte()}
                SELECT *
                FROM latest_leader_stock_scores
                WHERE stock_code = ?
                ORDER BY trade_date DESC, scored_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item.pop("components_json", None)
        return item

    def _recent_program_trade(
        self,
        stock_code: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*
                FROM program_trade_snapshots p
                WHERE p.stock_code = ?
                  AND p.id = (
                      SELECT MAX(latest.id)
                      FROM program_trade_snapshots latest
                      WHERE latest.stock_code = p.stock_code
                        AND COALESCE(latest.observed_date, '') =
                            COALESCE(p.observed_date, '')
                  )
                ORDER BY p.observed_date DESC, p.collected_at DESC, p.id DESC
                LIMIT ?
                """,
                (stock_code, limit),
            ).fetchall()
        return [_row_with_json(row, "metadata_json", "metadata") for row in rows]

    @staticmethod
    def _program_validation_summary(
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        collected = [
            row for row in rows if row.get("state") != "not_collected"
        ]
        return {
            "evaluated_count": len(rows),
            "collected_count": len(collected),
            "hard_block_count": sum(
                1 for row in collected if row.get("hard_block")
            ),
            "watch_count": sum(
                1 for row in collected if row.get("state") == "watch"
            ),
            "positive_count": sum(
                1 for row in collected if row.get("state") == "positive"
            ),
            "legacy_block_candidate_count": sum(
                1
                for row in collected
                if row.get("legacy_block_candidate")
            ),
            "legacy_block_relaxed_count": sum(
                1 for row in collected if row.get("legacy_block_relaxed")
            ),
            "rows": rows,
        }

    @staticmethod
    def _leader_supply_outflow(
        leader_context: dict[str, Any] | None,
        total_score: float,
        broker_score: float,
        foreign_score: float,
        investor_score: float,
        investor_details: dict[str, Any],
    ) -> bool:
        if not leader_context:
            return False
        if leader_context.get("role") not in {"leader", "second"}:
            return False
        weak_components = sum(
            1
            for value in (broker_score, foreign_score, investor_score)
            if value <= -25.0
        )
        consecutive_sell = int(
            investor_details.get("consecutive_net_sell_count") or 0
        )
        return total_score <= 35.0 or (
            total_score <= 45.0
            and weak_components >= 2
            and consecutive_sell >= 2
        )

    @staticmethod
    def _broker_score(
        data: dict[str, Any] | None,
        reasons: list[str],
    ) -> tuple[float, dict[str, Any]]:
        if not data:
            return 0.0, {"status": "missing"}
        snapshot = data["snapshot"]
        items = data["items"]
        buy = sum(max(0, _int(item.get("quantity"))) for item in items if item["side"] == "buy")
        sell = sum(abs(min(0, _int(item.get("quantity")))) for item in items if item["side"] == "sell")
        total = buy + sell
        pressure = (buy - sell) / total if total > 0 else 0.0
        score = max(-60.0, min(60.0, pressure * 70.0))
        if pressure >= 0.2:
            reasons.append("상위 거래원 매수 우위")
        elif pressure <= -0.2:
            reasons.append("상위 거래원 매도 우위")
        return score, {
            "stock_name": _first_text(item.get("stock_name") for item in items),
            "buy_quantity": buy,
            "sell_quantity": sell,
            "pressure": round(pressure, 4),
            "observed_at": snapshot["observed_at"],
        }

    @staticmethod
    def _foreign_score(
        main_broker: dict[str, Any] | None,
        foreign_rank: dict[str, Any] | None,
        reasons: list[str],
    ) -> tuple[float, dict[str, Any]]:
        score = 0.0
        details: dict[str, Any] = {"status": "missing"}
        if main_broker:
            snapshot = main_broker["snapshot"]
            net = _int(snapshot.get("foreign_net_estimated"))
            buy = abs(_int(snapshot.get("foreign_buy_estimated")))
            sell = abs(_int(snapshot.get("foreign_sell_estimated")))
            scale = max(buy + sell, 1)
            estimated_score = max(-45.0, min(45.0, (net / scale) * 70.0))
            score += estimated_score
            details = {
                "foreign_buy_estimated": snapshot.get("foreign_buy_estimated"),
                "foreign_sell_estimated": snapshot.get("foreign_sell_estimated"),
                "foreign_net_estimated": net,
                "estimated_score": round(estimated_score, 4),
                "observed_at": snapshot["observed_at"],
            }
            if net > 0:
                reasons.append("외국계 창구 매수 추정 우위")
            elif net < 0:
                reasons.append("외국계 창구 매도 추정 우위")
        if foreign_rank:
            quantity = _int(foreign_rank.get("quantity"))
            rank = foreign_rank.get("rank")
            rank_bonus = 15.0 if rank is not None and int(rank) <= 20 else 8.0
            if quantity > 0:
                score += rank_bonus
                reasons.append(f"외국계 창구 순매수 상위 {rank}위")
            elif quantity < 0:
                score -= rank_bonus
                reasons.append(f"외국계 창구 순매도 상위 {rank}위")
            details["rank"] = rank
            details["rank_quantity"] = quantity
            details["stock_name"] = foreign_rank.get("stock_name")
        return max(-60.0, min(60.0, score)), details

    @staticmethod
    def _program_score(
        history: list[dict[str, Any]],
        reasons: list[str],
    ) -> tuple[float, dict[str, Any]]:
        if not history:
            return 0.0, {"status": "not_collected"}
        latest = history[0]
        net_amount = _int(latest.get("net_buy_amount"))
        buy_amount = abs(_int(latest.get("buy_amount")))
        sell_amount = abs(_int(latest.get("sell_amount")))
        net_quantity = _int(latest.get("net_buy_quantity"))
        buy_quantity = abs(_int(latest.get("buy_quantity")))
        sell_quantity = abs(_int(latest.get("sell_quantity")))
        if buy_amount + sell_amount > 0:
            pressure = net_amount / max(buy_amount + sell_amount, 1)
            basis = "amount"
        else:
            pressure = net_quantity / max(buy_quantity + sell_quantity, 1)
            basis = "quantity"
        recent_net = 0
        recent_gross = 0
        for row in history:
            row_buy = abs(_int(row.get("buy_amount")))
            row_sell = abs(_int(row.get("sell_amount")))
            row_net = _int(row.get("net_buy_amount"))
            if row_buy + row_sell == 0:
                row_buy = abs(_int(row.get("buy_quantity")))
                row_sell = abs(_int(row.get("sell_quantity")))
                row_net = _int(row.get("net_buy_quantity"))
            recent_net += row_net
            recent_gross += row_buy + row_sell
        recent_pressure = recent_net / max(recent_gross, 1)
        score = max(
            -60.0,
            min(60.0, pressure * 45.0 + recent_pressure * 55.0),
        )
        consecutive_sell = 0
        for row in history:
            net = _int(row.get("net_buy_amount"))
            if net == 0:
                net = _int(row.get("net_buy_quantity"))
            if net >= 0:
                break
            consecutive_sell += 1
        if pressure >= 0.15 or recent_pressure >= 0.10:
            reasons.append("프로그램 순매수 우위")
        elif pressure <= -0.15 or recent_pressure <= -0.10:
            reasons.append("프로그램 순매도 우위")
        if consecutive_sell >= 2 and recent_pressure < 0:
            score = max(
                -60.0,
                score - min(12.0, (consecutive_sell - 1) * 3.0),
            )
            reasons.append(f"프로그램 {consecutive_sell}일 연속 순매도")
        hard_block = False
        block_reason = None
        if pressure <= -0.25:
            hard_block = True
            block_reason = "프로그램 당일 매도 집중"
        elif consecutive_sell >= 3 and recent_pressure <= -0.10:
            hard_block = True
            block_reason = "프로그램 연속·누적 순매도"
        elif (
            consecutive_sell >= 2
            and pressure <= -0.15
            and _int(latest.get("net_buy_amount_change")) < 0
        ):
            hard_block = True
            block_reason = "프로그램 순매도 확대"
        legacy_block_candidate = consecutive_sell >= 3 or score <= -45.0
        legacy_block_relaxed = legacy_block_candidate and not hard_block
        if hard_block:
            policy_state = "block"
        elif (
            legacy_block_candidate
            or pressure <= -0.08
            or recent_pressure <= -0.05
        ):
            policy_state = "watch"
        elif pressure >= 0.08 or recent_pressure >= 0.05:
            policy_state = "positive"
        else:
            policy_state = "neutral"
        return score, {
            "status": "collected",
            "policy_version": "2",
            "policy_state": policy_state,
            "hard_block": hard_block,
            "block_reason": block_reason,
            "legacy_block_candidate": legacy_block_candidate,
            "legacy_block_relaxed": legacy_block_relaxed,
            "basis": basis,
            "observed_date": latest.get("observed_date"),
            "net_buy_amount": net_amount,
            "net_buy_amount_change": latest.get("net_buy_amount_change"),
            "net_buy_quantity": net_quantity,
            "net_buy_quantity_change": latest.get("net_buy_quantity_change"),
            "pressure": round(pressure, 4),
            "recent_pressure": round(recent_pressure, 4),
            "recent_net_value": recent_net,
            "recent_gross_value": recent_gross,
            "unique_date_count": len(history),
            "consecutive_net_sell_count": consecutive_sell,
        }

    @staticmethod
    def _investor_score(
        total: dict[str, Any] | None,
        intraday: dict[str, Any] | None,
        total_history: list[dict[str, Any]],
        intraday_history: list[dict[str, Any]],
        reasons: list[str],
    ) -> tuple[float, dict[str, Any]]:
        details: dict[str, Any] = {}
        score = 0.0
        for label, item, weight in (
            ("기간합계", total, 1.0),
            ("장중", intraday, 0.8),
        ):
            if not item:
                continue
            foreigner = _int(item.get("foreigner"))
            institution = _int(item.get("institution"))
            combined = foreigner + institution
            scale = max(abs(foreigner) + abs(institution), 1)
            part = max(-45.0, min(45.0, (combined / scale) * 60.0)) * weight
            score += part
            details[label] = {
                "foreigner": foreigner,
                "institution": institution,
                "combined": combined,
                "score": round(part, 4),
                "observed_key": item.get("observed_key"),
            }
            details["stock_name"] = item.get("stock_name") or details.get("stock_name")
            if combined > 0:
                reasons.append(f"{label} 외국인·기관 순매수")
            elif combined < 0:
                reasons.append(f"{label} 외국인·기관 순매도")
        trend_penalty, trend_details = _trend_score(total_history, intraday_history)
        if trend_penalty:
            score += trend_penalty
            details["trend"] = trend_details
            if trend_details["consecutive_net_sell_count"] >= 3:
                reasons.append("외국인·기관 연속 순매도")
            elif trend_details["consecutive_net_buy_count"] >= 3:
                reasons.append("외국인·기관 연속 순매수")
        details["consecutive_net_sell_count"] = trend_details.get(
            "consecutive_net_sell_count",
            0,
        )
        details["consecutive_net_buy_count"] = trend_details.get(
            "consecutive_net_buy_count",
            0,
        )
        if not details:
            return 0.0, {"status": "missing"}
        return max(-60.0, min(60.0, score)), details


def _row_with_json(row: Any, json_key: str, target_key: str) -> dict[str, Any]:
    item = dict(row)
    try:
        item[target_key] = json.loads(item.pop(json_key) or "{}")
    except (TypeError, json.JSONDecodeError):
        item[target_key] = {}
    return item


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _trend_score(
    total_history: list[dict[str, Any]],
    intraday_history: list[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    combined_rows = total_history or intraday_history
    combined_values = [
        _int(row.get("foreigner")) + _int(row.get("institution"))
        for row in combined_rows
    ]
    sell_count = 0
    for value in combined_values:
        if value < 0:
            sell_count += 1
        else:
            break
    buy_count = 0
    for value in combined_values:
        if value > 0:
            buy_count += 1
        else:
            break
    score = 0.0
    if sell_count >= 3:
        score -= min(30.0, 10.0 + sell_count * 5.0)
    elif buy_count >= 3:
        score += min(25.0, 8.0 + buy_count * 4.0)
    if intraday_history:
        recent = [
            _int(row.get("foreigner")) + _int(row.get("institution"))
            for row in intraday_history[:3]
        ]
        if len(recent) >= 3 and recent[0] < recent[-1]:
            score -= 8.0
        elif len(recent) >= 3 and recent[0] > recent[-1]:
            score += 6.0
    return score, {
        "consecutive_net_sell_count": sell_count,
        "consecutive_net_buy_count": buy_count,
        "recent_combined_values": combined_values[:5],
        "intraday_recent_values": [
            _int(row.get("foreigner")) + _int(row.get("institution"))
            for row in intraday_history[:5]
        ],
        "score": round(score, 4),
    }
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_text(values: Any) -> str | None:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return None
