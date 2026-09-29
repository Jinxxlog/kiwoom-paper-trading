from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional


class ApiCallPriority(Enum):
    LOW = 20
    NORMAL = 50
    HIGH = 80
    CRITICAL = 100


@dataclass(frozen=True)
class ApiBudgetPolicy:
    api_group: str
    limit_count: int
    window_seconds: int = 60
    high_priority_reserve: int = 0


@dataclass(frozen=True)
class ApiBudgetDecision:
    allowed: bool
    api_group: str
    api_id: str
    endpoint: str
    stock_code: Optional[str]
    priority: ApiCallPriority
    consumed: bool
    blocked: bool
    reason: str
    limit_count: int
    used_count: int
    remaining_count: int
    window_started_at: str
    backoff_seconds: int = 0


@dataclass(frozen=True)
class ApiBackoffState:
    api_group: str
    backoff_until: Optional[str]
    backoff_seconds: int
    reason: str


class ApiBudgetBlockedError(RuntimeError):
    pass


DEFAULT_API_BUDGETS: dict[str, ApiBudgetPolicy] = {
    "token_auth": ApiBudgetPolicy("token_auth", limit_count=20, high_priority_reserve=3),
    "current_price": ApiBudgetPolicy("current_price", limit_count=600, high_priority_reserve=60),
    "daily_chart": ApiBudgetPolicy("daily_chart", limit_count=120, high_priority_reserve=0),
    "market_index": ApiBudgetPolicy("market_index", limit_count=60, high_priority_reserve=0),
    "minute_chart": ApiBudgetPolicy("minute_chart", limit_count=300, high_priority_reserve=40),
    "order_book": ApiBudgetPolicy("order_book", limit_count=120, high_priority_reserve=40),
    "trade_strength": ApiBudgetPolicy("trade_strength", limit_count=120, high_priority_reserve=40),
    "market_rank": ApiBudgetPolicy("market_rank", limit_count=80, high_priority_reserve=20),
    "account": ApiBudgetPolicy("account", limit_count=120, high_priority_reserve=20),
    "supply_flow": ApiBudgetPolicy("supply_flow", limit_count=180, high_priority_reserve=30),
    "default": ApiBudgetPolicy("default", limit_count=300, high_priority_reserve=30),
}


API_GROUP_BY_ID: dict[str, str] = {
    "au10001": "token_auth",
    "ka10006": "current_price",
    "ka10081": "daily_chart",
    "ka10080": "minute_chart",
    "ka20006": "market_index",
    "ka10004": "order_book",
    "ka10003": "trade_strength",
    "ka00198": "market_rank",
    "ka10027": "market_rank",
    "ka10029": "market_rank",
    "ka10032": "market_rank",
    "ka00001": "account",
    "kt00001": "account",
    "kt00005": "account",
    "kt00007": "account",
    "kt00018": "account",
    "ka10037": "supply_flow",
    "ka10040": "supply_flow",
    "ka10060": "supply_flow",
    "ka10061": "supply_flow",
    "ka10064": "supply_flow",
    "ka90013": "supply_flow",
    "ka10065": "supply_flow",
}


def normalize_priority(priority: ApiCallPriority | str | int) -> ApiCallPriority:
    if isinstance(priority, ApiCallPriority):
        return priority
    if isinstance(priority, str):
        key = priority.upper()
        if key in ApiCallPriority.__members__:
            return ApiCallPriority[key]
        try:
            priority = int(priority)
        except ValueError as exc:
            raise ValueError(f"알 수 없는 API 우선순위입니다: {priority}") from exc
    for candidate in sorted(ApiCallPriority, key=lambda item: item.value):
        if int(priority) <= candidate.value:
            return candidate
    return ApiCallPriority.CRITICAL


