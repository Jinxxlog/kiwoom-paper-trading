from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class DecisionAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    score: Decimal
    weight: Decimal
    reason: str
    source: str = "rule"
    confidence: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("점수 구성요소 이름은 비어 있을 수 없습니다.")
        if self.score < Decimal("-100") or self.score > Decimal("100"):
            raise ValueError("구성요소 점수는 -100 이상 100 이하여야 합니다.")
        if self.weight < 0 or self.weight > 1:
            raise ValueError("구성요소 가중치는 0 이상 1 이하여야 합니다.")
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("구성요소 신뢰도는 0 이상 1 이하여야 합니다.")
        if not self.reason.strip():
            raise ValueError("점수 산정 사유는 비어 있을 수 없습니다.")

    @property
    def weighted_score(self) -> Decimal:
        return self.score * self.weight * self.confidence


@dataclass(frozen=True)
class StrategyDecision:
    strategy_name: str
    stock_code: str
    decided_at: datetime
    action: DecisionAction
    score: Decimal
    confidence: Decimal
    reason: str
    components: tuple[ScoreComponent, ...] = ()

    def __post_init__(self) -> None:
        if not self.strategy_name.strip():
            raise ValueError("전략 이름은 비어 있을 수 없습니다.")
        if not self.stock_code.strip():
            raise ValueError("종목 코드는 비어 있을 수 없습니다.")
        if self.score < Decimal("-100") or self.score > Decimal("100"):
            raise ValueError("전략 점수는 -100 이상 100 이하여야 합니다.")
        if self.confidence < 0 or self.confidence > 1:
            raise ValueError("전략 신뢰도는 0 이상 1 이하여야 합니다.")
        if not self.reason.strip():
            raise ValueError("전략 판단 사유는 비어 있을 수 없습니다.")


@dataclass(frozen=True)
class TradePlan:
    decision_id: int
    stock_code: str
    entry_type: str
    entry_tags: tuple[str, ...]
    entry_price: int
    planned_quantity: int
    stop_loss_price: int
    stop_loss_reason: str
    first_take_profit_price: int
    first_take_profit_reason: str
    partial_exit_rate: Decimal
    breakeven_price: int
    breakeven_trigger_price: int
    trailing_rule: str
    invalidation_reason: str
    reward_risk_ratio: Decimal
    setup: dict

    def __post_init__(self) -> None:
        if self.decision_id < 1:
            raise ValueError("판단 ID는 1 이상이어야 합니다.")
        if not self.stock_code.strip():
            raise ValueError("종목 코드는 비어 있을 수 없습니다.")
        if not self.entry_type.strip():
            raise ValueError("진입 유형은 비어 있을 수 없습니다.")
        if self.entry_price < 1:
            raise ValueError("진입가는 1 이상이어야 합니다.")
        if self.planned_quantity < 0:
            raise ValueError("계획 수량은 0 이상이어야 합니다.")
        if self.stop_loss_price < 1:
            raise ValueError("손절가는 1 이상이어야 합니다.")
        if self.first_take_profit_price < 1:
            raise ValueError("1차 익절가는 1 이상이어야 합니다.")
        if self.breakeven_price < 1:
            raise ValueError("본절가는 1 이상이어야 합니다.")
        if self.breakeven_trigger_price < 1:
            raise ValueError("본절 이동 기준가는 1 이상이어야 합니다.")
        if self.partial_exit_rate <= 0 or self.partial_exit_rate > 1:
            raise ValueError("반익절 비율은 0 초과 1 이하여야 합니다.")
        if not self.stop_loss_reason.strip():
            raise ValueError("손절 사유는 비어 있을 수 없습니다.")
        if not self.first_take_profit_reason.strip():
            raise ValueError("1차 익절 사유는 비어 있을 수 없습니다.")
        if not self.trailing_rule.strip():
            raise ValueError("트레일링 규칙은 비어 있을 수 없습니다.")
        if not self.invalidation_reason.strip():
            raise ValueError("무효화 사유는 비어 있을 수 없습니다.")


@dataclass(frozen=True)
class OrderRequest:
    account_name: str
    stock_code: str
    side: OrderSide
    quantity: int
    decision: StrategyDecision
    order_type: OrderType = OrderType.MARKET
    decision_id: Optional[int] = None
    client_order_id: Optional[str] = None
    reason: Optional[str] = None
    risk_override: bool = False
    reentry_approval_lock_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.account_name.strip():
            raise ValueError("계좌 이름은 비어 있을 수 없습니다.")
        if not self.stock_code.strip():
            raise ValueError("종목 코드는 비어 있을 수 없습니다.")
        if self.quantity < 1:
            raise ValueError("주문 수량은 1주 이상이어야 합니다.")
        if self.stock_code != self.decision.stock_code:
            raise ValueError("주문 종목과 전략 판단 종목이 일치해야 합니다.")
        expected_action = (
            DecisionAction.BUY
            if self.side is OrderSide.BUY
            else DecisionAction.SELL
        )
        if self.decision.action is not expected_action:
            raise ValueError("주문 방향과 전략 판단이 일치해야 합니다.")
        if self.client_order_id is not None and not self.client_order_id.strip():
            raise ValueError("client_order_id는 비어 있을 수 없습니다.")
        if self.decision_id is not None and self.decision_id < 1:
            raise ValueError("decision_id는 1 이상이어야 합니다.")
        if (
            self.reentry_approval_lock_id is not None
            and self.reentry_approval_lock_id < 1
        ):
            raise ValueError("reentry_approval_lock_id는 1 이상이어야 합니다.")
