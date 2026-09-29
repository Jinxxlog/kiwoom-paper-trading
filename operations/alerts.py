import json
from datetime import datetime
from typing import Any, Optional

from config.settings import OperationsAlertSettings
from data.market_calendar import MarketCalendar, SEOUL_TIMEZONE
from db.database import Database
from notifications.telegram import TelegramNotifier
from operations.service import OperationsService


COLLECTION_DELAY = "collection_delay"
CONSECUTIVE_FAILURES = "consecutive_collection_failures"


class OperationsAlertService:
    def __init__(
        self,
        database: Database,
        settings: OperationsAlertSettings,
        operations: Optional[OperationsService] = None,
        notifier: Optional[TelegramNotifier] = None,
    ) -> None:
        if settings.collection_delay_seconds < 1:
            raise ValueError("수집 지연 경보 기준은 1초 이상이어야 합니다.")
        if settings.consecutive_failures < 1:
            raise ValueError("연속 실패 경보 기준은 1회 이상이어야 합니다.")
        self.database = database
        self.settings = settings
        self.operations = operations or OperationsService(database)
        self.calendar = MarketCalendar(database)
        self.notifier = notifier or TelegramNotifier.from_env()

    def evaluate(
        self,
        *,
        interval_seconds: int,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        if interval_seconds < 1:
            raise ValueError("수집 간격은 1초 이상이어야 합니다.")
        current = self._seoul_now(now)
        self._evaluate_collection_delay(current, interval_seconds)
        self._evaluate_consecutive_failures(current)
        return self.list_alerts()

    def list_alerts(self, *, include_resolved: bool = False) -> list[dict]:
        query = """
            SELECT alert_key, status, severity, message, occurrence_count,
                   first_triggered_at, last_triggered_at, resolved_at,
                   details_json
            FROM operation_alerts
        """
        parameters: tuple[object, ...] = ()
        if not include_resolved:
            query += " WHERE status = ?"
            parameters = ("active",)
        query += " ORDER BY status, severity DESC, last_triggered_at DESC"
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def _evaluate_collection_delay(
        self,
        current: datetime,
        interval_seconds: int,
    ) -> None:
        if not self.calendar.is_market_open(current):
            return
        allowed_seconds = max(
            self.settings.collection_delay_seconds,
            interval_seconds * 3,
        )
        day = current.date().isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, started_at
                FROM operation_runs
                WHERE run_type = 'periodic_collect'
                  AND substr(started_at, 1, 10) = ?
                ORDER BY started_at DESC, id DESC
                LIMIT 2
                """,
                (day,),
            ).fetchall()
        details: dict[str, Any] = {
            "allowed_seconds": allowed_seconds,
            "interval_seconds": interval_seconds,
        }
        delayed = False
        message = ""
        if not rows:
            seconds_since_open = (
                current.hour * 3600
                + current.minute * 60
                + current.second
                - 9 * 3600
            )
            delayed = seconds_since_open > allowed_seconds
            details["seconds_since_open"] = seconds_since_open
            message = (
                f"장 시작 후 {seconds_since_open}초 동안 주기 수집 기록이 없습니다."
            )
        else:
            latest = datetime.fromisoformat(rows[0]["started_at"])
            current_naive = current.replace(tzinfo=None)
            age_seconds = max(
                0,
                int((current_naive - latest).total_seconds()),
            )
            gap_seconds = 0
            if len(rows) > 1:
                previous = datetime.fromisoformat(rows[1]["started_at"])
                gap_seconds = int((latest - previous).total_seconds())
            delayed = age_seconds > allowed_seconds or gap_seconds > allowed_seconds
            details.update(
                {
                    "latest_run_id": int(rows[0]["id"]),
                    "latest_started_at": rows[0]["started_at"],
                    "age_seconds": age_seconds,
                    "previous_gap_seconds": gap_seconds,
                }
            )
            message = (
                f"주기 수집 지연 감지: 최신 경과={age_seconds}초, "
                f"직전 공백={gap_seconds}초, 허용={allowed_seconds}초"
            )
        if delayed:
            self._activate(
                COLLECTION_DELAY,
                "warning",
                message,
                details,
                current,
            )
        else:
            self._resolve(
                COLLECTION_DELAY,
                "주기 수집 지연이 정상화되었습니다.",
                details,
                current,
            )

    def _evaluate_consecutive_failures(self, current: datetime) -> None:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, status, failure_count, started_at
                FROM operation_runs
                WHERE run_type = 'periodic_collect'
                  AND status != 'running'
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.settings.consecutive_failures,),
            ).fetchall()
        failure_count = 0
        run_ids = []
        for row in rows:
            failed = (
                row["status"] in {"failed", "partial_failure"}
                or int(row["failure_count"]) > 0
            )
            if not failed:
                break
            failure_count += 1
            run_ids.append(int(row["id"]))
        details = {
            "consecutive_failures": failure_count,
            "threshold": self.settings.consecutive_failures,
            "run_ids": run_ids,
        }
        if failure_count >= self.settings.consecutive_failures:
            self._activate(
                CONSECUTIVE_FAILURES,
                "error",
                f"주기 수집이 {failure_count}회 연속 실패했습니다.",
                details,
                current,
            )
        else:
            self._resolve(
                CONSECUTIVE_FAILURES,
                "주기 수집 연속 실패가 정상화되었습니다.",
                details,
                current,
            )

    def _activate(
        self,
        alert_key: str,
        severity: str,
        message: str,
        details: dict[str, Any],
        current: datetime,
    ) -> None:
        timestamp = current.isoformat(timespec="seconds")
        activated = False
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT status FROM operation_alerts WHERE alert_key = ?",
                (alert_key,),
            ).fetchone()
            activated = existing is None or existing["status"] != "active"
            connection.execute(
                """
                INSERT INTO operation_alerts(
                    alert_key, status, severity, message, occurrence_count,
                    first_triggered_at, last_triggered_at, resolved_at,
                    details_json
                )
                VALUES (?, 'active', ?, ?, 1, ?, ?, NULL, ?)
                ON CONFLICT(alert_key) DO UPDATE SET
                    status = 'active',
                    severity = excluded.severity,
                    message = excluded.message,
                    occurrence_count = CASE
                        WHEN operation_alerts.status = 'active'
                            THEN operation_alerts.occurrence_count + 1
                        ELSE 1
                    END,
                    first_triggered_at = CASE
                        WHEN operation_alerts.status = 'active'
                            THEN operation_alerts.first_triggered_at
                        ELSE excluded.first_triggered_at
                    END,
                    last_triggered_at = excluded.last_triggered_at,
                    resolved_at = NULL,
                    details_json = excluded.details_json
                """,
                (
                    alert_key,
                    severity,
                    message,
                    timestamp,
                    timestamp,
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        if activated:
            self.operations.record_event(
                "alert",
                "alert_triggered",
                severity,
                message,
                details={"alert_key": alert_key, **details},
            )
            self.notifier.send(
                "\n".join(
                    [
                        "[자동투자 경보]",
                        f"등급: {severity}",
                        f"항목: {alert_key}",
                        f"내용: {message}",
                    ]
                )
            )

    def _resolve(
        self,
        alert_key: str,
        message: str,
        details: dict[str, Any],
        current: datetime,
    ) -> None:
        timestamp = current.isoformat(timespec="seconds")
        resolved = False
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT status
                FROM operation_alerts
                WHERE alert_key = ?
                """,
                (alert_key,),
            ).fetchone()
            if row is not None and row["status"] == "active":
                connection.execute(
                    """
                    UPDATE operation_alerts
                    SET status = 'resolved',
                        message = ?,
                        resolved_at = ?,
                        details_json = ?
                    WHERE alert_key = ?
                    """,
                    (
                        message,
                        timestamp,
                        json.dumps(
                            details,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        alert_key,
                    ),
                )
                resolved = True
        if resolved:
            self.operations.record_event(
                "alert",
                "alert_resolved",
                "info",
                message,
                details={"alert_key": alert_key, **details},
            )
            self.notifier.send(
                "\n".join(
                    [
                        "[자동투자 경보 해제]",
                        f"항목: {alert_key}",
                        f"내용: {message}",
                    ]
                )
            )

    @staticmethod
    def _seoul_now(now: Optional[datetime]) -> datetime:
        current = now or datetime.now(SEOUL_TIMEZONE)
        if current.tzinfo is None:
            return current.replace(tzinfo=SEOUL_TIMEZONE)
        return current.astimezone(SEOUL_TIMEZONE)
