from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from itertools import groupby
from typing import Optional

from config.settings import RiskSettings, StrategySettings
from data.watchlist import normalize_stock_code
from db.database import Database
from risk.exit_policy import PriceBar, build_market_context, evaluate_exit


ENTRY_CUTOFF = time(15, 20)


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class BacktestConfig:
    start_date: date
    end_date: date
    initial_cash: int
    commission_rate: Decimal
    sell_tax_rate: Decimal
    slippage_rate: Decimal
    interval_minutes: int = 5

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("백테스트 시작일은 종료일보다 늦을 수 없습니다.")
        if self.initial_cash <= 0:
            raise ValueError("백테스트 초기자금은 0원보다 커야 합니다.")
        if self.interval_minutes < 1:
            raise ValueError("백테스트 분봉 간격은 1 이상이어야 합니다.")
        for name, value in (
            ("commission_rate", self.commission_rate),
            ("sell_tax_rate", self.sell_tax_rate),
            ("slippage_rate", self.slippage_rate),
        ):
            if value < 0 or value >= 1:
                raise ValueError(f"{name}는 0 이상 1 미만이어야 합니다.")


@dataclass(frozen=True)
class BacktestTrade:
    stock_code: str
    entry_time: str
    exit_time: str
    quantity: int
    entry_market_price: int
    exit_market_price: int
    entry_price: int
    exit_price: int
    entry_commission: int
    exit_commission: int
    tax: int
    slippage_amount: int
    realized_pnl: int
    return_rate: Decimal
    exit_reason: str


@dataclass(frozen=True)
class BacktestEquityPoint:
    point_time: str
    cash_balance: int
    market_value: int
    total_equity: int


@dataclass(frozen=True)
class BacktestResult:
    start_date: date
    end_date: date
    initial_cash: int
    final_equity: int
    total_return_rate: Decimal
    max_drawdown_rate: Decimal
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal
    payoff_ratio: Optional[Decimal]
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[BacktestEquityPoint, ...]


@dataclass
class _Position:
    stock_code: str
    quantity: int
    entry_time: str
    entry_market_price: int
    entry_price: int
    entry_commission: int
    entry_slippage_amount: int
    cost_basis: int
    highest_price: int
    trailing_active: bool = False


@dataclass(frozen=True)
class _PendingEntry:
    signal_time: str
    signal_day: date
    volume_ratio: Decimal
    breakout_rate: Decimal
    moving_average_gap_rate: Decimal
    day_gain_rate: Decimal


@dataclass(frozen=True)
class _SignalEvaluation:
    volume_ratio: Decimal
    breakout_rate: Decimal
    moving_average_gap_rate: Decimal
    day_gain_rate: Decimal


