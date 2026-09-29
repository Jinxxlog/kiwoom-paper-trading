from dataclasses import dataclass
from datetime import datetime
from typing import Any

from db.database import Database
from domain.models import OrderRequest

LIVE_EXECUTION_MODES = {"live", "real", "production"}
LIVE_INITIAL_BUY_MAX_QUANTITY = 1
LIVE_MINIMUM_BUY_AMOUNT = 5_000
LIVE_DAILY_BUY_LIMIT = 3
LIVE_SAME_STOCK_BUY_LIMIT = 1
LIVE_CONSECUTIVE_FAILURE_LIMIT = 3
LIVE_REQUIRED_FRESHNESS_KEYS = ("leader", "supply", "chart", "market_mode")


@dataclass(frozen=True)
class OrderSafetyCheckResult:
    passed: bool
    reasons: tuple[str, ...]
    context: dict[str, Any]

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)


class OrderSafetyGuard:
    def __init__(self, database: Database) -> None:
        self.database = database

    def verify_before_submit(
        self,
        request: OrderRequest,
        *,
        execution_mode: str,
        current_price: int | None,
        expected_price: int | None,
        order_book_pass: bool,
        leader_mode: dict[str, Any] | None = None,
        supply_score: dict[str, Any] | None = None,
        allow_supply_blocked_paper: bool = False,
        chart_blocked: bool = False,
        market_mode_allowed: bool = True,
        quote_collected_at: datetime | str | None = None,
        max_quote_age_seconds: int = 180,
        freshness_checks: dict[str, bool] | None = None,
        record_failure: bool = True,
    ) -> OrderSafetyCheckResult:
        reasons: list[str] = []
        normalized_execution_mode = execution_mode.strip().lower()
        live_minimum_size_mode = normalized_execution_mode in LIVE_EXECUTION_MODES
        live_buy_mode = live_minimum_size_mode and request.side.value == "buy"
        context = {
            "requested_execution_mode": execution_mode,
            "current_price": current_price,
            "expected_price": expected_price,
            "order_book_pass": order_book_pass,
            "leader_mode": leader_mode or {},
            "supply_score": supply_score or {},
            "allow_supply_blocked_paper": allow_supply_blocked_paper,
            "chart_blocked": chart_blocked,
            "market_mode_allowed": market_mode_allowed,
            "quantity": request.quantity,
            "side": request.side.value,
            "quote_collected_at": (
                quote_collected_at.isoformat(timespec="seconds")
                if isinstance(quote_collected_at, datetime)
                else quote_collected_at
            ),
            "max_quote_age_seconds": max_quote_age_seconds,
            "freshness_checks": freshness_checks or {},
        }
        context["live_minimum_size_mode"] = live_minimum_size_mode
        if current_price is None or current_price <= 0:
            reasons.append("주문 직전 현재가 없음")
        if expected_price is None or expected_price <= 0:
            reasons.append("전략 기준가 없음")
        if current_price and expected_price:
            drift = abs(current_price - expected_price) / expected_price
            context["price_drift_rate"] = drift
            if drift > 0.03:
                reasons.append("주문 직전 가격 괴리 3% 초과")
        if request.quantity < 1:
            reasons.append("주문 수량 1주 미만")
        if (
            live_buy_mode
            and request.quantity > LIVE_INITIAL_BUY_MAX_QUANTITY
        ):
            reasons.append("live initial buy quantity limited to 1 share")
        if live_buy_mode and current_price is not None and current_price > 0:
            order_amount = current_price * request.quantity
            context["order_amount"] = order_amount
            context["live_minimum_order_amount"] = LIVE_MINIMUM_BUY_AMOUNT
            if order_amount < LIVE_MINIMUM_BUY_AMOUNT:
                reasons.append(
                    f"live minimum order amount {LIVE_MINIMUM_BUY_AMOUNT:,}원 미만"
                )
        if live_buy_mode:
            quote_time = _parse_datetime(quote_collected_at)
            if quote_time is None:
                reasons.append("실전 주문 현재가 최신성 미확인")
            else:
                quote_age_seconds = max(
                    0,
                    int((datetime.now() - quote_time).total_seconds()),
                )
                context["quote_age_seconds"] = quote_age_seconds
                if quote_age_seconds > max_quote_age_seconds:
                    reasons.append("실전 주문 현재가 오래됨")
            checks = freshness_checks or {}
            for key in LIVE_REQUIRED_FRESHNESS_KEYS:
                if checks.get(key) is not True:
                    reasons.append(f"실전 주문 {key} 최신성 미확인")
        if not order_book_pass:
            reasons.append("주문 직전 호가·체결 조건 미충족")
        if leader_mode and not leader_mode.get("entry_allowed", True):
            reasons.append(
                f"장중 전략 모드 차단: {leader_mode.get('label') or leader_mode.get('name')}"
            )
        if supply_score and supply_score.get("blocked") and not (
            allow_supply_blocked_paper and not live_buy_mode
        ):
            reasons.append(
                f"수급 차단: {supply_score.get('blocked_reason') or '-'}"
            )
        if chart_blocked:
            reasons.append("차트 위험 패턴 차단")
        if not market_mode_allowed:
            reasons.append("시장 모드 신규 진입 차단")
        if live_buy_mode:
            state = self.database.live_order_safety_state(
                account_name=request.account_name,
                stock_code=request.stock_code,
                client_order_id=request.client_order_id,
                failure_window=LIVE_CONSECUTIVE_FAILURE_LIMIT,
            )
            context["live_order_state"] = state
            context["live_daily_buy_limit"] = LIVE_DAILY_BUY_LIMIT
            context["live_same_stock_buy_limit"] = LIVE_SAME_STOCK_BUY_LIMIT
            context["live_consecutive_failure_limit"] = (
                LIVE_CONSECUTIVE_FAILURE_LIMIT
            )
            if state["duplicate_request_count"] > 0:
                reasons.append("live duplicate client_order_id")
            if state["pending_live_same_stock_count"] > 0:
                reasons.append("live same-stock pending order exists")
            if state["open_real_order_count"] > 0:
                reasons.append("live broker open/unfilled order exists")
            if state["daily_live_buy_count"] >= LIVE_DAILY_BUY_LIMIT:
                reasons.append("live daily buy order limit reached")
            if state["same_stock_live_buy_count"] >= LIVE_SAME_STOCK_BUY_LIMIT:
                reasons.append("live same-stock reentry limit reached")
            if (
                state["consecutive_live_failure_count"]
                >= LIVE_CONSECUTIVE_FAILURE_LIMIT
            ):
                reasons.append("live consecutive safety failure limit reached")

        result = OrderSafetyCheckResult(
            passed=not reasons,
            reasons=tuple(reasons),
            context=context,
        )
        if not result.passed and record_failure:
            self.database.save_order_safety_event(
                decision_id=request.decision_id,
                account_name=request.account_name,
                execution_mode=(
                    "live" if live_minimum_size_mode else normalized_execution_mode
                ),
                stock_code=request.stock_code,
                event_type="pre_order_recheck_failed",
                severity="warning",
                passed=False,
                reason=result.reason_text,
                context=context,
            )
        return result


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed
