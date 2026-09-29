from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from config.settings import MarketModeGateSettings
from db.database import Database


KOSDAQ_MARKET = "KOSDAQ"
RISK_ON_TRADE_AMOUNT = 10_000_000_000_000
RISK_OFF_TRADE_AMOUNT = 5_000_000_000_000


@dataclass(frozen=True)
class MarketModePolicy:
    mode: str
    quantity_multiplier: Decimal
    allowed_entry_grades: str
    new_entry_policy: str


POLICIES: dict[str, MarketModePolicy] = {
    "risk_on": MarketModePolicy(
        mode="risk_on",
        quantity_multiplier=Decimal("1.0"),
        allowed_entry_grades="A/B 일부",
        new_entry_policy="허용",
    ),
    "neutral": MarketModePolicy(
        mode="neutral",
        quantity_multiplier=Decimal("0.5"),
        allowed_entry_grades="A 중심, 강한 B 1주",
        new_entry_policy="제한 허용",
    ),
    "risk_off": MarketModePolicy(
        mode="risk_off",
        quantity_multiplier=Decimal("0.2"),
        allowed_entry_grades="A급 강한 돌파만",
        new_entry_policy="차단 또는 1주 탐색",
    ),
}


def classify_market_mode(
    *,
    above_ma20: bool | None,
    trade_amount: int | None,
) -> tuple[str, str, str]:
    reasons: list[str] = []
    if above_ma20 is None or trade_amount is None:
        return "risk_off", "unavailable", "20일선 또는 거래대금 데이터 없음"

    if not above_ma20:
        reasons.append("20일선 아래")
    else:
        reasons.append("20일선 위")

    if trade_amount >= RISK_ON_TRADE_AMOUNT:
        reasons.append("거래대금 10조 이상")
    elif trade_amount < RISK_OFF_TRADE_AMOUNT:
        reasons.append("거래대금 5조 미만")
    else:
        reasons.append("거래대금 5~10조")

    if not above_ma20 or trade_amount < RISK_OFF_TRADE_AMOUNT:
        return "risk_off", "reliable", "; ".join(reasons)
    if above_ma20 and trade_amount >= RISK_ON_TRADE_AMOUNT:
        return "risk_on", "reliable", "; ".join(reasons)
    return "neutral", "reliable", "; ".join(reasons)


def policy_for_mode(mode: str) -> MarketModePolicy:
    return POLICIES.get(mode, POLICIES["risk_off"])


def _read_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class MarketModeService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def latest_snapshot(self, market: str = KOSDAQ_MARKET) -> dict[str, Any] | None:
        row = self.database.latest_market_mode_snapshot(market)
        if row is None:
            return None
        item = dict(row)
        try:
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            item["details"] = {}
        policy = policy_for_mode(str(item.get("mode") or "risk_off"))
        item["policy"] = {
            "new_entry_policy": policy.new_entry_policy,
            "quantity_multiplier": str(policy.quantity_multiplier),
            "allowed_entry_grades": policy.allowed_entry_grades,
        }
        return item

    def settings_from_base(
        self,
        base: MarketModeGateSettings,
        *,
        market: str = KOSDAQ_MARKET,
        max_age_minutes: int = 30,
    ) -> MarketModeGateSettings:
        if base.requested_mode != "auto":
            return base

        latest = self.latest_snapshot(market)
        if latest is None:
            return MarketModeGateSettings(
                enabled=base.enabled,
                manual_mode="risk_off",
                source="auto_unavailable",
                requested_mode="auto",
                reason="자동 시장 모드 데이터가 아직 없어 risk_off fallback 적용",
                order_effect="fail_closed",
                quantity_multiplier=Decimal("0"),
                allowed_entry_grades="차단",
            )

        collected_at = _parse_datetime(latest.get("collected_at"))
        stale = True
        if collected_at is not None:
            stale = datetime.now() - collected_at > timedelta(minutes=max_age_minutes)
        if stale or latest.get("reliability") == "unavailable":
            return MarketModeGateSettings(
                enabled=base.enabled,
                manual_mode="risk_off",
                source="auto_unavailable",
                requested_mode="auto",
                reason="자동 시장 모드 데이터가 없거나 오래되어 risk_off fallback 적용",
                order_effect="fail_closed",
                quantity_multiplier=Decimal("0"),
                allowed_entry_grades="차단",
                shadow_mode=str(latest.get("mode") or ""),
            )

        mode = str(latest.get("mode") or "risk_off")
        policy = policy_for_mode(mode)
        apply_to_paper = _read_bool("MARKET_MODE_AUTO_APPLY_TO_PAPER", False)
        if not apply_to_paper:
            return MarketModeGateSettings(
                enabled=base.enabled,
                manual_mode="neutral",
                source="auto_shadow",
                requested_mode="auto",
                reason=(
                    f"자동 시장 모드 shadow={mode}: {latest.get('reason')}; "
                    "모의 BUY에는 아직 미적용"
                ),
                order_effect="shadow_only",
                quantity_multiplier=Decimal("1"),
                allowed_entry_grades="기존 정책",
                shadow_mode=mode,
            )

        return MarketModeGateSettings(
            enabled=base.enabled,
            manual_mode=mode,
            source="auto_kosdaq",
            requested_mode="auto",
            reason=f"코스닥 자동 시장 모드 적용: {latest.get('reason')}",
            order_effect="paper_apply",
            quantity_multiplier=policy.quantity_multiplier,
            allowed_entry_grades=policy.allowed_entry_grades,
            shadow_mode=mode,
        )


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed
