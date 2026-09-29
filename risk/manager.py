import json
from dataclasses import dataclass, replace
from datetime import datetime, time as clock_time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable, Optional

from config.settings import RiskSettings
from data.scheduler import SEOUL_TIMEZONE
from data.watchlist import normalize_stock_code
from db.database import Database
from domain.models import (
    DecisionAction,
    OrderRequest,
    OrderSide,
    ScoreComponent,
    StrategyDecision,
)
from execution.base import (
    OrderExecutionError,
    OrderExecutionResult,
    OrderExecutor,
)
from execution.paper import PaperOrderExecutor
from paper.engine import PaperTradingEngine, PaperTradingError
from risk.exit_policy import (
    ExitPlan,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TRAILING_STOP,
    MarketExitContext,
    PriceBar,
    build_market_context,
    evaluate_exit,
)
from strategy.registry import register_risk_strategy


MARKET_CLOSE_LIQUIDATION = clock_time(15, 20)
EXIT_PARTIAL_TAKE_PROFIT = "partial_take_profit"
SECOND_TARGET_DELAY_CUTOFF = clock_time(14, 30)
ENTRY_CUTOFF_FOR_REENTRY = clock_time(15, 10)
EARLY_LEADER_STOP_CONFIRMATION_CUTOFF = clock_time(10, 0)
EARLY_LEADER_EMERGENCY_STOP_RATE = Decimal("0.015")
ADAPTIVE_LEADER_PARTIAL_PROFIT_RATE = Decimal("0.045")
ADAPTIVE_LEADER_WIDE_TARGET_RATE = Decimal("0.08")
ADAPTIVE_LEADER_MIN_R_MULTIPLE = Decimal("0.5")
OPENING_LEADER_FAST_PROFIT_RATE = Decimal("0.03")
OPENING_LEADER_MIN_R_MULTIPLE = Decimal("0.35")
AGGRESSIVE_A_PLUS_FAST_PROFIT_RATE = Decimal("0.03")
AGGRESSIVE_A_PLUS_MIN_R_MULTIPLE = Decimal("0.35")
AGGRESSIVE_MIDDAY_FADE_CUTOFF = clock_time(10, 30)
AGGRESSIVE_TIGHT_TRAILING_RATE = Decimal("0.012")
SUPPLY_BLOCK_FAST_PROFIT_RATE = Decimal("0.025")
SUPPLY_BLOCK_FAST_R_MULTIPLE = Decimal("0.35")
STATE_MACHINE_A_PLUS_PARTIAL_RATE = Decimal("0.25")
STATE_MACHINE_STANDARD_PARTIAL_RATE = Decimal("0.30")
STATE_MACHINE_EXPANSION_R_MULTIPLE = Decimal("0.75")
STATE_MACHINE_STALL_PULLBACK_R_MULTIPLE = Decimal("0.20")
REENTRY_ENGINE_VERSION = "v0.12.96"
LEADER_ENTRY_TYPES = {
    "chart_breakout_hold",
    "opening_range_breakout",
    "breakout_probe",
    "volume_breakout",
    "order_flow_momentum",
}


@dataclass(frozen=True)
class RiskEvaluation:
    account_name: str
    stock_code: str
    current_price: int
    highest_price: int
    event_type: Optional[str]
    threshold_price: Optional[int]
    order_id: Optional[int]
    initial_stop_price: Optional[int] = None
    current_stop_price: Optional[int] = None
    take_profit_price: Optional[int] = None
    trailing_active: bool = False
    breakeven_active: bool = False
    trend_strength: str = "neutral"
    trend_score: int = 0
    expected_exit_reason: Optional[str] = None
    exit_class: Optional[str] = None
    exit_reason_class: Optional[str] = None
    setup_invalidated: bool = False
    exit_state: Optional[str] = None
    exit_state_machine: Optional[dict[str, Any]] = None


