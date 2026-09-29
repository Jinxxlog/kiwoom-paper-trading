import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from data.scheduler import SEOUL_TIMEZONE
from db.database import Database
from screening.stock_category import STOCK_CATEGORY_LABELS, stock_category_payload


class TradingReviewReport:
    """Builds an end-of-day paper trading review focused on loss drivers."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def build(
        self,
        target_date: date,
        *,
        now: Optional[datetime] = None,
        decision_limit: Optional[int] = None,
    ) -> dict[str, Any]:
        day = target_date.isoformat()
        generated_at = now or datetime.now(SEOUL_TIMEZONE)
        if generated_at.tzinfo is not None:
            generated_at = generated_at.astimezone(SEOUL_TIMEZONE)

        fills = self._fills(day)
        decisions = self._decisions(day, limit=decision_limit)
        risk_events = self._risk_events(day)
        forward_outcomes = self._forward_outcomes(day, limit=decision_limit)
        buy_contexts = self._buy_contexts(fills, decisions)
        matched_sells = self._matched_sells(fills, buy_contexts)
        exit_breakdown = self._risk_exit_breakdown(matched_sells, risk_events)
        router_diagnostics = self._strategy_router_diagnostics(
            decisions,
            buy_contexts,
            matched_sells,
        )
        blocked_forward_review = self._blocked_forward_review(
            decisions,
            forward_outcomes,
        )
        best_forward_outcome = self._best_outcome_by_decision(forward_outcomes)
        legacy_retirement_review = self._legacy_retirement_review(
            decisions,
            buy_contexts,
            matched_sells,
            best_forward_outcome,
            router_diagnostics,
        )
        armed_candidates = self.database.armed_candidates_for_day(day)

        return {
            "report_date": day,
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "summary": self._summary(fills, exit_breakdown),
            "risk_exit_breakdown": exit_breakdown["summary"],
            "time_buckets": self._time_buckets(fills),
            "entry_type_performance": self._entry_type_performance(
                buy_contexts,
                matched_sells,
                exit_breakdown,
            ),
            "strategy_router_diagnostics": router_diagnostics,
            "legacy_retirement_review": legacy_retirement_review,
            "a_plus_armed_candidates": armed_candidates,
            "strategy_dashboard_review": self._strategy_dashboard_review(
                decisions,
                buy_contexts,
                matched_sells,
                forward_outcomes,
                exit_breakdown,
                router_diagnostics,
                blocked_forward_review,
                legacy_retirement_review,
                armed_candidates,
            ),
            "stock_repeat_buys": self._stock_repeat_buys(
                buy_contexts,
                matched_sells,
            ),
            "missing_supply_performance": self._flag_performance(
                buy_contexts,
                matched_sells,
                "supply_missing",
            ),
            "force_entry_performance": self._flag_performance(
                buy_contexts,
                matched_sells,
                "force_entry",
            ),
            "buys_after_stop_loss": self._buys_after_stop_loss(
                buy_contexts,
                exit_breakdown,
            ),
            "preventable_loss_estimate": self._preventable_loss_estimate(
                matched_sells,
            ),
            "blocked_forward_review": blocked_forward_review,
        }

    def export_json(
        self,
        target_date: date,
        output: Optional[Path] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Path:
        path = output or Path("reports") / f"trading-review-{target_date.isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.build(target_date, now=now)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _fills(self, day: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.id AS fill_id, f.order_id, f.stock_code, f.side,
                       f.quantity, f.market_price, f.fill_price,
                       f.gross_amount, f.commission, f.tax,
                       f.slippage_amount, f.realized_pnl, f.price_time,
                       f.filled_at, o.account_id, a.name AS account_name,
                       o.reason AS order_reason, o.requested_at,
                       r.decision_id, d.score AS decision_score,
                       d.reason AS decision_reason, tp.entry_type,
                       tp.entry_tags_json, tp.setup_json
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                JOIN paper_accounts a ON a.id = o.account_id
                LEFT JOIN order_executions e
                  ON e.internal_order_id = o.id
                LEFT JOIN order_requests r
                  ON r.id = e.order_request_id
                LEFT JOIN strategy_decisions d
                  ON d.id = r.decision_id
                LEFT JOIN trade_plans tp
                  ON tp.decision_id = d.id
                WHERE substr(f.filled_at, 1, 10) = ?
                ORDER BY f.filled_at, f.id
                """,
                (day,),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def _decisions(
        self,
        day: str,
        *,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        limit_clause = ""
        parameters: list[Any] = [day]
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(max(1, int(limit)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT d.id AS decision_id, d.account_id,
                       a.name AS account_name, d.stock_code,
                       universe.stock_name, d.decided_at,
                       d.source_data_time, d.action, d.score, d.confidence,
                       d.reason, d.outcome, d.message, tp.entry_type,
                       tp.entry_tags_json, tp.setup_json,
                       replay.block_reasons_json,
                       replay.supply_score_json,
                       replay.leader_score_json
                FROM strategy_decisions d
                LEFT JOIN paper_accounts a ON a.id = d.account_id
                LEFT JOIN trade_plans tp ON tp.decision_id = d.id
                LEFT JOIN strategy_replay_snapshots replay
                  ON replay.decision_id = d.id
                LEFT JOIN stock_universe universe
                  ON universe.stock_code = d.stock_code
                WHERE substr(d.decided_at, 1, 10) = ?
                ORDER BY d.decided_at DESC, d.id DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        rows = list(reversed(rows))
        decisions = []
        for row in rows:
            item = self._row_dict(row)
            item["setup"] = self._loads(item.pop("setup_json", None), {})
            item["entry_tags"] = self._loads(item.pop("entry_tags_json", None), [])
            item["block_reasons"] = self._loads(
                item.pop("block_reasons_json", None),
                [],
            )
            item["supply_score_snapshot"] = self._loads(
                item.pop("supply_score_json", None),
                {},
            )
            item["leader_score_snapshot"] = self._loads(
                item.pop("leader_score_json", None),
                {},
            )
            item["stock_category"] = stock_category_payload(
                item.get("stock_code"),
                item.get("stock_name"),
            )
            item.update(self._context_flags(item))
            decisions.append(item)
        return decisions

    def _risk_events(self, day: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.account_id, a.name AS account_name,
                       e.stock_code, e.event_type, e.trigger_price,
                       e.threshold_price, e.highest_price, e.order_id,
                       e.status, e.exit_class, e.exit_reason_class,
                       e.setup_invalidated, e.classification_json,
                       e.message, e.occurred_at
                FROM paper_risk_events e
                JOIN paper_accounts a ON a.id = e.account_id
                WHERE substr(e.occurred_at, 1, 10) = ?
                ORDER BY e.occurred_at, e.id
                """,
                (day,),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def _forward_outcomes(
        self,
        day: str,
        *,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        decision_filter = ""
        parameters: list[Any] = [day]
        if limit is not None:
            decision_filter = """
                AND d.id IN (
                    SELECT recent.id
                    FROM strategy_decisions recent
                    WHERE substr(recent.decided_at, 1, 10) = ?
                    ORDER BY recent.decided_at DESC, recent.id DESC
                    LIMIT ?
                )
            """
            parameters.extend([day, max(1, int(limit))])
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT o.decision_id, o.stock_code, o.horizon_minutes,
                       o.max_up_rate, o.max_down_rate, o.close_rate,
                       o.actual_order_status, o.actual_outcome,
                       o.details_json, o.evaluated_at
                FROM decision_forward_outcomes o
                JOIN strategy_decisions d ON d.id = o.decision_id
                WHERE substr(d.decided_at, 1, 10) = ?
                {decision_filter}
                ORDER BY d.decided_at, o.decision_id, o.horizon_minutes
                """,
                parameters,
            ).fetchall()
        outcomes = []
        for row in rows:
            item = self._row_dict(row)
            item["details"] = self._loads(item.pop("details_json", None), {})
            outcomes.append(item)
        return outcomes

    def _buy_contexts(
        self,
        fills: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        decision_by_id = {
            int(item["decision_id"]): item
            for item in decisions
            if item.get("decision_id") is not None
        }
        contexts = []
        for fill in fills:
            if fill["side"] != "buy":
                continue
            decision = None
            if fill.get("decision_id") is not None:
                decision = decision_by_id.get(int(fill["decision_id"]))
            setup = self._loads(fill.get("setup_json"), {})
            if decision is None:
                decision = {
                    "decision_id": fill.get("decision_id"),
                    "account_id": fill.get("account_id"),
                    "account_name": fill.get("account_name"),
                    "stock_code": fill["stock_code"],
                    "decided_at": fill["filled_at"],
                    "score": fill.get("decision_score"),
                    "reason": fill.get("decision_reason")
                    or fill.get("order_reason")
                    or "",
                    "entry_type": fill.get("entry_type") or "manual",
                    "entry_tags": self._loads(fill.get("entry_tags_json"), []),
                    "setup": setup,
                }
                decision.update(self._context_flags(decision))
            context = dict(decision)
            context.update(
                {
                    "fill_id": fill["fill_id"],
                    "order_id": fill["order_id"],
                    "buy_filled_at": fill["filled_at"],
                    "buy_quantity": int(fill["quantity"] or 0),
                    "buy_amount": int(fill["gross_amount"] or 0),
                    "buy_price": int(fill["fill_price"] or 0),
                }
            )
            contexts.append(context)
        return sorted(
            contexts,
            key=lambda item: (
                str(item.get("account_id") or ""),
                str(item.get("stock_code") or ""),
                str(item.get("buy_filled_at") or item.get("decided_at") or ""),
                int(item.get("fill_id") or 0),
            ),
        )

    def _matched_sells(
        self,
        fills: list[dict[str, Any]],
        buy_contexts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        buys_by_key: dict[tuple[Any, str], list[dict[str, Any]]] = defaultdict(list)
        for buy in buy_contexts:
            buys_by_key[(buy.get("account_id"), buy["stock_code"])].append(buy)

        matched = []
        for fill in fills:
            if fill["side"] != "sell":
                continue
            key = (fill.get("account_id"), fill["stock_code"])
            sell_time = str(fill["filled_at"])
            candidates = [
                buy
                for buy in buys_by_key.get(key, [])
                if str(buy.get("buy_filled_at") or buy.get("decided_at") or "")
                <= sell_time
            ]
            buy = candidates[-1] if candidates else None
            item = dict(fill)
            if buy is not None:
                item["matched_buy"] = buy
                item["entry_type"] = buy.get("entry_type") or "unknown"
                item["force_entry"] = bool(buy.get("force_entry"))
                item["supply_missing"] = bool(buy.get("supply_missing"))
                item["entry_grade"] = buy.get("entry_grade")
            else:
                item["matched_buy"] = None
                item["entry_type"] = fill.get("entry_type") or "unmatched"
                item["force_entry"] = False
                item["supply_missing"] = False
                item["entry_grade"] = None
            matched.append(item)
        return matched

    def _summary(
        self,
        fills: list[dict[str, Any]],
        exit_breakdown: dict[str, Any],
    ) -> dict[str, Any]:
        buy_fills = [item for item in fills if item["side"] == "buy"]
        sell_fills = [item for item in fills if item["side"] == "sell"]
        realized_pnl = sum(int(item["realized_pnl"] or 0) for item in sell_fills)
        costs = sum(
            int(item["commission"] or 0)
            + int(item["tax"] or 0)
                + int(item["slippage_amount"] or 0)
            for item in fills
        )
        breakdown = exit_breakdown["summary"]
        return {
            "buy_count": len(buy_fills),
            "sell_count": len(sell_fills),
            "filled_count": len(fills),
            "gross_buy_amount": sum(int(item["gross_amount"] or 0) for item in buy_fills),
            "gross_sell_amount": sum(int(item["gross_amount"] or 0) for item in sell_fills),
            "realized_pnl": realized_pnl,
            "total_cost": costs,
            "winning_sell_count": sum(
                1 for item in sell_fills if int(item["realized_pnl"] or 0) > 0
            ),
            "losing_sell_count": sum(
                1 for item in sell_fills if int(item["realized_pnl"] or 0) < 0
            ),
            "stop_loss_count": int(breakdown["stop_loss_count"]),
            "loss_stop_count": int(breakdown["loss_stop_count"]),
            "profit_protect_stop_count": int(
                breakdown["profit_protect_stop_count"]
            ),
            "trailing_profit_lock_count": int(
                breakdown["trailing_profit_lock_count"]
            ),
            "risk_exit_realized_pnl": int(breakdown["risk_exit_realized_pnl"]),
            "loss_stop_realized_pnl": int(breakdown["loss_stop_realized_pnl"]),
            "profit_protect_stop_realized_pnl": int(
                breakdown["profit_protect_stop_realized_pnl"]
            ),
            "trailing_profit_lock_realized_pnl": int(
                breakdown["trailing_profit_lock_realized_pnl"]
            ),
        }

    def _risk_exit_breakdown(
        self,
        matched_sells: list[dict[str, Any]],
        risk_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sell_by_order_id = {
            int(item["order_id"]): item
            for item in matched_sells
            if item.get("order_id") is not None
        }
        events = []
        by_order_id: dict[int, dict[str, Any]] = {}
        summary = {
            "stop_loss_count": 0,
            "loss_stop_count": 0,
            "profit_protect_stop_count": 0,
            "trailing_profit_lock_count": 0,
            "trailing_loss_stop_count": 0,
            "risk_exit_count": 0,
            "risk_exit_realized_pnl": 0,
            "loss_stop_realized_pnl": 0,
            "profit_protect_stop_realized_pnl": 0,
            "trailing_profit_lock_realized_pnl": 0,
            "trailing_loss_stop_realized_pnl": 0,
            "unmatched_risk_exit_count": 0,
            "items": [],
        }
        for event in risk_events:
            if event.get("status") != "executed":
                continue
            event_type = str(event.get("event_type") or "")
            if event_type not in {"stop_loss", "trailing_stop"}:
                continue
            order_id = event.get("order_id")
            sell = (
                sell_by_order_id.get(int(order_id))
                if order_id is not None
                else None
            )
            pnl = int((sell or {}).get("realized_pnl") or 0)
            exit_class = str(
                event.get("exit_class")
                or self._risk_exit_class(event_type, pnl)
            )
            item = {
                **event,
                "exit_class": exit_class,
                "exit_reason_class": event.get("exit_reason_class")
                or exit_class,
                "setup_invalidated": bool(event.get("setup_invalidated")),
                "classification": self._loads(
                    event.get("classification_json"),
                    {},
                ),
                "realized_pnl": pnl,
                "entry_type": (sell or {}).get("entry_type"),
                "matched_sell": sell is not None,
            }
            events.append(item)
            if order_id is not None:
                by_order_id[int(order_id)] = item
            summary["risk_exit_count"] += 1
            summary["risk_exit_realized_pnl"] += pnl
            if sell is None:
                summary["unmatched_risk_exit_count"] += 1
            if event_type == "stop_loss":
                summary["stop_loss_count"] += 1
            count_key = f"{exit_class}_count"
            pnl_key = f"{exit_class}_realized_pnl"
            if count_key in summary:
                summary[count_key] += 1
            if pnl_key in summary:
                summary[pnl_key] += pnl
        summary["items"] = events
        return {
            "by_order_id": by_order_id,
            "events": events,
            "summary": summary,
        }

    @staticmethod
    def _risk_exit_class(event_type: str, realized_pnl: int) -> str:
        if event_type == "trailing_stop":
            return (
                "trailing_profit_lock"
                if realized_pnl >= 0
                else "trailing_loss_stop"
            )
        if realized_pnl < 0:
            return "loss_stop"
        return "profit_protect_stop"

    def _time_buckets(self, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = {}
        for fill in fills:
            bucket = self._ten_minute_bucket(str(fill["filled_at"]))
            item = buckets.setdefault(
                bucket,
                {
                    "bucket": bucket,
                    "buy_count": 0,
                    "sell_count": 0,
                    "realized_pnl": 0,
                    "total_cost": 0,
                    "stock_codes": set(),
                },
            )
            if fill["side"] == "buy":
                item["buy_count"] += 1
            else:
                item["sell_count"] += 1
                item["realized_pnl"] += int(fill["realized_pnl"] or 0)
            item["total_cost"] += (
                int(fill["commission"] or 0)
                + int(fill["tax"] or 0)
                + int(fill["slippage_amount"] or 0)
            )
            item["stock_codes"].add(fill["stock_code"])
        return [
            {
                **item,
                "stock_codes": sorted(item["stock_codes"]),
            }
            for _, item in sorted(buckets.items())
        ]

    def _entry_type_performance(
        self,
        buy_contexts: list[dict[str, Any]],
        matched_sells: list[dict[str, Any]],
        exit_breakdown: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        exit_by_order_id = exit_breakdown["by_order_id"]
        for buy in buy_contexts:
            entry_type = str(buy.get("entry_type") or "unknown")
            row = rows.setdefault(entry_type, self._performance_row(entry_type))
            row["buy_count"] += 1
            row["buy_amount"] += int(buy.get("buy_amount") or 0)
        for sell in matched_sells:
            entry_type = str(sell.get("entry_type") or "unknown")
            row = rows.setdefault(entry_type, self._performance_row(entry_type))
            pnl = int(sell["realized_pnl"] or 0)
            row["sell_count"] += 1
            row["realized_pnl"] += pnl
            if pnl > 0:
                row["winning_sell_count"] += 1
            elif pnl < 0:
                row["losing_sell_count"] += 1
            risk_exit = exit_by_order_id.get(int(sell["order_id"]))
            if risk_exit is not None:
                exit_class = str(risk_exit.get("exit_class") or "")
                row["risk_exit_count"] += 1
                if exit_class == "loss_stop":
                    row["loss_stop_count"] += 1
                    row["loss_stop_realized_pnl"] += pnl
                elif exit_class == "profit_protect_stop":
                    row["profit_protect_stop_count"] += 1
                    row["profit_protect_stop_realized_pnl"] += pnl
                elif exit_class == "trailing_profit_lock":
                    row["trailing_profit_lock_count"] += 1
                    row["trailing_profit_lock_realized_pnl"] += pnl
                elif exit_class == "trailing_loss_stop":
                    row["trailing_loss_stop_count"] += 1
                    row["trailing_loss_stop_realized_pnl"] += pnl
            if (
                risk_exit is not None
                and risk_exit.get("event_type") == "stop_loss"
            ):
                row["stop_loss_count"] += 1
        return sorted(
            rows.values(),
            key=lambda item: (item["realized_pnl"], -item["buy_count"], item["entry_type"]),
        )

    def _stock_repeat_buys(
        self,
        buy_contexts: list[dict[str, Any]],
        matched_sells: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: dict[tuple[Any, str], dict[str, Any]] = {}
        for buy in buy_contexts:
            key = (buy.get("account_name"), buy["stock_code"])
            row = rows.setdefault(
                key,
                {
                    "account_name": buy.get("account_name"),
                    "stock_code": buy["stock_code"],
                    "buy_count": 0,
                    "total_quantity": 0,
                    "buy_amount": 0,
                    "realized_pnl": 0,
                    "entry_types": set(),
                },
            )
            row["buy_count"] += 1
            row["total_quantity"] += int(buy.get("buy_quantity") or 0)
            row["buy_amount"] += int(buy.get("buy_amount") or 0)
            row["entry_types"].add(str(buy.get("entry_type") or "unknown"))
        for sell in matched_sells:
            key = (sell.get("account_name"), sell["stock_code"])
            if key in rows:
                rows[key]["realized_pnl"] += int(sell["realized_pnl"] or 0)
        result = []
        for row in rows.values():
            if row["buy_count"] < 2:
                continue
            result.append(
                {
                    **row,
                    "entry_types": sorted(row["entry_types"]),
                }
            )
        return sorted(result, key=lambda item: (item["realized_pnl"], item["stock_code"]))

    def _strategy_router_diagnostics(
        self,
        decisions: list[dict[str, Any]],
        buy_contexts: list[dict[str, Any]],
        matched_sells: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry_rows: dict[str, dict[str, Any]] = {}
        playbook_rows: dict[str, dict[str, Any]] = {}
        leader_rows: dict[str, dict[str, Any]] = {
            key: self._router_group_row(key, label)
            for key, label in (
                ("trend_day_runner", "A+ 대장주 Trend-Day Runner"),
                ("vwap_pullback", "VWAP 눌림목/회복"),
                ("new_high_rebreakout", "신고가 재돌파/재진입"),
                ("unmapped", "3대 전략 미매핑"),
            )
        }
        guard_status_counts: dict[str, int] = defaultdict(int)
        reentry_status_counts: dict[str, int] = defaultdict(int)
        router_status_counts: dict[str, int] = defaultdict(int)
        router_order_effect_counts: dict[str, int] = defaultdict(int)
        block_reason_counts: dict[str, int] = defaultdict(int)
        legacy_shadow_comparison: dict[str, Any] = {
            "old_logic_buy_count": 0,
            "new_logic_buy_count": 0,
            "new_logic_shadow_count": 0,
            "minimum_probe_count": 0,
            "removed_from_order_influence_count": 0,
            "by_legacy_route": defaultdict(
                lambda: {
                    "legacy_route": "",
                    "decision_count": 0,
                    "old_logic_buy_count": 0,
                    "new_logic_buy_count": 0,
                    "new_logic_shadow_count": 0,
                    "minimum_probe_count": 0,
                    "removed_from_order_influence_count": 0,
                    "entry_types": set(),
                }
            ),
        }

        for decision in decisions:
            setup = self._setup(decision)
            entry_type = str(decision.get("entry_type") or "unknown")
            playbook = self._playbook_name(setup)
            leader_strategy = self._leader_strategy_key(entry_type, playbook, setup)
            guard_status = self._policy_status(
                setup.get("entry_guard_policy"),
                default="not_recorded",
            )
            reentry_status = self._policy_status(
                setup.get("reentry_policy"),
                default="not_recorded",
            )
            router_policy = setup.get("router_execution_policy")
            router_status = self._policy_status(
                router_policy,
                default="not_recorded",
            )
            router_order_effect = self._router_order_effect(router_policy)
            legacy_reason = self._legacy_candidate_reason(entry_type, playbook)
            legacy_policy = setup.get("legacy_entry_policy")
            if not isinstance(legacy_policy, dict):
                legacy_policy = {}
            legacy_reasons = [
                str(reason)
                for reason in (legacy_policy.get("legacy_reasons") or [])
                if str(reason)
            ]
            if not legacy_reasons and legacy_reason is not None:
                legacy_reasons = [legacy_reason]
            legacy_candidate = bool(
                legacy_policy.get("legacy_candidate")
                if legacy_policy
                else legacy_reason is not None
            )
            legacy_route = str(
                legacy_policy.get("legacy_route")
                or (legacy_reasons[0] if legacy_reasons else "none")
            )
            action = str(decision.get("action") or "").lower()
            outcome = str(decision.get("outcome") or "").lower()
            old_logic_would_buy = bool(
                legacy_policy.get("old_logic_would_buy")
                if legacy_policy
                else legacy_candidate and action == "buy"
            )
            new_logic_would_buy = bool(
                legacy_policy.get("new_logic_would_buy")
                if legacy_policy
                else router_order_effect in {"allow_order", "allow_minimum_probe"}
            )
            removed_from_order_influence = bool(
                legacy_policy.get("removed_from_order_influence")
                if legacy_policy
                else legacy_candidate and router_order_effect == "shadow_only"
            )

            entry_row = entry_rows.setdefault(
                entry_type,
                self._router_entry_row(entry_type),
            )
            playbook_row = playbook_rows.setdefault(
                playbook,
                self._router_group_row(playbook, playbook),
            )
            leader_row = leader_rows.setdefault(
                leader_strategy,
                self._router_group_row(leader_strategy, leader_strategy),
            )
            for row in (entry_row, playbook_row, leader_row):
                row["decision_count"] += 1
                if action == "buy":
                    row["buy_signal_count"] += 1
                if outcome == "executed":
                    row["executed_decision_count"] += 1
                if outcome in {"skipped", "failed"} or action in {"hold", "sell"}:
                    row["blocked_decision_count"] += 1
                self._count_status(row["guard_status_counts"], guard_status)
                self._count_status(row["reentry_status_counts"], reentry_status)
                self._count_status(row["router_status_counts"], router_status)
                self._count_status(
                    row["router_order_effect_counts"],
                    router_order_effect,
                )
                if router_order_effect == "shadow_only":
                    row["shadow_only_count"] += 1

            if old_logic_would_buy:
                entry_row["old_logic_buy_count"] += 1
                legacy_shadow_comparison["old_logic_buy_count"] += 1
            if new_logic_would_buy:
                entry_row["new_logic_buy_count"] += 1
                legacy_shadow_comparison["new_logic_buy_count"] += 1
            if legacy_candidate and router_order_effect == "shadow_only":
                entry_row["legacy_shadow_count"] += 1
                legacy_shadow_comparison["new_logic_shadow_count"] += 1
            if legacy_candidate and router_order_effect == "allow_minimum_probe":
                entry_row["minimum_probe_count"] += 1
                legacy_shadow_comparison["minimum_probe_count"] += 1
            if removed_from_order_influence:
                entry_row["removed_from_order_influence_count"] += 1
                legacy_shadow_comparison["removed_from_order_influence_count"] += 1
            if legacy_candidate:
                route_row = legacy_shadow_comparison["by_legacy_route"][
                    legacy_route
                ]
                route_row["legacy_route"] = legacy_route
                route_row["decision_count"] += 1
                route_row["entry_types"].add(entry_type)
                if old_logic_would_buy:
                    route_row["old_logic_buy_count"] += 1
                if new_logic_would_buy:
                    route_row["new_logic_buy_count"] += 1
                if router_order_effect == "shadow_only":
                    route_row["new_logic_shadow_count"] += 1
                if router_order_effect == "allow_minimum_probe":
                    route_row["minimum_probe_count"] += 1
                if removed_from_order_influence:
                    route_row["removed_from_order_influence_count"] += 1

            entry_row["playbook_counts"][playbook] = (
                entry_row["playbook_counts"].get(playbook, 0) + 1
            )
            entry_row["leader_strategy_counts"][leader_strategy] = (
                entry_row["leader_strategy_counts"].get(leader_strategy, 0) + 1
            )
            entry_row["leader_strategy"] = self._dominant_key(
                entry_row["leader_strategy_counts"],
                default="unmapped",
            )
            entry_row["three_strategy_mapped"] = (
                entry_row["leader_strategy"] != "unmapped"
            )
            entry_row["legacy_candidate"] = bool(
                entry_row["legacy_candidate"] or legacy_candidate
            )
            for reason in legacy_reasons:
                entry_row["legacy_reasons"].add(reason)

            guard_status_counts[guard_status] += 1
            reentry_status_counts[reentry_status] += 1
            router_status_counts[router_status] += 1
            router_order_effect_counts[router_order_effect] += 1
            block_reasons = self._decision_block_reasons(decision)
            if not block_reasons and (
                outcome in {"skipped", "failed"}
                or router_order_effect == "shadow_only"
                or guard_status.startswith("blocked")
                or router_status.startswith("shadow_only")
            ):
                block_reasons = [
                    guard_status
                    if guard_status not in {"allowed", "not_recorded"}
                    else router_status
                ]
            for reason in block_reasons:
                block_reason_counts[reason] += 1
                entry_row["block_reason_counts"][reason] = (
                    entry_row["block_reason_counts"].get(reason, 0) + 1
                )

        for buy in buy_contexts:
            setup = self._setup(buy)
            entry_type = str(buy.get("entry_type") or "unknown")
            playbook = self._playbook_name(setup)
            leader_strategy = self._leader_strategy_key(entry_type, playbook, setup)
            for row in (
                entry_rows.setdefault(entry_type, self._router_entry_row(entry_type)),
                playbook_rows.setdefault(
                    playbook,
                    self._router_group_row(playbook, playbook),
                ),
                leader_rows.setdefault(
                    leader_strategy,
                    self._router_group_row(leader_strategy, leader_strategy),
                ),
            ):
                row["buy_count"] += 1
                row["buy_amount"] += int(buy.get("buy_amount") or 0)

        for sell in matched_sells:
            matched_buy = sell.get("matched_buy") or {}
            setup = self._setup(matched_buy)
            entry_type = str(sell.get("entry_type") or "unknown")
            playbook = self._playbook_name(setup)
            leader_strategy = self._leader_strategy_key(entry_type, playbook, setup)
            pnl = int(sell.get("realized_pnl") or 0)
            for row in (
                entry_rows.setdefault(entry_type, self._router_entry_row(entry_type)),
                playbook_rows.setdefault(
                    playbook,
                    self._router_group_row(playbook, playbook),
                ),
                leader_rows.setdefault(
                    leader_strategy,
                    self._router_group_row(leader_strategy, leader_strategy),
                ),
            ):
                row["sell_count"] += 1
                row["realized_pnl"] += pnl
                if pnl > 0:
                    row["winning_sell_count"] += 1
                elif pnl < 0:
                    row["losing_sell_count"] += 1

        entry_routes = [
            self._finalize_router_entry_row(row)
            for row in entry_rows.values()
        ]
        entry_routes.sort(
            key=lambda row: (
                0 if row["order_impact"] == "filled_order" else 1,
                row["legacy_candidate"],
                -row["buy_count"],
                row["entry_type"],
            )
        )
        playbooks = [
            self._finalize_router_group_row(row)
            for row in playbook_rows.values()
        ]
        playbooks.sort(key=lambda row: (-row["buy_count"], row["name"]))
        leader_strategy_mapping = [
            self._finalize_router_group_row(row)
            for row in leader_rows.values()
        ]
        leader_strategy_mapping.sort(
            key=lambda row: (
                row["name"] == "unmapped",
                -row["buy_count"],
                row["name"],
            )
        )
        legacy_candidates = [
            row for row in entry_routes if bool(row["legacy_candidate"])
        ]
        return {
            "entry_routes": entry_routes,
            "leader_strategy_mapping": leader_strategy_mapping,
            "playbooks": playbooks,
            "legacy_candidates": legacy_candidates,
            "guard_status_counts": self._counter_rows(guard_status_counts),
            "reentry_status_counts": self._counter_rows(reentry_status_counts),
            "router_status_counts": self._counter_rows(router_status_counts),
            "router_order_effect_counts": self._counter_rows(
                router_order_effect_counts
            ),
            "block_reason_counts": self._counter_rows(block_reason_counts),
            "legacy_shadow_comparison": self._finalize_legacy_shadow_comparison(
                legacy_shadow_comparison
            ),
        }

    def _strategy_dashboard_review(
        self,
        decisions: list[dict[str, Any]],
        buy_contexts: list[dict[str, Any]],
        matched_sells: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
        exit_breakdown: dict[str, Any],
        router_diagnostics: dict[str, Any],
        blocked_forward_review: dict[str, Any],
        legacy_retirement_review: dict[str, Any],
        armed_candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        buy_decision_ids = {
            int(item["decision_id"])
            for item in buy_contexts
            if item.get("decision_id") is not None
        }
        best_outcome = self._best_outcome_by_decision(outcomes)
        a_plus_candidates = []
        missed_leaders = []
        missed_reentries = []
        legacy_old_buy = []
        new_router_buy = []
        core_playbooks = {
            "a_plus_trend_day_runner",
            "vwap_pullback_reentry",
            "new_high_rebreakout_reentry_probe",
        }

        for decision in decisions:
            decision_id = decision.get("decision_id")
            setup = self._setup(decision)
            playbook = self._playbook_name(setup)
            entry_type = str(decision.get("entry_type") or "unknown")
            grade = self._entry_grade(setup)
            money_flow = setup.get("money_flow_policy")
            if not isinstance(money_flow, dict):
                money_flow = {}
            router = setup.get("router_execution_policy")
            if not isinstance(router, dict):
                router = {}
            legacy = setup.get("legacy_entry_policy")
            if not isinstance(legacy, dict):
                legacy = {}
            reentry_engine = setup.get("reentry_engine_policy")
            if not isinstance(reentry_engine, dict):
                reentry_engine = {}
            outcome = (
                best_outcome.get(int(decision_id))
                if decision_id is not None and int(decision_id) in best_outcome
                else {}
            )
            entered = (
                decision_id is not None and int(decision_id) in buy_decision_ids
            ) or str(decision.get("outcome") or "").lower() == "executed"
            max_up_rate = self._float_or_none(outcome.get("max_up_rate"))
            leader_strategy = self._leader_strategy_key(entry_type, playbook, setup)
            a_plus = grade == "A+" or bool(money_flow.get("a_plus_qualified"))
            base_item = self._dashboard_decision_item(
                decision,
                setup=setup,
                playbook=playbook,
                entered=entered,
                outcome=outcome,
            )
            if a_plus:
                a_plus_candidates.append(base_item)
            if (
                not entered
                and max_up_rate is not None
                and max_up_rate >= 0.03
                and (a_plus or leader_strategy != "unmapped")
            ):
                missed_leaders.append(base_item)
            if bool(legacy.get("old_logic_would_buy")):
                legacy_old_buy.append(
                    {
                        **base_item,
                        "legacy_route": legacy.get("legacy_route") or "unknown",
                        "legacy_reasons": legacy.get("legacy_reasons") or [],
                    }
                )
            if router.get("order_effect") in {"allow_order", "allow_minimum_probe"}:
                new_router_buy.append(
                    {
                        **base_item,
                        "router_status": router.get("status"),
                        "order_effect": router.get("order_effect"),
                    }
                )
            reentry_status = str(reentry_engine.get("status") or "")
            if (
                not entered
                and max_up_rate is not None
                and max_up_rate >= 0.03
                and (
                    playbook == "new_high_rebreakout_reentry_probe"
                    or bool(reentry_engine.get("new_setup_detected"))
                    or reentry_status
                    in {
                        "blocked_reward_risk",
                        "blocked_reentry_quality",
                        "blocked_waiting_new_setup",
                    }
                )
            ):
                missed_reentries.append(
                    {
                        **base_item,
                        "reentry_engine_status": reentry_status or "not_recorded",
                        "reentry_engine": reentry_engine,
                    }
                )

        playbook_performance = [
            {**row, "playbook": row.get("name")}
            for row in router_diagnostics.get("playbooks", [])
            if row.get("name") in core_playbooks
        ]
        playbook_names = {row.get("name") for row in playbook_performance}
        for name in sorted(core_playbooks - playbook_names):
            row = self._finalize_router_group_row(self._router_group_row(name, name))
            playbook_performance.append({**row, "playbook": row.get("name")})

        sold_too_early = self._sold_too_early_review(
            matched_sells,
            best_outcome,
            exit_breakdown,
        )
        ab_test = self._strategy_ab_test_review(
            decisions,
            buy_contexts,
            matched_sells,
            best_outcome,
            sold_too_early,
        )
        top_block_reasons = router_diagnostics.get("block_reason_counts", [])
        legacy_compare = router_diagnostics.get("legacy_shadow_comparison") or {}
        summary = {
            "a_plus_candidate_count": len(a_plus_candidates),
            "a_plus_entered_count": sum(
                1 for item in a_plus_candidates if item["entered"]
            ),
            "a_plus_not_entered_count": sum(
                1 for item in a_plus_candidates if not item["entered"]
            ),
            "core_playbook_buy_count": sum(
                int(row.get("buy_count") or 0) for row in playbook_performance
            ),
            "legacy_old_logic_buy_count": int(
                legacy_compare.get("old_logic_buy_count") or 0
            ),
            "new_router_buy_count": int(
                legacy_compare.get("new_logic_buy_count") or 0
            ),
            "top_block_reason": (
                top_block_reasons[0]["key"] if top_block_reasons else None
            ),
            "missed_leader_count": len(missed_leaders),
            "sold_too_early_count": len(sold_too_early),
            "missed_reentry_count": len(missed_reentries),
            "ab_action_diff_count": ab_test["summary"]["action_diff_count"],
            "a_plus_armed_count": len(armed_candidates),
            "a_plus_armed_entered_count": sum(
                1 for item in armed_candidates if item.get("status") == "entered"
            ),
            "a_plus_armed_waiting_count": sum(
                1 for item in armed_candidates if item.get("status") == "armed"
            ),
        }
        return {
            "version": "v0.12.92",
            "summary": summary,
            "ab_test": ab_test,
            "legacy_retirement_review": legacy_retirement_review,
            "a_plus_candidates": sorted(
                a_plus_candidates,
                key=lambda item: (
                    not item["entered"],
                    -(item.get("max_up_rate") or 0),
                    item["decided_at"],
                ),
            )[:30],
            "playbook_performance": sorted(
                playbook_performance,
                key=lambda row: (
                    row.get("name") not in core_playbooks,
                    -int(row.get("buy_count") or 0),
                    row.get("name") or "",
                ),
            ),
            "a_plus_armed_candidates": armed_candidates,
            "legacy_vs_new_router": {
                "summary": legacy_compare,
                "legacy_old_logic_buys": legacy_old_buy[:30],
                "new_router_buys": new_router_buy[:30],
            },
            "top_block_reasons": top_block_reasons[:20],
            "missed_leaders": sorted(
                missed_leaders,
                key=lambda item: (-(item.get("max_up_rate") or 0), item["decided_at"]),
            )[:30],
            "sold_too_early": sold_too_early[:30],
            "missed_reentries": sorted(
                missed_reentries,
                key=lambda item: (-(item.get("max_up_rate") or 0), item["decided_at"]),
            )[:30],
            "blocked_forward_reference": {
                "missed_spike_count": blocked_forward_review.get(
                    "missed_spike_count",
                    0,
                ),
                "evaluated_count": blocked_forward_review.get("evaluated_count", 0),
            },
        }

    def _legacy_retirement_review(
        self,
        decisions: list[dict[str, Any]],
        buy_contexts: list[dict[str, Any]],
        matched_sells: list[dict[str, Any]],
        best_outcome: dict[int, dict[str, Any]],
        router_diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        buy_ids_by_decision = {
            int(item["decision_id"]): item
            for item in buy_contexts
            if item.get("decision_id") is not None
        }
        pnl_by_decision: dict[int, int] = defaultdict(int)
        sell_count_by_decision: dict[int, int] = defaultdict(int)
        losing_sell_by_decision: dict[int, int] = defaultdict(int)
        winning_sell_by_decision: dict[int, int] = defaultdict(int)
        for sell in matched_sells:
            matched_buy = sell.get("matched_buy") or {}
            decision_id = matched_buy.get("decision_id")
            if decision_id is None:
                continue
            key = int(decision_id)
            pnl = int(sell.get("realized_pnl") or 0)
            pnl_by_decision[key] += pnl
            sell_count_by_decision[key] += 1
            if pnl < 0:
                losing_sell_by_decision[key] += 1
            elif pnl > 0:
                winning_sell_by_decision[key] += 1

        rows: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            setup = self._setup(decision)
            entry_type = str(decision.get("entry_type") or "unknown")
            playbook = self._playbook_name(setup)
            legacy_policy = setup.get("legacy_entry_policy")
            if not isinstance(legacy_policy, dict):
                legacy_policy = {}
            legacy_reason = self._legacy_candidate_reason(entry_type, playbook)
            legacy_reasons = [
                str(reason)
                for reason in (legacy_policy.get("legacy_reasons") or [])
                if str(reason)
            ]
            if not legacy_reasons and legacy_reason is not None:
                legacy_reasons = [legacy_reason]
            legacy_candidate = bool(
                legacy_policy.get("legacy_candidate")
                if legacy_policy
                else legacy_reason is not None
            )
            if not legacy_candidate:
                continue
            legacy_route = str(
                legacy_policy.get("legacy_route")
                or (legacy_reasons[0] if legacy_reasons else "unknown")
            )
            row = rows.setdefault(
                legacy_route,
                self._legacy_retirement_row(legacy_route),
            )
            row["decision_count"] += 1
            row["entry_types"].add(entry_type)
            row["playbooks"].add(playbook)
            row["legacy_reasons"].update(legacy_reasons)
            decision_id = decision.get("decision_id")
            decision_key = int(decision_id) if decision_id is not None else None
            router = setup.get("router_execution_policy")
            if not isinstance(router, dict):
                router = {}
            guard = setup.get("entry_guard_policy")
            if not isinstance(guard, dict):
                guard = {}
            old_logic_would_buy = bool(
                legacy_policy.get("old_logic_would_buy")
                if legacy_policy
                else str(decision.get("action") or "").lower() == "buy"
            )
            new_logic_would_buy = bool(
                legacy_policy.get("new_logic_would_buy")
                if legacy_policy
                else router.get("order_effect") in {"allow_order", "allow_minimum_probe"}
            )
            removed_from_order_influence = bool(
                legacy_policy.get("removed_from_order_influence")
                if legacy_policy
                else router.get("order_effect") == "shadow_only"
            )
            if old_logic_would_buy:
                row["old_logic_buy_count"] += 1
            if new_logic_would_buy:
                row["new_logic_buy_count"] += 1
            if removed_from_order_influence:
                row["removed_from_order_influence_count"] += 1
            if router.get("order_effect") == "shadow_only":
                row["shadow_only_count"] += 1
            if router.get("order_effect") == "allow_minimum_probe":
                row["minimum_probe_count"] += 1
            if decision_key is not None and decision_key in buy_ids_by_decision:
                row["actual_entry_count"] += 1
                row["actual_sell_count"] += sell_count_by_decision.get(
                    decision_key,
                    0,
                )
                row["realized_pnl"] += pnl_by_decision.get(decision_key, 0)
                row["winning_sell_count"] += winning_sell_by_decision.get(
                    decision_key,
                    0,
                )
                row["losing_sell_count"] += losing_sell_by_decision.get(
                    decision_key,
                    0,
                )
            if self._leader_strategy_key(entry_type, playbook, setup) != "unmapped":
                row["core_overlap_count"] += 1
            if self._legacy_low_explanation(legacy_route, legacy_reasons, entry_type):
                row["low_explanation_count"] += 1
            if self._legacy_data_gap_or_supply_gap(decision, setup):
                row["data_gap_or_supply_gap_count"] += 1
            if self._legacy_block_only(decision, setup):
                row["loss_or_block_count"] += 1
            if decision_key is not None:
                outcome = best_outcome.get(decision_key) or {}
                max_up_rate = self._float_or_none(outcome.get("max_up_rate"))
                max_down_rate = self._float_or_none(outcome.get("max_down_rate"))
                if old_logic_would_buy and max_up_rate is not None:
                    row["forward_evaluated_count"] += 1
                    row["max_up_rate_sum"] += max_up_rate
                    if max_up_rate >= 0.03:
                        row["missed_rally_count"] += 1
                if old_logic_would_buy and max_down_rate is not None:
                    if max_down_rate <= -0.02:
                        row["avoided_loss_count"] += 1
                    if max_down_rate <= -0.02 and (
                        max_up_rate is None or max_up_rate < 0.03
                    ):
                        row["unnecessary_entry_count"] += 1

        route_rows = [
            self._finalize_legacy_retirement_row(row)
            for row in rows.values()
        ]
        route_rows.sort(
            key=lambda row: (
                {"remove": 0, "permanent_shadow": 1, "keep_observe": 2}.get(
                    row["recommendation"],
                    9,
                ),
                -row["retirement_score"],
                -row["old_logic_buy_count"],
                row["legacy_route"],
            )
        )
        summary = {
            "candidate_route_count": len(route_rows),
            "remove_count": sum(
                1 for row in route_rows if row["recommendation"] == "remove"
            ),
            "permanent_shadow_count": sum(
                1
                for row in route_rows
                if row["recommendation"] == "permanent_shadow"
            ),
            "keep_observe_count": sum(
                1 for row in route_rows if row["recommendation"] == "keep_observe"
            ),
            "old_logic_buy_count": int(
                (router_diagnostics.get("legacy_shadow_comparison") or {}).get(
                    "old_logic_buy_count",
                    0,
                )
                or 0
            ),
            "removed_from_order_influence_count": int(
                (router_diagnostics.get("legacy_shadow_comparison") or {}).get(
                    "removed_from_order_influence_count",
                    0,
                )
                or 0
            ),
        }
        return {
            "version": "v0.12.90",
            "mode": "report_only",
            "deletion_criteria": [
                "no_profit_contribution",
                "loss_or_block_increase",
                "overlaps_three_core_playbooks",
                "low_review_explainability",
                "data_or_supply_gap_false_entry",
            ],
            "recommendation_labels": {
                "remove": "삭제 후보",
                "permanent_shadow": "영구 shadow 후보",
                "keep_observe": "관찰 유지",
            },
            "summary": summary,
            "routes": route_rows,
        }

    def _strategy_ab_test_review(
        self,
        decisions: list[dict[str, Any]],
        buy_contexts: list[dict[str, Any]],
        matched_sells: list[dict[str, Any]],
        best_outcome: dict[int, dict[str, Any]],
        sold_too_early: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rows = {
            "normal": self._strategy_ab_mode_row("normal"),
            "remake": self._strategy_ab_mode_row("remake"),
        }
        action_diff_count = 0
        quantity_diff_sum = 0
        for decision in decisions:
            decision_id = decision.get("decision_id")
            setup = self._setup(decision)
            compare = setup.get("strategy_ab_compare")
            if not isinstance(compare, dict):
                continue
            if (compare.get("diff") or {}).get("action_diff"):
                action_diff_count += 1
            quantity_diff_sum += int((compare.get("diff") or {}).get("quantity_diff") or 0)
            outcome = (
                best_outcome.get(int(decision_id))
                if decision_id is not None and int(decision_id) in best_outcome
                else {}
            )
            max_up_rate = self._float_or_none(outcome.get("max_up_rate"))
            max_down_rate = self._float_or_none(outcome.get("max_down_rate"))
            close_rate = self._float_or_none(outcome.get("close_rate"))
            for mode in ("normal", "remake"):
                variant = compare.get(mode)
                if not isinstance(variant, dict):
                    continue
                row = rows[mode]
                row["decision_count"] += 1
                if compare.get("actual_mode") == mode:
                    row["selected_mode_count"] += 1
                else:
                    row["shadow_mode_count"] += 1
                would_enter = bool(variant.get("would_enter"))
                if would_enter:
                    row["would_enter_count"] += 1
                    row["planned_quantity_sum"] += int(
                        variant.get("planned_quantity") or 0
                    )
                    if close_rate is not None:
                        row["estimated_rate_sum"] += close_rate
                        row["estimated_rate_count"] += 1
                    if max_up_rate is not None and max_up_rate >= 0.03:
                        row["estimated_win_count"] += 1
                    if max_down_rate is not None and max_down_rate <= -0.02:
                        row["unnecessary_entry_count"] += 1
                elif max_up_rate is not None and max_up_rate >= 0.03:
                    row["missed_rally_count"] += 1
        for buy in buy_contexts:
            compare = self._setup(buy).get("strategy_ab_compare")
            if not isinstance(compare, dict):
                continue
            mode = str(compare.get("actual_mode") or "remake")
            if mode in rows:
                rows[mode]["actual_entry_count"] += 1
        for sell in matched_sells:
            matched_buy = sell.get("matched_buy") or {}
            compare = self._setup(matched_buy).get("strategy_ab_compare")
            if not isinstance(compare, dict):
                continue
            mode = str(compare.get("actual_mode") or "remake")
            if mode in rows:
                pnl = int(sell.get("realized_pnl") or 0)
                rows[mode]["actual_sell_count"] += 1
                rows[mode]["actual_realized_pnl"] += pnl
                if pnl > 0:
                    rows[mode]["actual_win_count"] += 1
        for item in sold_too_early:
            decision_id = item.get("decision_id")
            if decision_id is None:
                continue
            decision = next(
                (
                    candidate
                    for candidate in decisions
                    if int(candidate.get("decision_id") or 0) == int(decision_id)
                ),
                None,
            )
            compare = self._setup(decision or {}).get("strategy_ab_compare")
            if not isinstance(compare, dict):
                continue
            mode = str(compare.get("actual_mode") or "remake")
            if mode in rows:
                rows[mode]["early_exit_count"] += 1
        finalized = [self._finalize_strategy_ab_mode_row(row) for row in rows.values()]
        return {
            "version": "v0.12.89",
            "actual_orders": "selected_mode_only",
            "shadow_orders": "opposite_mode_saved_only",
            "summary": {
                "decision_count": sum(row["decision_count"] for row in rows.values())
                // 2,
                "action_diff_count": action_diff_count,
                "quantity_diff_sum": quantity_diff_sum,
            },
            "modes": finalized,
        }

    def _dashboard_decision_item(
        self,
        decision: dict[str, Any],
        *,
        setup: dict[str, Any],
        playbook: str,
        entered: bool,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        entry_guard = setup.get("entry_guard_policy")
        if not isinstance(entry_guard, dict):
            entry_guard = {}
        router = setup.get("router_execution_policy")
        if not isinstance(router, dict):
            router = {}
        return {
            "decision_id": decision.get("decision_id"),
            "decided_at": decision.get("decided_at"),
            "stock_code": decision.get("stock_code"),
            "stock_name": decision.get("stock_name"),
            "entry_type": decision.get("entry_type") or "unknown",
            "playbook": playbook,
            "entry_grade": self._entry_grade(setup),
            "action": decision.get("action"),
            "outcome": decision.get("outcome"),
            "entered": entered,
            "score": self._float_or_none(decision.get("score")),
            "guard_status": entry_guard.get("status"),
            "router_status": router.get("status"),
            "order_effect": router.get("order_effect"),
            "max_up_rate": self._float_or_none(outcome.get("max_up_rate")),
            "max_down_rate": self._float_or_none(outcome.get("max_down_rate")),
            "actual_outcome": outcome.get("actual_outcome"),
            "block_reasons": self._decision_block_reasons(decision),
        }

    def _sold_too_early_review(
        self,
        matched_sells: list[dict[str, Any]],
        best_outcome: dict[int, dict[str, Any]],
        exit_breakdown: dict[str, Any],
    ) -> list[dict[str, Any]]:
        exit_by_order_id = exit_breakdown.get("by_order_id") or {}
        rows = []
        for sell in matched_sells:
            matched_buy = sell.get("matched_buy") or {}
            decision_id = matched_buy.get("decision_id")
            if decision_id is None:
                continue
            outcome = best_outcome.get(int(decision_id)) or {}
            max_up_rate = self._float_or_none(outcome.get("max_up_rate"))
            if max_up_rate is None or max_up_rate < 0.03:
                continue
            buy_price = int(matched_buy.get("buy_price") or 0)
            sell_price = int(sell.get("fill_price") or 0)
            realized_rate = (
                (sell_price - buy_price) / buy_price if buy_price > 0 else None
            )
            potential_left_rate = (
                max_up_rate - realized_rate
                if realized_rate is not None
                else None
            )
            risk_exit = (
                exit_by_order_id.get(int(sell["order_id"]))
                if sell.get("order_id") is not None
                else None
            )
            exit_class = str((risk_exit or {}).get("exit_class") or "")
            if exit_class not in {
                "profit_protect_stop",
                "trailing_profit_lock",
            } and not (
                realized_rate is not None
                and realized_rate >= 0
                and potential_left_rate is not None
                and potential_left_rate >= 0.02
            ):
                continue
            rows.append(
                {
                    "decision_id": decision_id,
                    "stock_code": sell.get("stock_code"),
                    "entry_type": sell.get("entry_type"),
                    "playbook": self._playbook_name(self._setup(matched_buy)),
                    "buy_at": matched_buy.get("buy_filled_at"),
                    "sell_at": sell.get("filled_at"),
                    "buy_price": buy_price,
                    "sell_price": sell_price,
                    "realized_pnl": int(sell.get("realized_pnl") or 0),
                    "realized_rate": realized_rate,
                    "max_up_rate": max_up_rate,
                    "potential_left_rate": potential_left_rate,
                    "exit_class": exit_class or "unknown",
                    "actual_outcome": outcome.get("actual_outcome"),
                }
            )
        return sorted(
            rows,
            key=lambda item: (
                -(item.get("potential_left_rate") or 0),
                -(item.get("max_up_rate") or 0),
            ),
        )

    def _flag_performance(
        self,
        buy_contexts: list[dict[str, Any]],
        matched_sells: list[dict[str, Any]],
        flag: str,
    ) -> dict[str, Any]:
        buy_ids = {
            int(item["fill_id"])
            for item in buy_contexts
            if bool(item.get(flag)) and item.get("fill_id") is not None
        }
        sells = [
            item
            for item in matched_sells
            if item.get("matched_buy") is not None
            and int(item["matched_buy"]["fill_id"]) in buy_ids
        ]
        pnl = sum(int(item["realized_pnl"] or 0) for item in sells)
        return {
            "flag": flag,
            "buy_count": len(buy_ids),
            "sell_count": len(sells),
            "realized_pnl": pnl,
            "losing_sell_count": sum(
                1 for item in sells if int(item["realized_pnl"] or 0) < 0
            ),
            "stock_codes": sorted(
                {
                    item["matched_buy"]["stock_code"]
                    for item in sells
                    if item.get("matched_buy") is not None
                }
            ),
        }

    def _buys_after_stop_loss(
        self,
        buy_contexts: list[dict[str, Any]],
        exit_breakdown: dict[str, Any],
    ) -> dict[str, Any]:
        stop_losses = [
            item
            for item in exit_breakdown["events"]
            if item["exit_class"] == "loss_stop"
        ]
        events = []
        for stop in stop_losses:
            for buy in buy_contexts:
                if buy.get("account_id") != stop.get("account_id"):
                    continue
                if buy["stock_code"] != stop["stock_code"]:
                    continue
                if str(buy.get("buy_filled_at") or "") <= str(stop["occurred_at"]):
                    continue
                events.append(
                    {
                        "account_name": buy.get("account_name"),
                        "stock_code": buy["stock_code"],
                        "stop_loss_at": stop["occurred_at"],
                        "next_buy_at": buy.get("buy_filled_at"),
                        "entry_type": buy.get("entry_type"),
                        "entry_grade": buy.get("entry_grade"),
                        "force_entry": bool(buy.get("force_entry")),
                    }
                )
        return {
            "count": len(events),
            "events": events,
        }

    def _preventable_loss_estimate(
        self,
        matched_sells: list[dict[str, Any]],
    ) -> dict[str, Any]:
        losing = [
            item
            for item in matched_sells
            if int(item["realized_pnl"] or 0) < 0
        ]
        by_reason: dict[str, dict[str, Any]] = {}
        for sell in losing:
            reasons = [f"entry_type:{sell.get('entry_type') or 'unknown'}"]
            if sell.get("supply_missing"):
                reasons.append("supply_missing")
            if sell.get("force_entry"):
                reasons.append("force_entry")
            for reason in reasons:
                row = by_reason.setdefault(
                    reason,
                    {
                        "reason": reason,
                        "losing_sell_count": 0,
                        "preventable_loss": 0,
                        "stock_codes": set(),
                    },
                )
                row["losing_sell_count"] += 1
                row["preventable_loss"] += abs(int(sell["realized_pnl"] or 0))
                row["stock_codes"].add(sell["stock_code"])
        return {
            "total_preventable_loss": sum(
                abs(int(item["realized_pnl"] or 0)) for item in losing
            ),
            "losing_sell_count": len(losing),
            "by_reason": [
                {
                    **row,
                    "stock_codes": sorted(row["stock_codes"]),
                }
                for row in sorted(
                    by_reason.values(),
                    key=lambda item: (-item["preventable_loss"], item["reason"]),
                )
            ],
        }

    def _blocked_forward_review(
        self,
        decisions: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        blocked_ids = {
            int(item["decision_id"])
            for item in decisions
            if item.get("decision_id") is not None
            and (
                item.get("outcome") in {"skipped", "observe", "failed"}
                or item.get("action") == "hold"
                or bool(item.get("block_reasons"))
            )
        }
        decisions_by_id = {
            int(item["decision_id"]): item
            for item in decisions
            if item.get("decision_id") is not None
        }
        rows = [
            {
                **item,
                "stock_name": decisions_by_id.get(
                    int(item["decision_id"]),
                    {},
                ).get("stock_name"),
                "stock_category": decisions_by_id.get(
                    int(item["decision_id"]),
                    {},
                ).get("stock_category")
                or stock_category_payload(item.get("stock_code")),
            }
            for item in outcomes
            if int(item["decision_id"]) in blocked_ids
        ]
        avoided = [
            item
            for item in rows
            if item.get("max_down_rate") is not None
            and float(item["max_down_rate"]) <= -0.01
        ]
        missed = [
            item
            for item in rows
            if item.get("max_up_rate") is not None
            and float(item["max_up_rate"]) >= 0.03
        ]
        by_category = {
            key: {
                "category": key,
                "label": label,
                "blocked_decision_count": 0,
                "evaluated_count": 0,
                "avoided_loss_count": 0,
                "missed_spike_count": 0,
                "max_up_rate_sum": 0.0,
                "max_up_rate_count": 0,
            }
            for key, label in STOCK_CATEGORY_LABELS.items()
        }
        for decision in decisions:
            if int(decision.get("decision_id") or 0) not in blocked_ids:
                continue
            category = (
                decision.get("stock_category")
                or stock_category_payload(decision.get("stock_code"))
            )
            by_category[category["key"]]["blocked_decision_count"] += 1
        for row in rows:
            category = row["stock_category"]
            group = by_category[category["key"]]
            group["evaluated_count"] += 1
            if (
                row.get("max_down_rate") is not None
                and float(row["max_down_rate"]) <= -0.01
            ):
                group["avoided_loss_count"] += 1
            if (
                row.get("max_up_rate") is not None
                and float(row["max_up_rate"]) >= 0.03
            ):
                group["missed_spike_count"] += 1
            if row.get("max_up_rate") is not None:
                group["max_up_rate_sum"] += float(row["max_up_rate"])
                group["max_up_rate_count"] += 1
        category_rows = []
        for key in STOCK_CATEGORY_LABELS:
            group = by_category[key]
            count = int(group.pop("max_up_rate_count"))
            total = float(group.pop("max_up_rate_sum"))
            group["average_max_up_rate"] = total / count if count else None
            category_rows.append(group)
        return {
            "blocked_decision_count": len(blocked_ids),
            "evaluated_count": len(rows),
            "avoided_loss_count": len(avoided),
            "missed_spike_count": len(missed),
            "by_stock_category": category_rows,
            "notable": rows[:30],
        }

    @staticmethod
    def _performance_row(entry_type: str) -> dict[str, Any]:
        return {
            "entry_type": entry_type,
            "buy_count": 0,
            "sell_count": 0,
            "buy_amount": 0,
            "realized_pnl": 0,
            "winning_sell_count": 0,
            "losing_sell_count": 0,
            "stop_loss_count": 0,
            "loss_stop_count": 0,
            "profit_protect_stop_count": 0,
            "trailing_profit_lock_count": 0,
            "trailing_loss_stop_count": 0,
            "risk_exit_count": 0,
            "loss_stop_realized_pnl": 0,
            "profit_protect_stop_realized_pnl": 0,
            "trailing_profit_lock_realized_pnl": 0,
            "trailing_loss_stop_realized_pnl": 0,
        }

    @staticmethod
    def _router_entry_row(entry_type: str) -> dict[str, Any]:
        return {
            "entry_type": entry_type,
            "decision_count": 0,
            "buy_signal_count": 0,
            "executed_decision_count": 0,
            "blocked_decision_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "buy_amount": 0,
            "realized_pnl": 0,
            "winning_sell_count": 0,
            "losing_sell_count": 0,
            "order_impact": "decision_only",
            "shadow_only_count": 0,
            "leader_strategy": "unmapped",
            "three_strategy_mapped": False,
            "legacy_candidate": False,
            "legacy_reasons": set(),
            "old_logic_buy_count": 0,
            "new_logic_buy_count": 0,
            "legacy_shadow_count": 0,
            "minimum_probe_count": 0,
            "removed_from_order_influence_count": 0,
            "playbook_counts": {},
            "leader_strategy_counts": {},
            "guard_status_counts": {},
            "reentry_status_counts": {},
            "router_status_counts": {},
            "router_order_effect_counts": {},
            "block_reason_counts": {},
        }

    @staticmethod
    def _router_group_row(name: str, label: str) -> dict[str, Any]:
        return {
            "name": name,
            "label": label,
            "decision_count": 0,
            "buy_signal_count": 0,
            "executed_decision_count": 0,
            "blocked_decision_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "buy_amount": 0,
            "realized_pnl": 0,
            "winning_sell_count": 0,
            "losing_sell_count": 0,
            "shadow_only_count": 0,
            "guard_status_counts": {},
            "reentry_status_counts": {},
            "router_status_counts": {},
            "router_order_effect_counts": {},
        }

    @staticmethod
    def _strategy_ab_mode_row(mode: str) -> dict[str, Any]:
        return {
            "mode": mode,
            "label": "일반" if mode == "normal" else "리메이크",
            "decision_count": 0,
            "selected_mode_count": 0,
            "shadow_mode_count": 0,
            "would_enter_count": 0,
            "actual_entry_count": 0,
            "actual_sell_count": 0,
            "actual_realized_pnl": 0,
            "actual_win_count": 0,
            "estimated_win_count": 0,
            "estimated_rate_sum": 0.0,
            "estimated_rate_count": 0,
            "planned_quantity_sum": 0,
            "missed_rally_count": 0,
            "unnecessary_entry_count": 0,
            "early_exit_count": 0,
        }

    @staticmethod
    def _legacy_retirement_row(legacy_route: str) -> dict[str, Any]:
        return {
            "legacy_route": legacy_route,
            "decision_count": 0,
            "old_logic_buy_count": 0,
            "new_logic_buy_count": 0,
            "shadow_only_count": 0,
            "minimum_probe_count": 0,
            "removed_from_order_influence_count": 0,
            "actual_entry_count": 0,
            "actual_sell_count": 0,
            "realized_pnl": 0,
            "winning_sell_count": 0,
            "losing_sell_count": 0,
            "forward_evaluated_count": 0,
            "missed_rally_count": 0,
            "avoided_loss_count": 0,
            "unnecessary_entry_count": 0,
            "loss_or_block_count": 0,
            "core_overlap_count": 0,
            "low_explanation_count": 0,
            "data_gap_or_supply_gap_count": 0,
            "max_up_rate_sum": 0.0,
            "entry_types": set(),
            "playbooks": set(),
            "legacy_reasons": set(),
        }

    @classmethod
    def _finalize_legacy_retirement_row(
        cls,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        score = 0
        evidence = []
        warnings = []
        old_buy = int(row["old_logic_buy_count"])
        forward_count = int(row["forward_evaluated_count"])
        actual_entries = int(row["actual_entry_count"])
        realized_pnl = int(row["realized_pnl"])
        if old_buy > 0 and int(row["new_logic_buy_count"]) == 0:
            score += 2
            evidence.append("legacy_old_buy_without_new_router_buy")
        if actual_entries == 0 or realized_pnl <= 0:
            score += 2
            evidence.append("no_profit_contribution")
        if int(row["losing_sell_count"]) > int(row["winning_sell_count"]):
            score += 2
            evidence.append("loss_contribution")
        if int(row["loss_or_block_count"]) > 0:
            score += 1
            evidence.append("loss_or_block_increase")
        if int(row["core_overlap_count"]) > 0:
            score += 1
            evidence.append("overlaps_three_core_playbooks")
        if int(row["low_explanation_count"]) > 0:
            score += 1
            evidence.append("low_review_explainability")
        if int(row["data_gap_or_supply_gap_count"]) > 0:
            score += 2
            evidence.append("data_or_supply_gap_false_entry")
        if int(row["unnecessary_entry_count"]) > 0:
            score += 2
            evidence.append("unnecessary_entry_risk")
        if int(row["avoided_loss_count"]) > 0:
            score += 1
            evidence.append("would_have_seen_material_drawdown")
        if int(row["missed_rally_count"]) > 0:
            score -= 3
            warnings.append("missed_rally_evidence_exists")
        if forward_count == 0:
            warnings.append("forward_outcome_not_ready")
        recommendation = "keep_observe"
        if (
            old_buy >= 3
            and score >= 7
            and int(row["missed_rally_count"]) == 0
            and forward_count > 0
        ):
            recommendation = "remove"
        elif old_buy > 0 and score >= 4:
            recommendation = "permanent_shadow"
        average_max_up_rate = (
            float(row["max_up_rate_sum"]) / forward_count
            if forward_count
            else None
        )
        return {
            **row,
            "entry_types": sorted(row["entry_types"]),
            "playbooks": sorted(row["playbooks"]),
            "legacy_reasons": sorted(row["legacy_reasons"]),
            "max_up_rate_sum": None,
            "average_max_up_rate": average_max_up_rate,
            "retirement_score": score,
            "recommendation": recommendation,
            "evidence": evidence,
            "warnings": warnings,
        }

    @staticmethod
    def _finalize_strategy_ab_mode_row(row: dict[str, Any]) -> dict[str, Any]:
        would_enter = int(row["would_enter_count"])
        actual_sells = int(row["actual_sell_count"])
        estimated_count = int(row.pop("estimated_rate_count"))
        estimated_sum = float(row.pop("estimated_rate_sum"))
        return {
            **row,
            "win_rate": (
                int(row["estimated_win_count"]) / would_enter
                if would_enter
                else None
            ),
            "actual_win_rate": (
                int(row["actual_win_count"]) / actual_sells
                if actual_sells
                else None
            ),
            "estimated_average_rate": (
                estimated_sum / estimated_count if estimated_count else None
            ),
            "average_planned_quantity": (
                int(row["planned_quantity_sum"]) / would_enter
                if would_enter
                else None
            ),
        }

    @classmethod
    def _finalize_router_entry_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        order_impact = "decision_only"
        if row["buy_count"] > 0:
            order_impact = "filled_order"
        elif row["shadow_only_count"] > 0:
            order_impact = "shadow_only"
        elif row["buy_signal_count"] > 0:
            order_impact = "buy_signal_no_fill"
        result = {
            **row,
            "order_impact": order_impact,
            "legacy_reasons": sorted(row["legacy_reasons"]),
            "playbook_counts": cls._counter_rows(row["playbook_counts"]),
            "leader_strategy_counts": cls._counter_rows(
                row["leader_strategy_counts"]
            ),
            "guard_status_counts": cls._counter_rows(row["guard_status_counts"]),
            "reentry_status_counts": cls._counter_rows(row["reentry_status_counts"]),
            "router_status_counts": cls._counter_rows(row["router_status_counts"]),
            "router_order_effect_counts": cls._counter_rows(
                row["router_order_effect_counts"]
            ),
            "block_reason_counts": cls._counter_rows(row["block_reason_counts"]),
        }
        return result

    @classmethod
    def _finalize_legacy_shadow_comparison(
        cls,
        comparison: dict[str, Any],
    ) -> dict[str, Any]:
        route_rows = []
        for row in comparison["by_legacy_route"].values():
            route_rows.append(
                {
                    **row,
                    "entry_types": sorted(row["entry_types"]),
                }
            )
        route_rows.sort(
            key=lambda row: (
                -int(row["removed_from_order_influence_count"]),
                -int(row["old_logic_buy_count"]),
                row["legacy_route"],
            )
        )
        return {
            "old_logic_buy_count": int(comparison["old_logic_buy_count"]),
            "new_logic_buy_count": int(comparison["new_logic_buy_count"]),
            "new_logic_shadow_count": int(comparison["new_logic_shadow_count"]),
            "minimum_probe_count": int(comparison["minimum_probe_count"]),
            "removed_from_order_influence_count": int(
                comparison["removed_from_order_influence_count"]
            ),
            "by_legacy_route": route_rows,
        }

    @classmethod
    def _finalize_router_group_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "guard_status_counts": cls._counter_rows(row["guard_status_counts"]),
            "reentry_status_counts": cls._counter_rows(row["reentry_status_counts"]),
            "router_status_counts": cls._counter_rows(row["router_status_counts"]),
            "router_order_effect_counts": cls._counter_rows(
                row["router_order_effect_counts"]
            ),
        }

    @staticmethod
    def _context_flags(item: dict[str, Any]) -> dict[str, Any]:
        setup = item.get("setup") if isinstance(item.get("setup"), dict) else {}
        chart = setup.get("chart_trade")
        if not isinstance(chart, dict):
            chart = {}
        supply_flow = setup.get("supply_flow")
        entry_policy = setup.get("entry_type_policy")
        if not isinstance(entry_policy, dict):
            entry_policy = {}
        supply_score = setup.get("supply_score")
        supply_missing = (
            supply_score is None
            and not supply_flow
            and not item.get("supply_score_snapshot")
        )
        return {
            "force_entry": bool(chart.get("force_entry") or setup.get("force_entry")),
            "supply_missing": bool(supply_missing),
            "entry_grade": (
                setup.get("entry_grade")
                or entry_policy.get("entry_grade")
                or chart.get("entry_grade")
            ),
        }

    @staticmethod
    def _setup(item: dict[str, Any]) -> dict[str, Any]:
        setup = item.get("setup")
        return setup if isinstance(setup, dict) else {}

    @staticmethod
    def _playbook_name(setup: dict[str, Any]) -> str:
        playbook = setup.get("strategy_playbook")
        if isinstance(playbook, dict) and playbook.get("name"):
            return str(playbook["name"])
        return "none"

    @classmethod
    def _leader_strategy_key(
        cls,
        entry_type: str,
        playbook: str,
        setup: dict[str, Any],
    ) -> str:
        if playbook == "a_plus_trend_day_runner":
            return "trend_day_runner"
        if playbook == "vwap_pullback_reentry":
            return "vwap_pullback"
        if playbook == "new_high_rebreakout_reentry_probe":
            return "new_high_rebreakout"
        if entry_type == "vwap_pullback":
            return "vwap_pullback"
        if entry_type in {"opening_range_breakout", "chart_breakout_hold"}:
            grade = cls._entry_grade(setup)
            if grade == "A+":
                return "trend_day_runner"
        return "unmapped"

    @staticmethod
    def _entry_grade(setup: dict[str, Any]) -> str:
        entry_guard = setup.get("entry_guard_policy")
        if not isinstance(entry_guard, dict):
            entry_guard = {}
        entry_type_policy = setup.get("entry_type_policy")
        if not isinstance(entry_type_policy, dict):
            entry_type_policy = {}
        return str(
            setup.get("entry_grade")
            or entry_guard.get("entry_grade")
            or entry_type_policy.get("entry_grade")
            or ""
        )

    @staticmethod
    def _policy_status(policy: Any, *, default: str) -> str:
        if not isinstance(policy, dict):
            return default
        return str(policy.get("status") or policy.get("policy_status") or default)

    @staticmethod
    def _router_order_effect(policy: Any) -> str:
        if not isinstance(policy, dict):
            return "not_recorded"
        return str(policy.get("order_effect") or "not_recorded")

    @staticmethod
    def _legacy_candidate_reason(
        entry_type: str,
        playbook: str,
    ) -> str | None:
        if playbook in {
            "a_plus_trend_day_runner",
            "vwap_pullback_reentry",
            "new_high_rebreakout_reentry_probe",
        }:
            return None
        if entry_type in {
            "volume_breakout",
            "breakout_probe",
            "paper_probe",
            "order_flow_momentum",
        }:
            return "legacy_base_signal"
        if entry_type in {
            "chart_pattern_entry",
            "chart_long_bullish",
            "chart_cup_handle",
            "chart_ascending_triangle",
        }:
            return "legacy_chart_pattern"
        if entry_type in {"chart_vwap_reclaim", "chart_order_block_reentry"}:
            return "legacy_reentry_route"
        return None

    @staticmethod
    def _legacy_low_explanation(
        legacy_route: str,
        legacy_reasons: list[str],
        entry_type: str,
    ) -> bool:
        tokens = {
            legacy_route,
            entry_type,
            *legacy_reasons,
        }
        broad_markers = {
            "legacy_base_signal",
            "legacy_chart_pattern",
            "broad_chart_pattern_entry",
            "simple_volume_breakout",
            "simple_breakout_probe",
            "mean_reversion_rebound",
            "chart_pattern_entry",
            "volume_breakout",
            "breakout_probe",
            "paper_probe",
        }
        return any(token in broad_markers for token in tokens)

    @classmethod
    def _legacy_data_gap_or_supply_gap(
        cls,
        decision: dict[str, Any],
        setup: dict[str, Any],
    ) -> bool:
        text_parts = cls._decision_block_reasons(decision)
        for key in (
            "entry_guard_policy",
            "entry_type_policy",
            "reentry_policy",
            "router_execution_policy",
        ):
            policy = setup.get(key)
            if isinstance(policy, dict):
                text_parts.extend(
                    str(policy.get(name) or "")
                    for name in ("status", "message", "blocked_reason")
                )
        supply = setup.get("supply_flow") or setup.get("supply_score")
        if isinstance(supply, dict):
            text_parts.extend(
                str(supply.get(name) or "")
                for name in ("status", "blocked_reason", "message")
            )
        lowered = " ".join(text_parts).lower()
        markers = (
            "data_gap",
            "데이터 공백",
            "수급 공백",
            "supply_missing",
            "수급 없음",
            "수급/대장주 점수 없음",
            "freshness",
            "stale",
        )
        return any(marker.lower() in lowered for marker in markers)

    @classmethod
    def _legacy_block_only(
        cls,
        decision: dict[str, Any],
        setup: dict[str, Any],
    ) -> bool:
        outcome = str(decision.get("outcome") or "").lower()
        if outcome in {"skipped", "failed"}:
            return True
        for key in (
            "entry_guard_policy",
            "entry_type_policy",
            "reentry_policy",
            "router_execution_policy",
        ):
            status = cls._policy_status(setup.get(key), default="")
            if status.startswith(("blocked", "shadow_only")):
                return True
        return bool(cls._decision_block_reasons(decision))

    @staticmethod
    def _decision_block_reasons(item: dict[str, Any]) -> list[str]:
        reasons = item.get("block_reasons")
        if isinstance(reasons, list) and reasons:
            return [str(reason) for reason in reasons if str(reason)]
        message = str(item.get("message") or "")
        outcome = str(item.get("outcome") or "").lower()
        action = str(item.get("action") or "").lower()
        if message and (outcome in {"skipped", "failed"} or action != "buy"):
            return [message]
        return []

    @staticmethod
    def _best_outcome_by_decision(
        outcomes: list[dict[str, Any]],
    ) -> dict[int, dict[str, Any]]:
        best: dict[int, dict[str, Any]] = {}
        for outcome in outcomes:
            decision_id = outcome.get("decision_id")
            if decision_id is None:
                continue
            key = int(decision_id)
            current = best.get(key)
            current_up = (
                TradingReviewReport._float_or_none(current.get("max_up_rate"))
                if current is not None
                else None
            )
            next_up = TradingReviewReport._float_or_none(outcome.get("max_up_rate"))
            if current is None or (next_up or 0.0) > (current_up or 0.0):
                best[key] = outcome
        return best

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _count_status(counts: dict[str, int], status: str) -> None:
        counts[status] = counts.get(status, 0) + 1

    @staticmethod
    def _dominant_key(counts: dict[str, int], *, default: str) -> str:
        if not counts:
            return default
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    @staticmethod
    def _counter_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"key": key, "count": int(count)}
            for key, count in sorted(
                counts.items(),
                key=lambda item: (-int(item[1]), str(item[0])),
            )
        ]

    @staticmethod
    def _ten_minute_bucket(timestamp: str) -> str:
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            return timestamp[:16]
        return f"{parsed.hour:02d}:{(parsed.minute // 10) * 10:02d}"

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}
