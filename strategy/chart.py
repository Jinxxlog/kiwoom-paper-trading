from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Sequence


@dataclass(frozen=True)
class ChartTradeDecision:
    stock_code: str
    evaluated_at: str
    daily_setup_name: str
    daily_setup_score: Decimal
    intraday_trigger_name: str
    intraday_trigger_state: str
    intraday_trigger_score: Decimal
    risk_score: Decimal
    total_score: Decimal
    force_entry: bool
    blocked: bool
    block_reason: str | None
    entry_type: str
    entry_tags: tuple[str, ...]
    entry_price: int
    invalidation_price: int | None
    first_take_profit_price: int | None
    second_take_profit_price: int | None
    reasons: tuple[str, ...]
    risk_events: tuple[str, ...]
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "daily_setup_score",
            "intraday_trigger_score",
            "risk_score",
            "total_score",
        ):
            value[key] = str(value[key])
        value["entry_tags"] = list(self.entry_tags)
        value["reasons"] = list(self.reasons)
        value["risk_events"] = list(self.risk_events)
        return value


def evaluate_chart_trade(
    *,
    stock_code: str,
    evaluated_at: str,
    current_price: int,
    features: dict[str, Any],
    minute_rows: Sequence[Any],
    intraday_pattern: dict[str, Any] | None,
    vwap: Decimal | None,
    intraday_high: int | None,
    breakout_level: int | None,
    previous_high: int | None,
    order_flow_pass: bool,
    trade_strength: Decimal | None,
    information_score: Decimal,
) -> ChartTradeDecision:
    daily_name, daily_score, daily_reasons = _daily_setup(features)
    trigger_name, trigger_state, trigger_score, trigger_reasons = _intraday_trigger(
        current_price=current_price,
        minute_rows=minute_rows,
        intraday_pattern=intraday_pattern,
        vwap=vwap,
        intraday_high=intraday_high,
        breakout_level=breakout_level,
        previous_high=previous_high,
        order_flow_pass=order_flow_pass,
        trade_strength=trade_strength,
    )
    risk_score, risk_events, block_reason = _risk_patterns(
        current_price=current_price,
        features=features,
        minute_rows=minute_rows,
        vwap=vwap,
        information_score=information_score,
    )
    total = daily_score + trigger_score + risk_score
    blocked = block_reason is not None
    force_entry = not blocked and (
        (
            daily_score >= Decimal("35")
            and trigger_score >= Decimal("30")
            and total >= Decimal("65")
        )
        or (
            daily_score >= Decimal("42")
            and trigger_score >= Decimal("22")
            and total >= Decimal("62")
        )
        or (
            daily_score >= Decimal("30")
            and trigger_score >= Decimal("38")
            and total >= Decimal("62")
        )
    )
    entry_type = _entry_type(daily_name, trigger_name)
    invalidation = _invalidation_price(
        current_price=current_price,
        minute_rows=minute_rows,
        vwap=vwap,
        breakout_level=breakout_level,
        intraday_pattern=intraday_pattern,
    )
    risk_per_share = (
        current_price - invalidation
        if invalidation is not None and invalidation < current_price
        else max(1, int(current_price * 0.025))
    )
    first_target = current_price + risk_per_share
    second_target = current_price + risk_per_share * 2
    tags = tuple(
        dict.fromkeys(
            item
            for item in (
                "chart",
                daily_name,
                trigger_name,
                "forced_entry" if force_entry else "",
                "risk_blocked" if blocked else "",
            )
            if item
        )
    )
    details = {
        "daily": {"name": daily_name, "score": str(daily_score)},
        "trigger": {
            "name": trigger_name,
            "state": trigger_state,
            "score": str(trigger_score),
        },
        "risk": {"score": str(risk_score), "events": list(risk_events)},
        "features": features,
        "intraday_pattern": intraday_pattern,
    }
    return ChartTradeDecision(
        stock_code=stock_code,
        evaluated_at=evaluated_at,
        daily_setup_name=daily_name,
        daily_setup_score=_clamp_decimal(daily_score),
        intraday_trigger_name=trigger_name,
        intraday_trigger_state=trigger_state,
        intraday_trigger_score=_clamp_decimal(trigger_score),
        risk_score=_clamp_decimal(risk_score),
        total_score=_clamp_decimal(total),
        force_entry=force_entry,
        blocked=blocked,
        block_reason=block_reason,
        entry_type=entry_type,
        entry_tags=tags,
        entry_price=current_price,
        invalidation_price=invalidation,
        first_take_profit_price=first_target,
        second_take_profit_price=second_target,
        reasons=tuple((*daily_reasons, *trigger_reasons)),
        risk_events=tuple(risk_events),
        details=details,
    )


