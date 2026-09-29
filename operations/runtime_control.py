import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import BASE_DIR
from data.scheduler import ProcessLock, is_process_running


class CollectorRuntimeControl:
    def __init__(self, database_path: Path) -> None:
        base_path = Path(database_path)
        self.lock_path = base_path.with_suffix(".collector.lock")
        self.stop_path = base_path.with_suffix(".collector.stop")
        self.metadata_path = base_path.with_suffix(".collector.json")
        self.log_path = base_path.with_suffix(".collector.log")

    def status(self) -> dict[str, Any]:
        lock = ProcessLock(self.lock_path).inspect()
        metadata = self._load_metadata()
        metadata_pid = metadata.get("pid")
        metadata_active = (
            isinstance(metadata_pid, int) and is_process_running(metadata_pid)
        )
        return {
            "running": lock.active,
            "pid": lock.pid,
            "starting": metadata_active and not lock.exists,
            "stop_requested": self.stop_path.exists(),
            "stale_lock": lock.stale,
            "metadata": metadata,
            "log_path": str(self.log_path),
        }

    def start(
        self,
        *,
        interval: int = 60,
        auto_strategy: bool = True,
        account: str = "default",
        with_depth: bool = False,
        with_trades: bool = False,
        unattended: bool = False,
        candidate_limit: int = 10,
        preopen_scan: bool = True,
        small_account_aggressive: bool = False,
        strategy_ab_active_mode: str = "remake",
        bootstrap_token: str | None = None,
        bootstrap_expires_dt: str | None = None,
    ) -> dict[str, Any]:
        if interval < 5:
            raise ValueError("수집 간격은 5초 이상이어야 합니다.")
        if candidate_limit < 1:
            raise ValueError("후보 종목 수는 1 이상이어야 합니다.")
        account = account.strip()
        if not account:
            raise ValueError("모의계좌 이름은 비어 있을 수 없습니다.")
        strategy_ab_active_mode = strategy_ab_active_mode.strip().lower()
        if strategy_ab_active_mode not in {"normal", "remake"}:
            raise ValueError("strategy_ab_active_mode must be normal or remake")

        status = self.status()
        if status["running"]:
            raise RuntimeError(f"자동매매가 이미 실행 중입니다. PID={status['pid']}")
        if status["starting"]:
            starting_pid = status["metadata"].get("pid")
            raise RuntimeError(
                f"자동매매 시작 처리 중입니다. PID={starting_pid}"
            )
        if status["stale_lock"]:
            ProcessLock(self.lock_path).recover_stale()
        self.stop_path.unlink(missing_ok=True)

        command = [
            sys.executable,
            str(BASE_DIR / "main.py"),
            "run",
            "--interval",
            str(interval),
            "--account",
            account,
        ]
        if auto_strategy:
            command.append("--auto-strategy")
        if with_depth:
            command.append("--with-depth")
        if with_trades:
            command.append("--with-trades")
        if unattended:
            command.extend(
                ["--unattended", "--candidate-limit", str(candidate_limit)]
            )
            if preopen_scan:
                command.append("--preopen-scan")

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = self.log_path.open("a", encoding="utf-8")
        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        child_env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "STRATEGY_SMALL_ACCOUNT_AGGRESSIVE_ENABLED": (
                "true" if small_account_aggressive else "false"
            ),
            "STRATEGY_AB_TEST_ENABLED": "true",
            "STRATEGY_AB_ACTIVE_MODE": strategy_ab_active_mode,
        }
        if bootstrap_token and bootstrap_expires_dt:
            child_env["KIWOOM_BOOTSTRAP_TOKEN"] = bootstrap_token
            child_env["KIWOOM_BOOTSTRAP_EXPIRES_DT"] = bootstrap_expires_dt
        try:
            process = subprocess.Popen(
                command,
                cwd=BASE_DIR,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                close_fds=True,
            )
        finally:
            log_file.close()

        metadata = {
            "pid": process.pid,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "interval_seconds": interval,
            "auto_strategy": auto_strategy,
            "account_name": account,
            "with_depth": with_depth,
            "with_trades": with_trades,
            "unattended": unattended,
            "candidate_limit": candidate_limit,
            "preopen_scan": bool(unattended and preopen_scan),
            "small_account_aggressive": small_account_aggressive,
            "strategy_ab_active_mode": strategy_ab_active_mode,
            "command": command,
        }
        self._write_metadata(metadata)
        return metadata

    def request_stop(self) -> bool:
        status = self.status()
        if not status["running"] and not status["starting"]:
            self.stop_path.unlink(missing_ok=True)
            return False
        self.stop_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(
            datetime.now().isoformat(timespec="seconds"),
            encoding="ascii",
        )
        return True

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    def clear_stop_request(self) -> None:
        self.stop_path.unlink(missing_ok=True)

    def _load_metadata(self) -> dict[str, Any]:
        if not self.metadata_path.exists():
            return {}
        try:
            return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}

    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.metadata_path.with_suffix(
            self.metadata_path.suffix + ".tmp"
        )
        temporary_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self.metadata_path)