class BreakoutBacktester:
    def __init__(
        self,
        database: Database,
        strategy_settings: StrategySettings,
        risk_settings: RiskSettings,
    ) -> None:
        self.database = database
        self.strategy_settings = strategy_settings
        self.risk_settings = risk_settings

    def run(
        self,
        stock_codes: list[str],
        config: BacktestConfig,
    ) -> BacktestResult:
        codes = sorted({normalize_stock_code(code) for code in stock_codes})
        if not codes:
            raise ValueError("백테스트 종목을 1개 이상 지정해야 합니다.")

        minute_rows = self.database.list_candles(
            codes,
            candle_type="minute",
            interval_minutes=config.interval_minutes,
            start_time=f"{config.start_date.isoformat()}T00:00:00",
            end_time=f"{config.end_date.isoformat()}T23:59:59",
        )
        if not minute_rows:
            raise ValueError("지정한 기간에 백테스트할 분봉 데이터가 없습니다.")
        daily_rows = self.database.list_candles(
            codes,
            candle_type="daily",
            end_time=f"{config.end_date.isoformat()}T23:59:59",
        )

        previous_daily = self._build_previous_daily(daily_rows, minute_rows)
        cash = config.initial_cash
        positions: dict[str, _Position] = {}
        pending: dict[str, _PendingEntry] = {}
        closes: dict[str, int] = {}
        day_bars: dict[tuple[str, date], list[dict]] = {}
        daily_realized_pnl: dict[date, int] = {}
        consecutive_losses = 0
        trades: list[BacktestTrade] = []
        equity_curve = [
            BacktestEquityPoint(
                point_time=f"{config.start_date.isoformat()}T00:00:00",
                cash_balance=config.initial_cash,
                market_value=0,
                total_equity=config.initial_cash,
            )
        ]

        for candle_time_text, source_rows in groupby(
            minute_rows,
            key=lambda row: row["candle_time"],
        ):
            bars_by_code = {
                row["stock_code"]: dict(row)
                for row in source_rows
            }
            candle_time = datetime.fromisoformat(candle_time_text)
            candle_day = candle_time.date()
            for stock_code, bar in bars_by_code.items():
                closes[stock_code] = int(bar["close_price"])

            for stock_code in sorted(bars_by_code):
                position = positions.get(stock_code)
                if position is None:
                    continue
                bar = bars_by_code[stock_code]
                exit_reason, market_price = self._exit_decision(
                    position,
                    bar,
                    candle_time,
                    [
                        *day_bars.get((stock_code, candle_day), []),
                        bar,
                    ],
                )
                if exit_reason is not None and market_price is not None:
                    trade, cash = self._exit(
                        position,
                        bar["candle_time"],
                        market_price,
                        exit_reason,
                        cash,
                        config,
                    )
                    trades.append(trade)
                    daily_realized_pnl[candle_day] = (
                        daily_realized_pnl.get(candle_day, 0)
                        + trade.realized_pnl
                    )
                    consecutive_losses = (
                        consecutive_losses + 1
                        if trade.realized_pnl < 0
                        else 0
                    )
                    del positions[stock_code]

            candidates = []
            for stock_code, bar in bars_by_code.items():
                queued = pending.pop(stock_code, None)
                if (
                    queued is not None
                    and queued.signal_day == candle_day
                    and stock_code not in positions
                    and not self._entry_blocked(
                        candle_day,
                        daily_realized_pnl,
                        consecutive_losses,
                        config.initial_cash,
                    )
                ):
                    candidates.append((stock_code, bar, queued))
            candidates.sort(key=self._entry_priority)
            available_slots = max(
                0,
                self.risk_settings.max_positions - len(positions),
            )
            selected = candidates[:available_slots]
            entered_codes = set()
            equal_cash_share = (
                cash // len(selected)
                if selected
                else 0
            )
            for stock_code, bar, _ in selected:
                equity = self._equity(cash, positions, closes)
                position_limit = _floor(
                    Decimal(equity)
                    * self.risk_settings.max_position_rate
                )
                budget = min(position_limit, equal_cash_share)
                position, cash = self._enter(
                    stock_code,
                    bar,
                    cash,
                    budget,
                    config,
                )
                if position is not None:
                    positions[stock_code] = position
                    entered_codes.add(stock_code)

            for stock_code, bar in bars_by_code.items():
                bars = day_bars.setdefault((stock_code, candle_day), [])
                bars.append(bar)
                if (
                    stock_code not in positions
                    and stock_code not in pending
                    and stock_code not in entered_codes
                    and candle_time.time() < ENTRY_CUTOFF
                ):
                    signal = self._evaluate_signal(
                        stock_code,
                        candle_day,
                        bar,
                        bars,
                        previous_daily,
                    )
                    if signal is not None:
                        pending[stock_code] = _PendingEntry(
                            signal_time=bar["candle_time"],
                            signal_day=candle_day,
                            volume_ratio=signal.volume_ratio,
                            breakout_rate=signal.breakout_rate,
                            moving_average_gap_rate=(
                                signal.moving_average_gap_rate
                            ),
                            day_gain_rate=signal.day_gain_rate,
                        )

            market_value = self._market_value(positions, closes)
            equity_curve.append(
                BacktestEquityPoint(
                    point_time=candle_time_text,
                    cash_balance=cash,
                    market_value=market_value,
                    total_equity=cash + market_value,
                )
            )

        final_time_by_code = {
            row["stock_code"]: row["candle_time"] for row in minute_rows
        }
        for stock_code, position in list(positions.items()):
            trade, cash = self._exit(
                position,
                final_time_by_code[stock_code],
                closes[stock_code],
                "end_of_test",
                cash,
                config,
            )
            trades.append(trade)
            del positions[stock_code]
        final_equity = cash
        final_point_time = max(final_time_by_code.values())
        equity_curve.append(
            BacktestEquityPoint(
                point_time=final_point_time,
                cash_balance=final_equity,
                market_value=0,
                total_equity=final_equity,
            )
        )
        return self._result(config, final_equity, trades, equity_curve)

    def _entry_blocked(
        self,
        candle_day: date,
        daily_realized_pnl: dict[date, int],
        consecutive_losses: int,
        initial_cash: int,
    ) -> bool:
        daily_loss_limit = _floor(
            Decimal(initial_cash) * self.risk_settings.daily_max_loss_rate
        )
        return (
            daily_realized_pnl.get(candle_day, 0) <= -daily_loss_limit
            or (
                self.risk_settings.max_consecutive_losses > 0
                and consecutive_losses >= self.risk_settings.max_consecutive_losses
            )
        )

    @staticmethod
    def _build_previous_daily(
        daily_rows: list,
        minute_rows: list,
    ) -> dict[tuple[str, date], dict]:
        grouped: dict[str, list[dict]] = {}
        for row in daily_rows:
            grouped.setdefault(row["stock_code"], []).append(dict(row))
        result: dict[tuple[str, date], dict] = {}
        minute_days = {
            (
                row["stock_code"],
                datetime.fromisoformat(row["candle_time"]).date(),
            )
            for row in minute_rows
        }
        for stock_code, candle_day in minute_days:
            prior = None
            for row in grouped.get(stock_code, []):
                daily_day = datetime.fromisoformat(row["candle_time"]).date()
                if daily_day >= candle_day:
                    break
                prior = row
            if prior is not None:
                result[(stock_code, candle_day)] = prior
        return result

    @staticmethod
    def _entry_priority(
        candidate: tuple[str, dict, _PendingEntry],
    ) -> tuple:
        stock_code, _, signal = candidate
        return (
            -signal.volume_ratio,
            -signal.breakout_rate,
            -signal.moving_average_gap_rate,
            signal.day_gain_rate,
            stock_code,
        )

    def _evaluate_signal(
        self,
        stock_code: str,
        candle_day: date,
        bar: dict,
        day_bars: list[dict],
        previous_daily: dict[tuple[str, date], dict],
    ) -> Optional[_SignalEvaluation]:
        previous = previous_daily.get((stock_code, candle_day))
        period = self.strategy_settings.moving_average_period
        if previous is None or len(day_bars) < period:
            return None
        recent = day_bars[-period:]
        if any(row["close_price"] is None for row in recent):
            return None

        previous_high = previous.get("high_price")
        previous_close = previous.get("close_price")
        previous_volume = previous.get("volume")
        if previous_high is None or previous_close in (None, 0):
            return None
        if previous_volume in (None, 0):
            return None

        current_price = int(bar["close_price"])
        accumulated_volume = bar.get("accumulated_volume")
        current_volume = (
            int(accumulated_volume)
            if accumulated_volume is not None
            else sum(int(row.get("volume") or 0) for row in day_bars)
        )
        volume_ratio = Decimal(current_volume) / Decimal(previous_volume)
        moving_average = sum(
            Decimal(int(row["close_price"])) for row in recent
        ) / Decimal(period)
        day_gain_rate = (
            Decimal(current_price) / Decimal(int(previous_close)) - Decimal("1")
        )
        breakout_rate = (
            Decimal(current_price) / Decimal(int(previous_high)) - Decimal("1")
        )
        moving_average_gap_rate = (
            Decimal(current_price) / moving_average - Decimal("1")
        )
        passed = (
            volume_ratio >= self.strategy_settings.volume_multiple
            and current_price > int(previous_high)
            and Decimal(current_price) > moving_average
            and (
                self.strategy_settings.max_day_gain_rate <= Decimal("0")
                or day_gain_rate <= self.strategy_settings.max_day_gain_rate
            )
        )
        if not passed:
            return None
        return _SignalEvaluation(
            volume_ratio=volume_ratio,
            breakout_rate=breakout_rate,
            moving_average_gap_rate=moving_average_gap_rate,
            day_gain_rate=day_gain_rate,
        )

    def _enter(
        self,
        stock_code: str,
        bar: dict,
        cash: int,
        budget: int,
        config: BacktestConfig,
    ) -> tuple[Optional[_Position], int]:
        market_price = int(bar["open_price"])
        fill_price = _ceil(
            Decimal(market_price) * (Decimal("1") + config.slippage_rate)
        )
        budget = min(cash, budget)
        estimated_per_share = Decimal(fill_price) * (
            Decimal("1") + config.commission_rate
        )
        quantity = _floor(Decimal(budget) / estimated_per_share)
        if quantity < 1:
            return None, cash
        gross = fill_price * quantity
        commission = self._commission(gross, config.commission_rate)
        while quantity > 0 and gross + commission > cash:
            quantity -= 1
            gross = fill_price * quantity
            commission = self._commission(gross, config.commission_rate)
        if quantity < 1:
            return None, cash
        cost_basis = gross + commission
        return (
            _Position(
                stock_code=stock_code,
                quantity=quantity,
                entry_time=bar["candle_time"],
                entry_market_price=market_price,
                entry_price=fill_price,
                entry_commission=commission,
                entry_slippage_amount=abs(fill_price - market_price) * quantity,
                cost_basis=cost_basis,
                highest_price=fill_price,
            ),
            cash - cost_basis,
        )

    def _exit_decision(
        self,
        position: _Position,
        bar: dict,
        candle_time: datetime,
        recent_bars: list[dict],
    ) -> tuple[Optional[str], Optional[int]]:
        close_price = int(bar["close_price"])
        if candle_time.time() >= ENTRY_CUTOFF:
            return "market_close", close_price

        average_cost = Decimal(position.cost_basis) / Decimal(position.quantity)
        low_price = int(bar["low_price"])
        high_price = int(bar["high_price"])
        context = build_market_context(
            [
                PriceBar(
                    high=int(item["high_price"]),
                    low=int(item["low_price"]),
                    close=int(item["close_price"]),
                    volume=(
                        int(item["volume"])
                        if item.get("volume") is not None
                        else None
                    ),
                )
                for item in recent_bars
                if (
                    item.get("high_price") is not None
                    and item.get("low_price") is not None
                    and item.get("close_price") is not None
                )
            ],
            atr_period=self.risk_settings.atr_period,
        )
        plan = evaluate_exit(
            average_cost=average_cost,
            current_price=close_price,
            observed_low=low_price,
            observed_high=high_price,
            highest_price=position.highest_price,
            trailing_active=position.trailing_active,
            context=context,
            settings=self.risk_settings,
            defer_new_protection=True,
        )
        position.highest_price = max(position.highest_price, high_price)
        position.trailing_active = plan.trailing_active
        return plan.exit_reason, plan.exit_price

    def _exit(
        self,
        position: _Position,
        exit_time: str,
        market_price: int,
        reason: str,
        cash: int,
        config: BacktestConfig,
    ) -> tuple[BacktestTrade, int]:
        fill_price = _floor(
            Decimal(market_price) * (Decimal("1") - config.slippage_rate)
        )
        gross = fill_price * position.quantity
        commission = self._commission(gross, config.commission_rate)
        tax = _floor(Decimal(gross) * config.sell_tax_rate)
        proceeds = gross - commission - tax
        realized_pnl = proceeds - position.cost_basis
        return_rate = (
            Decimal(realized_pnl) / Decimal(position.cost_basis)
            if position.cost_basis
            else Decimal("0")
        )
        trade = BacktestTrade(
            stock_code=position.stock_code,
            entry_time=position.entry_time,
            exit_time=exit_time,
            quantity=position.quantity,
            entry_market_price=position.entry_market_price,
            exit_market_price=market_price,
            entry_price=position.entry_price,
            exit_price=fill_price,
            entry_commission=position.entry_commission,
            exit_commission=commission,
            tax=tax,
            slippage_amount=(
                position.entry_slippage_amount
                + abs(fill_price - market_price) * position.quantity
            ),
            realized_pnl=realized_pnl,
            return_rate=return_rate,
            exit_reason=reason,
        )
        return trade, cash + proceeds

    @staticmethod
    def _commission(gross: int, rate: Decimal) -> int:
        if gross <= 0 or rate == 0:
            return 0
        return max(1, _floor(Decimal(gross) * rate))

    @staticmethod
    def _equity(
        cash: int,
        positions: dict[str, _Position],
        closes: dict[str, int],
    ) -> int:
        return cash + BreakoutBacktester._market_value(positions, closes)

    @staticmethod
    def _market_value(
        positions: dict[str, _Position],
        closes: dict[str, int],
    ) -> int:
        return sum(
            position.quantity * closes.get(stock_code, position.entry_price)
            for stock_code, position in positions.items()
        )

    @staticmethod
    def _result(
        config: BacktestConfig,
        final_equity: int,
        trades: list[BacktestTrade],
        equity_curve: list[BacktestEquityPoint],
    ) -> BacktestResult:
        wins = [trade.realized_pnl for trade in trades if trade.realized_pnl > 0]
        losses = [trade.realized_pnl for trade in trades if trade.realized_pnl < 0]
        peak = equity_curve[0].total_equity
        max_drawdown = Decimal("0")
        for point in equity_curve:
            equity = point.total_equity
            peak = max(peak, equity)
            if peak > 0:
                drawdown = Decimal(peak - equity) / Decimal(peak)
                max_drawdown = max(max_drawdown, drawdown)
        payoff_ratio = None
        if wins and losses:
            average_win = Decimal(sum(wins)) / Decimal(len(wins))
            average_loss = Decimal(abs(sum(losses))) / Decimal(len(losses))
            payoff_ratio = average_win / average_loss
        trade_count = len(trades)
        return BacktestResult(
            start_date=config.start_date,
            end_date=config.end_date,
            initial_cash=config.initial_cash,
            final_equity=final_equity,
            total_return_rate=(
                Decimal(final_equity) / Decimal(config.initial_cash) - Decimal("1")
            ),
            max_drawdown_rate=max_drawdown,
            trade_count=trade_count,
            win_count=len(wins),
            loss_count=len(losses),
            win_rate=(
                Decimal(len(wins)) / Decimal(trade_count)
                if trade_count
                else Decimal("0")
            ),
            payoff_ratio=payoff_ratio,
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
        )
