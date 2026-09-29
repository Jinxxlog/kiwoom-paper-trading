from domain.models import OrderRequest, OrderType
from execution.base import (
    OrderExecutionError,
    OrderExecutionResult,
    OrderExecutor,
)
from notifications.telegram import TelegramNotifier
from paper.engine import PaperTradingEngine, PaperTradingError


class PaperOrderExecutor(OrderExecutor):
    def __init__(
        self,
        engine: PaperTradingEngine,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.engine = engine
        self.notifier = notifier or TelegramNotifier.from_env()

    def submit(self, request: OrderRequest) -> OrderExecutionResult:
        if request.order_type is not OrderType.MARKET:
            raise OrderExecutionError("모의투자는 현재 시장가 주문만 지원합니다.")
        if request.decision_id is None:
            raise OrderExecutionError("저장된 전략 판단 ID가 필요합니다.")
        try:
            order_request_id = self.engine.database.save_order_request(
                request,
                decision_id=request.decision_id,
                execution_mode="paper",
            )
        except Exception as exc:
            if "UNIQUE constraint failed: order_requests" in str(exc):
                raise OrderExecutionError(
                    "이미 저장된 동일 주문 요청입니다."
                ) from exc
            raise
        try:
            fill = self.engine.place_market_order(
                request.account_name,
                request.stock_code,
                request.side.value,
                request.quantity,
                reason=request.reason or request.decision.reason,
                client_order_id=request.client_order_id,
                risk_override=request.risk_override,
                reentry_approval_lock_id=request.reentry_approval_lock_id,
            )
        except PaperTradingError as exc:
            internal_order_id = None
            if request.client_order_id is not None:
                with self.engine.database.connect() as connection:
                    row = connection.execute(
                        """
                        SELECT o.id
                        FROM paper_orders o
                        JOIN paper_accounts a ON a.id = o.account_id
                        WHERE a.name = ? AND o.client_order_id = ?
                        """,
                        (request.account_name, request.client_order_id),
                    ).fetchone()
                internal_order_id = int(row["id"]) if row is not None else None
            self.engine.database.save_order_execution(
                order_request_id,
                status="rejected",
                quantity=request.quantity,
                internal_order_id=internal_order_id,
                message=str(exc),
            )
            self._notify(
                "모의 주문 거절",
                request.account_name,
                request.stock_code,
                request.side.value,
                request.quantity,
                message=str(exc),
            )
            raise OrderExecutionError(str(exc)) from exc
        except Exception as exc:
            self.engine.database.save_order_execution(
                order_request_id,
                status="failed",
                quantity=request.quantity,
                message=str(exc),
            )
            raise
        execution_id = self.engine.database.save_order_execution(
            order_request_id,
            status="filled",
            quantity=fill.quantity,
            internal_order_id=fill.order_id,
            fill_price=fill.fill_price,
            message=request.reason or request.decision.reason,
        )
        self._notify(
            "모의 주문 체결",
            fill.account_name,
            fill.stock_code,
            fill.side,
            fill.quantity,
            fill_price=fill.fill_price,
            message=request.reason or request.decision.reason,
        )
        return OrderExecutionResult(
            status="filled",
            account_name=fill.account_name,
            stock_code=fill.stock_code,
            side=fill.side,
            quantity=fill.quantity,
            decision_id=request.decision_id,
            order_request_id=order_request_id,
            execution_id=execution_id,
            internal_order_id=fill.order_id,
            fill_price=fill.fill_price,
            message=request.reason or request.decision.reason,
        )

    def _notify(
        self,
        title: str,
        account_name: str,
        stock_code: str,
        side: str,
        quantity: int,
        *,
        fill_price: int | None = None,
        message: str | None = None,
    ) -> None:
        side_label = "매수" if side == "buy" else "매도"
        lines = [
            f"[{title}]",
            f"계좌: {account_name}",
            f"종목: {stock_code}",
            f"구분: {side_label}",
            f"수량: {quantity}주",
        ]
        if fill_price is not None:
            lines.append(f"체결가: {fill_price:,}원")
        if message:
            lines.append(f"근거: {message}")
        self.notifier.send("\n".join(lines))
