from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Optional, Sequence

from config.settings import RiskSettings


EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT = "take_profit"
EXIT_TRAILING_STOP = "trailing_stop"
EXIT_SUPPORT_BREAK = "support_break"
EXIT_VOLUME_DRY_UP = "volume_dry_up"
EXIT_MARKET_CLOSE = "market_close"


@dataclass(frozen=True)
class PriceBar:
    high: int
    low: int
    close: int
    volume: Optional[int] = None


@dataclass(frozen=True)
class MarketExitContext:
    atr: Optional[Decimal]
    moving_average: Optional[Decimal]
    moving_average_slope: Optional[Decimal]
    support_price: Optional[int]
    volume_ratio: Optional[Decimal]
    trend_strength: str
    trend_score: int


@dataclass(frozen=True)
class ExitPlan:
    initial_stop_price: int
    atr_stop_price: Optional[int]
    current_stop_price: int
    take_profit_price: Optional[int]
    trailing_price: Optional[int]
    trailing_active: bool
    breakeven_active: bool
    trailing_drawdown_rate: Decimal
    trend_strength: str
    trend_score: int
    support_price: Optional[int]
    volume_ratio: Optional[Decimal]
    expected_exit_reason: Optional[str]
    exit_reason: Optional[str]
    exit_price: Optional[int]
    rule_reason: str


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def build_market_context(
    bars: Sequence[PriceBar],
    *,
    atr_period: int,
    moving_average_period: int = 5,
) -> MarketExitContext:
    usable = [bar for bar in bars if bar.high > 0 and bar.low > 0 and bar.close > 0]
    atr = _calculate_atr(usable, atr_period)
    closes = [Decimal(bar.close) for bar in usable]
    moving_average = None
    slope = None
    if len(closes) >= moving_average_period:
        recent = closes[-moving_average_period:]
        moving_average = sum(recent) / Decimal(moving_average_period)
        if len(closes) >= moving_average_period * 2:
            prior = closes[-moving_average_period * 2 : -moving_average_period]
            prior_average = sum(prior) / Decimal(moving_average_period)
            if prior_average:
                slope = moving_average / prior_average - Decimal("1")

    support_price = (
        min(bar.low for bar in usable[-moving_average_period - 1 : -1])
        if len(usable) > moving_average_period
        else None
    )
    volume_ratio = None
    volumes = [bar.volume for bar in usable if bar.volume is not None]
    if len(volumes) >= moving_average_period + 1:
        current_volume = Decimal(volumes[-1])
        prior_volumes = volumes[-moving_average_period - 1 : -1]
        average_volume = sum(Decimal(value) for value in prior_volumes) / Decimal(
            len(prior_volumes)
        )
        if average_volume > 0:
            volume_ratio = current_volume / average_volume

    score = 0
    if usable and moving_average is not None:
        score += 1 if Decimal(usable[-1].close) > moving_average else -1
    if slope is not None:
        score += 1 if slope > 0 else -1
    if len(usable) >= 3:
        recent = usable[-3:]
        if recent[-1].high > recent[-2].high > recent[-3].high:
            score += 1
        if recent[-1].low < recent[-2].low:
            score -= 1
    if volume_ratio is not None:
        if volume_ratio >= Decimal("0.8"):
            score += 1
        elif volume_ratio < Decimal("0.4"):
            score -= 1

    strength = "strong" if score >= 3 else ("weak" if score <= -1 else "neutral")
    return MarketExitContext(
        atr=atr,
        moving_average=moving_average,
        moving_average_slope=slope,
        support_price=support_price,
        volume_ratio=volume_ratio,
        trend_strength=strength,
        trend_score=score,
    )