class ApiBudgetManager:
    def __init__(
        self,
        database: Any,
        *,
        policies: Optional[dict[str, ApiBudgetPolicy]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.database = database
        self.policies = policies or DEFAULT_API_BUDGETS
        self.clock = clock or datetime.now

    def api_group_for(self, api_id: str, endpoint: str = "") -> str:
        if api_id in API_GROUP_BY_ID:
            return API_GROUP_BY_ID[api_id]
        if endpoint.endswith("/oauth2/token"):
            return "token_auth"
        if "/acnt" in endpoint:
            return "account"
        if "/rkinfo" in endpoint:
            return "market_rank"
        return "default"

    def before_call(
        self,
        *,
        api_id: str,
        endpoint: str,
        stock_code: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiBudgetDecision:
        normalized_priority = normalize_priority(priority)
        api_group = self.api_group_for(api_id, endpoint)
        policy = self.policies.get(api_group, self.policies["default"])
        now = self.clock()
        window = self._current_window(api_group, api_id, endpoint, policy, now)
        backoff_until = _parse_datetime(window.get("backoff_until"))
        if backoff_until is not None and now < backoff_until:
            backoff_seconds = max(1, int((backoff_until - now).total_seconds()))
            decision = self._decision(
                allowed=False,
                api_group=api_group,
                api_id=api_id,
                endpoint=endpoint,
                stock_code=stock_code,
                priority=normalized_priority,
                consumed=False,
                blocked=True,
                reason="blocked_by_backoff",
                policy=policy,
                used_count=int(window["used_count"]),
                window_started_at=str(window["window_started_at"]),
                backoff_seconds=backoff_seconds,
            )
            self.database.save_api_budget_event(decision=decision, status_code=None)
            return decision

        used_count = int(window["used_count"])
        remaining_count = max(0, policy.limit_count - used_count)
        if remaining_count <= 0:
            decision = self._decision(
                allowed=False,
                api_group=api_group,
                api_id=api_id,
                endpoint=endpoint,
                stock_code=stock_code,
                priority=normalized_priority,
                consumed=False,
                blocked=True,
                reason="skipped_by_budget",
                policy=policy,
                used_count=used_count,
                window_started_at=str(window["window_started_at"]),
            )
            self.database.save_api_budget_event(decision=decision, status_code=None)
            return decision

        reserve = max(0, policy.high_priority_reserve)
        if (
            reserve > 0
            and remaining_count <= reserve
            and normalized_priority.value < ApiCallPriority.HIGH.value
        ):
            decision = self._decision(
                allowed=False,
                api_group=api_group,
                api_id=api_id,
                endpoint=endpoint,
                stock_code=stock_code,
                priority=normalized_priority,
                consumed=False,
                blocked=True,
                reason="skipped_by_budget_reserved",
                policy=policy,
                used_count=used_count,
                window_started_at=str(window["window_started_at"]),
            )
            self.database.save_api_budget_event(decision=decision, status_code=None)
            return decision

        used_count += 1
        self.database.upsert_api_budget_window(
            api_group=api_group,
            api_id=api_id,
            endpoint=endpoint,
            window_started_at=str(window["window_started_at"]),
            window_seconds=policy.window_seconds,
            limit_count=policy.limit_count,
            used_count=used_count,
            backoff_until=window.get("backoff_until"),
            updated_at=now.isoformat(timespec="seconds"),
        )
        return self._decision(
            allowed=True,
            api_group=api_group,
            api_id=api_id,
            endpoint=endpoint,
            stock_code=stock_code,
            priority=normalized_priority,
            consumed=True,
            blocked=False,
            reason="allowed",
            policy=policy,
            used_count=used_count,
            window_started_at=str(window["window_started_at"]),
        )

    def after_call(
        self,
        decision: ApiBudgetDecision,
        *,
        status_code: Optional[int],
        reason: Optional[str] = None,
    ) -> Optional[ApiBackoffState]:
        backoff_state = None
        event_decision = decision
        if status_code == 429:
            backoff_seconds = self._next_backoff_seconds(decision.api_group)
            backoff_until = self.clock() + timedelta(seconds=backoff_seconds)
            self.database.set_api_budget_backoff(
                api_group=decision.api_group,
                api_id=decision.api_id,
                endpoint=decision.endpoint,
                backoff_until=backoff_until.isoformat(timespec="seconds"),
                updated_at=self.clock().isoformat(timespec="seconds"),
            )
            event_decision = self._replace_reason(
                decision,
                reason or "http_429_backoff",
                backoff_seconds=backoff_seconds,
            )
            backoff_state = ApiBackoffState(
                api_group=decision.api_group,
                backoff_until=backoff_until.isoformat(timespec="seconds"),
                backoff_seconds=backoff_seconds,
                reason=event_decision.reason,
            )
        elif reason is not None:
            event_decision = self._replace_reason(decision, reason)
        self.database.save_api_budget_event(
            decision=event_decision,
            status_code=status_code,
        )
        return backoff_state

    def _current_window(
        self,
        api_group: str,
        api_id: str,
        endpoint: str,
        policy: ApiBudgetPolicy,
        now: datetime,
    ) -> dict[str, Any]:
        window = self.database.get_api_budget_window(api_group)
        if window is not None:
            started_at = _parse_datetime(window["window_started_at"])
            if (
                started_at is not None
                and now < started_at + timedelta(seconds=int(window["window_seconds"]))
            ):
                return dict(window)
        started = now.isoformat(timespec="seconds")
        self.database.upsert_api_budget_window(
            api_group=api_group,
            api_id=api_id,
            endpoint=endpoint,
            window_started_at=started,
            window_seconds=policy.window_seconds,
            limit_count=policy.limit_count,
            used_count=0,
            backoff_until=None,
            updated_at=started,
        )
        return {
            "api_group": api_group,
            "api_id": api_id,
            "endpoint": endpoint,
            "window_started_at": started,
            "window_seconds": policy.window_seconds,
            "limit_count": policy.limit_count,
            "used_count": 0,
            "backoff_until": None,
        }

    def _next_backoff_seconds(self, api_group: str) -> int:
        window = self.database.get_api_budget_window(api_group)
        if window is None:
            return 30
        row = dict(window)
        backoff_until = _parse_datetime(row.get("backoff_until"))
        now = self.clock()
        if backoff_until is None or backoff_until <= now:
            return 30
        remaining = int((backoff_until - now).total_seconds())
        return min(300, max(30, remaining * 2))

    @staticmethod
    def _decision(
        *,
        allowed: bool,
        api_group: str,
        api_id: str,
        endpoint: str,
        stock_code: Optional[str],
        priority: ApiCallPriority,
        consumed: bool,
        blocked: bool,
        reason: str,
        policy: ApiBudgetPolicy,
        used_count: int,
        window_started_at: str,
        backoff_seconds: int = 0,
    ) -> ApiBudgetDecision:
        return ApiBudgetDecision(
            allowed=allowed,
            api_group=api_group,
            api_id=api_id,
            endpoint=endpoint,
            stock_code=stock_code,
            priority=priority,
            consumed=consumed,
            blocked=blocked,
            reason=reason,
            limit_count=policy.limit_count,
            used_count=used_count,
            remaining_count=max(0, policy.limit_count - used_count),
            window_started_at=window_started_at,
            backoff_seconds=backoff_seconds,
        )

    @staticmethod
    def _replace_reason(
        decision: ApiBudgetDecision,
        reason: str,
        *,
        backoff_seconds: int = 0,
    ) -> ApiBudgetDecision:
        return ApiBudgetDecision(
            allowed=decision.allowed,
            api_group=decision.api_group,
            api_id=decision.api_id,
            endpoint=decision.endpoint,
            stock_code=decision.stock_code,
            priority=decision.priority,
            consumed=decision.consumed,
            blocked=decision.blocked,
            reason=reason,
            limit_count=decision.limit_count,
            used_count=decision.used_count,
            remaining_count=decision.remaining_count,
            window_started_at=decision.window_started_at,
            backoff_seconds=backoff_seconds,
        )


def stock_code_from_body(body: Optional[dict[str, Any]]) -> Optional[str]:
    if not body:
        return None
    value = body.get("stk_cd") or body.get("stock_code")
    if value is None:
        return None
    return str(value)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