def _daily_setup(features: dict[str, Any]) -> tuple[str, Decimal, list[str]]:
    candidates: list[tuple[str, Decimal, str]] = []
    if features.get("new_60d_high"):
        candidates.append(("daily_new_high", Decimal("35"), "일봉 60일 신고가 돌파"))
    if features.get("recent5_high_breakout"):
        candidates.append(("daily_5d_high_breakout", Decimal("28"), "5일 고점 돌파"))
    if _float(features.get("return20")) > 0 and features.get("recent5_high_breakout"):
        candidates.append(("daily_20d_momentum", Decimal("24"), "20일 상승 흐름에서 5일 고점 돌파"))
    if features.get("resistance_breakout"):
        candidates.append(("daily_resistance_breakout", Decimal("34"), "주요 저항선 돌파"))
    if features.get("box_breakout_state") in {"confirmed", "breakout_pending"}:
        candidates.append(("daily_box_breakout", Decimal("36"), "박스권 상단 돌파"))
    if features.get("cup_handle_state") in {"confirmed", "breakout_pending", "ready"}:
        candidates.append(("daily_cup_handle", Decimal("38"), "컵앤핸들 셋업"))
    if features.get("ascending_triangle_state") in {"confirmed", "breakout_pending", "ready"}:
        candidates.append(("daily_ascending_triangle", Decimal("36"), "상승 삼각형 셋업"))
    if features.get("big_bullish_candle") and features.get("relative_trade_amount20"):
        candidates.append(("daily_big_candle_pullback", Decimal("26"), "장대양봉 이후 눌림 후보"))
    if _float(features.get("relative_trade_amount20")) >= 2.5 and _float(features.get("return20")) > 0:
        candidates.append(("daily_amount_trend_turn", Decimal("30"), "거래대금 동반 추세 전환"))
    if not candidates:
        return "daily_unconfirmed", Decimal("0"), ["강한 일봉 셋업 없음"]
    name, score, reason = max(candidates, key=lambda item: item[1])
    reasons = [reason]
    if _float(features.get("relative_trade_amount20")) >= 2:
        score += Decimal("8")
        reasons.append("20일 평균 대비 거래대금 2배 이상")
    if features.get("near_resistance") and not features.get("resistance_breakout"):
        score -= Decimal("8")
        reasons.append("저항 근처, 돌파 확인 부족")
    return name, min(Decimal("50"), score), reasons