def evaluate_exit(
    *,
    average_cost: Decimal,
    current_price: int,
    observed_low: int,
    observed_high: int,
    highest_price: int,
    trailing_active: bool,
    context: MarketExitContext,
    settings: RiskSettings,
    defer_new_protection: bool = False,
    planned_stop_price: Optional[int] = None,
) -> ExitPlan:
    fixed_stop = _floor(
        average_cost * (Decimal("1") - settings.stop_loss_rate)
    )
    initial_stop = (
        int(planned_stop_price)
        if planned_stop_price is not None
        and planned_stop_price > 0
        and Decimal(planned_stop_price) < average_cost
        else fixed_stop
    )
    atr_stop = None
    if context.atr is not None:
        atr_stop = _floor(
            average_cost - context.atr * settings.atr_stop_multiple
        )
    # ATR and any future model may tighten risk, but never loosen fixed loss.
    current_stop = max(
        initial_stop,
        atr_stop if atr_stop is not None else initial_stop,
    )
    observed_highest = max(highest_price, observed_high, current_price)
    protection_highest = (
        highest_price if defer_new_protection else observed_highest
    )
    profit_rate = Decimal(protection_highest) / average_cost - Decimal("1")
    breakeven_active = (
        profit_rate >= settings.breakeven_activation_rate
    )
    if breakeven_active:
        breakeven_price = _ceil(
            average_cost * (Decimal("1") + settings.breakeven_buffer_rate)
        )
        current_stop = max(current_stop, breakeven_price)
    non_trailing_stop = current_stop

    active = trailing_active or (
        profit_rate >= settings.trailing_activation_rate
    )
    trailing_drawdown = settings.trailing_drawdown_rate
    if context.trend_strength == "strong":
        trailing_drawdown = settings.strong_trend_trailing_drawdown_rate
    elif context.trend_strength == "weak":
        trailing_drawdown = settings.weak_trend_trailing_drawdown_rate
    trailing_price = (
        _floor(protection_highest * (Decimal("1") - trailing_drawdown))
        if active
        else None
    )
    if trailing_price is not None:
        current_stop = max(current_stop, trailing_price)

    take_profit = None
    if settings.take_profit_enabled:
        take_rate = (
            settings.strong_trend_take_profit_rate
            if context.trend_strength == "strong"
            else settings.take_profit_rate
        )
        take_profit = _ceil(average_cost * (Decimal("1") + take_rate))

    exit_reason = None
    exit_price = None
    expected_reason = None
    reasons = [
        f"trend={context.trend_strength}({context.trend_score})",
        (
            f"planned_stop={initial_stop}"
            if initial_stop != fixed_stop
            else f"fixed_stop={initial_stop}"
        ),
    ]
    if atr_stop is not None:
        reasons.append(f"atr_stop={atr_stop}")
    if breakeven_active:
        reasons.append("breakeven")
    if active:
        reasons.append(f"trailing={trailing_drawdown}")

    if observed_low <= current_stop:
        if observed_low <= non_trailing_stop:
            exit_reason = EXIT_STOP_LOSS
        elif trailing_price is not None and current_stop == trailing_price:
            exit_reason = EXIT_TRAILING_STOP
        else:
            exit_reason = EXIT_STOP_LOSS
        exit_price = current_stop
    elif (
        context.support_price is not None
        and current_price < context.support_price
        and context.trend_strength == "weak"
    ):
        expected_reason = EXIT_SUPPORT_BREAK
    elif (
        context.volume_ratio is not None
        and context.volume_ratio < settings.volume_dry_up_ratio
        and context.trend_strength == "weak"
        and current_price > average_cost
    ):
        expected_reason = EXIT_VOLUME_DRY_UP
    elif take_profit is not None and observed_high >= take_profit:
        exit_reason = EXIT_TAKE_PROFIT
        exit_price = take_profit
    else:
        if context.trend_strength == "weak":
            expected_reason = (
                EXIT_SUPPORT_BREAK
                if context.support_price is not None
                else EXIT_TRAILING_STOP
            )
        elif active:
            expected_reason = EXIT_TRAILING_STOP
        else:
            expected_reason = (
                EXIT_TAKE_PROFIT
                if take_profit is not None
                else EXIT_TRAILING_STOP
            )

    next_profit_rate = Decimal(observed_highest) / average_cost - Decimal("1")
    next_breakeven_active = breakeven_active or (
        next_profit_rate >= settings.breakeven_activation_rate
    )
    next_trailing_active = active or (
        next_profit_rate >= settings.trailing_activation_rate
    )

    return ExitPlan(
        initial_stop_price=initial_stop,
        atr_stop_price=atr_stop,
        current_stop_price=current_stop,
        take_profit_price=take_profit,
        trailing_price=trailing_price,
        trailing_active=next_trailing_active,
        breakeven_active=next_breakeven_active,
        trailing_drawdown_rate=trailing_drawdown,
        trend_strength=context.trend_strength,
        trend_score=context.trend_score,
        support_price=context.support_price,
        volume_ratio=context.volume_ratio,
        expected_exit_reason=expected_reason,
        exit_reason=exit_reason,
        exit_price=exit_price,
        rule_reason=", ".join(reasons),
    )


def _calculate_atr(
    bars: Sequence[PriceBar],
    period: int,
) -> Optional[Decimal]:
    if period < 1 or len(bars) < period + 1:
        return None
    true_ranges = []
    for previous, current in zip(bars[-period - 1 : -1], bars[-period:]):
        true_ranges.append(
            max(
                Decimal(current.high - current.low),
                abs(Decimal(current.high - previous.close)),
                abs(Decimal(current.low - previous.close)),
            )
        )
    return sum(true_ranges) / Decimal(len(true_ranges))