def _floor_price(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil_price(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _trade_plan_setup(trade_plan: object) -> dict:
    raw = trade_plan["setup_json"] if "setup_json" in trade_plan.keys() else "{}"
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confirmed_supply_net_sell(supply: Optional[dict[str, Any]]) -> bool:
    if not supply:
        return False
    components = supply.get("components")
    if components is None and supply.get("components_json"):
        try:
            components = json.loads(str(supply.get("components_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            components = {}
    components = components if isinstance(components, dict) else {}
    program = components.get("program") or {}
    trend = components.get("trend") or {}
    score = _float_or_none(supply.get("total_score"))
    return bool(
        program.get("hard_block")
        or program.get("policy_state") == "block"
        or int(program.get("consecutive_net_sell_count") or 0) >= 3
        or int(trend.get("consecutive_net_sell_count") or 0) >= 3
        or (score is not None and score <= 20.0)
    )


def _risk_exit_class(event_type: str, realized_pnl: Optional[int]) -> str:
    pnl = int(realized_pnl or 0)
    if event_type == EXIT_PARTIAL_TAKE_PROFIT:
        return "partial_take_profit" if pnl >= 0 else "partial_loss_exit"
    if event_type == EXIT_TRAILING_STOP:
        return "trailing_profit_lock" if pnl >= 0 else "trailing_loss_stop"
    if event_type == EXIT_STOP_LOSS:
        return "profit_protect_stop" if pnl >= 0 else "loss_stop"
    return event_type or "unknown"


def _reentry_lock_until(timestamp: str) -> str:
    try:
        current = datetime.fromisoformat(timestamp)
    except ValueError:
        current = datetime.now(SEOUL_TIMEZONE).replace(tzinfo=None)
    if current.tzinfo is not None:
        current = current.astimezone(SEOUL_TIMEZONE).replace(tzinfo=None)
    if current.time().replace(tzinfo=None) < ENTRY_CUTOFF_FOR_REENTRY:
        return current.replace(
            hour=ENTRY_CUTOFF_FOR_REENTRY.hour,
            minute=ENTRY_CUTOFF_FOR_REENTRY.minute,
            second=0,
            microsecond=0,
        ).isoformat(timespec="seconds")
    return (current + timedelta(days=1)).replace(
        hour=9,
        minute=0,
        second=0,
        microsecond=0,
    ).isoformat(timespec="seconds")


class RiskManager:
    def __init__(
        self,
        database: Database,
        paper_engine: PaperTradingEngine,
        settings: RiskSettings,
        order_executor: Optional[OrderExecutor] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.database = database
        self.paper_engine = paper_engine
        self.settings = settings
        self.order_executor = order_executor or PaperOrderExecutor(paper_engine)
        self.now_provider = now_provider
        self._validate_dynamic_settings()
        (
            self.strategy_version_id,
            self.strategy_config_id,
        ) = register_risk_strategy(database, settings)

    def _now(self) -> datetime:
        if self.now_provider is not None:
            return self.now_provider()
        return datetime.now(SEOUL_TIMEZONE)

    def _validate_dynamic_settings(self) -> None:
        if self.settings.atr_period < 1:
            raise ValueError("atr_period는 1 이상이어야 합니다.")
        if self.settings.atr_stop_multiple <= 0:
            raise ValueError("atr_stop_multiple은 0보다 커야 합니다.")
        for name, value in (
            ("breakeven_activation_rate", self.settings.breakeven_activation_rate),
            ("breakeven_buffer_rate", self.settings.breakeven_buffer_rate),
            (
                "strong_trend_take_profit_rate",
                self.settings.strong_trend_take_profit_rate,
            ),
            (
                "strong_trend_trailing_drawdown_rate",
                self.settings.strong_trend_trailing_drawdown_rate,
            ),
            (
                "weak_trend_trailing_drawdown_rate",
                self.settings.weak_trend_trailing_drawdown_rate,
            ),
            ("volume_dry_up_ratio", self.settings.volume_dry_up_ratio),
        ):
            if value < 0 or value >= 1:
                raise ValueError(f"{name}는 0 이상 1 미만이어야 합니다.")
        if (
            self.settings.strong_trend_take_profit_rate
            < self.settings.take_profit_rate
        ):
            raise ValueError("강한 추세 익절률은 기본 익절률 이상이어야 합니다.")

    def configure_position(
        self,
        account_name: str,
        stock_code: str,
        *,
        stop_loss_rate: Optional[Decimal] = None,
        take_profit_rate: Optional[Decimal] = None,
        trailing_activation_rate: Optional[Decimal] = None,
        trailing_drawdown_rate: Optional[Decimal] = None,
    ) -> None:
        normalized_code = normalize_stock_code(stock_code)
        values = {
            "stop_loss_rate": (
                stop_loss_rate
                if stop_loss_rate is not None
                else self.settings.stop_loss_rate
            ),
            "take_profit_rate": (
                take_profit_rate
                if take_profit_rate is not None
                else self.settings.take_profit_rate
            ),
            "trailing_activation_rate": (
                trailing_activation_rate
                if trailing_activation_rate is not None
                else self.settings.trailing_activation_rate
            ),
            "trailing_drawdown_rate": (
                trailing_drawdown_rate
                if trailing_drawdown_rate is not None
                else self.settings.trailing_drawdown_rate
            ),
        }
        for name, value in values.items():
            if value < 0 or value >= 1:
                raise ValueError(f"{name}는 0 이상 1 미만이어야 합니다.")
        timestamp = self._now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT a.id AS account_id, p.quantity
                FROM paper_accounts a
                JOIN paper_positions p ON p.account_id = a.id
                WHERE a.name = ? AND p.stock_code = ?
                """,
                (account_name, normalized_code),
            ).fetchone()
            if row is None:
                raise PaperTradingError(
                    f"보유 포지션을 찾을 수 없습니다: {account_name} {normalized_code}"
                )
            price = self.paper_engine.get_trade_price(normalized_code)
            connection.execute(
                """
                INSERT INTO paper_risk_profiles(
                    account_id, stock_code, stop_loss_rate, take_profit_rate,
                    trailing_activation_rate, trailing_drawdown_rate,
                    highest_price, trailing_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(account_id, stock_code) DO UPDATE SET
                    stop_loss_rate = excluded.stop_loss_rate,
                    take_profit_rate = excluded.take_profit_rate,
                    trailing_activation_rate = excluded.trailing_activation_rate,
                    trailing_drawdown_rate = excluded.trailing_drawdown_rate,
                    updated_at = excluded.updated_at
                """,
                (
                    row["account_id"],
                    normalized_code,
                    float(values["stop_loss_rate"]),
                    float(values["take_profit_rate"]),
                    float(values["trailing_activation_rate"]),
                    float(values["trailing_drawdown_rate"]),
                    int(price["price"]),
                    timestamp,
                    timestamp,
                ),
            )

    def evaluate_account(self, account_name: str) -> list[RiskEvaluation]:
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = ?",
                (account_name,),
            ).fetchone()
            if account is None:
                raise PaperTradingError(f"모의계좌를 찾을 수 없습니다: {account_name}")
            positions = connection.execute(
                """
                SELECT p.stock_code, p.quantity, p.cost_basis,
                       r.stop_loss_rate, r.take_profit_rate,
                       r.trailing_activation_rate, r.trailing_drawdown_rate,
                       r.highest_price, r.trailing_active,
                       r.initial_stop_price, r.atr_value, r.atr_stop_price,
                       r.current_stop_price, r.current_take_profit_price,
                       r.breakeven_active, r.trend_strength, r.trend_score,
                       r.current_trailing_drawdown_rate, r.support_price,
                       r.volume_ratio, r.expected_exit_reason,
                       r.last_rule_reason
                FROM paper_positions p
                LEFT JOIN paper_risk_profiles r
                  ON r.account_id = p.account_id AND r.stock_code = p.stock_code
                WHERE p.account_id = ?
                ORDER BY p.stock_code
                """,
                (account["id"],),
            ).fetchall()

        evaluations = []
        for position in positions:
            if position["stop_loss_rate"] is None:
                self.configure_position(account_name, position["stock_code"])
            evaluations.append(self._evaluate_position(account_name, position["stock_code"]))
        return evaluations

    def evaluate_all_accounts(self) -> list[RiskEvaluation]:
        with self.database.connect() as connection:
            accounts = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM paper_accounts ORDER BY id"
                ).fetchall()
            ]
        evaluations = []
        for account_name in accounts:
            evaluations.extend(self.evaluate_account(account_name))
        return evaluations

    def liquidate_market_close(
        self,
        account_name: Optional[str] = None,
        *,
        now: Optional[datetime] = None,
        force: bool = False,
    ) -> list[RiskEvaluation]:
        current = now or datetime.now(SEOUL_TIMEZONE)
        if current.tzinfo is not None:
            current = current.astimezone(SEOUL_TIMEZONE)
        local_time = current.time().replace(tzinfo=None)
        if not force and local_time < MARKET_CLOSE_LIQUIDATION:
            return []

        with self.database.connect() as connection:
            query = """
                SELECT a.name AS account_name, p.stock_code, p.quantity
                FROM paper_positions p
                JOIN paper_accounts a ON a.id = p.account_id
            """
            parameters: tuple[object, ...] = ()
            if account_name is not None:
                query += " WHERE a.name = ?"
                parameters = (account_name,)
            query += " ORDER BY a.id, p.stock_code"
            positions = connection.execute(query, parameters).fetchall()

        results = []
        date_key = current.date().isoformat()
        for position in positions:
            price = self.paper_engine.get_trade_price(
                position["stock_code"],
                now=current,
            )
            reason = "장 마감 10분 전 전량 청산"
            execution = self._submit_sell(
                position["account_name"],
                position["stock_code"],
                int(position["quantity"]),
                reason=reason,
                event_type="market_close",
                decision_time=current,
                client_order_id=(
                    f"market-close:{position['account_name']}:"
                    f"{position['stock_code']}:{date_key}"
                ),
            )
            results.append(
                RiskEvaluation(
                    account_name=position["account_name"],
                    stock_code=position["stock_code"],
                    current_price=int(price["price"]),
                    highest_price=int(price["price"]),
                    event_type="market_close",
                    threshold_price=None,
                    order_id=execution.internal_order_id,
                )
            )
        return results

    def _evaluate_position(
        self,
        account_name: str,
        stock_code: str,
    ) -> RiskEvaluation:
        price_row = self.paper_engine.get_trade_price(stock_code)
        current_price = int(price_row["price"])
        timestamp = self._now().isoformat(timespec="seconds")
        context = self._market_context(stock_code)

        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT a.id AS account_id, p.quantity, p.cost_basis,
                       r.stop_loss_rate, r.take_profit_rate,
                       r.trailing_activation_rate, r.trailing_drawdown_rate,
                       r.highest_price, r.trailing_active
                FROM paper_accounts a
                JOIN paper_positions p ON p.account_id = a.id
                JOIN paper_risk_profiles r
                  ON r.account_id = p.account_id AND r.stock_code = p.stock_code
                WHERE a.name = ? AND p.stock_code = ?
                """,
                (account_name, stock_code),
            ).fetchone()
            if row is None:
                raise PaperTradingError(
                    f"리스크 평가 대상 포지션이 없습니다: {account_name} {stock_code}"
                )
            trade_plan = connection.execute(
                """
                SELECT tp.*
                FROM trade_plans tp
                JOIN strategy_decisions d ON d.id = tp.decision_id
                WHERE d.account_id = ?
                  AND tp.stock_code = ?
                  AND d.action = 'buy'
                ORDER BY d.decided_at DESC, tp.id DESC
                LIMIT 1
                """,
                (row["account_id"], stock_code),
            ).fetchone()

        trade_plan_id = int(trade_plan["id"]) if trade_plan is not None else None
        persisted_state = self.database.position_exit_state(
            account_id=int(row["account_id"]),
            stock_code=stock_code,
            trade_plan_id=trade_plan_id,
        )
        highest_price = max(
            int(row["highest_price"]),
            int((persisted_state or {}).get("entry_highest_price") or 0),
            current_price,
        )
        recent_higher_low = self._recent_higher_low(
            stock_code,
            prior_value=(persisted_state or {}).get("recent_higher_low"),
        )
        if persisted_state is None:
            persisted_state = {
                "state": "initial_risk",
                "previous_state": None,
                "entry_highest_price": highest_price,
                "recent_higher_low": recent_higher_low,
                "protection_stop_price": (
                    int(trade_plan["stop_loss_price"])
                    if trade_plan is not None
                    else None
                ),
                "vwap_warning_at": None,
                "vwap_warning_count": 0,
                "state_data": {},
            }

        partial = self._maybe_execute_partial_take_profit(
            account_name=account_name,
            stock_code=stock_code,
            row=row,
            trade_plan=trade_plan,
            current_price=current_price,
            timestamp=timestamp,
            price_time=str(price_row["price_time"]),
            persisted_state=persisted_state,
            highest_price=highest_price,
            recent_higher_low=recent_higher_low,
        )
        if partial is not None:
            return partial

        average_cost = Decimal(row["cost_basis"]) / Decimal(row["quantity"])
        plan = evaluate_exit(
            average_cost=average_cost,
            current_price=current_price,
            observed_low=current_price,
            observed_high=current_price,
            highest_price=highest_price,
            trailing_active=bool(row["trailing_active"]),
            context=context,
            settings=replace(
                self.settings,
                stop_loss_rate=Decimal(str(row["stop_loss_rate"])),
                take_profit_rate=Decimal(str(row["take_profit_rate"])),
                take_profit_enabled=(
                    bool(self.settings.take_profit_enabled)
                    and trade_plan is None
                ),
                trailing_activation_rate=Decimal(
                    str(row["trailing_activation_rate"])
                ),
                trailing_drawdown_rate=Decimal(
                    str(row["trailing_drawdown_rate"])
                ),
            ),
            planned_stop_price=(
                int(trade_plan["stop_loss_price"])
                if trade_plan is not None
                and trade_plan["stop_loss_price"] is not None
                else None
            ),
        )
        plan = self._apply_trade_plan_breakeven(
            plan,
            trade_plan=trade_plan,
            current_price=current_price,
        )
        plan = self._apply_state_machine_base_protection(
            plan,
            trade_plan=trade_plan,
            current_price=current_price,
        )
        plan = self._apply_state_machine_structure_stop(
            plan,
            trade_plan=trade_plan,
            current_price=current_price,
            recent_higher_low=recent_higher_low,
        )
        residual_control = self._residual_exit_control(
            plan=plan,
            trade_plan=trade_plan,
            stock_code=stock_code,
            current_price=current_price,
            context=context,
            timestamp=timestamp,
            persisted_state=persisted_state,
        )
        plan = self._apply_residual_exit_target(
            plan,
            trade_plan=trade_plan,
            current_price=current_price,
            residual_control=residual_control,
        )
        plan = self._apply_supply_hard_block_exit_watch(
            plan,
            trade_plan=trade_plan,
            stock_code=stock_code,
            current_price=current_price,
            context=context,
        )
        stop_confirmation = self._early_leader_stop_confirmation(
            plan=plan,
            trade_plan=trade_plan,
            stock_code=stock_code,
            current_price=current_price,
            context=context,
            timestamp=timestamp,
        )
        if stop_confirmation.get("deferred"):
            plan = replace(
                plan,
                expected_exit_reason="stop_loss_confirmation",
                exit_reason=None,
                exit_price=None,
                rule_reason=(
                    f"{plan.rule_reason}, "
                    f"early_leader_stop={stop_confirmation.get('state')}"
                ),
            )
        exit_state_machine = self._exit_state_machine(
            plan=plan,
            trade_plan=trade_plan,
            residual_control=residual_control,
            context=context,
            current_price=current_price,
            timestamp=timestamp,
            persisted_state=persisted_state,
            highest_price=highest_price,
            recent_higher_low=recent_higher_low,
        )
        if exit_state_machine.get("state"):
            plan = replace(
                plan,
                rule_reason=(
                    f"{plan.rule_reason}, "
                    f"exit_state={exit_state_machine['state']}"
                ),
            )
        self._save_persistent_exit_state(
            account_id=int(row["account_id"]),
            stock_code=stock_code,
            trade_plan_id=trade_plan_id,
            state_machine=exit_state_machine,
            persisted_state=persisted_state,
            highest_price=highest_price,
            recent_higher_low=recent_higher_low,
            protection_stop_price=plan.current_stop_price,
            timestamp=timestamp,
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE paper_risk_profiles
                SET highest_price = ?, trailing_active = ?,
                    initial_stop_price = ?, atr_value = ?,
                    atr_stop_price = ?, current_stop_price = ?,
                    current_take_profit_price = ?, breakeven_active = ?,
                    trend_strength = ?, trend_score = ?,
                    current_trailing_drawdown_rate = ?,
                    support_price = ?, volume_ratio = ?,
                    expected_exit_reason = ?, last_rule_reason = ?,
                    updated_at = ?
                WHERE account_id = ? AND stock_code = ?
                """,
                (
                    highest_price,
                    int(plan.trailing_active),
                    plan.initial_stop_price,
                    float(context.atr) if context.atr is not None else None,
                    plan.atr_stop_price,
                    plan.current_stop_price,
                    plan.take_profit_price,
                    int(plan.breakeven_active),
                    plan.trend_strength,
                    plan.trend_score,
                    float(plan.trailing_drawdown_rate),
                    plan.support_price,
                    (
                        float(plan.volume_ratio)
                        if plan.volume_ratio is not None
                        else None
                    ),
                    plan.expected_exit_reason,
                    plan.rule_reason,
                    timestamp,
                    row["account_id"],
                    stock_code,
                ),
            )
            self._store_risk_state(
                connection,
                account_id=int(row["account_id"]),
                stock_code=stock_code,
                current_price=current_price,
                highest_price=highest_price,
                context=context,
                plan=plan,
                timestamp=timestamp,
            )
        if trade_plan is not None:
            self.database.update_partial_exit_state(
                int(trade_plan["id"]),
                current_price=current_price,
                highest_price=highest_price,
                trail_price=plan.trailing_price,
                trailing_active=plan.trailing_active,
                current_zone=self._partial_exit_zone(
                    trade_plan=trade_plan,
                    current_price=current_price,
                    plan=plan,
                ),
                exit_condition=plan.expected_exit_reason or plan.exit_reason,
                state={
                    "current_stop_price": plan.current_stop_price,
                    "take_profit_price": plan.take_profit_price,
                    "residual_control": residual_control,
                    "stop_confirmation": stop_confirmation,
                    "exit_state_machine": exit_state_machine,
                    "trend_strength": plan.trend_strength,
                    "trend_score": plan.trend_score,
                    "rule_reason": plan.rule_reason,
                },
                updated_at=timestamp,
            )

        event_type = plan.exit_reason
        threshold = plan.exit_price

        if event_type is None:
            return RiskEvaluation(
                account_name=account_name,
                stock_code=stock_code,
                current_price=current_price,
                highest_price=highest_price,
                event_type=None,
                threshold_price=None,
                order_id=None,
                initial_stop_price=plan.initial_stop_price,
                current_stop_price=plan.current_stop_price,
                take_profit_price=plan.take_profit_price,
                trailing_active=plan.trailing_active,
                breakeven_active=plan.breakeven_active,
                trend_strength=plan.trend_strength,
                trend_score=plan.trend_score,
                expected_exit_reason=plan.expected_exit_reason,
                exit_state=str(exit_state_machine.get("state") or ""),
                exit_state_machine=exit_state_machine,
            )

        client_order_id = (
            f"risk:{account_name}:{stock_code}:{event_type}:{price_row['price_time']}"
        )
        reason = (
            f"자동 {event_type}: 현재가={current_price:,}원, "
            f"기준가={threshold:,}원, 최고가={highest_price:,}원"
        )
        try:
            result = self._submit_sell(
                account_name,
                stock_code,
                int(row["quantity"]),
                reason=reason,
                event_type=event_type,
                decision_time=datetime.fromisoformat(timestamp),
                client_order_id=client_order_id,
            )
            status = "executed"
            order_id = result.internal_order_id
            message = reason
        except OrderExecutionError as exc:
            status = "failed"
            order_id = None
            message = str(exc)
        realized_pnl = self._sell_realized_pnl(order_id) if order_id else None
        taxonomy = self._classify_exit_event(
            event_type=event_type,
            status=status,
            realized_pnl=realized_pnl,
            plan=plan,
            trade_plan=trade_plan,
            residual_control=residual_control,
            context=context,
            exit_state_machine=exit_state_machine,
        )

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_risk_events(
                    account_id, stock_code, event_type, trigger_price,
                    threshold_price, highest_price, order_id, status,
                    exit_class, exit_reason_class, setup_invalidated,
                    classification_json, message, occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["account_id"],
                    stock_code,
                    event_type,
                    current_price,
                    threshold,
                    highest_price,
                    order_id,
                    status,
                    taxonomy["exit_class"],
                    taxonomy["exit_reason_class"],
                    int(bool(taxonomy["setup_invalidated"])),
                    json.dumps(
                        taxonomy["evidence"],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    message,
                    timestamp,
                ),
            )
        if status == "failed":
            raise PaperTradingError(message)
        if event_type != EXIT_PARTIAL_TAKE_PROFIT:
            lock_reason = (
                "exit_reentry_block:"
                f"{taxonomy['exit_reason_class']}"
            )
            if bool(residual_control.get("force_residual_exit")):
                lock_reason += ":vwap_actual_exit"
            self.database.save_reentry_lock(
                stock_code=stock_code,
                lock_until=_reentry_lock_until(timestamp),
                reason=lock_reason,
                source_decision_id=(
                    int(trade_plan["decision_id"])
                    if trade_plan is not None
                    else None
                ),
                created_at=timestamp,
                setup_version=REENTRY_ENGINE_VERSION,
                setup=taxonomy["evidence"]["reentry_setup"],
            )
        return RiskEvaluation(
            account_name=account_name,
            stock_code=stock_code,
            current_price=current_price,
            highest_price=highest_price,
            event_type=event_type,
            threshold_price=threshold,
            order_id=order_id,
            initial_stop_price=plan.initial_stop_price,
            current_stop_price=plan.current_stop_price,
            take_profit_price=plan.take_profit_price,
            trailing_active=plan.trailing_active,
            breakeven_active=plan.breakeven_active,
            trend_strength=plan.trend_strength,
            trend_score=plan.trend_score,
            expected_exit_reason=plan.expected_exit_reason,
            exit_class=str(taxonomy["exit_class"]),
            exit_reason_class=str(taxonomy["exit_reason_class"]),
            setup_invalidated=bool(taxonomy["setup_invalidated"]),
            exit_state=str(exit_state_machine.get("state") or ""),
            exit_state_machine=exit_state_machine,
        )

    def _maybe_execute_partial_take_profit(
        self,
        *,
        account_name: str,
        stock_code: str,
        row: object,
        trade_plan: object | None,
        current_price: int,
        timestamp: str,
        price_time: str,
        persisted_state: dict[str, Any],
        highest_price: int,
        recent_higher_low: Optional[int],
    ) -> Optional[RiskEvaluation]:
        if trade_plan is None:
            return None
        if int(trade_plan["partial_exit_executed"] or 0):
            return None
        trigger = self._partial_take_profit_trigger(
            trade_plan=trade_plan,
            row=row,
            stock_code=stock_code,
            current_price=current_price,
            timestamp=timestamp,
        )
        if trigger is None:
            return None
        trigger_price = int(trigger["trigger_price"])
        setup = _trade_plan_setup(trade_plan)
        persistent_machine = self._uses_persistent_exit_machine(setup)
        position_quantity = int(row["quantity"])
        partial_exit_rate = Decimal(
            str(
                trigger.get("partial_exit_rate")
                or trade_plan["partial_exit_rate"]
            )
        )
        partial_quantity = int(
            (
                Decimal(position_quantity)
                * partial_exit_rate
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        partial_quantity = min(partial_quantity, position_quantity - 1)
        if partial_quantity < 1:
            return None
        residual_quantity = position_quantity - partial_quantity

        protection_stop = int(trade_plan["stop_loss_price"])
        if (
            persistent_machine
            and recent_higher_low is not None
            and protection_stop < recent_higher_low < current_price
        ):
            protection_stop = recent_higher_low
        reason = (
            "상태 기반 1차 부분익절: "
            f"현재가={current_price:,}원, "
            f"1차익절={trigger_price:,}원, "
            f"잔여보호={protection_stop:,}원"
        )
        try:
            execution = self._submit_sell(
                account_name,
                stock_code,
                partial_quantity,
                reason=reason,
                event_type=EXIT_PARTIAL_TAKE_PROFIT,
                decision_time=datetime.fromisoformat(timestamp),
                client_order_id=(
                    f"plan-partial:{account_name}:{stock_code}:"
                    f"{trade_plan['id']}:{price_time}"
                ),
            )
        except OrderExecutionError as exc:
            raise PaperTradingError(str(exc)) from exc

        self.database.mark_trade_plan_partial_exit(
            int(trade_plan["id"]),
            quantity=partial_quantity,
            order_id=execution.internal_order_id,
            fill_price=execution.fill_price,
            executed_at=timestamp,
            activate_breakeven=not persistent_machine,
        )
        self.database.record_partial_exit_fill(
            int(trade_plan["id"]),
            quantity=partial_quantity,
            order_id=execution.internal_order_id,
            fill_price=execution.fill_price,
            residual_quantity=residual_quantity,
            filled_at=timestamp,
            activate_breakeven=not persistent_machine,
        )
        self.database.save_reentry_lock(
            stock_code=stock_code,
            lock_until=_reentry_lock_until(timestamp),
            reason="partial_take_profit_reentry_block",
            source_decision_id=int(trade_plan["decision_id"]),
            created_at=timestamp,
            setup_version=REENTRY_ENGINE_VERSION,
            setup=self._reentry_exit_setup(
                trade_plan=trade_plan,
                exit_class="partial_take_profit",
                exit_reason_class="partial_take_profit",
                setup_invalidated=False,
                exit_price=current_price,
            ),
        )
        self.database.update_trade_plan_setup(
            int(trade_plan["id"]),
            {
                **(
                    {"adaptive_partial_exit": trigger}
                    if trigger.get("adaptive")
                    else {}
                ),
                "partial_exit_policy": {
                    "first_exit_executed": True,
                    "partial_exit_quantity": partial_quantity,
                    "residual_quantity": residual_quantity,
                    "residual_exit_rule": setup.get("residual_exit_rule"),
                    "first_exit_reason": trigger.get("reason")
                    or "planned_take_profit",
                    "planned_first_take_profit_price": int(
                        trade_plan["first_take_profit_price"]
                    ),
                    "actual_first_exit_trigger_price": trigger_price,
                    "actual_first_exit_rate": str(partial_exit_rate),
                    "post_partial_state": (
                        "trend_runner"
                        if persistent_machine
                        else "partial_profit_locked"
                    ),
                    "protection_stop_price": protection_stop,
                },
                "reentry_policy": {
                    "status": "locked_after_partial_exit",
                    "lock_until": _reentry_lock_until(timestamp),
                    "reason": "1차익절 후 같은 종목 즉시 재진입 금지",
                    "allow_conditions": [
                        "new_high_breakout",
                        "vwap_reclaimed",
                        "leader_score_maintained",
                        "previous_exit_was_planned_take_profit",
                    ],
                },
            },
            updated_at=timestamp,
        )
        breakeven_price = int(trade_plan["breakeven_price"])
        exit_state = (
            "trend_runner" if persistent_machine else "partial_profit_locked"
        )
        exit_state_machine = {
            "version": "v0.12.95" if persistent_machine else "v0.12.86",
            "state": exit_state,
            "phase": "post_partial_exit",
            "previous_state": str(persisted_state.get("state") or "initial_risk"),
            "expected_next_states": [
                "trend_runner",
                "vwap_warning",
                "structure_break_exit",
                "volume_selloff_exit",
                "time_decay_exit",
            ],
            "reason": (
                "first partial profit completed; residual follows higher-low structure"
                if persistent_machine
                else "first partial take profit executed and breakeven protection enabled"
            ),
            "event_type": EXIT_PARTIAL_TAKE_PROFIT,
            "partial_exit_executed": True,
            "breakeven_active": not persistent_machine,
            "trailing_active": not persistent_machine,
            "recent_higher_low": recent_higher_low,
            "protection_stop_price": protection_stop,
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE paper_risk_profiles
                SET highest_price = ?,
                    trailing_active = ?,
                    current_stop_price = MAX(
                        COALESCE(current_stop_price, 0), ?
                    ),
                    breakeven_active = ?,
                    expected_exit_reason = ?,
                    last_rule_reason = ?,
                    updated_at = ?
                WHERE account_id = ? AND stock_code = ?
                """,
                (
                    highest_price,
                    int(not persistent_machine),
                    protection_stop if persistent_machine else breakeven_price,
                    int(not persistent_machine),
                    (
                        "trend_day_structure_runner"
                        if persistent_machine
                        else "trailing_stop"
                    ),
                    (
                        "plan_partial_take_profit, plan_breakeven, "
                        f"exit_state={exit_state}"
                    ),
                    timestamp,
                    row["account_id"],
                    stock_code,
                ),
            )
        self.database.update_partial_exit_state(
            int(trade_plan["id"]),
            current_price=current_price,
            highest_price=highest_price,
            trail_price=None,
            trailing_active=not persistent_machine,
            current_zone=("trend_runner" if persistent_machine else "breakeven_active"),
            exit_condition=(
                "trend_day_structure_runner"
                if persistent_machine
                else "trailing_stop"
            ),
            state={
                "partial_exit_quantity": partial_quantity,
                "residual_quantity": residual_quantity,
                "first_take_profit_price": int(trade_plan["first_take_profit_price"]),
                "actual_first_exit_trigger_price": trigger_price,
                "adaptive_partial_exit": trigger,
                "breakeven_price": breakeven_price,
                "protection_stop_price": protection_stop,
                "exit_state_machine": exit_state_machine,
            },
            updated_at=timestamp,
        )
        self._save_persistent_exit_state(
            account_id=int(row["account_id"]),
            stock_code=stock_code,
            trade_plan_id=int(trade_plan["id"]),
            state_machine=exit_state_machine,
            persisted_state=persisted_state,
            highest_price=highest_price,
            recent_higher_low=recent_higher_low,
            protection_stop_price=protection_stop,
            timestamp=timestamp,
        )
        return RiskEvaluation(
            account_name=account_name,
            stock_code=stock_code,
            current_price=current_price,
            highest_price=highest_price,
            event_type=EXIT_PARTIAL_TAKE_PROFIT,
            threshold_price=trigger_price,
            order_id=execution.internal_order_id,
            current_stop_price=(
                protection_stop if persistent_machine else breakeven_price
            ),
            trailing_active=not persistent_machine,
            breakeven_active=not persistent_machine,
            expected_exit_reason=(
                "trend_day_structure_runner"
                if persistent_machine
                else "trailing_stop"
            ),
            exit_state=exit_state,
            exit_state_machine=exit_state_machine,
        )

    def _partial_take_profit_trigger(
        self,
        *,
        trade_plan: object,
        row: object,
        stock_code: str,
        current_price: int,
        timestamp: str,
    ) -> Optional[dict[str, Any]]:
        first_target = int(trade_plan["first_take_profit_price"])
        setup = _trade_plan_setup(trade_plan)
        if self._uses_persistent_exit_machine(setup):
            entry_price = int(trade_plan["entry_price"])
            stop_price = int(trade_plan["stop_loss_price"])
            risk_per_share = max(1, entry_price - stop_price)
            one_r_price = entry_price + risk_per_share
            highest_price = max(int(row["highest_price"]), current_price)
            stalled = self._first_expansion_stalled(
                stock_code=stock_code,
                entry_price=entry_price,
                risk_per_share=risk_per_share,
                highest_price=highest_price,
                current_price=current_price,
            )
            if current_price < one_r_price and not stalled:
                return None
            playbook_name = str(
                (setup.get("strategy_playbook") or {}).get("name") or ""
            )
            partial_rate = (
                STATE_MACHINE_A_PLUS_PARTIAL_RATE
                if playbook_name == "a_plus_trend_day_runner"
                else STATE_MACHINE_STANDARD_PARTIAL_RATE
            )
            return {
                "trigger_price": current_price if stalled else one_r_price,
                "adaptive": True,
                "reason": (
                    "first_expansion_stall_partial"
                    if stalled and current_price < one_r_price
                    else "one_r_partial"
                ),
                "version": "v0.12.95",
                "entry_price": entry_price,
                "stop_loss_price": stop_price,
                "risk_per_share": risk_per_share,
                "one_r_price": one_r_price,
                "highest_price": highest_price,
                "partial_exit_rate": str(partial_rate),
                "residual_rate": str(Decimal("1") - partial_rate),
                "first_expansion_stalled": stalled,
            }
        if current_price >= first_target:
            return {
                "trigger_price": first_target,
                "adaptive": False,
                "reason": "planned_take_profit",
            }
        supply_policy = setup.get("supply_hard_block_policy") or {}
        if (
            supply_policy.get("status") == "relaxed"
            and supply_policy.get("fast_profit_lock")
        ):
            entry_price = int(trade_plan["entry_price"])
            if entry_price > 0 and first_target > entry_price:
                stop_price = int(trade_plan["stop_loss_price"])
                risk_per_share = max(1, entry_price - stop_price)
                profit_floor = _ceil_price(
                    Decimal(entry_price)
                    * (Decimal("1") + SUPPLY_BLOCK_FAST_PROFIT_RATE)
                )
                r_floor = entry_price + _ceil_price(
                    Decimal(risk_per_share) * SUPPLY_BLOCK_FAST_R_MULTIPLE
                )
                trigger_price = min(first_target, max(profit_floor, r_floor))
                if current_price >= trigger_price:
                    return {
                        "trigger_price": trigger_price,
                        "adaptive": True,
                        "reason": "supply_hard_block_fast_profit_lock",
                        "planned_first_take_profit_price": first_target,
                        "entry_price": entry_price,
                        "stop_loss_price": stop_price,
                        "profit_lock_rate": str(SUPPLY_BLOCK_FAST_PROFIT_RATE),
                        "r_multiple": str(SUPPLY_BLOCK_FAST_R_MULTIPLE),
                        "supply_policy": supply_policy,
                    }
        opening_leader_policy = setup.get("opening_leader_mode")
        aggressive_exit_policy = setup.get("aggressive_exit_policy") or {}
        if (
            isinstance(aggressive_exit_policy, dict)
            and aggressive_exit_policy.get("active")
            and str(aggressive_exit_policy.get("entry_grade") or "") == "A+"
        ):
            entry_price = int(trade_plan["entry_price"])
            if entry_price > 0 and first_target > entry_price:
                stop_price = int(trade_plan["stop_loss_price"])
                risk_per_share = max(1, entry_price - stop_price)
                profit_rate = Decimal(
                    str(
                        aggressive_exit_policy.get("fast_partial_profit_rate")
                        or AGGRESSIVE_A_PLUS_FAST_PROFIT_RATE
                    )
                )
                min_r = Decimal(
                    str(
                        aggressive_exit_policy.get("fast_partial_min_r")
                        or AGGRESSIVE_A_PLUS_MIN_R_MULTIPLE
                    )
                )
                profit_floor = _ceil_price(
                    Decimal(entry_price) * (Decimal("1") + profit_rate)
                )
                r_floor = entry_price + _ceil_price(
                    Decimal(risk_per_share) * min_r
                )
                trigger_price = min(first_target, max(profit_floor, r_floor))
                if current_price >= trigger_price:
                    return {
                        "trigger_price": trigger_price,
                        "adaptive": True,
                        "reason": "aggressive_a_plus_fast_partial_profit",
                        "planned_first_take_profit_price": first_target,
                        "entry_price": entry_price,
                        "stop_loss_price": stop_price,
                        "profit_lock_rate": str(profit_rate),
                        "r_multiple": str(min_r),
                        "aggressive_exit_policy": aggressive_exit_policy,
                    }
        if (
            isinstance(opening_leader_policy, dict)
            and opening_leader_policy.get("active")
            and self._is_early_leader_trade(
                trade_plan=trade_plan,
                setup=setup,
                stock_code=stock_code,
                timestamp=timestamp,
            )
        ):
            entry_price = int(trade_plan["entry_price"])
            if entry_price > 0 and first_target > entry_price:
                stop_price = int(trade_plan["stop_loss_price"])
                risk_per_share = max(1, entry_price - stop_price)
                profit_floor = _ceil_price(
                    Decimal(entry_price)
                    * (Decimal("1") + OPENING_LEADER_FAST_PROFIT_RATE)
                )
                r_floor = entry_price + _ceil_price(
                    Decimal(risk_per_share) * OPENING_LEADER_MIN_R_MULTIPLE
                )
                trigger_price = min(first_target, max(profit_floor, r_floor))
                if current_price >= trigger_price:
                    return {
                        "trigger_price": trigger_price,
                        "adaptive": True,
                        "reason": "opening_leader_fast_partial_profit",
                        "planned_first_take_profit_price": first_target,
                        "entry_price": entry_price,
                        "stop_loss_price": stop_price,
                        "profit_lock_rate": str(OPENING_LEADER_FAST_PROFIT_RATE),
                        "r_multiple": str(OPENING_LEADER_MIN_R_MULTIPLE),
                        "opening_leader_mode": opening_leader_policy,
                    }
        if not self._is_early_leader_trade(
            trade_plan=trade_plan,
            setup=setup,
            stock_code=stock_code,
            timestamp=timestamp,
        ):
            return None
        entry_price = int(trade_plan["entry_price"])
        if entry_price <= 0 or first_target <= entry_price:
            return None
        target_rate = Decimal(first_target) / Decimal(entry_price) - Decimal("1")
        if target_rate < ADAPTIVE_LEADER_WIDE_TARGET_RATE:
            return None
        stop_price = int(trade_plan["stop_loss_price"])
        risk_per_share = max(1, entry_price - stop_price)
        profit_floor = _ceil_price(
            Decimal(entry_price)
            * (Decimal("1") + ADAPTIVE_LEADER_PARTIAL_PROFIT_RATE)
        )
        r_floor = entry_price + _ceil_price(
            Decimal(risk_per_share) * ADAPTIVE_LEADER_MIN_R_MULTIPLE
        )
        trigger_price = min(first_target, max(profit_floor, r_floor))
        if current_price < trigger_price:
            return None
        return {
            "trigger_price": trigger_price,
            "adaptive": True,
            "reason": "adaptive_leader_profit_lock",
            "planned_first_take_profit_price": first_target,
            "entry_price": entry_price,
            "stop_loss_price": stop_price,
            "target_rate": str(target_rate),
            "profit_lock_rate": str(ADAPTIVE_LEADER_PARTIAL_PROFIT_RATE),
            "r_multiple": str(ADAPTIVE_LEADER_MIN_R_MULTIPLE),
        }

    @staticmethod
    def _uses_persistent_exit_machine(setup: dict[str, Any]) -> bool:
        playbook = str((setup.get("strategy_playbook") or {}).get("name") or "")
        return bool(
            playbook
            in {
                "a_plus_trend_day_runner",
                "vwap_pullback_reentry",
                "new_high_rebreakout_reentry_probe",
            }
            or str(setup.get("residual_exit_rule") or "")
            == "structure_break_runner"
        )

    def _first_expansion_stalled(
        self,
        *,
        stock_code: str,
        entry_price: int,
        risk_per_share: int,
        highest_price: int,
        current_price: int,
    ) -> bool:
        expansion_price = entry_price + _ceil_price(
            Decimal(risk_per_share) * STATE_MACHINE_EXPANSION_R_MULTIPLE
        )
        pullback_size = _ceil_price(
            Decimal(risk_per_share) * STATE_MACHINE_STALL_PULLBACK_R_MULTIPLE
        )
        if highest_price < expansion_price or current_price > highest_price - pullback_size:
            return False
        bars = self._recent_minute_bars(stock_code, limit=4)
        if len(bars) < 2:
            return False
        latest_close = _float_or_none(bars[-1].get("close_price"))
        previous_close = _float_or_none(bars[-2].get("close_price"))
        latest_high = _float_or_none(bars[-1].get("high_price"))
        previous_high = _float_or_none(bars[-2].get("high_price"))
        return bool(
            latest_close is not None
            and previous_close is not None
            and latest_close <= previous_close
            and latest_high is not None
            and previous_high is not None
            and latest_high <= previous_high
        )

    def _early_leader_stop_confirmation(
        self,
        *,
        plan: ExitPlan,
        trade_plan: object | None,
        stock_code: str,
        current_price: int,
        context: MarketExitContext,
        timestamp: str,
    ) -> dict[str, Any]:
        if trade_plan is None:
            return {}
        setup = _trade_plan_setup(trade_plan)
        previous = setup.get("early_leader_stop_state")
        previous_state = previous if isinstance(previous, dict) else {}
        if plan.exit_reason != EXIT_STOP_LOSS:
            if previous_state.get("active"):
                self.database.update_trade_plan_setup(
                    int(trade_plan["id"]),
                    {
                        "early_leader_stop_state": {
                            **previous_state,
                            "active": False,
                            "state": "recovered",
                            "recovered_at": timestamp,
                            "last_price": current_price,
                        }
                    },
                    updated_at=timestamp,
                )
            return {}
        if int(trade_plan["partial_exit_executed"] or 0):
            return {}
        if not self._is_early_leader_trade(
            trade_plan=trade_plan,
            setup=setup,
            stock_code=stock_code,
            timestamp=timestamp,
        ):
            return {}
        emergency_price = _floor_price(
            Decimal(plan.current_stop_price)
            * (Decimal("1") - EARLY_LEADER_EMERGENCY_STOP_RATE)
        )
        evidence = self._early_leader_stop_evidence(
            stock_code=stock_code,
            current_price=current_price,
            context=context,
            setup=setup,
        )
        previous_count = int(previous_state.get("below_stop_count") or 0)
        below_count = previous_count + 1
        emergency = current_price <= emergency_price
        confirmed = emergency or evidence["actual_breakdown"] or below_count >= 2
        state = (
            "emergency_stop"
            if emergency
            else ("confirmed_breakdown" if confirmed else "confirmation_pending")
        )
        stop_state = {
            "active": not confirmed,
            "state": state,
            "below_stop_count": below_count,
            "last_price": current_price,
            "stop_price": plan.current_stop_price,
            "emergency_price": emergency_price,
            "checked_at": timestamp,
            "evidence": evidence,
        }
        self.database.update_trade_plan_setup(
            int(trade_plan["id"]),
            {"early_leader_stop_state": stop_state},
            updated_at=timestamp,
        )
        return {
            "deferred": not confirmed,
            **stop_state,
        }

    def _is_early_leader_trade(
        self,
        *,
        trade_plan: object,
        setup: dict[str, Any],
        stock_code: str,
        timestamp: str,
    ) -> bool:
        try:
            current_time = datetime.fromisoformat(timestamp).time().replace(tzinfo=None)
        except ValueError:
            current_time = self._now().time().replace(tzinfo=None)
        if current_time >= EARLY_LEADER_STOP_CONFIRMATION_CUTOFF:
            return False
        entry_type = str(trade_plan["entry_type"] or "")
        if entry_type not in LEADER_ENTRY_TYPES:
            return False
        entry_policy = setup.get("entry_type_policy")
        entry_grade = ""
        if isinstance(entry_policy, dict):
            entry_grade = str(entry_policy.get("entry_grade") or "")
        intraday_mode = setup.get("intraday_strategy_mode")
        mode_name = str(
            setup.get("strategy_mode")
            or (
                intraday_mode.get("name")
                if isinstance(intraday_mode, dict)
                else ""
            )
            or ""
        )
        leader = self._latest_leader_score(stock_code)
        leader_role = str((leader or {}).get("role") or "")
        leader_score = _float_or_none((leader or {}).get("total_score"))
        leader_ok = (
            leader_role in {"leader", "second"}
            and (leader_score is None or leader_score >= 65.0)
            and not bool((leader or {}).get("blocked"))
        )
        mode_ok = mode_name in {
            "opening_leader_core",
            "opening_leader_active",
            "morning_active",
            "morning_aggressive",
            "aggressive_leader",
            "verified_leader",
        }
        return (
            entry_grade == "A"
            or bool(setup.get("leader_mode_pass"))
            or mode_ok
            or leader_ok
        )

    def _early_leader_stop_evidence(
        self,
        *,
        stock_code: str,
        current_price: int,
        context: MarketExitContext,
        setup: dict[str, Any],
    ) -> dict[str, Any]:
        intraday = self._latest_intraday_pattern(stock_code)
        supply = self._latest_supply_score(stock_code)
        leader = self._latest_leader_score(stock_code)
        trade_strength = self._latest_trade_strength(stock_code)
        vwap = _float_or_none((intraday or {}).get("vwap"))
        if vwap is None:
            vwap = _float_or_none(setup.get("vwap"))
        moving_average = (
            context.moving_average
            if context.moving_average is not None
            else _float_or_none(setup.get("moving_average"))
        )
        opening_range_low = _float_or_none(setup.get("opening_range_low"))
        below_vwap = vwap is not None and current_price < vwap
        below_ma = (
            moving_average is not None
            and Decimal(current_price) < Decimal(str(moving_average))
        )
        below_orb_low = (
            opening_range_low is not None and current_price < opening_range_low
        )
        bars = self._recent_minute_bars(stock_code, limit=5)
        below_vwap_bars = 0
        if vwap is not None:
            for bar in reversed(bars):
                close_price = _float_or_none(bar.get("close_price"))
                if close_price is None or close_price >= vwap:
                    break
                below_vwap_bars += 1
        trade_strength_value = _float_or_none((trade_strength or {}).get("strength"))
        flow_weak = (
            bool((supply or {}).get("blocked"))
            or bool((leader or {}).get("blocked"))
            or (trade_strength_value is not None and trade_strength_value < 95.0)
        )
        structure_lost = below_orb_low or below_vwap or below_ma
        actual_breakdown = bool(
            structure_lost
            and (
                below_vwap_bars >= 2
                or flow_weak
                or context.trend_strength == "weak"
            )
        )
        return {
            "actual_breakdown": actual_breakdown,
            "structure_lost": structure_lost,
            "below_vwap": below_vwap,
            "below_vwap_bars": below_vwap_bars,
            "below_moving_average": below_ma,
            "below_opening_range_low": below_orb_low,
            "flow_weak": flow_weak,
            "trend_strength": context.trend_strength,
            "trade_strength": trade_strength_value,
            "supply_blocked": bool((supply or {}).get("blocked")),
            "leader_blocked": bool((leader or {}).get("blocked")),
        }

    @staticmethod
    def _apply_trade_plan_breakeven(
        plan: ExitPlan,
        *,
        trade_plan: object | None,
        current_price: int,
    ) -> ExitPlan:
        if trade_plan is None or not int(trade_plan["breakeven_activated"] or 0):
            return plan
        breakeven_price = int(trade_plan["breakeven_price"])
        current_stop = max(plan.current_stop_price, breakeven_price)
        rule_reason = f"{plan.rule_reason}, plan_breakeven={breakeven_price}"
        if current_price <= current_stop:
            return replace(
                plan,
                current_stop_price=current_stop,
                breakeven_active=True,
                expected_exit_reason=None,
                exit_reason=EXIT_STOP_LOSS,
                exit_price=current_stop,
                rule_reason=rule_reason,
            )
        return replace(
            plan,
            current_stop_price=current_stop,
            breakeven_active=True,
            rule_reason=rule_reason,
        )

    @classmethod
    def _apply_state_machine_base_protection(
        cls,
        plan: ExitPlan,
        *,
        trade_plan: object | None,
        current_price: int,
    ) -> ExitPlan:
        if trade_plan is None:
            return plan
        setup = _trade_plan_setup(trade_plan)
        if not cls._uses_persistent_exit_machine(setup):
            return plan
        partial_executed = bool(int(trade_plan["partial_exit_executed"] or 0))
        initial_stop = int(trade_plan["stop_loss_price"])
        rule_reason = (
            f"{plan.rule_reason}, state_machine_base_stop={initial_stop}, "
            "percentage_protection=disabled"
        )
        if current_price <= initial_stop:
            return replace(
                plan,
                current_stop_price=initial_stop,
                trailing_price=None,
                trailing_active=False,
                breakeven_active=False,
                expected_exit_reason=None,
                exit_reason=EXIT_STOP_LOSS,
                exit_price=initial_stop,
                rule_reason=rule_reason,
            )
        return replace(
            plan,
            current_stop_price=initial_stop,
            trailing_price=None,
            trailing_active=False,
            breakeven_active=False,
            expected_exit_reason=(
                "trend_day_structure_runner"
                if partial_executed
                else "initial_structure_stop"
            ),
            exit_reason=None,
            exit_price=None,
            rule_reason=rule_reason,
        )

    @classmethod
    def _apply_state_machine_structure_stop(
        cls,
        plan: ExitPlan,
        *,
        trade_plan: object | None,
        current_price: int,
        recent_higher_low: Optional[int],
    ) -> ExitPlan:
        if trade_plan is None or not int(trade_plan["partial_exit_executed"] or 0):
            return plan
        setup = _trade_plan_setup(trade_plan)
        if not cls._uses_persistent_exit_machine(setup):
            return plan
        if recent_higher_low is None or recent_higher_low <= plan.current_stop_price:
            return replace(plan, breakeven_active=False)
        current_stop = recent_higher_low
        rule_reason = (
            f"{plan.rule_reason}, higher_low_protection={current_stop}"
        )
        if current_price <= current_stop:
            return replace(
                plan,
                current_stop_price=current_stop,
                breakeven_active=False,
                expected_exit_reason=None,
                exit_reason=EXIT_STOP_LOSS,
                exit_price=current_stop,
                rule_reason=rule_reason,
            )
        return replace(
            plan,
            current_stop_price=current_stop,
            breakeven_active=False,
            rule_reason=rule_reason,
        )

    def _apply_residual_exit_target(
        self,
        plan: ExitPlan,
        *,
        trade_plan: object | None,
        current_price: int,
        residual_control: Optional[dict[str, Any]] = None,
    ) -> ExitPlan:
        if trade_plan is None or not int(trade_plan["partial_exit_executed"] or 0):
            return plan
        setup = _trade_plan_setup(trade_plan)
        residual_rule = str(setup.get("residual_exit_rule") or "")
        if residual_rule not in {
            "hold_to_second_target",
            "prior_high_or_trailing",
            "structure_break_runner",
        }:
            return plan
        second_target = (residual_control or {}).get("target")
        if second_target is None:
            second_target = setup.get("adjusted_second_take_profit_price")
        if second_target is None:
            second_target = setup.get("second_take_profit_price")
        if second_target is None and residual_rule == "prior_high_or_trailing":
            second_target = setup.get("previous_high")
        if bool((residual_control or {}).get("force_residual_exit")):
            evidence = (residual_control or {}).get("evidence") or {}
            exit_reason = evidence.get("aggressive_exit_reason") or (
                "vwap_actual_breakdown"
            )
            return replace(
                plan,
                expected_exit_reason=None,
                exit_reason=EXIT_STOP_LOSS,
                exit_price=current_price,
                rule_reason=f"{plan.rule_reason}, {exit_reason}",
            )
        if bool((residual_control or {}).get("defer_protection_exit")):
            evidence = (residual_control or {}).get("evidence") or {}
            return replace(
                plan,
                expected_exit_reason="residual_structure_hold",
                exit_reason=None,
                exit_price=None,
                rule_reason=(
                    f"{plan.rule_reason}, residual_structure_hold:"
                    f"{evidence.get('deferred_exit_reason')}"
                ),
            )
        tight_trailing_stop = (residual_control or {}).get("tight_trailing_stop")
        if tight_trailing_stop is not None:
            tightened_stop = max(plan.current_stop_price, int(tight_trailing_stop))
            if current_price <= tightened_stop:
                return replace(
                    plan,
                    current_stop_price=tightened_stop,
                    expected_exit_reason=None,
                    exit_reason=EXIT_TRAILING_STOP,
                    exit_price=tightened_stop,
                    rule_reason=(
                        f"{plan.rule_reason}, aggressive_tight_trailing_stop="
                        f"{tightened_stop}"
                    ),
                )
            plan = replace(
                plan,
                current_stop_price=tightened_stop,
                expected_exit_reason=EXIT_TRAILING_STOP,
                rule_reason=(
                    f"{plan.rule_reason}, aggressive_tight_trailing_stop="
                    f"{tightened_stop}"
                ),
            )
        if second_target is None:
            return plan
        if residual_rule == "structure_break_runner":
            return replace(
                plan,
                expected_exit_reason="trend_day_structure_runner",
                rule_reason=f"{plan.rule_reason}, trend_day_structure_runner",
            )
        target = int(float(second_target))
        defer_second_target = bool((residual_control or {}).get("defer_second_target"))
        if target > 0 and current_price >= target:
            if defer_second_target:
                return replace(
                    plan,
                    expected_exit_reason=EXIT_TRAILING_STOP,
                    rule_reason=(
                        f"{plan.rule_reason}, residual_target={target}, "
                        "second_target_deferred"
                    ),
                )
            return replace(
                plan,
                expected_exit_reason=None,
                exit_reason=EXIT_TAKE_PROFIT,
                exit_price=target,
                rule_reason=f"{plan.rule_reason}, residual_target={target}",
            )
        return replace(
            plan,
            expected_exit_reason=plan.expected_exit_reason or EXIT_TAKE_PROFIT,
            rule_reason=f"{plan.rule_reason}, residual_target={target}",
        )

    def _classify_exit_event(
        self,
        *,
        event_type: str,
        status: str,
        realized_pnl: Optional[int],
        plan: ExitPlan,
        trade_plan: object | None,
        residual_control: dict[str, Any],
        context: MarketExitContext,
        exit_state_machine: dict[str, Any],
    ) -> dict[str, Any]:
        setup = _trade_plan_setup(trade_plan) if trade_plan is not None else {}
        plan_text = ""
        if trade_plan is not None:
            plan_text = " ".join(
                str(trade_plan[name] or "")
                for name in ("stop_loss_reason", "invalidation_reason")
                if name in trade_plan.keys()
            )
        plan_text_lower = plan_text.lower()
        exit_class = _risk_exit_class(event_type, realized_pnl)
        evidence = residual_control.get("evidence") or {}
        evidence = evidence if isinstance(evidence, dict) else {}
        rule_reason = plan.rule_reason or ""
        force_residual_exit = bool(residual_control.get("force_residual_exit"))
        supply_evidence = bool(
            "supply_confirmed_net_sell_fast_exit" in rule_reason
            or evidence.get("supply_blocked")
            or evidence.get("flow_weak")
            and (
                evidence.get("supply_blocked")
                or setup.get("supply_hard_block_policy", {}).get("status")
                == "relaxed"
            )
        )
        structure_evidence = bool(
            force_residual_exit
            or "vwap_actual_breakdown" in rule_reason
            or "structure_break" in rule_reason
            or evidence.get("structure_lost")
            or evidence.get("below_vwap")
            or evidence.get("below_moving_average")
            or evidence.get("below_opening_range_low")
            or any(
                token in plan_text_lower
                for token in (
                    "vwap",
                    "orb",
                    "support",
                    "structure",
                    "breakdown",
                    "invalidation",
                )
            )
            or any(token in plan_text for token in ("지지", "이탈", "구조", "무효"))
        )
        if status != "executed":
            exit_reason_class = "system_or_fill_error"
            setup_invalidated = False
        elif event_type == EXIT_STOP_LOSS:
            if supply_evidence:
                exit_reason_class = "news_or_supply_stop"
                setup_invalidated = True
            elif structure_evidence:
                exit_reason_class = "structure_loss_stop"
                setup_invalidated = True
            elif exit_class == "profit_protect_stop" or plan.breakeven_active:
                exit_reason_class = "profit_protect_stop"
                setup_invalidated = False
            else:
                exit_reason_class = "noise_stop"
                setup_invalidated = context.trend_strength == "weak"
        elif event_type == EXIT_TRAILING_STOP:
            exit_reason_class = exit_class
            setup_invalidated = exit_class == "trailing_loss_stop"
        elif event_type == EXIT_TAKE_PROFIT:
            exit_reason_class = "take_profit"
            setup_invalidated = False
        elif event_type == EXIT_PARTIAL_TAKE_PROFIT:
            exit_reason_class = "partial_take_profit"
            setup_invalidated = False
        else:
            exit_reason_class = event_type or "unknown"
            setup_invalidated = False
        return {
            "exit_class": exit_class,
            "exit_reason_class": exit_reason_class,
            "setup_invalidated": bool(setup_invalidated),
            "evidence": {
                "event_type": event_type,
                "realized_pnl": realized_pnl,
                "status": status,
                "breakeven_active": bool(plan.breakeven_active),
                "trailing_active": bool(plan.trailing_active),
                "trend_strength": context.trend_strength,
                "trend_score": context.trend_score,
                "structure_evidence": structure_evidence,
                "supply_evidence": supply_evidence,
                "force_residual_exit": force_residual_exit,
                "exit_state_machine": exit_state_machine,
                "reentry_setup": self._reentry_exit_setup(
                    trade_plan=trade_plan,
                    exit_class=exit_class,
                    exit_reason_class=exit_reason_class,
                    setup_invalidated=bool(setup_invalidated),
                    exit_price=plan.exit_price,
                ),
                "rule_reason": rule_reason,
            },
        }

    @staticmethod
    def _reentry_exit_setup(
        *,
        trade_plan: object | None,
        exit_class: str,
        exit_reason_class: str,
        setup_invalidated: bool,
        exit_price: Optional[int],
    ) -> dict[str, Any]:
        setup = _trade_plan_setup(trade_plan) if trade_plan is not None else {}
        levels = {
            name: setup.get(name)
            for name in (
                "breakout_level",
                "opening_range_high",
                "vwap",
                "previous_high",
                "intraday_high",
                "pullback_support_level",
            )
            if setup.get(name) is not None
        }
        if trade_plan is not None:
            levels.update(
                {
                    "entry_price": int(trade_plan["entry_price"]),
                    "stop_loss_price": int(trade_plan["stop_loss_price"]),
                }
            )
        return {
            "version": REENTRY_ENGINE_VERSION,
            "exit_class": exit_class,
            "exit_reason_class": exit_reason_class,
            "setup_invalidated": bool(setup_invalidated),
            "exit_price": exit_price,
            "entry_type": (
                str(trade_plan["entry_type"])
                if trade_plan is not None
                else None
            ),
            "strategy_playbook": (setup.get("strategy_playbook") or {}).get("name"),
            "levels": levels,
            "required_reentry_conditions": [
                "vwap_reanchored",
                "new_high_breakout",
                "volume_recovered",
                "supply_maintained",
                "reward_risk_passed",
                "same_level_failure_passed",
            ],
        }

    def _exit_state_machine(
        self,
        *,
        plan: ExitPlan,
        trade_plan: object | None,
        residual_control: dict[str, Any],
        context: MarketExitContext,
        current_price: int,
        timestamp: str,
        persisted_state: Optional[dict[str, Any]] = None,
        highest_price: Optional[int] = None,
        recent_higher_low: Optional[int] = None,
    ) -> dict[str, Any]:
        setup = _trade_plan_setup(trade_plan) if trade_plan is not None else {}
        evidence = residual_control.get("evidence") or {}
        evidence = evidence if isinstance(evidence, dict) else {}
        vwap_state = evidence.get("vwap_exit_state") or {}
        vwap_state = vwap_state if isinstance(vwap_state, dict) else {}
        partial_executed = bool(
            trade_plan is not None and int(trade_plan["partial_exit_executed"] or 0)
        )
        runner_active = bool(
            self._uses_persistent_exit_machine(setup)
            or plan.expected_exit_reason == "trend_day_structure_runner"
        )
        rule_reason = plan.rule_reason or ""
        prior_state = str(
            (persisted_state or {}).get("state")
            or ("partial_profit_locked" if partial_executed else "initial_risk")
        )
        try:
            current_time = datetime.fromisoformat(timestamp).time().replace(tzinfo=None)
        except ValueError:
            current_time = self._now().time().replace(tzinfo=None)
        near_time_decay = bool(
            current_time >= SECOND_TARGET_DELAY_CUTOFF
            or evidence.get("near_close")
            or evidence.get("after_midday_fade_cutoff")
        )
        volume_selloff = bool(
            evidence.get("high_volume_bearish")
            or "volume_selloff" in rule_reason
            or "volume_dry_up" in rule_reason
            or plan.exit_reason == "volume_dry_up"
        )
        structure_break = bool(
            residual_control.get("force_residual_exit")
            or evidence.get("structure_break_confirmed")
            or "vwap_actual_breakdown" in rule_reason
            or "structure_break_exit" in rule_reason
            or "aggressive_structure_break_exit" in rule_reason
        )
        warning_signal = (
            evidence.get("latest_close_below_vwap")
            if runner_active
            else (
                evidence.get("below_vwap")
                or vwap_state.get("state") in {"watch", "shakeout_recovered"}
                or plan.expected_exit_reason == "residual_structure_hold"
            )
        )
        vwap_warning = bool(not structure_break and warning_signal)

        state = "initial_risk"
        reason = "position is before first partial profit and protected by initial stop"
        if plan.exit_reason is not None:
            if volume_selloff:
                state = "volume_selloff_exit"
                reason = "high-volume bearish bar or volume selloff triggered exit"
            elif structure_break:
                state = "structure_break_exit"
                reason = "VWAP, prior low, moving average, or structure break triggered exit"
            elif near_time_decay and partial_executed:
                state = "time_decay_exit"
                reason = "late-session or post-runner time decay triggered exit"
            elif partial_executed or plan.breakeven_active or plan.trailing_active:
                state = "partial_profit_locked"
                reason = "profit protection exit after breakeven or trailing lock"
            else:
                state = "initial_risk"
                reason = "initial risk exit before profit protection"
        elif near_time_decay and partial_executed and not runner_active:
            state = "time_decay_exit"
            reason = "late-session fade watch after partial profit"
        elif vwap_warning:
            state = "vwap_warning"
            reason = "VWAP warning without confirmed structure break"
        elif runner_active and partial_executed:
            state = "trend_runner"
            reason = "partial profit is locked and runner structure is still valid"
        elif partial_executed or plan.breakeven_active or plan.trailing_active:
            state = "partial_profit_locked"
            reason = "profit is protected while waiting for structure or runner signal"

        if state == "initial_risk":
            expected_next_states = [
                "partial_profit_locked",
                "structure_break_exit",
            ]
        elif state == "partial_profit_locked":
            expected_next_states = [
                "trend_runner",
                "vwap_warning",
                "structure_break_exit",
                "volume_selloff_exit",
                "time_decay_exit",
            ]
        elif state == "trend_runner":
            expected_next_states = [
                "vwap_warning",
                "structure_break_exit",
                "volume_selloff_exit",
                "time_decay_exit",
            ]
        elif state == "vwap_warning":
            expected_next_states = [
                "trend_runner",
                "structure_break_exit",
                "volume_selloff_exit",
            ]
        else:
            expected_next_states = []

        return {
            "version": (
                "v0.12.95"
                if self._uses_persistent_exit_machine(setup)
                else "v0.12.86"
            ),
            "state": state,
            "previous_state": prior_state,
            "expected_next_states": expected_next_states,
            "reason": reason,
            "event_type": plan.exit_reason,
            "expected_exit_reason": plan.expected_exit_reason,
            "partial_exit_executed": partial_executed,
            "runner_active": runner_active,
            "breakeven_active": bool(plan.breakeven_active),
            "trailing_active": bool(plan.trailing_active),
            "trend_strength": context.trend_strength,
            "trend_score": context.trend_score,
            "current_price": current_price,
            "entry_highest_price": highest_price,
            "recent_higher_low": recent_higher_low,
            "current_stop_price": plan.current_stop_price,
            "take_profit_price": plan.take_profit_price,
            "structure_break_confirmed": bool(
                evidence.get("structure_break_confirmed")
            ),
            "below_vwap": bool(evidence.get("below_vwap")),
            "high_volume_bearish": bool(evidence.get("high_volume_bearish")),
            "vwap_exit_state": vwap_state.get("state"),
            "vwap_warning_bar_time": evidence.get("latest_bar_time"),
            "near_time_decay": near_time_decay,
        }

    def _apply_supply_hard_block_exit_watch(
        self,
        plan: ExitPlan,
        *,
        trade_plan: object | None,
        stock_code: str,
        current_price: int,
        context: MarketExitContext,
    ) -> ExitPlan:
        if trade_plan is None:
            return plan
        setup = _trade_plan_setup(trade_plan)
        supply_policy = setup.get("supply_hard_block_policy") or {}
        if supply_policy.get("status") != "relaxed":
            return plan
        supply = self._latest_supply_score(stock_code)
        confirmed_net_sell = _confirmed_supply_net_sell(supply)
        entry_price = int(trade_plan["entry_price"])
        intraday = self._latest_intraday_pattern(stock_code)
        vwap = _float_or_none((intraday or {}).get("vwap"))
        trade_strength = self._latest_trade_strength(stock_code)
        trade_strength_value = _float_or_none((trade_strength or {}).get("strength"))
        below_vwap = vwap is not None and current_price < vwap
        below_ma = (
            context.moving_average is not None
            and Decimal(current_price) < context.moving_average
        )
        weak_tape = (
            trade_strength_value is not None and trade_strength_value < 95.0
        )
        fast_exit = bool(
            confirmed_net_sell
            and (
                current_price <= entry_price
                or below_vwap
                or below_ma
                or weak_tape
                or context.trend_strength == "weak"
            )
        )
        if fast_exit:
            return replace(
                plan,
                expected_exit_reason=None,
                exit_reason=EXIT_STOP_LOSS,
                exit_price=current_price,
                current_stop_price=max(plan.current_stop_price, current_price),
                rule_reason=(
                    f"{plan.rule_reason}, "
                    "supply_confirmed_net_sell_fast_exit"
                ),
            )
        return replace(
            plan,
            current_stop_price=max(plan.current_stop_price, entry_price),
            expected_exit_reason=(
                plan.expected_exit_reason or "supply_hard_block_watch"
            ),
            rule_reason=f"{plan.rule_reason}, supply_hard_block_watch",
        )

    def _residual_exit_control(
        self,
        *,
        plan: ExitPlan,
        trade_plan: object | None,
        stock_code: str,
        current_price: int,
        context: MarketExitContext,
        timestamp: str,
        persisted_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if trade_plan is None or not int(trade_plan["partial_exit_executed"] or 0):
            return {}
        setup = _trade_plan_setup(trade_plan)
        evidence = self._residual_exit_evidence(
            stock_code=stock_code,
            current_price=current_price,
            context=context,
            setup=setup,
            timestamp=timestamp,
        )
        target = self._current_second_target(trade_plan, setup)
        adjusted_target = setup.get("adjusted_second_take_profit_price")
        if adjusted_target is not None:
            target = int(float(adjusted_target))
        vwap_exit_state = evidence.get("vwap_exit_state") or {}
        force_residual_exit = vwap_exit_state.get("state") == "actual_breakdown"
        runner_active = self._uses_persistent_exit_machine(setup)
        if runner_active:
            prior_state = str((persisted_state or {}).get("state") or "")
            state_data = (persisted_state or {}).get("state_data") or {}
            warning_bar_time = str(state_data.get("vwap_warning_bar_time") or "")
            latest_bar_time = str(evidence.get("latest_bar_time") or "")
            failed_reclaim = bool(
                prior_state == "vwap_warning"
                and evidence.get("latest_close_below_vwap")
                and latest_bar_time
                and warning_bar_time
                and latest_bar_time > warning_bar_time
            )
            bearish_break = bool(
                evidence.get("latest_close_below_vwap")
                and evidence.get("high_volume_bearish")
            )
            force_residual_exit = bool(failed_reclaim or bearish_break)
            evidence["structure_break_confirmed"] = force_residual_exit
            if failed_reclaim or bearish_break:
                evidence["state_machine_failed_vwap_reclaim"] = failed_reclaim
                evidence["state_machine_volume_bearish_break"] = bearish_break
                evidence["vwap_exit_state"] = {
                    **vwap_exit_state,
                    "state": "actual_breakdown",
                    "reason": (
                        "VWAP 경고 다음 5분봉도 재안착 실패"
                        if failed_reclaim
                        else "VWAP 아래 거래량 동반 음봉"
                    ),
                }
                vwap_exit_state = evidence["vwap_exit_state"]
        aggressive_policy = setup.get("aggressive_exit_policy") or {}
        aggressive_active = bool(
            isinstance(aggressive_policy, dict)
            and aggressive_policy.get("active")
        )
        aggressive_structure_break = bool(
            aggressive_active
            and (
                force_residual_exit
                or not runner_active
                and evidence.get("structure_lost")
                and (
                    not evidence.get("flow_ok")
                    or not evidence.get("trend_ok")
                    or evidence.get("below_previous_low")
                )
            )
        )
        aggressive_midday_fade = bool(
            aggressive_active
            and not runner_active
            and evidence.get("after_midday_fade_cutoff")
            and not evidence.get("flow_ok")
            and not evidence.get("trend_ok")
        )
        if aggressive_structure_break or aggressive_midday_fade:
            force_residual_exit = True
            evidence["aggressive_exit_reason"] = (
                "aggressive_midday_fade_exit"
                if aggressive_midday_fade
                else "aggressive_structure_break_exit"
            )
        hard_initial_stop_hit = (
            plan.exit_reason == EXIT_STOP_LOSS
            and plan.exit_price is not None
            and int(trade_plan["stop_loss_price"] or 0) > 0
            and int(plan.exit_price) <= int(trade_plan["stop_loss_price"])
        )
        residual_protection_exit = (
            plan.exit_reason in {EXIT_STOP_LOSS, EXIT_TRAILING_STOP}
            and not hard_initial_stop_hit
        )
        if (
            residual_protection_exit
            and not force_residual_exit
            and evidence.get("structure_evidence_available")
            and not evidence.get("structure_break_confirmed")
        ):
            evidence["residual_protection_deferred"] = True
            evidence["deferred_exit_reason"] = plan.exit_reason
            evidence["deferred_exit_price"] = plan.exit_price
            return {
                "target": target,
                "defer_second_target": False,
                "force_residual_exit": False,
                "tight_trailing_stop": None,
                "evidence": evidence,
                "defer_protection_exit": True,
            }
        tight_trailing_stop = None
        if (
            aggressive_active
            and not runner_active
            and not force_residual_exit
            and (
                evidence.get("after_midday_fade_cutoff")
                or not evidence.get("vwap_support")
                or not evidence.get("trend_ok")
            )
        ):
            tight_trailing_stop = _floor_price(
                Decimal(current_price)
                * (Decimal("1") - AGGRESSIVE_TIGHT_TRAILING_RATE)
            )
            evidence["aggressive_tight_trailing_stop"] = tight_trailing_stop
        if (
            force_residual_exit
            and not self._partial_adjustment_exists(
                int(trade_plan["id"]),
                "vwap_actual_exit",
            )
        ):
            self.database.record_partial_exit_adjustment(
                int(trade_plan["id"]),
                adjustment_type="vwap_actual_exit",
                old_target=int(target) if target is not None else None,
                new_target=None,
                reason="VWAP 실제 이탈로 잔여 청산 후보",
                evidence=evidence,
                created_at=timestamp,
            )

        can_raise = (
            target is not None
            and evidence["vwap_support"]
            and evidence["leader_strength"]
            and evidence["flow_ok"]
            and evidence["trend_ok"]
            and not force_residual_exit
        )
        if (
            can_raise
            and adjusted_target is None
            and not self._partial_adjustment_exists(
                int(trade_plan["id"]),
                "raise_residual_target",
            )
        ):
            old_target = int(target)
            new_target = max(
                old_target + 1,
                _ceil_price(Decimal(old_target) * Decimal("1.015")),
                _ceil_price(Decimal(current_price) * Decimal("1.008")),
            )
            reason = "1차익절 후 VWAP·대장주 강도 유지로 잔여 목표 약한 상향"
            self.database.record_partial_exit_adjustment(
                int(trade_plan["id"]),
                adjustment_type="raise_residual_target",
                old_target=old_target,
                new_target=new_target,
                reason=reason,
                evidence=evidence,
                created_at=timestamp,
            )
            target = new_target
            setup["adjusted_second_take_profit_price"] = new_target
            evidence["raised_target"] = new_target

        defer = (
            target is not None
            and current_price >= int(target)
            and evidence["leader_delay_ok"]
            and evidence["mode_delay_ok"]
            and evidence["flow_ok"]
            and not force_residual_exit
            and not evidence["climax_risk"]
            and not evidence["near_close"]
        )
        if (
            defer
            and not self._partial_adjustment_exists(
                int(trade_plan["id"]),
                "delay_second_take_profit",
            )
        ):
            self.database.record_partial_exit_adjustment(
                int(trade_plan["id"]),
                adjustment_type="delay_second_take_profit",
                old_target=int(target),
                new_target=int(target),
                reason="대장주 강도 유지로 2차익절 대신 trailing 우선",
                evidence=evidence,
                created_at=timestamp,
            )
        self.database.update_trade_plan_setup(
            int(trade_plan["id"]),
            {
                "partial_exit_policy": {
                    "first_exit_executed": True,
                    "residual_exit_rule": setup.get("residual_exit_rule"),
                    "current_residual_target": target,
                    "second_target_deferred": defer,
                },
                "residual_target_adjustment": {
                    "target": target,
                    "defer_second_target": defer,
                    "raised_target": evidence.get("raised_target"),
                },
                "vwap_exit_state": vwap_exit_state,
                "reentry_policy": {
                    "status": (
                        "blocked_by_vwap_actual_exit"
                        if force_residual_exit
                        else "locked_after_partial_exit"
                    ),
                    "reason": (
                        "VWAP 실제 이탈"
                        if force_residual_exit
                        else "1차익절 후 같은 종목 즉시 재진입 금지"
                    ),
                    "lock_until": _reentry_lock_until(timestamp),
                    "allow_conditions": [
                        "new_high_breakout",
                        "vwap_reclaimed",
                        "leader_score_maintained",
                        "previous_exit_was_planned_take_profit",
                    ],
                },
            },
            updated_at=timestamp,
        )
        return {
            "target": target,
            "defer_second_target": defer,
            "force_residual_exit": force_residual_exit,
            "tight_trailing_stop": tight_trailing_stop,
            "evidence": evidence,
        }

    def _residual_exit_evidence(
        self,
        *,
        stock_code: str,
        current_price: int,
        context: MarketExitContext,
        setup: dict[str, Any],
        timestamp: str,
    ) -> dict[str, Any]:
        intraday = self._latest_intraday_pattern(stock_code)
        leader = self._latest_leader_score(stock_code)
        supply = self._latest_supply_score(stock_code)
        trade_strength = self._latest_trade_strength(stock_code)
        try:
            current = datetime.fromisoformat(timestamp).time().replace(tzinfo=None)
        except ValueError:
            current = self._now().time().replace(tzinfo=None)
        vwap_support = False
        if intraday and intraday.get("vwap"):
            vwap_support = current_price >= float(intraday["vwap"])
        elif context.moving_average is not None:
            vwap_support = Decimal(current_price) >= context.moving_average
        vwap_value = _float_or_none((intraday or {}).get("vwap"))
        setup_vwap = _float_or_none(setup.get("vwap"))
        if vwap_value is None:
            vwap_value = setup_vwap
        if not vwap_support and vwap_value is not None:
            vwap_support = current_price >= vwap_value
        below_vwap = vwap_value is not None and current_price < vwap_value
        moving_average = (
            context.moving_average
            if context.moving_average is not None
            else _float_or_none(setup.get("moving_average"))
        )
        below_moving_average = (
            moving_average is not None
            and Decimal(current_price) < Decimal(str(moving_average))
        )
        bars = self._recent_minute_bars(stock_code, limit=5)
        latest_bar = bars[-1] if bars else {}
        latest_open = _float_or_none(latest_bar.get("open_price"))
        latest_close = _float_or_none(latest_bar.get("close_price"))
        latest_low = _float_or_none(latest_bar.get("low_price"))
        latest_bar_time = str(latest_bar.get("candle_time") or "") or None
        latest_close_below_vwap = bool(
            vwap_value is not None
            and latest_close is not None
            and latest_close < vwap_value
        )
        recent_lows = [
            low
            for low in (
                _float_or_none(bar.get("low_price")) for bar in bars[:-1]
            )
            if low is not None
        ]
        recent_previous_low = min(recent_lows) if recent_lows else None
        high_volume_bearish = bool(
            latest_open is not None
            and latest_close is not None
            and latest_close < latest_open
            and self._volume_increased(bars)
        )
        previous_low = _float_or_none(setup.get("opening_range_low"))
        if previous_low is None:
            previous_low = _float_or_none(setup.get("pullback_support_level"))
        if previous_low is None and context.support_price is not None:
            previous_low = float(context.support_price)
        if previous_low is None:
            previous_low = recent_previous_low
        below_previous_low = (
            previous_low is not None and current_price < previous_low
        )
        leader_role = str((leader or {}).get("role") or "")
        leader_score = _float_or_none((leader or {}).get("total_score"))
        supply_score = _float_or_none((supply or {}).get("total_score"))
        trade_strength_value = _float_or_none(
            (trade_strength or {}).get("strength")
        )
        vwap_exit_state = self._vwap_exit_state(
            stock_code=stock_code,
            current_price=current_price,
            intraday=intraday,
            leader=leader,
            supply=supply,
            trade_strength_value=trade_strength_value,
        )
        volume_ok = context.volume_ratio is None or context.volume_ratio >= Decimal("0.45")
        strength_ok = (
            trade_strength_value is None
            or trade_strength_value >= 95.0
        )
        supply_ok = not bool((supply or {}).get("blocked")) and (
            supply_score is None or supply_score >= 45.0
        )
        mode_name = str(
            setup.get("strategy_mode")
            or (setup.get("intraday_strategy_mode") or {}).get("name")
            or ""
        )
        climax_text = " ".join(
            str(value or "")
            for value in (
                (leader or {}).get("blocked_reason"),
                setup.get("chart_block_reason"),
                setup.get("risk_block_reason"),
            )
        )
        structure_lost = bool(
            below_vwap or below_moving_average or below_previous_low
        )
        structure_break_confirmed = bool(
            structure_lost
            and (
                high_volume_bearish
                or not supply_ok
                or (
                    trade_strength_value is not None
                    and trade_strength_value < 95.0
                )
                or context.trend_strength == "weak"
            )
        )
        structure_evidence_available = bool(
            vwap_value is not None
            or moving_average is not None
            or previous_low is not None
        )
        return {
            "vwap_support": vwap_support,
            "leader_role": leader_role or None,
            "leader_score": leader_score,
            "leader_strength": leader_role in {"leader", "second"}
            and (leader_score is None or leader_score >= 65.0)
            and not bool((leader or {}).get("blocked")),
            "leader_delay_ok": leader_role in {"leader", "second"}
            and (leader_score is None or leader_score >= 70.0)
            and not bool((leader or {}).get("blocked")),
            "mode_name": mode_name or None,
            "mode_delay_ok": mode_name in {
                "aggressive_leader",
                "verified_leader",
                "morning_aggressive",
                "morning_active",
                "midday_conservative",
            },
            "supply_score": supply_score,
            "supply_blocked": bool((supply or {}).get("blocked")),
            "trade_strength": trade_strength_value,
            "volume_ratio": (
                float(context.volume_ratio)
                if context.volume_ratio is not None
                else None
            ),
            "flow_ok": supply_ok and volume_ok and strength_ok,
            "trend_ok": context.trend_strength != "weak",
            "structure_lost": structure_lost,
            "structure_break_confirmed": structure_break_confirmed,
            "structure_evidence_available": structure_evidence_available,
            "below_vwap": below_vwap,
            "latest_close_below_vwap": latest_close_below_vwap,
            "latest_bar_time": latest_bar_time,
            "below_moving_average": below_moving_average,
            "previous_low": previous_low,
            "below_previous_low": below_previous_low,
            "recent_previous_low": recent_previous_low,
            "latest_bar_low": latest_low,
            "high_volume_bearish": high_volume_bearish,
            "climax_risk": "클라이맥스" in climax_text or "climax" in climax_text.lower(),
            "near_close": current >= SECOND_TARGET_DELAY_CUTOFF,
            "after_midday_fade_cutoff": current >= AGGRESSIVE_MIDDAY_FADE_CUTOFF,
            "intraday_vwap": (intraday or {}).get("vwap"),
            "intraday_vwap_distance_rate": (
                (intraday or {}).get("vwap_distance_rate")
            ),
            "vwap_exit_state": vwap_exit_state,
        }

    def _vwap_exit_state(
        self,
        *,
        stock_code: str,
        current_price: int,
        intraday: Optional[dict[str, Any]],
        leader: Optional[dict[str, Any]],
        supply: Optional[dict[str, Any]],
        trade_strength_value: Optional[float],
    ) -> dict[str, Any]:
        vwap = _float_or_none((intraday or {}).get("vwap"))
        if vwap is None or vwap <= 0:
            return {
                "state": "unknown",
                "reason": "VWAP 데이터 없음",
            }
        bars = self._recent_minute_bars(stock_code, limit=5)
        recovered = current_price >= vwap
        below_count = 0
        counted_bars = bars
        if recovered and bars:
            latest_close = _float_or_none(bars[-1].get("close_price"))
            if latest_close is not None and latest_close >= vwap:
                counted_bars = bars[:-1]
        for bar in reversed(counted_bars):
            close_price = _float_or_none(bar.get("close_price"))
            if close_price is None or close_price >= vwap:
                break
            below_count += 1
        volume_increased = self._volume_increased(bars)
        trade_strength_weak = (
            trade_strength_value is not None and trade_strength_value < 95.0
        )
        leader_declined = self._score_declined(
            "leader_stock_scores",
            stock_code,
            score_column="total_score",
            time_columns=("trade_date", "scored_at", "id"),
        ) or bool((leader or {}).get("blocked"))
        supply_declined = self._score_declined(
            "supply_scores",
            stock_code,
            score_column="total_score",
            time_columns=("evaluated_at", "id"),
        ) or bool((supply or {}).get("blocked"))
        state = "holding"
        reason = "VWAP 위 유지"
        if (
            below_count >= 3
            and volume_increased
            and trade_strength_weak
            and (leader_declined or supply_declined)
        ):
            state = "actual_breakdown"
            reason = "3개 이상 봉 VWAP 이탈과 거래량 증가·체결강도 약화·점수 하락"
        elif 1 <= below_count <= 2 and recovered and not volume_increased:
            state = "shakeout_recovered"
            reason = "1~2개 봉 이탈 후 거래량 작고 VWAP 재회복"
        elif below_count > 0:
            state = "watch"
            reason = "VWAP 이탈 관찰"
        return {
            "state": state,
            "reason": reason,
            "vwap": vwap,
            "current_price": current_price,
            "below_vwap_bars": below_count,
            "recovered": recovered,
            "volume_increased": volume_increased,
            "trade_strength_weak": trade_strength_weak,
            "leader_declined": leader_declined,
            "supply_declined": supply_declined,
        }

    @staticmethod
    def _current_second_target(
        trade_plan: object,
        setup: dict[str, Any],
    ) -> Optional[int]:
        for value in (
            setup.get("adjusted_second_take_profit_price"),
            setup.get("second_take_profit_price"),
            trade_plan["second_take_profit_price"]
            if "second_take_profit_price" in trade_plan.keys()
            else None,
            setup.get("previous_high"),
        ):
            if value is None:
                continue
            try:
                target = int(float(value))
            except (TypeError, ValueError):
                continue
            if target > 0:
                return target
        return None

    def _partial_adjustment_exists(
        self,
        plan_id: int,
        adjustment_type: str,
    ) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM partial_exit_adjustments
                WHERE trade_plan_id = ? AND adjustment_type = ?
                LIMIT 1
                """,
                (plan_id, adjustment_type),
            ).fetchone()
        return row is not None

    def _latest_intraday_pattern(self, stock_code: str) -> Optional[dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT current_price, vwap, vwap_distance_rate,
                       reclaim_state, bounce_state, source_data_time
                FROM intraday_pattern_evaluations
                WHERE stock_code = ?
                ORDER BY source_data_time DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _latest_leader_score(self, stock_code: str) -> Optional[dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM leader_stock_scores
                WHERE stock_code = ?
                ORDER BY trade_date DESC, scored_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _latest_supply_score(self, stock_code: str) -> Optional[dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM supply_scores
                WHERE stock_code = ?
                ORDER BY evaluated_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _latest_trade_strength(self, stock_code: str) -> Optional[dict[str, Any]]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT strength, trade_time
                FROM trade_strength_snapshots
                WHERE stock_code = ? AND strength IS NOT NULL
                ORDER BY trade_time DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _recent_minute_bars(
        self,
        stock_code: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'minute'
                  AND interval_minutes = 5
                  AND close_price IS NOT NULL
                ORDER BY candle_time DESC, id DESC
                LIMIT ?
                """,
                (stock_code, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _recent_higher_low(
        self,
        stock_code: str,
        *,
        prior_value: Any = None,
    ) -> Optional[int]:
        prior = int(prior_value) if prior_value not in (None, "") else None
        bars = self._recent_minute_bars(stock_code, limit=9)
        lows = [
            int(bar["low_price"])
            for bar in bars
            if bar.get("low_price") is not None
        ]
        pivots = [
            lows[index]
            for index in range(1, len(lows) - 1)
            if lows[index] <= lows[index - 1]
            and lows[index] <= lows[index + 1]
        ]
        candidate: Optional[int] = None
        if len(pivots) >= 2 and pivots[-1] > pivots[-2]:
            candidate = pivots[-1]
        elif pivots and prior is not None and pivots[-1] > prior:
            candidate = pivots[-1]
        if candidate is None:
            return prior
        return max(prior or 0, candidate)

    def _save_persistent_exit_state(
        self,
        *,
        account_id: int,
        stock_code: str,
        trade_plan_id: Optional[int],
        state_machine: dict[str, Any],
        persisted_state: dict[str, Any],
        highest_price: int,
        recent_higher_low: Optional[int],
        protection_stop_price: Optional[int],
        timestamp: str,
    ) -> None:
        state = str(state_machine.get("state") or "initial_risk")
        prior_state = str(persisted_state.get("state") or "initial_risk")
        warning_at = persisted_state.get("vwap_warning_at")
        warning_count = int(persisted_state.get("vwap_warning_count") or 0)
        prior_data = persisted_state.get("state_data") or {}
        state_data = {
            **(prior_data if isinstance(prior_data, dict) else {}),
            **state_machine,
        }
        if state == "vwap_warning":
            if prior_state != "vwap_warning":
                warning_at = timestamp
                warning_count += 1
            state_data["vwap_warning_bar_time"] = state_machine.get(
                "vwap_warning_bar_time"
            )
        self.database.save_position_exit_state(
            account_id=account_id,
            stock_code=stock_code,
            trade_plan_id=trade_plan_id,
            state=state,
            entry_highest_price=highest_price,
            recent_higher_low=recent_higher_low,
            protection_stop_price=protection_stop_price,
            vwap_warning_at=(str(warning_at) if warning_at else None),
            vwap_warning_count=warning_count,
            state_data=state_data,
            updated_at=timestamp,
        )

    @staticmethod
    def _volume_increased(bars: list[dict[str, Any]]) -> bool:
        if len(bars) < 4:
            return False
        latest = _float_or_none(bars[-1].get("volume"))
        previous = [
            _float_or_none(bar.get("volume"))
            for bar in bars[-4:-1]
        ]
        previous = [value for value in previous if value is not None]
        if latest is None or not previous:
            return False
        average = sum(previous) / len(previous)
        return average > 0 and latest >= average * 1.2

    def _score_declined(
        self,
        table: str,
        stock_code: str,
        *,
        score_column: str,
        time_columns: tuple[str, ...],
    ) -> bool:
        if table not in {"leader_stock_scores", "supply_scores"}:
            return False
        order_by = ", ".join(f"{column} DESC" for column in time_columns)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {score_column} AS score
                FROM {table}
                WHERE stock_code = ?
                ORDER BY {order_by}
                LIMIT 2
                """,
                (stock_code,),
            ).fetchall()
        if len(rows) < 2:
            return False
        latest = _float_or_none(rows[0]["score"])
        previous = _float_or_none(rows[1]["score"])
        if latest is None or previous is None:
            return False
        return latest <= previous - 5.0

    @staticmethod
    def _partial_exit_zone(
        *,
        trade_plan: object,
        current_price: int,
        plan: ExitPlan,
    ) -> str:
        if int(trade_plan["partial_exit_executed"] or 0):
            setup = _trade_plan_setup(trade_plan)
            second_target = setup.get("second_take_profit_price")
            if second_target is not None and current_price >= int(float(second_target)):
                return "second_target_reached"
            if plan.trailing_active:
                return "trailing_active"
            if int(trade_plan["breakeven_activated"] or 0) or plan.breakeven_active:
                return "breakeven_active"
            return "residual_position"
        first_target = int(trade_plan["first_take_profit_price"])
        if current_price >= first_target:
            return "first_target_reached"
        breakeven = int(trade_plan["breakeven_price"])
        if current_price >= breakeven:
            return "between_entry_and_first_target"
        return "below_breakeven"

    def _market_context(self, stock_code: str) -> MarketExitContext:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT high_price, low_price, close_price, volume
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'minute'
                  AND interval_minutes = 5
                  AND high_price IS NOT NULL
                  AND low_price IS NOT NULL
                  AND close_price IS NOT NULL
                ORDER BY candle_time DESC, id DESC
                LIMIT ?
                """,
                (stock_code, max(self.settings.atr_period + 1, 20)),
            ).fetchall()
        bars = [
            PriceBar(
                high=int(row["high_price"]),
                low=int(row["low_price"]),
                close=int(row["close_price"]),
                volume=(
                    int(row["volume"]) if row["volume"] is not None else None
                ),
            )
            for row in reversed(rows)
        ]
        return build_market_context(
            bars,
            atr_period=self.settings.atr_period,
        )

    @staticmethod
    def _store_risk_state(
        connection: object,
        *,
        account_id: int,
        stock_code: str,
        current_price: int,
        highest_price: int,
        context: MarketExitContext,
        plan: ExitPlan,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO paper_risk_state_events(
                account_id, stock_code, current_price, highest_price,
                initial_stop_price, atr_value, atr_stop_price,
                current_stop_price, current_take_profit_price,
                trailing_price, trailing_active, breakeven_active,
                trend_strength, trend_score, trailing_drawdown_rate,
                support_price, volume_ratio, expected_exit_reason,
                triggered_exit_reason, rule_reason, evaluated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                stock_code,
                current_price,
                highest_price,
                plan.initial_stop_price,
                float(context.atr) if context.atr is not None else None,
                plan.atr_stop_price,
                plan.current_stop_price,
                plan.take_profit_price,
                plan.trailing_price,
                int(plan.trailing_active),
                int(plan.breakeven_active),
                plan.trend_strength,
                plan.trend_score,
                float(plan.trailing_drawdown_rate),
                plan.support_price,
                (
                    float(plan.volume_ratio)
                    if plan.volume_ratio is not None
                    else None
                ),
                plan.expected_exit_reason,
                plan.exit_reason,
                plan.rule_reason,
                timestamp,
            ),
        )

    def _sell_realized_pnl(self, order_id: Optional[int]) -> Optional[int]:
        if order_id is None:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT SUM(realized_pnl) AS realized_pnl
                FROM paper_fills
                WHERE order_id = ? AND side = 'sell'
                """,
                (order_id,),
            ).fetchone()
        if row is None or row["realized_pnl"] is None:
            return None
        return int(row["realized_pnl"])

    def _submit_sell(
        self,
        account_name: str,
        stock_code: str,
        quantity: int,
        *,
        reason: str,
        event_type: str,
        decision_time: datetime,
        client_order_id: str,
    ) -> OrderExecutionResult:
        decision = StrategyDecision(
            strategy_name="risk",
            stock_code=stock_code,
            decided_at=decision_time,
            action=DecisionAction.SELL,
            score=Decimal("-100"),
            confidence=Decimal("1"),
            reason=reason,
            components=(
                ScoreComponent(
                    name=event_type,
                    score=Decimal("-100"),
                    weight=Decimal("1"),
                    reason=reason,
                    source="risk",
                ),
            ),
        )
        decision_id = self.database.save_strategy_decision(
            decision,
            account_name=account_name,
            strategy_version_id=self.strategy_version_id,
            strategy_config_id=self.strategy_config_id,
            source_data_time=decision_time.isoformat(timespec="seconds"),
        )
        try:
            result = self.order_executor.submit(
                OrderRequest(
                    account_name=account_name,
                    stock_code=stock_code,
                    side=OrderSide.SELL,
                    quantity=quantity,
                    decision=decision,
                    decision_id=decision_id,
                    client_order_id=client_order_id,
                    reason=reason,
                )
            )
        except OrderExecutionError as exc:
            self.database.update_strategy_decision(
                decision_id,
                outcome="failed",
                message=str(exc),
            )
            raise
        self.database.update_strategy_decision(
            decision_id,
            outcome="executed",
            message=reason,
        )
        return result

    def status(self, account_name: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.stock_code, p.quantity, p.cost_basis,
                       r.stop_loss_rate, r.take_profit_rate,
                       r.trailing_activation_rate, r.trailing_drawdown_rate,
                       r.highest_price, r.trailing_active,
                       r.initial_stop_price, r.atr_value, r.atr_stop_price,
                       r.current_stop_price, r.current_take_profit_price,
                       r.breakeven_active, r.trend_strength, r.trend_score,
                       r.current_trailing_drawdown_rate, r.support_price,
                       r.volume_ratio, r.expected_exit_reason,
                       r.last_rule_reason, r.updated_at AS risk_evaluated_at
                FROM paper_accounts a
                JOIN paper_positions p ON p.account_id = a.id
                LEFT JOIN paper_risk_profiles r
                  ON r.account_id = p.account_id AND r.stock_code = p.stock_code
                WHERE a.name = ?
                ORDER BY p.stock_code
                """,
                (account_name,),
            ).fetchall()
            return [dict(row) for row in rows]

    def events(self, account_name: str, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.stock_code, e.event_type, e.trigger_price,
                       e.threshold_price, e.highest_price, e.order_id,
                       e.status, e.exit_class, e.exit_reason_class,
                       e.setup_invalidated, e.classification_json,
                       e.message, e.occurred_at
                FROM paper_risk_events e
                JOIN paper_accounts a ON a.id = e.account_id
                WHERE a.name = ?
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (account_name, limit),
            ).fetchall()
            return [dict(row) for row in rows]
