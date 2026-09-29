from dataclasses import dataclass
from datetime import datetime, time as clock_time
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SessionProfile:
    name: str
    label: str
    start: clock_time
    end: clock_time
    entry_allowed: bool
    paper_probe_allowed: bool
    minimum_score: Decimal
    paper_probe_score_adjustment: Decimal
    quantity_multiplier: Decimal
    allowed_entry_types: tuple[str, ...]
    reason: str

    def allows_entry_type(self, entry_type: str) -> bool:
        return not self.allowed_entry_types or entry_type in self.allowed_entry_types

    def paper_probe_threshold(self, base_threshold: Decimal) -> Decimal:
        return max(
            self.minimum_score,
            base_threshold + self.paper_probe_score_adjustment,
        )


@dataclass(frozen=True)
class IntradayStrategyMode:
    name: str
    label: str
    session_profile: str
    entry_allowed: bool
    paper_probe_allowed: bool
    leader_required: bool
    leader_pass: bool
    supply_pass: bool
    market_mode_pass: bool
    minimum_score: Decimal
    quantity_multiplier: Decimal
    reason: str
    details: dict[str, Any]

    @property
    def blocked(self) -> bool:
        return not self.entry_allowed

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "session_profile": self.session_profile,
            "entry_allowed": self.entry_allowed,
            "paper_probe_allowed": self.paper_probe_allowed,
            "leader_required": self.leader_required,
            "leader_pass": self.leader_pass,
            "supply_pass": self.supply_pass,
            "market_mode_pass": self.market_mode_pass,
            "minimum_score": str(self.minimum_score),
            "quantity_multiplier": str(self.quantity_multiplier),
            "reason": self.reason,
            "details": self.details,
        }


ALWAYS_ACTIVE_PROFILE = SessionProfile(
    name="all_day_active",
    label="전일 동일 운전",
    start=clock_time(0, 0),
    end=clock_time(23, 59, 59),
    entry_allowed=True,
    paper_probe_allowed=True,
    minimum_score=Decimal("0"),
    paper_probe_score_adjustment=Decimal("0"),
    quantity_multiplier=Decimal("1.0"),
    allowed_entry_types=(),
    reason="시간대별 세션 프로필 비활성",
)


SESSION_PROFILES: tuple[SessionProfile, ...] = (
    SessionProfile(
        name="opening_leader_core",
        label="장초반 대장주",
        start=clock_time(0, 0),
        end=clock_time(9, 20),
        entry_allowed=True,
        paper_probe_allowed=True,
        minimum_score=Decimal("60"),
        paper_probe_score_adjustment=Decimal("0"),
        quantity_multiplier=Decimal("1.0"),
        allowed_entry_types=(
            "volume_breakout",
            "opening_range_breakout",
            "vwap_pullback",
            "breakout_probe",
            "order_flow_momentum",
            "paper_probe",
        ),
        reason="09:00~09:20 대장주 선별·빠른 부분익절·잔여 보호 청산 핵심 구간",
    ),
    SessionProfile(
        name="morning_aggressive",
        label="오전 집중",
        start=clock_time(9, 20),
        end=clock_time(9, 30),
        entry_allowed=True,
        paper_probe_allowed=True,
        minimum_score=Decimal("60"),
        paper_probe_score_adjustment=Decimal("0"),
        quantity_multiplier=Decimal("1.0"),
        allowed_entry_types=(
            "volume_breakout",
            "opening_range_breakout",
            "vwap_pullback",
            "breakout_probe",
            "order_flow_momentum",
            "paper_probe",
        ),
        reason="거래가 가장 활발한 장초반 집중 매매 구간",
    ),
    SessionProfile(
        name="morning_active",
        label="오전 적극",
        start=clock_time(9, 30),
        end=clock_time(10, 0),
        entry_allowed=True,
        paper_probe_allowed=True,
        minimum_score=Decimal("65"),
        paper_probe_score_adjustment=Decimal("3"),
        quantity_multiplier=Decimal("0.85"),
        allowed_entry_types=(
            "volume_breakout",
            "opening_range_breakout",
            "vwap_pullback",
            "breakout_probe",
            "order_flow_momentum",
            "paper_probe",
        ),
        reason="오전 주도주 매매는 유지하되 장초반보다 기준을 소폭 강화",
    ),
    SessionProfile(
        name="midday_conservative",
        label="중간장 보수",
        start=clock_time(10, 0),
        end=clock_time(14, 30),
        entry_allowed=True,
        paper_probe_allowed=True,
        minimum_score=Decimal("70"),
        paper_probe_score_adjustment=Decimal("5"),
        quantity_multiplier=Decimal("0.50"),
        allowed_entry_types=(
            "volume_breakout",
            "vwap_pullback",
            "breakout_probe",
        ),
        reason="거래가 줄어드는 중간장은 고점수 돌파·눌림만 소량 진입",
    ),
    SessionProfile(
        name="afternoon_focus",
        label="오후 재집중",
        start=clock_time(14, 30),
        end=clock_time(15, 10),
        entry_allowed=True,
        paper_probe_allowed=True,
        minimum_score=Decimal("68"),
        paper_probe_score_adjustment=Decimal("3"),
        quantity_multiplier=Decimal("0.60"),
        allowed_entry_types=(
            "volume_breakout",
            "vwap_pullback",
            "breakout_probe",
            "order_flow_momentum",
        ),
        reason="오후 모멘텀은 관찰하되 오버나잇 없이 당일 청산 기준 유지",
    ),
    SessionProfile(
        name="liquidation_prep",
        label="마감 정리",
        start=clock_time(15, 10),
        end=clock_time(23, 59, 59),
        entry_allowed=False,
        paper_probe_allowed=False,
        minimum_score=Decimal("999"),
        paper_probe_score_adjustment=Decimal("999"),
        quantity_multiplier=Decimal("0"),
        allowed_entry_types=(),
        reason="마감 10분 전부터 신규 진입 금지와 보유 정리 준비",
    ),
)


