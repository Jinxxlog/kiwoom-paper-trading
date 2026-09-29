import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from data.market_calendar import SEOUL_TIMEZONE
from db.database import Database
from operations.daily_check import DailyOperationsChecker
from operations.replay_outcomes import ReplayOutcomeService


class DailyOperationsReport:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.checker = DailyOperationsChecker(database)
        self.replay_outcomes = ReplayOutcomeService(database)

    def build(
        self,
        target_date: date,
        *,
        now: Optional[datetime] = None,
        expected_interval_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        check = self.checker.check(
            target_date,
            now=now,
            expected_interval_seconds=expected_interval_seconds,
        )
        day = target_date.isoformat()
        generated_at = now or datetime.now(SEOUL_TIMEZONE)
        if generated_at.tzinfo is not None:
            generated_at = generated_at.astimezone(SEOUL_TIMEZONE)
        forward_outcomes = self.replay_outcomes.recompute(
            target_date,
            now=generated_at,
        )
        return {
            "report_date": day,
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "validation": {
                "status": check.status,
                "trading_day": check.trading_day,
                "full_day_expected": check.full_day_expected,
                "checks": [
                    {
                        "code": item.code,
                        "status": item.status,
                        "message": item.message,
                        "details": item.details,
                    }
                    for item in check.checks
                ],
            },
            "collection": self._collection(day),
            "events": self._events(day),
            "prices": self._prices(day),
            "trading": self._trading(day),
            "strategy": self._strategy(day),
            "forward_outcomes": forward_outcomes,
            "risk": self._risk(day),
        }

    def export_json(
        self,
        target_date: date,
        output: Optional[Path] = None,
        *,
        now: Optional[datetime] = None,
        expected_interval_seconds: Optional[int] = None,
    ) -> Path:
        path = output or Path("reports") / f"operations-{target_date.isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.build(
            target_date,
            now=now,
            expected_interval_seconds=expected_interval_seconds,
        )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _collection(self, day: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            summary = connection.execute(
                """
                SELECT COUNT(*) AS run_count,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_runs,
                       SUM(CASE WHEN status = 'partial_failure' THEN 1 ELSE 0 END) AS partial_runs,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_runs,
                       SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_runs,
                       COALESCE(SUM(success_count), 0) AS success_count,
                       COALESCE(SUM(failure_count), 0) AS failure_count,
                       MIN(started_at) AS first_started_at,
                       MAX(started_at) AS last_started_at
                FROM operation_runs
                WHERE run_type = 'periodic_collect'
                  AND substr(started_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT id, status, started_at, completed_at,
                       success_count, failure_count, message, metadata_json
                FROM operation_runs
                WHERE run_type = 'periodic_collect'
                  AND substr(started_at, 1, 10) = ?
                ORDER BY started_at, id
                """,
                (day,),
            ).fetchall()
        return {
            "summary": {
                key: int(summary[key] or 0)
                if key not in {"first_started_at", "last_started_at"}
                else summary[key]
                for key in summary.keys()
            },
            "runs": [
                {
                    **{
                        key: row[key]
                        for key in row.keys()
                        if key != "metadata_json"
                    },
                    "metadata": json.loads(row["metadata_json"]),
                }
                for row in rows
            ],
        }

    def _events(self, day: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            severity_rows = connection.execute(
                """
                SELECT severity, COUNT(*) AS count
                FROM operation_events
                WHERE substr(occurred_at, 1, 10) = ?
                GROUP BY severity
                ORDER BY severity
                """,
                (day,),
            ).fetchall()
            module_rows = connection.execute(
                """
                SELECT module, event_type, severity, COUNT(*) AS count
                FROM operation_events
                WHERE substr(occurred_at, 1, 10) = ?
                GROUP BY module, event_type, severity
                ORDER BY module, event_type, severity
                """,
                (day,),
            ).fetchall()
            notable = connection.execute(
                """
                SELECT id, run_id, module, event_type, severity,
                       stock_code, message, details_json, occurred_at
                FROM operation_events
                WHERE substr(occurred_at, 1, 10) = ?
                  AND severity IN ('warning', 'error')
                ORDER BY occurred_at, id
                LIMIT 100
                """,
                (day,),
            ).fetchall()
        severity = {"info": 0, "warning": 0, "error": 0}
        for row in severity_rows:
            severity[row["severity"]] = int(row["count"])
        return {
            "severity": severity,
            "by_module": [dict(row) for row in module_rows],
            "notable": [
                {
                    **{
                        key: row[key]
                        for key in row.keys()
                        if key != "details_json"
                    },
                    "details": json.loads(row["details_json"]),
                }
                for row in notable
            ],
        }

    def _prices(self, day: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT w.stock_code, w.stock_name,
                       COUNT(p.id) AS snapshot_count,
                       MIN(p.collected_at) AS first_collected_at,
                       MAX(p.collected_at) AS last_collected_at,
                       MIN(p.current_price) AS minimum_price,
                       MAX(p.current_price) AS maximum_price
                FROM watchlist w
                LEFT JOIN price_snapshots p
                  ON p.stock_code = w.stock_code
                 AND substr(p.collected_at, 1, 10) = ?
                WHERE w.enabled = 1
                GROUP BY w.stock_code, w.stock_name
                ORDER BY w.stock_code
                """,
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _trading(self, day: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            accounts = connection.execute(
                """
                SELECT a.name AS account_name,
                       COUNT(DISTINCT o.id) AS order_count,
                       SUM(CASE WHEN o.status = 'filled' THEN 1 ELSE 0 END) AS filled_orders,
                       SUM(CASE WHEN o.status = 'rejected' THEN 1 ELSE 0 END) AS rejected_orders,
                       SUM(CASE WHEN o.side = 'buy' AND o.status = 'filled' THEN 1 ELSE 0 END) AS buy_orders,
                       SUM(CASE WHEN o.side = 'sell' AND o.status = 'filled' THEN 1 ELSE 0 END) AS sell_orders,
                       COALESCE(SUM(f.realized_pnl), 0) AS realized_pnl,
                       COALESCE(SUM(f.commission), 0) AS commission,
                       COALESCE(SUM(f.tax), 0) AS tax,
                       COALESCE(SUM(f.slippage_amount), 0) AS slippage
                FROM paper_accounts a
                LEFT JOIN paper_orders o
                  ON o.account_id = a.id
                 AND substr(o.requested_at, 1, 10) = ?
                LEFT JOIN paper_fills f ON f.order_id = o.id
                GROUP BY a.id, a.name
                HAVING COUNT(DISTINCT o.id) > 0
                ORDER BY a.id
                """,
                (day,),
            ).fetchall()
            orders = connection.execute(
                """
                SELECT o.id, a.name AS account_name, o.stock_code,
                       o.side, o.quantity, o.status, o.reason,
                       o.rejection_reason, o.requested_at,
                       f.fill_price, f.commission, f.tax,
                       f.slippage_amount, f.realized_pnl
                FROM paper_orders o
                JOIN paper_accounts a ON a.id = o.account_id
                LEFT JOIN paper_fills f ON f.order_id = o.id
                WHERE substr(o.requested_at, 1, 10) = ?
                ORDER BY o.requested_at, o.id
                """,
                (day,),
            ).fetchall()
        account_rows = []
        for row in accounts:
            item = dict(row)
            for key in item:
                if key != "account_name":
                    item[key] = int(item[key] or 0)
            item["total_cost"] = (
                item["commission"] + item["tax"] + item["slippage"]
            )
            account_rows.append(item)
        return {
            "accounts": account_rows,
            "orders": [dict(row) for row in orders],
        }

    def _strategy(self, day: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            summary = connection.execute(
                """
                SELECT COUNT(*) AS signal_count,
                       SUM(CASE WHEN signal_pass = 1 THEN 1 ELSE 0 END) AS passed_count,
                       SUM(CASE WHEN action = 'executed' THEN 1 ELSE 0 END) AS executed_count,
                       SUM(CASE WHEN action = 'skipped' THEN 1 ELSE 0 END) AS skipped_count,
                       SUM(CASE WHEN action = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                       SUM(CASE WHEN action = 'observe' THEN 1 ELSE 0 END) AS observed_count
                FROM strategy_signals
                WHERE substr(signal_time, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            by_stock = connection.execute(
                """
                SELECT stock_code, COUNT(*) AS signal_count,
                       SUM(CASE WHEN signal_pass = 1 THEN 1 ELSE 0 END) AS passed_count,
                       SUM(CASE WHEN action = 'executed' THEN 1 ELSE 0 END) AS executed_count,
                       SUM(CASE WHEN action = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM strategy_signals
                WHERE substr(signal_time, 1, 10) = ?
                GROUP BY stock_code
                ORDER BY stock_code
                """,
                (day,),
            ).fetchall()
        return {
            "summary": {
                key: int(summary[key] or 0) for key in summary.keys()
            },
            "by_stock": [dict(row) for row in by_stock],
        }

    def _risk(self, day: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            summary = connection.execute(
                """
                SELECT COUNT(*) AS event_count,
                       SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) AS executed_count,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM paper_risk_events
                WHERE substr(occurred_at, 1, 10) = ?
                """,
                (day,),
            ).fetchone()
            by_type = connection.execute(
                """
                SELECT event_type, status, COUNT(*) AS count
                FROM paper_risk_events
                WHERE substr(occurred_at, 1, 10) = ?
                GROUP BY event_type, status
                ORDER BY event_type, status
                """,
                (day,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT e.id, a.name AS account_name, e.stock_code,
                       e.event_type, e.trigger_price, e.threshold_price,
                       e.highest_price, e.order_id, e.status,
                       e.message, e.occurred_at
                FROM paper_risk_events e
                JOIN paper_accounts a ON a.id = e.account_id
                WHERE substr(e.occurred_at, 1, 10) = ?
                ORDER BY e.occurred_at, e.id
                """,
                (day,),
            ).fetchall()
        return {
            "summary": {
                key: int(summary[key] or 0) for key in summary.keys()
            },
            "by_type": [dict(row) for row in by_type],
            "events": [dict(row) for row in events],
        }
