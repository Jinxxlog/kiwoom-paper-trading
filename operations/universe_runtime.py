import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import BASE_DIR
from data.scheduler import ProcessLock, is_process_running


class UniverseCollectionRuntime:
    def __init__(self, database_path: Path) -> None:
        base = Path(database_path)
        self.lock_path = base.with_suffix(".universe.lock")
        self.stop_path = base.with_suffix(".universe.stop")
        self.metadata_path = base.with_suffix(".universe.json")
        self.log_path = base.with_suffix(".universe.log")

    def status(self) -> dict[str, Any]:
        lock = ProcessLock(self.lock_path).inspect()
        metadata = self._load_metadata()
        pid = metadata.get("pid")
        starting = isinstance(pid, int) and is_process_running(pid) and not lock.exists
        return {
            "running": lock.active,
            "starting": starting,
            "pid": lock.pid or (pid if starting else None),
            "stop_requested": self.stop_path.exists(),
            "metadata": metadata,
            "log_path": str(self.log_path),
        }

    def start(
        self,
        *,
        target_bars: int = 200,
        pages: int = 1,
        delay_seconds: float = 0.5,
    ) -> dict[str, Any]:
        status = self.status()
        if status["running"] or status["starting"]:
            raise RuntimeError("전 종목 일봉 수집이 이미 실행 중입니다.")
        if target_bars < 60:
            raise ValueError("일봉 목표 개수는 60 이상이어야 합니다.")
        self.stop_path.unlink(missing_ok=True)
        command = [
            sys.executable,
            str(BASE_DIR / "main.py"),
            "universe",
            "complete",
            "--sync-first",
            "--target-bars",
            str(target_bars),
            "--pages",
            str(pages),
            "--batch-size",
            "10",
            "--delay",
            str(delay_seconds),
            "--round-delay",
            "10",
        ]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = self.log_path.open("a", encoding="utf-8")
        creation_flags = 0
        if os.name == "nt":
            creation_flags = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        try:
            process = subprocess.Popen(
                command,
                cwd=BASE_DIR,
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
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
            "target_bars": target_bars,
            "pages": pages,
            "delay_seconds": delay_seconds,
            "command": command,
        }
        self._write_metadata(metadata)
        return metadata

    def request_stop(self) -> bool:
        status = self.status()
        if not status["running"] and not status["starting"]:
            self.stop_path.unlink(missing_ok=True)
            return False
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
        temporary = self.metadata_path.with_suffix(
            self.metadata_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.metadata_path)
