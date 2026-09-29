import logging
import os
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from data.api_budget import ApiBudgetBlockedError
from data.collector import MarketDataCollector


SEOUL_TIMEZONE = timezone(timedelta(hours=9), name="Asia/Seoul")
MARKET_OPEN = clock_time(9, 0)
MARKET_CLOSE = clock_time(15, 30)
PREOPEN_SCAN_START = clock_time(8, 50)


def is_market_open(now: Optional[datetime] = None) -> bool:
    current = now or datetime.now(SEOUL_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SEOUL_TIMEZONE)
    else:
        current = current.astimezone(SEOUL_TIMEZONE)
    local_time = current.time().replace(tzinfo=None)
    return current.weekday() < 5 and MARKET_OPEN <= local_time <= MARKET_CLOSE


def is_preopen_scan_time(now: Optional[datetime] = None) -> bool:
    current = now or datetime.now(SEOUL_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SEOUL_TIMEZONE)
    else:
        current = current.astimezone(SEOUL_TIMEZONE)
    local_time = current.time().replace(tzinfo=None)
    return current.weekday() < 5 and PREOPEN_SCAN_START <= local_time < MARKET_OPEN


class ProcessLock(AbstractContextManager["ProcessLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._acquired = False
        self.recovered_stale = False

    def inspect(self) -> "ProcessLockStatus":
        if not self.path.exists():
            return ProcessLockStatus(False, None, False, False)
        try:
            content = self.path.read_text(encoding="ascii").strip()
            pid = int(content)
        except (OSError, ValueError):
            return ProcessLockStatus(True, None, False, True)
        active = _is_process_running(pid)
        return ProcessLockStatus(True, pid, active, not active)

    def recover_stale(self) -> bool:
        if not self.path.exists():
            return False
        try:
            original_content = self.path.read_text(encoding="ascii")
        except OSError:
            return False
        status = self.inspect()
        if not status.stale:
            return False
        try:
            if self.path.read_text(encoding="ascii") != original_content:
                return False
            self.path.unlink()
        except FileNotFoundError:
            return False
        self.recovered_stale = True
        return True

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = None
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                break
            except FileExistsError as exc:
                status = self.inspect()
                if status.stale and self.recover_stale():
                    continue
                owner = f" PID={status.pid}" if status.pid is not None else ""
                raise RuntimeError(
                    f"수집기가 이미 실행 중입니다:{owner} 잠금={self.path}"
                ) from exc
        if descriptor is None:
            raise RuntimeError(f"잠금 파일을 생성할 수 없습니다: {self.path}")
        with os.fdopen(descriptor, "w", encoding="ascii") as lock_file:
            lock_file.write(str(os.getpid()))
        self._acquired = True
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False


@dataclass(frozen=True)
class ProcessLockStatus:
    exists: bool
    pid: Optional[int]
    active: bool
    stale: bool


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _is_windows_process_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_process_running(pid: int) -> bool:
    return _is_process_running(pid)


def _is_windows_process_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        # Access denied means the process exists but cannot be inspected.
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class PeriodicCollector:
    def __init__(
        self,
        collector: MarketDataCollector,
        stock_code_provider: Callable[[], Iterable[str]],
        after_collection: Optional[Callable[[], object]] = None,
        event_callback: Optional[
            Callable[[str, dict[str, object]], None]
        ] = None,
        current_price_priority_provider: Optional[
            Callable[[str], object]
        ] = None,
        market_open_check: Callable[[], bool] = is_market_open,
        pre_market_callback: Optional[Callable[[], object]] = None,
        pre_market_check: Callable[[], bool] = is_preopen_scan_time,
        current_price_call_spacing_seconds: float = 0,
    ) -> None:
        self.collector = collector
        self.stock_code_provider = stock_code_provider
        self.after_collection = after_collection
        self.event_callback = event_callback
        self.current_price_priority_provider = current_price_priority_provider
        self.market_open_check = market_open_check
        self.pre_market_callback = pre_market_callback
        self.pre_market_check = pre_market_check
        self.current_price_call_spacing_seconds = max(
            0.0,
            float(current_price_call_spacing_seconds),
        )

    def _emit(self, event_type: str, **payload: object) -> None:
        if self.event_callback is not None:
            try:
                self.event_callback(event_type, payload)
            except Exception:
                logging.exception("운영 이벤트 기록 실패: %s", event_type)

    def _control_requested(
        self,
        callback: Optional[Callable[[], bool]],
        *,
        label: str,
    ) -> bool:
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception as exc:
            logging.exception("%s 확인 실패; 수집을 계속합니다.", label)
            self._emit(
                "runtime_control_check_failed",
                message=f"{label} 확인 실패: {exc}",
            )
            return False

    def collect_once(self) -> tuple[int, int]:
        success_count = 0
        failure_count = 0
        stock_codes = list(
            dict.fromkeys(str(stock_code) for stock_code in self.stock_code_provider())
        )
        self._emit("cycle_started", stock_count=len(stock_codes))
        if not stock_codes:
            logging.warning("활성화된 관심 종목이 없습니다.")
            self._emit(
                "cycle_completed",
                success_count=0,
                failure_count=0,
            )
            return success_count, failure_count

        last_call_at: Optional[float] = None
        for stock_code in stock_codes:
            try:
                if (
                    last_call_at is not None
                    and self.current_price_call_spacing_seconds > 0
                ):
                    remaining = (
                        self.current_price_call_spacing_seconds
                        - (time.monotonic() - last_call_at)
                    )
                    if remaining > 0:
                        time.sleep(remaining)
                if self.current_price_priority_provider is None:
                    result = self.collector.collect_current_price(stock_code)
                else:
                    try:
                        result = self.collector.collect_current_price(
                            stock_code,
                            priority=self.current_price_priority_provider(stock_code),
                        )
                    except TypeError as exc:
                        if "priority" not in str(exc):
                            raise
                        result = self.collector.collect_current_price(stock_code)
                if self.current_price_call_spacing_seconds > 0:
                    last_call_at = time.monotonic()
                logging.info(
                    "%s 저장 완료: 현재가=%s 거래량=%s DB ID=%s",
                    result.stock_code,
                    result.current_price,
                    result.volume,
                    result.database_id,
                )
                success_count += 1
                self._emit(
                    "stock_collected",
                    stock_code=stock_code,
                    message=f"{stock_code} 현재가 수집 성공",
                    database_id=result.database_id,
                )
            except ApiBudgetBlockedError as exc:
                logging.info("%s 현재가 수집 생략: %s", stock_code, exc)
                self._emit(
                    "stock_skipped",
                    stock_code=stock_code,
                    message=str(exc),
                    reason="api_budget",
                )
            except Exception as exc:
                if self.current_price_call_spacing_seconds > 0:
                    last_call_at = time.monotonic()
                logging.exception("%s 수집 실패", stock_code)
                failure_count += 1
                self._emit(
                    "stock_failed",
                    stock_code=stock_code,
                    message=str(exc),
                )
        if self.after_collection is not None:
            try:
                self.after_collection()
                self._emit(
                    "after_collection_succeeded",
                    message="수집 후 자동화 성공",
                )
            except Exception as exc:
                logging.exception("수집 후 리스크 평가 실패")
                failure_count += 1
                self._emit(
                    "after_collection_failed",
                    message=str(exc),
                )
        self._emit(
            "cycle_completed",
            success_count=success_count,
            failure_count=failure_count,
        )
        return success_count, failure_count

    def run(
        self,
        interval_seconds: int,
        *,
        market_hours_only: bool,
        sleep: Callable[[float], None] = time.sleep,
        stop_requested: Optional[Callable[[], bool]] = None,
        auto_stop_requested: Optional[Callable[[], bool]] = None,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("수집 간격은 1초 이상이어야 합니다.")

        logging.info(
            "주기 수집 시작: 간격=%s초, 장중 전용=%s",
            interval_seconds,
            market_hours_only,
        )
        pre_market_attempted_date = None
        while True:
            if self._control_requested(
                auto_stop_requested,
                label="예약 자동 종료",
            ):
                logging.info("예약된 자동 종료 시각에 도달했습니다.")
                return
            if self._control_requested(stop_requested, label="정상 종료 요청"):
                logging.info("정상 종료 요청을 받아 주기 수집을 종료합니다.")
                return
            if (
                market_hours_only
                and self.pre_market_callback is not None
                and self.pre_market_check()
            ):
                current_date = datetime.now(SEOUL_TIMEZONE).date()
                if pre_market_attempted_date != current_date:
                    pre_market_attempted_date = current_date
                    try:
                        self.pre_market_callback()
                        self._emit(
                            "pre_market_succeeded",
                            message="장전 후보 스캔 성공",
                        )
                    except Exception as exc:
                        logging.exception("장전 후보 스캔 실패")
                        self._emit(
                            "pre_market_failed",
                            message=str(exc),
                        )
            if not market_hours_only or self.market_open_check():
                self.collect_once()
            else:
                logging.info("정규장 운영 시간이 아니므로 이번 수집을 건너뜁니다.")
            if stop_requested is None:
                sleep(interval_seconds)
                continue
            deadline = time.monotonic() + interval_seconds
            while time.monotonic() < deadline:
                if self._control_requested(
                    auto_stop_requested,
                    label="예약 자동 종료",
                ):
                    logging.info("예약된 자동 종료 시각에 도달했습니다.")
                    return
                if self._control_requested(stop_requested, label="정상 종료 요청"):
                    logging.info("정상 종료 요청을 받아 주기 수집을 종료합니다.")
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sleep(min(0.5, remaining))