def session_profile_for(timestamp: datetime) -> SessionProfile:
    current = timestamp.time().replace(tzinfo=None)
    for profile in SESSION_PROFILES:
        if profile.start <= current < profile.end:
            return profile
    return SESSION_PROFILES[-1]


def intraday_strategy_mode_for(
    timestamp: datetime,
    session_profile: SessionProfile,
    *,
    leader_score: dict[str, Any] | None = None,
    supply_score: dict[str, Any] | None = None,
    market_mode_enabled: bool = False,
    market_mode: str = "neutral",
    market_mode_reason: str = "",
    market_mode_quantity_multiplier: Decimal = Decimal("1"),
) -> IntradayStrategyMode:
    current = timestamp.time().replace(tzinfo=None)
    leader_role = str((leader_score or {}).get("role") or "")
    leader_total = _float_or_none((leader_score or {}).get("total_score"))
    leader_data_available = bool(leader_score)
    supply_blocked = bool((supply_score or {}).get("blocked"))
    supply_relaxed_by_leader = (
        supply_blocked
        and leader_role in {"leader", "second"}
        and (leader_total is None or leader_total >= 70.0)
        and not bool((leader_score or {}).get("blocked"))
    )
    market_mode_pass = (not market_mode_enabled) or market_mode != "risk_off"
    effective_quantity_multiplier = (
        session_profile.quantity_multiplier * market_mode_quantity_multiplier
    )

    if session_profile.name == ALWAYS_ACTIVE_PROFILE.name:
        supply_pass = (not supply_blocked) or supply_relaxed_by_leader
        entry_allowed = session_profile.entry_allowed and supply_pass and market_mode_pass
        reasons = [session_profile.reason]
        if supply_blocked and supply_relaxed_by_leader:
            reasons.append("수급 차단 대장주 재평가")
        elif not supply_pass:
            reasons.append("수급 차단")
        if not market_mode_pass:
            reasons.append("시장 모드 risk_off")
        if market_mode_reason:
            reasons.append(market_mode_reason)
        return IntradayStrategyMode(
            name="all_day_active",
            label="전일 동일 운전",
            session_profile=session_profile.name,
            entry_allowed=entry_allowed,
            paper_probe_allowed=(
                session_profile.paper_probe_allowed and supply_pass and market_mode_pass
            ),
            leader_required=False,
            leader_pass=True,
            supply_pass=supply_pass,
            market_mode_pass=market_mode_pass,
            minimum_score=session_profile.minimum_score,
            quantity_multiplier=effective_quantity_multiplier,
            reason="; ".join(reason for reason in reasons if reason),
            details={
                "time": current.isoformat(),
                "leader_role": leader_role or None,
                "leader_total_score": leader_total,
                "leader_data_available": leader_data_available,
                "supply_blocked": supply_blocked,
                "supply_block_reason": (supply_score or {}).get("blocked_reason"),
                "market_mode": market_mode,
                "market_mode_enabled": market_mode_enabled,
                "market_mode_quantity_multiplier": str(
                    market_mode_quantity_multiplier
                ),
            },
        )

    name = "verified_leader"
    label = "검증 대장주"
    leader_required = False
    leader_pass = True
    entry_allowed = session_profile.entry_allowed
    paper_probe_allowed = session_profile.paper_probe_allowed
    minimum_score = session_profile.minimum_score
    quantity_multiplier = effective_quantity_multiplier
    reasons = [session_profile.reason]

    if current < clock_time(9, 20):
        name = "opening_leader_core"
        label = "장초반 대장주"
        leader_pass = (not leader_data_available) or (
            leader_role in {"leader", "second", "follower"}
            and not bool((leader_score or {}).get("blocked"))
        )
        reasons.append("09:00~09:20 대장주 선별·빠른 부분익절·잔여 보호 청산")
    elif current < clock_time(10, 0):
        name = "aggressive_leader"
        label = "오전 적극 대장주"
        leader_pass = (not leader_data_available) or leader_role in {
            "leader",
            "second",
            "follower",
        }
        reasons.append("09:10~10:00 대장주·수급 우선 적극 구간")
    elif current < clock_time(14, 30):
        name = "verified_leader"
        label = "검증 대장주"
        leader_required = leader_data_available
        leader_pass = (not leader_data_available) or (
            leader_role in {"leader", "second"}
            and (leader_total is None or leader_total >= 65.0)
        )
        reasons.append("10:00~14:30 검증된 대장주 중심")
    elif current < clock_time(15, 10):
        name = "late_risk_off"
        label = "오후 신규 제한"
        leader_required = leader_data_available
        leader_pass = (not leader_data_available) or (
            leader_role == "leader"
            and (leader_total is None or leader_total >= 85.0)
        )
        entry_allowed = False
        paper_probe_allowed = False
        minimum_score = Decimal("999")
        quantity_multiplier = Decimal("0")
        reasons.append("14:30 이후 신규 진입 제한")
    else:
        name = "closing_cleanup"
        label = "마감 정리"
        entry_allowed = False
        paper_probe_allowed = False
        minimum_score = Decimal("999")
        quantity_multiplier = Decimal("0")
        reasons.append("15:10 이후 정리 모드")

    supply_pass = (not supply_blocked) or supply_relaxed_by_leader
    if not leader_pass:
        entry_allowed = False
        paper_probe_allowed = False
        reasons.append("대장주 조건 미충족")
    if supply_blocked and supply_relaxed_by_leader:
        reasons.append("수급 차단 대장주 재평가")
    elif not supply_pass:
        entry_allowed = False
        paper_probe_allowed = False
        reasons.append("수급 차단")
    if not market_mode_pass:
        entry_allowed = False
        paper_probe_allowed = False
        reasons.append("시장 모드 risk_off")
    if market_mode_reason:
        reasons.append(market_mode_reason)

    return IntradayStrategyMode(
        name=name,
        label=label,
        session_profile=session_profile.name,
        entry_allowed=entry_allowed,
        paper_probe_allowed=paper_probe_allowed,
        leader_required=leader_required,
        leader_pass=leader_pass,
        supply_pass=supply_pass,
        market_mode_pass=market_mode_pass,
        minimum_score=minimum_score,
        quantity_multiplier=quantity_multiplier,
        reason="; ".join(reason for reason in reasons if reason),
        details={
            "time": current.isoformat(),
            "leader_role": leader_role or None,
            "leader_total_score": leader_total,
            "leader_data_available": leader_data_available,
            "supply_blocked": supply_blocked,
            "supply_block_reason": (supply_score or {}).get("blocked_reason"),
            "market_mode": market_mode,
            "market_mode_enabled": market_mode_enabled,
            "market_mode_quantity_multiplier": str(market_mode_quantity_multiplier),
        },
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
