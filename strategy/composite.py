from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from config.settings import CompositeScoreSettings
from domain.models import DecisionAction, ScoreComponent, StrategyDecision


@dataclass(frozen=True)
class CompositeEvaluation:
    decision: StrategyDecision
    status: str
    blocked_reason: str | None


class CompositeScoringEngine:
    def __init__(self, settings: CompositeScoreSettings) -> None:
        self.settings = settings

    def evaluate(
        self,
        *,
        stock_code: str,
        decided_at: datetime,
        technical_score: float,
        pattern_score: float,
        relative_strength_score: float,
        liquidity_score: float,
        information_score: float,
        market_return: float,
        excluded_reason: str | None,
        information_item_count: int,
        has_features: bool,
    ) -> CompositeEvaluation:
        normalized = {
            "technical": _scale(technical_score, -25, 40),
            "pattern": _scale(pattern_score, -50, 75),
            "relative_strength": _scale(relative_strength_score, -20, 20),
            "liquidity": _scale(liquidity_score, 0, 15),
            "information": _scale(information_score, -20, 20),
            "market": _clamp(50 + market_return * 250),
        }
        components = (
            self._component(
                "technical",
                normalized["technical"],
                self.settings.technical_weight,
                f"기술 원점수 {technical_score:+.2f}",
            ),
            self._component(
                "pattern",
                normalized["pattern"],
                self.settings.pattern_weight,
                f"패턴 원점수 {pattern_score:+.2f}",
            ),
            self._component(
                "relative_strength",
                normalized["relative_strength"],
                self.settings.relative_strength_weight,
                f"상대강도 원점수 {relative_strength_score:+.2f}",
            ),
            self._component(
                "liquidity",
                normalized["liquidity"],
                self.settings.liquidity_weight,
                f"유동성 원점수 {liquidity_score:+.2f}",
            ),
            self._component(
                "information",
                normalized["information"],
                self.settings.information_weight,
                (
                    f"뉴스·공시 {information_item_count}건, "
                    f"원점수 {information_score:+.2f}"
                ),
            ),
            self._component(
                "market",
                normalized["market"],
                self.settings.market_weight,
                f"시장 20일 평균수익률 {market_return:+.2%}",
            ),
        )
        # 뉴스 데이터가 없을 때 중립점수의 가중치가 사라지지 않도록 최종 합산은
        # 정규화 점수와 배점만 사용하고 confidence는 데이터 완전성 표시에 사용한다.
        score = sum(
            component.score * component.weight
            for component in components
        )
        confidence = Decimal("0")
        if has_features:
            confidence += Decimal("0.85")
        if information_item_count:
            confidence += Decimal("0.10")
        if market_return != 0:
            confidence += Decimal("0.05")
        confidence = min(Decimal("1"), confidence)

        blocked_reason = excluded_reason
        if blocked_reason:
            status = "blocked"
            action = DecisionAction.HOLD
            reason = f"진입 차단: {blocked_reason}"
        elif confidence < self.settings.minimum_confidence:
            status = "insufficient_data"
            action = DecisionAction.HOLD
            reason = (
                f"데이터 신뢰도 부족 {confidence:.0%} "
                f"< {self.settings.minimum_confidence:.0%}"
            )
        elif score >= self.settings.buy_threshold:
            status = "buy_candidate"
            action = DecisionAction.BUY
            reason = f"종합 {score:.2f}점: shadow 매수 후보"
        elif score >= self.settings.observe_threshold:
            status = "observe"
            action = DecisionAction.HOLD
            reason = f"종합 {score:.2f}점: 관찰"
        else:
            status = "excluded"
            action = DecisionAction.HOLD
            reason = f"종합 {score:.2f}점: 기준 미달"

        return CompositeEvaluation(
            decision=StrategyDecision(
                strategy_name="composite",
                stock_code=stock_code,
                decided_at=decided_at,
                action=action,
                score=score.quantize(Decimal("0.0001")),
                confidence=confidence.quantize(Decimal("0.0001")),
                reason=reason,
                components=components,
            ),
            status=status,
            blocked_reason=blocked_reason,
        )

    @staticmethod
    def _component(
        name: str,
        score: float,
        weight: Decimal,
        reason: str,
    ) -> ScoreComponent:
        return ScoreComponent(
            name=name,
            score=Decimal(str(round(score, 4))),
            weight=weight,
            confidence=Decimal("1"),
            reason=reason,
            source="composite_rule",
        )


def _scale(value: float, minimum: float, maximum: float) -> float:
    if maximum <= minimum:
        raise ValueError("정규화 범위가 올바르지 않습니다.")
    return _clamp((value - minimum) / (maximum - minimum) * 100)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))