def _intraday_trigger(
    *,
    current_price: int,
    minute_rows: Sequence[Any],
    intraday_pattern: dict[str, Any] | None,
    vwap: Decimal | None,
    intraday_high: int | None,
    breakout_level: int | None,
    previous_high: int | None,
    order_flow_pass: bool,
    trade_strength: Decimal | None,
) -> tuple[str, str, Decimal, list[str]]:
    rows = list(minute_rows)
    latest = rows[0] if rows else None
    triggers: list[tuple[str, str, Decimal, str]] = []
    if latest is not None and _is_long_bullish_bar(latest, rows[1:6]):
        triggers.append(("intraday_long_bullish", "confirmed", Decimal("30"), "분봉 장대양봉 발생"))
    if intraday_pattern:
        if intraday_pattern.get("reclaim_state") == "confirmed":
            triggers.append(("intraday_vwap_reclaim", "confirmed", Decimal("38"), "VWAP 눌림 후 재돌파"))
        if intraday_pattern.get("bounce_state") == "confirmed":
            triggers.append(("intraday_vwap_bounce", "confirmed", Decimal("34"), "VWAP 지지 후 반등"))
        if (
            intraday_pattern.get("order_block_state") == "confirmed"
            and intraday_pattern.get("order_block_side") == "bullish"
        ):
            triggers.append(("intraday_order_block_reentry", "confirmed", Decimal("40"), "상승 오더블럭 재진입"))
    if breakout_level is not None and current_price >= breakout_level:
        triggers.append(("intraday_breakout_hold", "confirmed", Decimal("34"), "전고점 돌파 후 유지"))
    elif previous_high is not None and current_price > previous_high:
        triggers.append(("intraday_previous_high_break", "ready", Decimal("26"), "전일 고점 돌파"))
    if _volume_reentry(rows):
        triggers.append(("intraday_amount_reentry", "confirmed", Decimal("30"), "분봉 거래대금 재유입"))
    if order_flow_pass:
        triggers.append(("intraday_order_flow", "confirmed", Decimal("30"), "호가/체결강도 동반"))
    elif trade_strength is not None and trade_strength >= Decimal("150"):
        triggers.append(("intraday_trade_strength", "ready", Decimal("22"), "체결강도 단독 우위"))
    if not triggers:
        return "intraday_unconfirmed", "none", Decimal("0"), ["강한 분봉 트리거 없음"]
    name, state, score, reason = max(triggers, key=lambda item: item[2])
    reasons = [reason]
    if vwap is not None and current_price > vwap:
        score += Decimal("6")
        reasons.append("현재가 VWAP 상단")
    return name, state, min(Decimal("45"), score), reasons


def _risk_patterns(
    *,
    current_price: int,
    features: dict[str, Any],
    minute_rows: Sequence[Any],
    vwap: Decimal | None,
    information_score: Decimal,
) -> tuple[Decimal, list[str], str | None]:
    rows = list(minute_rows)
    events: list[str] = []
    score = Decimal("0")
    block_reason = None
    if _four_consecutive_bullish(rows):
        events.append("4연속 양봉 추격 차단")
        score -= Decimal("45")
        block_reason = block_reason or "4연속 양봉 추격"
    if _third_wave_or_later(rows):
        events.append("3파동 이후 신규 진입 차단")
        score -= Decimal("35")
        block_reason = block_reason or "3파동 이후 신규 진입"
    if _volume_climax(rows):
        events.append("고점 거래대금 클라이맥스")
        score -= Decimal("40")
        block_reason = block_reason or "고점 거래대금 클라이맥스"
    if information_score > 0 and _float(features.get("return20")) >= 0.18:
        events.append("뉴스 전 선상승 의심")
        score -= Decimal("18")
    if rows and _near_long_bar_high_chase(rows[0], current_price):
        events.append("장대양봉 고점 부근 추격")
        score -= Decimal("35")
        block_reason = block_reason or "장대양봉 고점 추격"
    if vwap is not None and rows and _vwap_recovery_failed(rows, vwap):
        events.append("VWAP 이탈 후 회복 실패")
        score -= Decimal("45")
        block_reason = block_reason or "VWAP 회복 실패"
    if features.get("gap_overheated") or features.get("day_gain_overheated"):
        events.append("일봉 과열")
        score -= Decimal("30")
        block_reason = block_reason or "일봉 과열"
    return score, events, block_reason


def _entry_type(daily_name: str, trigger_name: str) -> str:
    if "order_block" in trigger_name:
        return "chart_order_block_reentry"
    if "vwap" in trigger_name:
        return "chart_vwap_reclaim"
    if "breakout" in trigger_name or "break" in trigger_name:
        return "chart_breakout_hold"
    if "long_bullish" in trigger_name:
        return "chart_long_bullish"
    if "cup_handle" in daily_name:
        return "chart_cup_handle"
    if "ascending_triangle" in daily_name:
        return "chart_ascending_triangle"
    return "chart_pattern_entry"


