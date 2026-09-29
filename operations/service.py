import json
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from db.database import Database


class OperationsService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def start_run(
        self,
        run_type: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operation_runs(
                    run_type, status, started_at, metadata_json
                )
                VALUES (?, 'running', ?, ?)
                """,
                (
                    run_type,
                    timestamp,
                    self._serialize(metadata or {}),
                ),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        success_count: int,
        failure_count: int,
        message: Optional[str] = None,
    ) -> None:
        if success_count < 0 or failure_count < 0:
            raise ValueError("성공·실패 건수는 0 이상이어야 합니다.")
        status = "success"
        if failure_count and success_count:
            status = "partial_failure"
        elif failure_count:
            status = "failed"
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE operation_runs
                SET status = ?, completed_at = ?, success_count = ?,
                    failure_count = ?, message = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    timestamp,
                    success_count,
                    failure_count,
                    message,
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"실행 중인 운영 기록을 찾을 수 없습니다: {run_id}")

    def recover_running_runs(
        self,
        stale_minutes: int = 10,
        *,
        now: Optional[datetime] = None,
    ) -> list[int]:
        if stale_minutes < 1:
            raise ValueError("복구 기준은 1분 이상이어야 합니다.")
        current = now or datetime.now()
        cutoff = (current - timedelta(minutes=stale_minutes)).isoformat(
            timespec="seconds"
        )
        completed_at = current.isoformat(timespec="seconds")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM operation_runs
                WHERE status = 'running' AND started_at <= ?
                ORDER BY id
                """,
                (cutoff,),
            ).fetchall()
            run_ids = [int(row["id"]) for row in rows]
            if run_ids:
                placeholders = ", ".join("?" for _ in run_ids)
                connection.execute(
                    f"""
                    UPDATE operation_runs
                    SET status = 'failed',
                        completed_at = ?,
                        failure_count = CASE
                            WHEN failure_count = 0 THEN 1
                            ELSE failure_count
                        END,
                        message = ?
                    WHERE id IN ({placeholders}) AND status = 'running'
                    """,
                    (
                        completed_at,
                        "비정상 종료로 완료되지 않아 복구 처리됨",
                        *run_ids,
                    ),
                )
        if run_ids:
            self.record_event(
                "system",
                "interrupted_runs_recovered",
                "warning",
                f"중단된 실행 {len(run_ids)}건을 실패 상태로 복구했습니다.",
                details={
                    "run_ids": run_ids,
                    "stale_minutes": stale_minutes,
                },
            )
        return run_ids

    def record_event(
        self,
        module: str,
        event_type: str,
        severity: str,
        message: str,
        *,
        run_id: Optional[int] = None,
        stock_code: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> int:
        normalized_severity = severity.strip().lower()
        if normalized_severity not in {"info", "warning", "error"}:
            raise ValueError("이벤트 심각도는 info, warning, error 중 하나여야 합니다.")
        timestamp = datetime.now().isoformat(timespec="seconds")
        status = "failed" if normalized_severity == "error" else "success"
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO operation_events(
                    run_id, module, event_type, severity, stock_code,
                    message, details_json, occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    module,
                    event_type,
                    normalized_severity,
                    stock_code,
                    message,
                    self._serialize(details or {}),
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO operation_module_status(
                    module, status, last_success_at, last_failure_at,
                    last_message, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(module) DO UPDATE SET
                    status = excluded.status,
                    last_success_at = COALESCE(
                        excluded.last_success_at,
                        operation_module_status.last_success_at
                    ),
                    last_failure_at = COALESCE(
                        excluded.last_failure_at,
                        operation_module_status.last_failure_at
                    ),
                    last_message = excluded.last_message,
                    updated_at = excluded.updated_at
                """,
                (
                    module,
                    status,
                    timestamp if status == "success" else None,
                    timestamp if status == "failed" else None,
                    message,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def list_runs(self, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_type, status, started_at, completed_at,
                       success_count, failure_count, message, metadata_json
                FROM operation_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._decode(row, "metadata_json", "metadata") for row in rows]

    def list_events(
        self,
        *,
        limit: int = 50,
        module: Optional[str] = None,
        severity: Optional[str] = None,
    ) -> list[dict]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        query = """
            SELECT id, run_id, module, event_type, severity, stock_code,
                   message, details_json, occurred_at
            FROM operation_events
            WHERE 1 = 1
        """
        parameters: list[Any] = []
        if module:
            query += " AND module = ?"
            parameters.append(module)
        if severity:
            normalized = severity.strip().lower()
            if normalized not in {"info", "warning", "error"}:
                raise ValueError(
                    "이벤트 심각도는 info, warning, error 중 하나여야 합니다."
                )
            query += " AND severity = ?"
            parameters.append(normalized)
        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode(row, "details_json", "details") for row in rows]

    def module_status(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT module, status, last_success_at, last_failure_at,
                       last_message, updated_at
                FROM operation_module_status
                ORDER BY module
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def health(self, stale_minutes: int = 10) -> dict:
        if stale_minutes < 1:
            raise ValueError("신선도 기준은 1분 이상이어야 합니다.")
        statuses = self.module_status()
        now = datetime.now()
        checks = []
        for row in statuses:
            stale = False
            if row["last_success_at"]:
                last_success = datetime.fromisoformat(row["last_success_at"])
                stale = (now - last_success).total_seconds() > stale_minutes * 60
            healthy = row["status"] != "failed"
            checks.append({**row, "healthy": healthy, "stale": stale})
        with self.database.connect() as connection:
            alert_rows = connection.execute(
                """
                SELECT alert_key, severity, message, occurrence_count,
                       first_triggered_at, last_triggered_at
                FROM operation_alerts
                WHERE status = 'active'
                ORDER BY severity DESC, last_triggered_at DESC
                """
            ).fetchall()
        active_alerts = [dict(row) for row in alert_rows]
        return {
            "healthy": (
                bool(checks)
                and all(row["healthy"] for row in checks)
                and not active_alerts
            ),
            "stale_minutes": stale_minutes,
            "checks": checks,
            "active_alerts": active_alerts,
        }

    @staticmethod
    def _serialize(value: dict[str, Any]) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _decode(row: Any, source_key: str, target_key: str) -> dict:
        result = dict(row)
        result[target_key] = json.loads(result.pop(source_key))
        return result


class CollectorOperationsTracker:
    def __init__(
        self,
        service: OperationsService,
        run_type: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
        cycle_completed_callback: Optional[
            Callable[[dict[str, Any]], None]
        ] = None,
    ) -> None:
        self.service = service
        self.run_type = run_type
        self.metadata = metadata or {}
        self.cycle_completed_callback = cycle_completed_callback
        self.current_run_id: Optional[int] = None

    def handle(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "cycle_started":
            self.current_run_id = self.service.start_run(
                self.run_type,
                metadata={**self.metadata, **payload},
            )
            return
        if event_type == "cycle_completed":
            if self.current_run_id is None:
                return
            success_count = int(payload["success_count"])
            failure_count = int(payload["failure_count"])
            severity = "info"
            if failure_count and success_count:
                severity = "warning"
            elif failure_count:
                severity = "error"
            self.service.record_event(
                "collection",
                "cycle_completed",
                severity,
                (
                    f"수집 주기 완료: 성공={success_count}, "
                    f"실패={failure_count}"
                ),
                run_id=self.current_run_id,
                details=payload,
            )
            self.service.finish_run(
                self.current_run_id,
                success_count=success_count,
                failure_count=failure_count,
            )
            self.current_run_id = None
            if self.cycle_completed_callback is not None:
                self.cycle_completed_callback(payload)
            return
        severity = "error" if event_type.endswith("_failed") else "info"
        module = "collection"
        if event_type.startswith("after_collection"):
            module = "automation"
        self.service.record_event(
            module,
            event_type,
            severity,
            str(payload.get("message") or event_type),
            run_id=self.current_run_id,
            stock_code=payload.get("stock_code"),
            details=payload,
        )

    def record(
        self,
        module: str,
        event_type: str,
        severity: str,
        message: str,
        *,
        stock_code: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        self.service.record_event(
            module,
            event_type,
            severity,
            message,
            run_id=self.current_run_id,
            stock_code=stock_code,
            details=details,
        )
