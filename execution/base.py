from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from domain.models import OrderRequest


class OrderExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderExecutionResult:
    status: str
    account_name: str
    stock_code: str
    side: str
    quantity: int
    decision_id: Optional[int] = None
    order_request_id: Optional[int] = None
    execution_id: Optional[int] = None
    internal_order_id: Optional[int] = None
    broker_order_id: Optional[str] = None
    fill_price: Optional[int] = None
    message: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in {"submitted", "filled", "rejected"}:
            raise ValueError(f"지원하지 않는 주문 상태입니다: {self.status}")


class OrderExecutor(ABC):
    @abstractmethod
    def submit(self, request: OrderRequest) -> OrderExecutionResult:
        raise NotImplementedError