def _invalidation_price(
    *,
    current_price: int,
    minute_rows: Sequence[Any],
    vwap: Decimal | None,
    breakout_level: int | None,
    intraday_pattern: dict[str, Any] | None,
) -> int | None:
    supports: list[int] = []
    if breakout_level is not None and breakout_level < current_price:
        supports.append(int(breakout_level))
    if vwap is not None and vwap < current_price:
        supports.append(max(1, int(vwap.to_integral_value(rounding=ROUND_FLOOR))))
    if intraday_pattern:
        lower = intraday_pattern.get("order_block_lower")
        if lower is not None and float(lower) < current_price:
            supports.append(max(1, int(float(lower))))
    lows = [
        int(row["low_price"])
        for row in list(minute_rows)[:5]
        if row["low_price"] is not None and int(row["low_price"]) < current_price
    ]
    if lows:
        supports.append(min(lows))
    if not supports:
        return None
    return max(1, min(supports))


def _is_long_bullish_bar(row: Any, previous_rows: Sequence[Any]) -> bool:
    open_price = _float(row["open_price"])
    close = _float(row["close_price"])
    high = _float(row["high_price"])
    low = _float(row["low_price"])
    if open_price <= 0 or close <= open_price or high <= low:
        return False
    body_rate = (close - open_price) / max(high - low, 1)
    range_rate = (high - low) / close
    average_volume = _average(_float(item["volume"]) for item in previous_rows)
    volume = _float(row["volume"])
    volume_ratio = volume / average_volume if average_volume > 0 else 0
    return body_rate >= 0.55 and range_rate >= 0.012 and volume_ratio >= 1.5


def _four_consecutive_bullish(rows: Sequence[Any]) -> bool:
    recent = list(rows)[:4]
    return len(recent) == 4 and all(_float(row["close_price"]) > _float(row["open_price"]) for row in recent)


def _third_wave_or_later(rows: Sequence[Any]) -> bool:
    ordered = list(reversed(list(rows)[:18]))
    if len(ordered) < 10:
        return False
    waves = 0
    previous_high = _float(ordered[0]["high_price"])
    for row in ordered[1:]:
        high = _float(row["high_price"])
        close = _float(row["close_price"])
        open_price = _float(row["open_price"])
        if high > previous_high * 1.004 and close > open_price:
            waves += 1
            previous_high = high
    return waves >= 3


def _volume_climax(rows: Sequence[Any]) -> bool:
    recent = list(rows)
    if len(recent) < 8:
        return False
    latest = recent[0]
    latest_volume = _float(latest["volume"])
    previous_volume = _average(_float(row["volume"]) for row in recent[1:8])
    high = _float(latest["high_price"])
    close = _float(latest["close_price"])
    day_high = max(_float(row["high_price"]) for row in recent[:12])
    upper_tail_rate = (high - close) / max(high, 1)
    return (
        previous_volume > 0
        and latest_volume >= previous_volume * 3
        and high >= day_high * 0.997
        and upper_tail_rate >= 0.004
    )


def _near_long_bar_high_chase(row: Any, current_price: int) -> bool:
    open_price = _float(row["open_price"])
    close = _float(row["close_price"])
    high = _float(row["high_price"])
    low = _float(row["low_price"])
    if open_price <= 0 or close <= open_price or high <= low:
        return False
    body_rate = (close - open_price) / max(high - low, 1)
    range_rate = (high - low) / close
    if body_rate < 0.55 or range_rate < 0.012:
        return False
    return current_price >= high - (high - low) * 0.2


def _vwap_recovery_failed(rows: Sequence[Any], vwap: Decimal) -> bool:
    recent = list(rows)[:3]
    if len(recent) < 3:
        return False
    vwap_float = float(vwap)
    below_count = sum(_float(row["close_price"]) < vwap_float * 0.998 for row in recent)
    return below_count >= 2 and _float(recent[0]["close_price"]) < vwap_float


def _volume_reentry(rows: Sequence[Any]) -> bool:
    recent = list(rows)
    if len(recent) < 6:
        return False
    latest_volume = _float(recent[0]["volume"])
    average_volume = _average(_float(row["volume"]) for row in recent[1:6])
    return average_volume > 0 and latest_volume >= average_volume * 1.8


def _float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _average(values: Any) -> float:
    numbers = [float(value) for value in values if value is not None]
    return sum(numbers) / len(numbers) if numbers else 0.0


def _clamp_decimal(value: Decimal) -> Decimal:
    return max(Decimal("-100"), min(Decimal("100"), value))
