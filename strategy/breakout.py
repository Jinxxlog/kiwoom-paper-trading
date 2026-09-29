import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, time as clock_time
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Optional

from config.settings import (
    MarketModeGateSettings,
    PaperTradingSettings,
    RiskSettings,
    StrategySettings,
)
from data.scheduler import SEOUL_TIMEZONE
from data.candle_freshness import CandleFreshness
from db.database import Database
from domain.models import (
    DecisionAction,
    OrderRequest,
    OrderSide,
    ScoreComponent,
    StrategyDecision,
    TradePlan,
)
from execution.base import OrderExecutionError, OrderExecutor
from execution.paper import PaperOrderExecutor
from execution.safety import OrderSafetyGuard
from paper.engine import PaperTradingEngine, PaperTradingError
from strategy.registry import register_breakout_strategy
from strategy.chart import ChartTradeDecision, evaluate_chart_trade
from strategy.session_profile import (
    ALWAYS_ACTIVE_PROFILE,
    IntradayStrategyMode,
    SessionProfile,
    intraday_strategy_mode_for,
    session_profile_for,
)
from screening.stock_category import COMMON_STOCK, stock_category_payload


ENTRY_CUTOFF = clock_time(15, 20)
REENTRY_CUTOFF = clock_time(14, 30)
REENTRY_ENGINE_VERSION = "v0.12.96"
MARKET_REGIME_PLAYBOOK_VERSION = "v0.12.97"


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


@dataclass(frozen=True)
class BreakoutSignal:
    stock_code: str
    signal_time: str
    current_price: int
    previous_high: Optional[int]
    previous_close: Optional[int]
    current_volume: Optional[int]
    previous_volume: Optional[int]
    volume_ratio: Optional[Decimal]
    projected_volume_ratio: Optional[Decimal]
    projected_trade_amount: Optional[int]
    moving_average: Optional[Decimal]
    vwap: Optional[Decimal]
    intraday_high: Optional[int]
    intraday_midline: Optional[Decimal]
    breakout_level: Optional[int]
    day_gain_rate: Optional[Decimal]
    volume_pass: bool
    liquidity_pass: bool
    breakout_pass: bool
    breakout_hold_pass: bool
    vwap_pass: bool
    moving_average_pass: bool
    chase_pass: bool
    active_session_pass: bool
    pullback_depth_rate: Optional[Decimal] = None
    pullback_pass: bool = False
    pullback_support_level: Optional[Decimal] = None
    pullback_support_pass: bool = False
    deep_pullback_blocked: bool = False
    order_book_spread_rate: Optional[Decimal] = None
    order_book_bid_ask_ratio: Optional[Decimal] = None
    trade_strength: Optional[Decimal] = None
    order_flow_score: Optional[Decimal] = None
    ask_wall_absorption: bool = False
    bid_wall_risk: bool = False
    ask_quantity_change_rate: Optional[Decimal] = None
    bid_quantity_change_rate: Optional[Decimal] = None
    order_flow_reasons: tuple[str, ...] = ()
    order_flow_pass: bool = False
    opening_range_high: Optional[int] = None
    opening_range_low: Optional[int] = None
    opening_range_volume: Optional[int] = None
    opening_range_start: Optional[str] = None
    opening_range_end: Optional[str] = None
    opening_range_breakout_rate: Optional[Decimal] = None
    opening_range_pass: bool = False
    information_blocked: bool = False
    information_score: Decimal = Decimal("0")
    pattern_score: Decimal = Decimal("0")
    patterns: tuple[str, ...] = ()
    chart: ChartTradeDecision | None = None
    supply: dict[str, Any] | None = None
    entry_candle_quality: dict[str, Any] | None = None
    recent_swing_low: Optional[int] = None
    higher_low_pass: bool = False
    minute_atr: Optional[Decimal] = None
    recent_candle_range: Optional[Decimal] = None

    @property
    def passed(self) -> bool:
        return (
            self.volume_pass
            and self.liquidity_pass
            and self.breakout_pass
            and self.breakout_hold_pass
            and self.vwap_pass
            and self.moving_average_pass
            and self.chase_pass
        )


class BreakoutStrategy:
    def __init__(
        self,
        database: Database,
        paper_engine: PaperTradingEngine,
        strategy_settings: StrategySettings,
        paper_settings: PaperTradingSettings,
        risk_settings: RiskSettings,
        order_executor: Optional[OrderExecutor] = None,
        market_mode_settings: Optional[MarketModeGateSettings] = None,
        candle_freshness: Optional[CandleFreshness] = None,
    ) -> None:
        if strategy_settings.volume_multiple <= 0:
            raise ValueError("volume_multiple은 0보다 커야 합니다.")
        if strategy_settings.max_day_gain_rate < 0:
            raise ValueError("max_day_gain_rate는 0 이상이어야 합니다.")
        if strategy_settings.moving_average_period < 1:
            raise ValueError("moving_average_period는 1 이상이어야 합니다.")
        if strategy_settings.minute_interval < 1:
            raise ValueError("minute_interval은 1 이상이어야 합니다.")
        if strategy_settings.intraday_breakout_lookback < 2:
            raise ValueError("intraday_breakout_lookback은 2 이상이어야 합니다.")
        if strategy_settings.minimum_projected_trade_amount < 0:
            raise ValueError("최소 예상 거래대금은 0 이상이어야 합니다.")
        if strategy_settings.candidate_context_max_age_minutes < 0:
            raise ValueError("후보 스냅샷 유효 시간은 0 이상이어야 합니다.")
        if strategy_settings.pullback_min_depth_rate < 0:
            raise ValueError("눌림목 최소 되돌림률은 0 이상이어야 합니다.")
        if (
            strategy_settings.pullback_max_depth_rate
            < strategy_settings.pullback_min_depth_rate
        ):
            raise ValueError("눌림목 최대 되돌림률은 최소 되돌림률 이상이어야 합니다.")
        if strategy_settings.intraday_midline_lookback < 2:
            raise ValueError("분봉 중심선 계산 기간은 2 이상이어야 합니다.")
        if strategy_settings.intraday_breakout_hold_buffer_rate < 0:
            raise ValueError("돌파 유지 확인 폭은 0 이상이어야 합니다.")
        if strategy_settings.pullback_support_touch_buffer_rate < 0:
            raise ValueError("눌림 지지 터치 허용 폭은 0 이상이어야 합니다.")
        if strategy_settings.order_flow_max_spread_rate < 0:
            raise ValueError("호가 스프레드 한도는 0 이상이어야 합니다.")
        if strategy_settings.order_flow_min_bid_ask_ratio < 0:
            raise ValueError("매수/매도 잔량비 기준은 0 이상이어야 합니다.")
        if strategy_settings.order_flow_min_strength < 0:
            raise ValueError("체결강도 기준은 0 이상이어야 합니다.")
        if strategy_settings.order_flow_history_limit < 1:
            raise ValueError("호가 변화 비교 개수는 1 이상이어야 합니다.")
        if strategy_settings.order_flow_ask_absorption_drop_rate < 0:
            raise ValueError("매도벽 흡수 기준은 0 이상이어야 합니다.")
        if strategy_settings.order_flow_bid_wall_risk_ratio < 0:
            raise ValueError("매수벽 위험 기준은 0 이상이어야 합니다.")
        if strategy_settings.opening_range_minutes < 1:
            raise ValueError("장초반 ORB 범위 시간은 1분 이상이어야 합니다.")
        if (
            strategy_settings.opening_range_entry_end_minutes
            <= strategy_settings.opening_range_minutes
        ):
            raise ValueError("장초반 ORB 진입 종료 시간은 범위 시간보다 커야 합니다.")
        if strategy_settings.opening_range_breakout_buffer_rate < 0:
            raise ValueError("장초반 ORB 돌파 버퍼는 0 이상이어야 합니다.")
        if (
            strategy_settings.plan_fallback_stop_rate <= 0
            or strategy_settings.plan_fallback_stop_rate >= 1
        ):
            raise ValueError("계획 fallback 손절률은 0 초과 1 미만이어야 합니다.")
        if strategy_settings.morning_entry_limit_start_minutes < 0:
            raise ValueError("morning_entry_limit_start_minutes must be >= 0")
        if (
            strategy_settings.morning_entry_limit_end_minutes
            <= strategy_settings.morning_entry_limit_start_minutes
        ):
            raise ValueError(
                "morning_entry_limit_end_minutes must exceed start_minutes"
            )
        if strategy_settings.morning_max_new_buys < 0:
            raise ValueError("morning_max_new_buys must be >= 0")
        if strategy_settings.morning_max_a_grade_buys < 0:
            raise ValueError("morning_max_a_grade_buys must be >= 0")
        if strategy_settings.morning_max_b_grade_buys < 0:
            raise ValueError("morning_max_b_grade_buys must be >= 0")
        if strategy_settings.entry_bucket_minutes < 1:
            raise ValueError("entry_bucket_minutes must be >= 1")
        if strategy_settings.max_new_buys_per_bucket < 0:
            raise ValueError("max_new_buys_per_bucket must be >= 0")
        if strategy_settings.max_same_stock_buys_per_day < 0:
            raise ValueError("max_same_stock_buys_per_day must be >= 0")
        if strategy_settings.data_gap_minimum_probe_max_quantity < 1:
            raise ValueError("data_gap_minimum_probe_max_quantity must be >= 1")
        for name, value in (
            (
                "risk_loss_limit_a_grade_quantity_multiplier",
                strategy_settings.risk_loss_limit_a_grade_quantity_multiplier,
            ),
            (
                "risk_cooldown_a_grade_quantity_multiplier",
                strategy_settings.risk_cooldown_a_grade_quantity_multiplier,
            ),
            (
                "data_gap_minimum_probe_quantity_multiplier",
                strategy_settings.data_gap_minimum_probe_quantity_multiplier,
            ),
            (
                "entry_type_minimum_probe_quantity_multiplier",
                strategy_settings.entry_type_minimum_probe_quantity_multiplier,
            ),
            (
                "small_account_a_plus_position_rate",
                strategy_settings.small_account_a_plus_position_rate,
            ),
            (
                "small_account_a_position_rate",
                strategy_settings.small_account_a_position_rate,
            ),
            (
                "small_account_b_position_rate",
                strategy_settings.small_account_b_position_rate,
            ),
            (
                "small_account_a_plus_per_trade_risk_rate",
                strategy_settings.small_account_a_plus_per_trade_risk_rate,
            ),
            (
                "small_account_a_per_trade_risk_rate",
                strategy_settings.small_account_a_per_trade_risk_rate,
            ),
            (
                "small_account_b_per_trade_risk_rate",
                strategy_settings.small_account_b_per_trade_risk_rate,
            ),
        ):
            if value <= 0 or value > 1:
                raise ValueError(f"{name} must be > 0 and <= 1")
        if strategy_settings.small_account_max_equity < 1:
            raise ValueError("small_account_max_equity must be >= 1")
        if strategy_settings.small_account_minimum_quantity_if_affordable < 0:
            raise ValueError(
                "small_account_minimum_quantity_if_affordable must be >= 0"
            )
        if strategy_settings.strategy_ab_active_mode not in {"normal", "remake"}:
            raise ValueError("strategy_ab_active_mode must be 'normal' or 'remake'")
        if strategy_settings.a_plus_armed_expiry_minutes_after_open <= 20:
            raise ValueError(
                "a_plus_armed_expiry_minutes_after_open must exceed 20"
            )
        if strategy_settings.a_plus_armed_min_money_flow_score < 0:
            raise ValueError("a_plus_armed_min_money_flow_score must be >= 0")
        if strategy_settings.data_gap_market_rank_max_age_minutes < 1:
            raise ValueError("data_gap_market_rank_max_age_minutes must be >= 1")
        if strategy_settings.data_gap_trade_amount_rank_limit < 1:
            raise ValueError("data_gap_trade_amount_rank_limit must be >= 1")
        if strategy_settings.data_gap_realtime_rank_limit < 1:
            raise ValueError("data_gap_realtime_rank_limit must be >= 1")
        if (
            risk_settings.per_trade_risk_rate <= 0
            or risk_settings.per_trade_risk_rate >= 1
        ):
            raise ValueError("1회 거래 허용 손실률은 0 초과 1 미만이어야 합니다.")
        if risk_settings.max_daily_stop_losses < 0:
            raise ValueError("max_daily_stop_losses must be >= 0")
        self.database = database
        self.candle_freshness = candle_freshness
        self.paper_engine = paper_engine
        self.strategy_settings = strategy_settings
        self.paper_settings = paper_settings
        self.risk_settings = risk_settings
        self.order_executor = order_executor or PaperOrderExecutor(paper_engine)
        self.order_safety_guard = OrderSafetyGuard(database)
        self.market_mode_settings = market_mode_settings or MarketModeGateSettings(
            enabled=False,
            manual_mode="neutral",
        )
        (
            self.strategy_version_id,
            self.strategy_config_id,
        ) = register_breakout_strategy(
            database,
            strategy_settings,
            paper_settings,
            risk_settings,
        )

    def evaluate(self, stock_code: str) -> BreakoutSignal:
        with self.database.connect() as connection:
            current = connection.execute(
                """
                SELECT current_price, open_price, high_price, low_price,
                       volume, collected_at
                FROM price_snapshots
                WHERE stock_code = ? AND current_price IS NOT NULL
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
            if current is None:
                raise PaperTradingError(f"현재가 스냅샷이 없습니다: {stock_code}")
            self.paper_engine.ensure_fresh_price(
                current["collected_at"],
                stock_code,
            )
            signal_day = current["collected_at"][:10]
            previous = connection.execute(
                """
                SELECT candle_time, high_price, close_price, volume
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'daily'
                  AND candle_time < ?
                ORDER BY candle_time DESC
                LIMIT 1
                """,
                (stock_code, signal_day),
            ).fetchone()
            minute_rows = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'minute'
                  AND interval_minutes = ?
                  AND substr(candle_time, 1, 10) = ?
                  AND close_price IS NOT NULL
                ORDER BY candle_time DESC
                """,
                (
                    stock_code,
                    self.strategy_settings.minute_interval,
                    signal_day,
                ),
            ).fetchall()
            if self.candle_freshness is not None:
                source_time = datetime.fromisoformat(current["collected_at"])
                readiness = self.candle_freshness.require_entry_data(
                    stock_code, source_time, self.strategy_settings.minute_interval)
                confirmed_cutoff = readiness[1]["confirmed_cutoff"]
                minute_rows = [bar for bar in minute_rows if bar["candle_time"] <= confirmed_cutoff]
            candidate = connection.execute(
                """
                SELECT information_blocked, information_score,
                       pattern_score, patterns_json, features_json,
                       evaluated_at
                FROM candidate_scores
                WHERE stock_code = ?
                  AND evaluated_at <= ?
                  AND evaluated_at >= ?
                ORDER BY evaluated_at DESC, id DESC
                LIMIT 1
                """,
                (
                    stock_code,
                    current["collected_at"],
                    (
                        datetime.fromisoformat(current["collected_at"])
                        - timedelta(
                            minutes=(
                                self.strategy_settings
                                .candidate_context_max_age_minutes
                            )
                        )
                    ).isoformat(timespec="seconds"),
                ),
            ).fetchone()
            order_books = connection.execute(
                """
                SELECT best_ask_price, best_ask_quantity,
                       best_bid_price, best_bid_quantity,
                       total_ask_quantity, total_bid_quantity,
                       collected_at
                FROM order_book_snapshots
                WHERE stock_code = ?
                  AND collected_at <= ?
                ORDER BY collected_at DESC, id DESC
                LIMIT ?
                """,
                (
                    stock_code,
                    current["collected_at"],
                    self.strategy_settings.order_flow_history_limit,
                ),
            ).fetchall()
            trade_strength_rows = connection.execute(
                """
                SELECT strength, accumulated_amount, current_price,
                       trade_quantity, collected_at
                FROM trade_strength_snapshots
                WHERE stock_code = ?
                  AND collected_at <= ?
                ORDER BY collected_at DESC, id DESC
                LIMIT ?
                """,
                (
                    stock_code,
                    current["collected_at"],
                    self.strategy_settings.order_flow_history_limit,
                ),
            ).fetchall()
            intraday_pattern = connection.execute(
                """
                SELECT *
                FROM intraday_pattern_evaluations
                WHERE stock_code = ?
                  AND interval_minutes = ?
                  AND source_data_time <= ?
                ORDER BY source_data_time DESC, id DESC
                LIMIT 1
                """,
                (
                    stock_code,
                    self.strategy_settings.minute_interval,
                    current["collected_at"],
                ),
            ).fetchone()
            supply = connection.execute(
                """
                SELECT *
                FROM supply_scores
                WHERE stock_code = ?
                  AND evaluated_at <= ?
                ORDER BY evaluated_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code, current["collected_at"]),
            ).fetchone()

        current_price = int(current["current_price"])
        candidate_context = _candidate_context(candidate)
        current_volume = (
            int(current["volume"]) if current["volume"] is not None else None
        )
        previous_high = (
            int(previous["high_price"])
            if previous is not None and previous["high_price"] is not None
            else None
        )
        previous_close = (
            int(previous["close_price"])
            if previous is not None and previous["close_price"] is not None
            else None
        )
        previous_volume = (
            int(previous["volume"])
            if previous is not None and previous["volume"] is not None
            else None
        )
        volume_ratio = (
            Decimal(current_volume) / Decimal(previous_volume)
            if current_volume is not None and previous_volume not in (None, 0)
            else None
        )
        projected_volume = self._project_volume(
            signal_time=current["collected_at"],
            current_volume=current_volume,
        )
        projected_volume_ratio = (
            Decimal(projected_volume) / Decimal(previous_volume)
            if projected_volume is not None and previous_volume not in (None, 0)
            else None
        )
        projected_trade_amount = (
            int(Decimal(current_price) * Decimal(projected_volume))
            if projected_volume is not None
            else None
        )
        moving_average_rows = minute_rows[: self.strategy_settings.moving_average_period]
        moving_average = (
            sum(Decimal(row["close_price"]) for row in moving_average_rows)
            / Decimal(len(moving_average_rows))
            if len(moving_average_rows) == self.strategy_settings.moving_average_period
            else None
        )
        intraday_high_rows = minute_rows[
            1 : self.strategy_settings.intraday_breakout_lookback + 1
        ]
        intraday_high_values = [
            int(row["high_price"])
            for row in intraday_high_rows
            if row["high_price"] is not None
        ]
        intraday_high = max(intraday_high_values) if intraday_high_values else None
        intraday_midline = self._calculate_intraday_midline(
            minute_rows,
            self.strategy_settings.intraday_midline_lookback,
        )
        vwap = self._calculate_vwap(reversed(minute_rows))
        minute_structure = self._minute_structure_context(minute_rows)
        day_gain_rate = (
            Decimal(current_price) / Decimal(previous_close) - Decimal("1")
            if previous_close not in (None, 0)
            else None
        )
        volume_pass = (
            projected_volume_ratio is not None
            and projected_volume_ratio >= self.strategy_settings.volume_multiple
        )
        liquidity_pass = (
            projected_trade_amount is not None
            and projected_trade_amount
            >= self.strategy_settings.minimum_projected_trade_amount
        )
        breakout_pass = (
            (previous_high is not None and current_price > previous_high)
            or (intraday_high is not None and current_price > intraday_high)
        )
        breakout_level = self._breakout_level(
            current_price=current_price,
            previous_high=previous_high,
            intraday_high=intraday_high,
        )
        breakout_hold_pass = self._breakout_hold_pass(
            current_price=current_price,
            breakout_level=breakout_level,
            latest_minute=minute_rows[0] if minute_rows else None,
        )
        vwap_pass = vwap is not None and Decimal(current_price) > vwap
        entry_candle_quality = self._entry_candle_quality_context(
            current=current,
            minute_rows=minute_rows,
            breakout_level=breakout_level,
            vwap=vwap,
            volume_pass=volume_pass,
        )
        moving_average_pass = (
            moving_average is not None
            and Decimal(current_price) > moving_average
        )
        chase_pass = day_gain_rate is not None and (
            self.strategy_settings.max_day_gain_rate <= Decimal("0")
            or day_gain_rate <= self.strategy_settings.max_day_gain_rate
        )
        pullback_depth_rate = (
            Decimal(intraday_high - current_price) / Decimal(intraday_high)
            if intraday_high not in (None, 0) and current_price < intraday_high
            else None
        )
        deep_pullback_blocked = (
            pullback_depth_rate is not None
            and pullback_depth_rate > self.strategy_settings.pullback_max_depth_rate
        )
        pullback_support_level = self._pullback_support_level(
            current_price=current_price,
            vwap=vwap,
            moving_average=moving_average,
            intraday_midline=intraday_midline,
        )
        pullback_support_pass = self._pullback_support_pass(
            current_price=current_price,
            support_level=pullback_support_level,
            recent_minutes=minute_rows[:3],
        )
        pullback_pass = (
            pullback_depth_rate is not None
            and pullback_depth_rate >= self.strategy_settings.pullback_min_depth_rate
            and pullback_depth_rate <= self.strategy_settings.pullback_max_depth_rate
            and pullback_support_pass
            and volume_pass
            and liquidity_pass
            and day_gain_rate is not None
            and day_gain_rate >= Decimal("0")
            and chase_pass
        )
        opening_range = self._opening_range_context(
            signal_time=current["collected_at"],
            current_price=current_price,
            minute_rows=minute_rows,
            volume_pass=volume_pass,
            liquidity_pass=liquidity_pass,
            vwap_pass=vwap_pass,
            chase_pass=chase_pass,
        )
        order_flow = self._order_flow_context(
            current_price=current_price,
            order_books=order_books,
            trade_strength_rows=trade_strength_rows,
        )
        order_flow_pass = self._order_flow_pass(order_flow)
        chart = evaluate_chart_trade(
            stock_code=stock_code,
            evaluated_at=current["collected_at"],
            current_price=current_price,
            features=candidate_context["features"],
            minute_rows=minute_rows,
            intraday_pattern=_json_intraday_pattern(intraday_pattern),
            vwap=vwap,
            intraday_high=intraday_high,
            breakout_level=breakout_level,
            previous_high=previous_high,
            order_flow_pass=order_flow_pass,
            trade_strength=order_flow["trade_strength"],
            information_score=candidate_context["information_score"],
        )
        return BreakoutSignal(
            stock_code=stock_code,
            signal_time=current["collected_at"],
            current_price=current_price,
            previous_high=previous_high,
            previous_close=previous_close,
            current_volume=current_volume,
            previous_volume=previous_volume,
            volume_ratio=volume_ratio,
            projected_volume_ratio=projected_volume_ratio,
            projected_trade_amount=projected_trade_amount,
            moving_average=moving_average,
            vwap=vwap,
            intraday_high=intraday_high,
            intraday_midline=intraday_midline,
            breakout_level=breakout_level,
            day_gain_rate=day_gain_rate,
            volume_pass=volume_pass,
            liquidity_pass=liquidity_pass,
            breakout_pass=breakout_pass,
            breakout_hold_pass=breakout_hold_pass,
            vwap_pass=vwap_pass,
            moving_average_pass=moving_average_pass,
            chase_pass=chase_pass,
            active_session_pass=self._active_session_pass(current["collected_at"]),
            pullback_depth_rate=pullback_depth_rate,
            pullback_pass=pullback_pass,
            pullback_support_level=pullback_support_level,
            pullback_support_pass=pullback_support_pass,
            deep_pullback_blocked=deep_pullback_blocked,
            order_book_spread_rate=order_flow["spread_rate"],
            order_book_bid_ask_ratio=order_flow["bid_ask_ratio"],
            trade_strength=order_flow["trade_strength"],
            order_flow_score=order_flow["score"],
            ask_wall_absorption=order_flow["ask_wall_absorption"],
            bid_wall_risk=order_flow["bid_wall_risk"],
            ask_quantity_change_rate=order_flow["ask_quantity_change_rate"],
            bid_quantity_change_rate=order_flow["bid_quantity_change_rate"],
            order_flow_reasons=tuple(order_flow["reasons"]),
            order_flow_pass=order_flow_pass,
            opening_range_high=opening_range["high"],
            opening_range_low=opening_range["low"],
            opening_range_volume=opening_range["volume"],
            opening_range_start=opening_range["start"],
            opening_range_end=opening_range["end"],
            opening_range_breakout_rate=opening_range["breakout_rate"],
            opening_range_pass=opening_range["passed"],
            information_blocked=candidate_context["information_blocked"],
            information_score=candidate_context["information_score"],
            pattern_score=candidate_context["pattern_score"],
            patterns=candidate_context["patterns"],
            chart=chart,
            supply=_supply_context(supply),
            entry_candle_quality=entry_candle_quality,
            recent_swing_low=minute_structure["recent_swing_low"],
            higher_low_pass=minute_structure["higher_low_pass"],
            minute_atr=minute_structure["minute_atr"],
            recent_candle_range=minute_structure["recent_candle_range"],
        )

    def decide(self, signal: BreakoutSignal) -> StrategyDecision:
        pattern_component = self._pattern_component(signal)
        information_component = self._information_component(signal)
        chart_component = self._chart_component(signal)
        supply_component = self._supply_component(signal)
        components = (
            ScoreComponent(
                name="volume",
                score=Decimal("100") if signal.volume_pass else Decimal("0"),
                weight=Decimal("0.13"),
                reason=(
                    "예상 거래량 조건 충족"
                    if signal.volume_pass
                    else "예상 거래량 조건 미충족"
                ),
            ),
            ScoreComponent(
                name="liquidity",
                score=Decimal("100") if signal.liquidity_pass else Decimal("0"),
                weight=Decimal("0.13"),
                reason=(
                    "예상 거래대금 500억원 이상"
                    if signal.liquidity_pass
                    else "예상 거래대금 500억원 미만"
                ),
            ),
            ScoreComponent(
                name="breakout",
                score=(
                    Decimal("100")
                    if signal.breakout_hold_pass
                    else (
                        Decimal("60")
                        if signal.breakout_pass
                        else Decimal("0")
                    )
                ),
                weight=Decimal("0.13"),
                reason=(
                    "전일 고가 또는 장중 고점 돌파 후 유지"
                    if signal.breakout_hold_pass
                    else (
                        "돌파했지만 유지 확인 부족"
                        if signal.breakout_pass
                        else "전일 고가·장중 고점 미돌파"
                    )
                ),
            ),
            self._pullback_component(signal),
            self._opening_range_component(signal),
            ScoreComponent(
                name="vwap",
                score=Decimal("100") if signal.vwap_pass else Decimal("0"),
                weight=Decimal("0.13"),
                reason="VWAP 상단" if signal.vwap_pass else "VWAP 조건 미충족",
            ),
            ScoreComponent(
                name="moving_average",
                score=Decimal("100") if signal.moving_average_pass else Decimal("0"),
                weight=Decimal("0.12"),
                reason="이동평균 상단" if signal.moving_average_pass else "이동평균 조건 미충족",
            ),
            ScoreComponent(
                name="chase_limit",
                score=Decimal("100") if signal.chase_pass else Decimal("0"),
                weight=Decimal("0.09"),
                reason="추격 제한 이내" if signal.chase_pass else "추격 제한 초과",
            ),
            self._order_flow_component(signal),
            ScoreComponent(
                name="active_session",
                score=Decimal("100") if signal.active_session_pass else Decimal("0"),
                weight=Decimal("0.04"),
                reason=(
                    "단타 집중 시간대"
                    if signal.active_session_pass
                    else "단타 비집중 시간대"
                ),
            ),
            pattern_component,
            chart_component,
            supply_component,
            information_component,
        )
        score = sum(
            (component.weighted_score for component in components),
            Decimal("0"),
        )
        chart_blocked = signal.chart is not None and signal.chart.blocked
        chart_force_entry = signal.chart is not None and signal.chart.force_entry
        supply_blocked = bool((signal.supply or {}).get("blocked"))
        action = DecisionAction.HOLD
        if not chart_blocked:
            if signal.passed or chart_force_entry:
                action = DecisionAction.BUY
        if signal.information_blocked and action is DecisionAction.BUY:
            reason = "뉴스·공시 위험은 data confidence와 수량 정책에서 반영"
        elif supply_blocked and action is DecisionAction.BUY:
            reason = (
                "수급 위험은 data confidence와 수량 정책에서 반영: "
                f"{(signal.supply or {}).get('blocked_reason') or '-'}"
            )
        elif chart_blocked and signal.chart is not None:
            reason = f"차트 위험 패턴 차단: {signal.chart.block_reason}"
        elif chart_force_entry and signal.chart is not None:
            reason = (
                "차트 기반 강제 진입: "
                f"{signal.chart.daily_setup_name} / "
                f"{signal.chart.intraday_trigger_name}"
            )
        elif signal.passed:
            reason = self._signal_reason(signal)
        else:
            reason = "기본 돌파 전략 조건 미충족"
        return StrategyDecision(
            strategy_name="breakout",
            stock_code=signal.stock_code,
            decided_at=datetime.fromisoformat(signal.signal_time),
            action=action,
            score=_clamp_score(score),
            confidence=Decimal("1"),
            reason=reason,
            components=components,
        )

    def run(
        self,
        account_name: str,
        stock_codes: list[str],
        *,
        execute: bool,
        now: Optional[datetime] = None,
    ) -> list[dict]:
        current = now or datetime.now(SEOUL_TIMEZONE)
        if current.tzinfo is not None:
            current = current.astimezone(SEOUL_TIMEZONE)
        entry_allowed = current.time().replace(tzinfo=None) < ENTRY_CUTOFF
        session_profile = self._session_profile(current)
        results = []
        for stock_code in stock_codes:
            try:
                if self.candle_freshness is not None:
                    self.candle_freshness.require_entry_data(
                        stock_code, current, self.strategy_settings.minute_interval)
                signal = self.evaluate(stock_code)
            except PaperTradingError as exc:
                results.append(
                    {
                        "stock_code": stock_code,
                        "passed": False,
                        "action": "failed",
                        "order_id": None,
                        "message": str(exc),
                        "signal": None,
                        "decision": None,
                        "decision_id": None,
                        "trade_plan": None,
                        "session_profile": session_profile,
                    }
                )
                continue
            decision = self.decide(signal)
            leader_score = self._latest_leader_score(signal.stock_code)
            market_rank_proxy = self._market_rank_proxy(signal.stock_code, current)
            leader_score_for_policy = leader_score
            if leader_score_for_policy is None and market_rank_proxy.get("usable"):
                leader_score_for_policy = self._proxy_leader_score(
                    stock_code=signal.stock_code,
                    market_rank_proxy=market_rank_proxy,
                )
            strategy_mode = self._strategy_mode(
                current,
                session_profile,
                leader_score=leader_score_for_policy,
                supply_score=signal.supply,
            )
            account = self._account_and_position(account_name, stock_code)
            provisional_plan = self._build_trade_plan(
                signal=signal,
                decision=decision,
                decision_id=1,
                planned_quantity=0,
                paper_probe=False,
                session_profile=session_profile,
                strategy_mode=strategy_mode,
            )
            provisional_grade = self._entry_grade(
                signal=signal,
                trade_plan=provisional_plan,
                leader_score=leader_score_for_policy,
                market_rank_proxy=market_rank_proxy,
                decision_score=decision.score,
            )
            provisional_money_flow = self._money_flow_policy(
                signal=signal,
                trade_plan=provisional_plan,
                grade_details=provisional_grade,
                leader_score=leader_score,
                market_rank_proxy=market_rank_proxy,
                now=current.replace(tzinfo=None),
            )
            armed_policy = self._a_plus_armed_policy(
                account_id=account["account_id"],
                signal=signal,
                trade_plan=provisional_plan,
                grade_details=provisional_grade,
                money_flow_policy=provisional_money_flow,
                leader_score=leader_score_for_policy,
                now=current,
            )
            if (
                decision.action is DecisionAction.HOLD
                and armed_policy.get("trigger_ready")
            ):
                decision = self._a_plus_armed_decision(decision, armed_policy)
            paper_probe = False
            if not (
                armed_policy.get("active")
                and not armed_policy.get("trigger_ready")
            ):
                paper_probe = self._paper_probe_pass(
                    signal,
                    decision,
                    session_profile=session_profile,
                    strategy_mode=strategy_mode,
                )
            if paper_probe:
                decision = self._paper_probe_decision(signal, decision)
            decision_blocked = bool(
                signal.information_blocked
                or (signal.chart is not None and signal.chart.blocked)
                or (signal.supply or {}).get("blocked")
            )
            collapse_unchanged = (
                decision.action is DecisionAction.HOLD
                and not decision_blocked
            )
            decision_id, decision_created = (
                self.database.save_strategy_decision_record(
                    decision,
                    account_name=account_name,
                    strategy_version_id=self.strategy_version_id,
                    strategy_config_id=self.strategy_config_id,
                    source_data_time=signal.signal_time,
                    collapse_unchanged=collapse_unchanged,
                    state_key=(
                        f"breakout:{decision.action.value}:{decision.reason}"
                    ),
                )
            )
            if decision_created and signal.chart is not None:
                self.database.save_chart_trade_decision(
                    decision_id=decision_id,
                    chart=signal.chart.as_dict(),
                )
            existing = self._existing_signal(account_name, signal)
            if existing is not None:
                if decision_created:
                    self.database.update_strategy_decision(
                        decision_id,
                        outcome=existing["action"],
                        message="이미 처리된 동일 시점 신호",
                        legacy_signal_id=existing["id"],
                    )
                    self._save_replay_snapshot(
                        decision_id,
                        signal,
                        decision,
                        replay_action=self._replay_action(
                            decision,
                            str(existing["action"] or ""),
                        ),
                        replay_reason="이미 처리된 동일 시점 신호",
                    )
                results.append(
                    {
                        "stock_code": stock_code,
                        "passed": signal.passed,
                        "action": existing["action"],
                        "order_id": existing["order_id"],
                        "message": "이미 처리된 동일 시점 신호",
                        "signal": signal,
                        "decision": decision,
                        "decision_id": decision_id,
                        "trade_plan": None,
                        "session_profile": session_profile,
                        "strategy_mode": strategy_mode,
                    }
                )
                continue
            action = "observe"
            message = "조건 미충족"
            order_id = None

            planned_quantity = 0
            trade_plan = None
            reentry_blocked = False
            reentry_policy: dict[str, Any] = {"status": "not_checked"}
            entry_guard_blocked = False
            entry_guard_message = ""
            entry_guard_policy: dict[str, Any] = {"status": "not_checked"}
            mode_shadow_compare: dict[str, Any] = {}
            strategy_ab_compare: dict[str, Any] = {}
            router_execution_policy: dict[str, Any] = {"status": "not_checked"}
            reentry_engine_execution_policy: dict[str, Any] = {
                "status": "allowed_non_reentry",
                "order_effect": "follow_entry_guard",
            }
            risk_override = False
            if decision.action is DecisionAction.BUY:
                trade_plan = self._build_trade_plan(
                    signal=signal,
                    decision=decision,
                    decision_id=decision_id,
                    planned_quantity=0,
                    paper_probe=paper_probe,
                    session_profile=session_profile,
                    strategy_mode=strategy_mode,
                )
                planned_quantity = self._calculate_quantity(
                    account["total_equity"],
                    account["cash_balance"],
                    signal.current_price,
                    decision.score,
                    account["position_value"],
                    stop_loss_price=trade_plan.stop_loss_price,
                    quantity_multiplier=strategy_mode.quantity_multiplier,
                )
                trade_plan = self._build_trade_plan(
                    signal=signal,
                    decision=decision,
                    decision_id=decision_id,
                    planned_quantity=planned_quantity,
                    paper_probe=paper_probe,
                    session_profile=session_profile,
                    strategy_mode=strategy_mode,
                )
                reentry_blocked, reentry_message, reentry_policy = (
                    self._reentry_policy(
                        signal=signal,
                        leader_score=leader_score,
                        now=current,
                        trade_plan=trade_plan,
                    )
                )
                trade_plan = replace(
                    trade_plan,
                    setup={
                        **trade_plan.setup,
                        "reentry_policy": reentry_policy,
                    },
                )
                entry_guard_blocked, entry_guard_message, entry_guard_policy = (
                    self._entry_guard_policy(
                        account_name=account_name,
                        account_id=account["account_id"],
                        account_total_equity=account["total_equity"],
                        signal=signal,
                        trade_plan=trade_plan,
                        now=current,
                        has_position=account["has_position"],
                        leader_score=leader_score_for_policy,
                        actual_leader_score=leader_score,
                        market_rank_proxy=market_rank_proxy,
                        decision_score=decision.score,
                        reentry_policy=reentry_policy,
                        armed_policy=armed_policy,
                    )
                )
                if not reentry_blocked:
                    self._apply_reentry_quantity_policy(
                        entry_guard_policy,
                        reentry_policy,
                    )
                strategy_playbook = self._strategy_playbook_policy(
                    signal=signal,
                    trade_plan=trade_plan,
                    entry_guard_policy=entry_guard_policy,
                    reentry_policy=reentry_policy,
                )
                data_confidence_policy = self._data_confidence_policy(
                    signal=signal,
                    entry_guard_policy=entry_guard_policy,
                )
                market_regime_playbook_policy = (
                    self._market_regime_playbook_policy(
                        strategy_playbook=strategy_playbook,
                        entry_guard_policy=entry_guard_policy,
                        data_confidence_policy=data_confidence_policy,
                    )
                )
                if entry_guard_blocked and self._data_confidence_guard_status(
                    entry_guard_policy.get("status")
                ):
                    entry_guard_blocked = False
                    entry_guard_message = ""
                    entry_guard_policy = {
                        **entry_guard_policy,
                        "status_before_data_confidence": entry_guard_policy.get(
                            "status"
                        ),
                        "status": "delegated_to_data_confidence",
                        "blocked_by_entry_guard": False,
                    }
                entry_guard_policy = self._apply_regime_quantity_policy(
                    entry_guard_policy=entry_guard_policy,
                    data_confidence_policy=data_confidence_policy,
                    market_regime_playbook_policy=market_regime_playbook_policy,
                )
                position_build_policy = self._position_build_policy(
                    signal=signal,
                    strategy_playbook=strategy_playbook,
                    entry_guard_policy=entry_guard_policy,
                    has_position=account["has_position"],
                    position_value=account["position_value"],
                    total_equity=account["total_equity"],
                )
                if position_build_policy.get("target_position_rate") is not None:
                    entry_guard_policy = {
                        **entry_guard_policy,
                        "target_position_rate": str(
                            position_build_policy["target_position_rate"]
                        ),
                        "position_build_policy": position_build_policy,
                    }
                entry_candle_quality_policy = self._entry_candle_quality_policy(
                    signal=signal,
                    trade_plan=trade_plan,
                    strategy_playbook=strategy_playbook,
                )
                legacy_entry_policy = self._legacy_entry_policy(
                    signal=signal,
                    trade_plan=trade_plan,
                    entry_guard_policy=entry_guard_policy,
                    strategy_playbook=strategy_playbook,
                )
                router_execution_policy = self._router_execution_policy(
                    trade_plan=trade_plan,
                    strategy_playbook=strategy_playbook,
                    legacy_entry_policy=legacy_entry_policy,
                    entry_candle_quality_policy=entry_candle_quality_policy,
                    position_build_policy=position_build_policy,
                    market_regime_playbook_policy=(
                        market_regime_playbook_policy
                    ),
                )
                legacy_entry_policy = {
                    **legacy_entry_policy,
                    "router_status": router_execution_policy.get("status"),
                    "new_logic_order_effect": router_execution_policy.get(
                        "order_effect"
                    ),
                    "new_logic_would_buy": router_execution_policy.get(
                        "order_effect"
                    )
                    in {"allow_order", "allow_minimum_probe"},
                    "removed_from_order_influence": (
                        bool(legacy_entry_policy.get("legacy_candidate"))
                        and router_execution_policy.get("order_effect")
                        == "shadow_only"
                    ),
                    "downgraded_to_minimum_probe": (
                        bool(legacy_entry_policy.get("legacy_candidate"))
                        and router_execution_policy.get("order_effect")
                        == "allow_minimum_probe"
                    ),
                }
                mode_shadow_compare = self._mode_shadow_compare(
                    account_name=account_name,
                    account_id=account["account_id"],
                    account_total_equity=account["total_equity"],
                    cash_balance=account["cash_balance"],
                    position_value=account["position_value"],
                    signal=signal,
                    trade_plan=trade_plan,
                    now=current,
                    has_position=account["has_position"],
                    leader_score=leader_score_for_policy,
                    actual_leader_score=leader_score,
                    market_rank_proxy=market_rank_proxy,
                    decision_score=decision.score,
                    base_planned_quantity=planned_quantity,
                    reentry_blocked=reentry_blocked,
                    reentry_policy=reentry_policy,
                    actual_policy=entry_guard_policy,
                    armed_policy=armed_policy,
                )
                strategy_ab_compare = self._strategy_ab_compare(
                    account_total_equity=account["total_equity"],
                    cash_balance=account["cash_balance"],
                    position_value=account["position_value"],
                    signal=signal,
                    trade_plan=trade_plan,
                    decision_score=decision.score,
                    base_planned_quantity=planned_quantity,
                    reentry_blocked=reentry_blocked,
                    entry_guard_blocked=entry_guard_blocked,
                    reentry_policy=reentry_policy,
                    entry_guard_policy=entry_guard_policy,
                    router_execution_policy=router_execution_policy,
                    legacy_entry_policy=legacy_entry_policy,
                    entry_candle_quality_policy=entry_candle_quality_policy,
                    strategy_playbook=strategy_playbook,
                )
                quantity_multiplier = _decimal_or_none(
                    entry_guard_policy.get("quantity_multiplier")
                )
                target_position_rate = _decimal_or_none(
                    entry_guard_policy.get("target_position_rate")
                )
                if not entry_guard_blocked and target_position_rate is not None:
                    effective_multiplier = quantity_multiplier or Decimal("1")
                    effective_target_rate = target_position_rate * effective_multiplier
                    per_trade_risk_rate = _decimal_or_none(
                        entry_guard_policy.get("per_trade_risk_rate")
                    )
                    if per_trade_risk_rate is not None:
                        per_trade_risk_rate *= effective_multiplier
                    planned_quantity = self._calculate_quantity(
                        account["total_equity"],
                        account["cash_balance"],
                        signal.current_price,
                        decision.score,
                        account["position_value"],
                        stop_loss_price=trade_plan.stop_loss_price,
                        target_position_rate=effective_target_rate,
                        per_trade_risk_rate=per_trade_risk_rate,
                        minimum_quantity_if_affordable=int(
                            entry_guard_policy.get(
                                "minimum_quantity_if_affordable",
                                0,
                            )
                            or 0
                        ),
                    )
                    max_quantity = entry_guard_policy.get("max_quantity")
                    if max_quantity is not None:
                        planned_quantity = min(planned_quantity, int(max_quantity))
                    trade_plan = replace(
                        trade_plan,
                        planned_quantity=planned_quantity,
                    )
                elif not entry_guard_blocked and quantity_multiplier is not None:
                    planned_quantity = self._apply_entry_guard_quantity(
                        planned_quantity,
                        entry_guard_policy,
                    )
                    trade_plan = replace(
                        trade_plan,
                        planned_quantity=planned_quantity,
                    )
                router_max_quantity = router_execution_policy.get("max_quantity")
                if (
                    not entry_guard_blocked
                    and router_execution_policy.get("order_effect")
                    == "allow_minimum_probe"
                    and router_max_quantity is not None
                ):
                    planned_quantity = min(
                        planned_quantity,
                        int(router_max_quantity),
                    )
                    trade_plan = replace(
                        trade_plan,
                        planned_quantity=planned_quantity,
                    )
                risk_override = bool(entry_guard_policy.get("risk_override"))
                reentry_engine_execution_policy = (
                    entry_guard_policy.get("reentry_engine_policy") or {}
                )
                trade_plan = replace(
                    trade_plan,
                    setup={
                        **trade_plan.setup,
                        "entry_guard_policy": entry_guard_policy,
                        "entry_grade": entry_guard_policy.get("entry_grade"),
                        "money_flow_policy": entry_guard_policy.get(
                            "money_flow_policy"
                        ),
                        "risk_mode": entry_guard_policy.get("risk_mode"),
                        "add_buy_policy": entry_guard_policy.get("add_buy_policy"),
                        "entry_reentry_policy": entry_guard_policy.get(
                            "reentry_policy"
                        ),
                        "reentry_engine_policy": entry_guard_policy.get(
                            "reentry_engine_policy"
                        ),
                        "stop_policy": entry_guard_policy.get("stop_policy"),
                        "entry_type_policy": entry_guard_policy.get(
                            "entry_type_policy"
                        ),
                        "data_availability_policy": entry_guard_policy.get(
                            "data_availability"
                        ),
                        "supply_hard_block_policy": entry_guard_policy.get(
                            "supply_block_policy"
                        ),
                        "mode_shadow_compare": mode_shadow_compare,
                        "strategy_ab_compare": strategy_ab_compare,
                        "aggressive_mode_policy": entry_guard_policy.get(
                            "small_account_aggressive"
                        ),
                        "strategy_playbook": strategy_playbook,
                        "data_confidence_policy": data_confidence_policy,
                        "market_regime_playbook_policy": (
                            market_regime_playbook_policy
                        ),
                        "position_build_policy": position_build_policy,
                        "a_plus_armed_policy": armed_policy,
                        "entry_candle_quality_policy": entry_candle_quality_policy,
                        "router_execution_policy": router_execution_policy,
                        "legacy_entry_policy": legacy_entry_policy,
                        "residual_exit_rule": (
                            strategy_playbook.get("residual_exit_rule")
                            or trade_plan.setup.get("residual_exit_rule")
                        ),
                        "aggressive_exit_policy": (
                            {
                                "active": True,
                                "mode": "small_account_aggressive",
                                "entry_grade": entry_guard_policy.get("entry_grade"),
                                "playbook": strategy_playbook.get("name"),
                                "first_profit_rate_range": "0.02~0.03",
                                "fast_partial_profit_rate": (
                                    strategy_playbook.get("fast_partial_profit_rate")
                                    or "0.03"
                                ),
                                "fast_partial_min_r": (
                                    strategy_playbook.get("fast_partial_min_r")
                                    or "0.35"
                                ),
                                "residual_protection": (
                                    strategy_playbook.get("residual_protection")
                                    or "vwap_5m_ma_previous_low_or_trailing"
                                ),
                                "structure_break_exit": True,
                                "midday_time_or_tight_trailing": (
                                    strategy_playbook.get("name")
                                    != "a_plus_trend_day_runner"
                                ),
                            }
                            if (
                                (
                                    entry_guard_policy.get(
                                        "small_account_aggressive"
                                    )
                                    or {}
                                ).get("active")
                                and entry_guard_policy.get("entry_grade") == "A+"
                            )
                            else {"active": False}
                        ),
                    },
                )
            if (
                decision.action is DecisionAction.BUY
                and reentry_blocked
            ):
                action = "skipped"
                message = reentry_message
            elif (
                decision.action is DecisionAction.BUY
                and entry_guard_blocked
            ):
                action = "skipped"
                message = entry_guard_message
            elif (
                decision.action is DecisionAction.BUY
                and reentry_engine_execution_policy.get("order_effect") == "block"
            ):
                action = "skipped"
                message = (
                    "통합 재진입 엔진 차단: "
                    f"{reentry_engine_execution_policy.get('status')}"
                )
            elif (
                decision.action is DecisionAction.BUY
                and not strategy_mode.entry_allowed
                and self.market_mode_settings.entry_allowed
            ):
                action = "skipped"
                message = f"{strategy_mode.label}: {strategy_mode.reason}"
            elif (
                decision.action is DecisionAction.BUY
                and decision.score < strategy_mode.minimum_score
                and not self._chart_overrides_session_score(signal)
            ):
                action = "skipped"
                message = (
                    f"{strategy_mode.label}: 점수 {decision.score:.1f}점, "
                    f"필요 {strategy_mode.minimum_score:.1f}점 미만"
                )
            elif (
                trade_plan is not None
                and not session_profile.allows_entry_type(trade_plan.entry_type)
                and not self._chart_overrides_session_entry_type(signal)
            ):
                action = "skipped"
                message = (
                    f"{session_profile.label}: {trade_plan.entry_type} 진입 유형 제한"
                )
            elif decision.action is DecisionAction.BUY and not entry_allowed:
                action = "skipped"
                message = "장 마감 청산 구간에는 신규 진입하지 않음"
            elif (
                decision.action is DecisionAction.BUY
                and not self.market_mode_settings.entry_allowed
                and market_regime_playbook_policy.get("order_effect")
                not in {"allow_order", "allow_reduced", "shadow_only"}
            ):
                action = "skipped"
                message = self._market_mode_gate_message()
            elif (
                decision.action is DecisionAction.BUY
                and router_execution_policy.get("order_effect") == "shadow_only"
                and (
                    not strategy_ab_compare
                    or strategy_ab_compare.get("actual_mode") != "normal"
                )
            ):
                action = "observe"
                message = str(
                    router_execution_policy.get("message")
                    or "router shadow-only route"
                )
            elif decision.action is DecisionAction.BUY and execute:
                quantity = planned_quantity
                if quantity < 1:
                    action = "skipped"
                    buy_diagnostics = self._minimum_buy_diagnostics(
                        account=account,
                        signal=signal,
                        trade_plan=trade_plan,
                    )
                    if trade_plan is not None:
                        trade_plan = replace(
                            trade_plan,
                            setup={
                                **trade_plan.setup,
                                "minimum_buy_diagnostics": buy_diagnostics,
                            },
                        )
                    message = "점수·현금·손절 리스크 한도로 1주도 추가 매수할 수 없음"
                    message = self._minimum_buy_message(buy_diagnostics)
                else:
                    request = OrderRequest(
                        account_name=account_name,
                        stock_code=stock_code,
                        side=OrderSide.BUY,
                        quantity=quantity,
                        decision=decision,
                        decision_id=decision_id,
                        reason=decision.reason,
                        client_order_id=(
                            f"strategy:{account_name}:{stock_code}:"
                            f"{signal.signal_time}"
                        ),
                        risk_override=risk_override,
                        reentry_approval_lock_id=(
                            int(
                                reentry_engine_execution_policy[
                                    "approved_reentry_lock_id"
                                ]
                            )
                            if reentry_engine_execution_policy.get(
                                "approved_reentry_lock_id"
                            )
                            else None
                        ),
                    )
                    safety = self.order_safety_guard.verify_before_submit(
                        request,
                        execution_mode="paper",
                        current_price=signal.current_price,
                        expected_price=trade_plan.entry_price if trade_plan else None,
                        order_book_pass=signal.order_flow_pass or not signal.bid_wall_risk,
                        leader_mode={
                            **strategy_mode.as_dict(),
                            "entry_allowed": (
                                strategy_mode.entry_allowed
                                or market_regime_playbook_policy.get(
                                    "order_effect"
                                )
                                == "allow_reduced"
                            ),
                            "market_regime_override": (
                                market_regime_playbook_policy
                                if market_regime_playbook_policy.get(
                                    "order_effect"
                                )
                                == "allow_reduced"
                                else None
                            ),
                        },
                        supply_score=signal.supply,
                        allow_supply_blocked_paper=(
                            (
                                entry_guard_policy.get("supply_block_policy")
                                or {}
                            ).get("status")
                            == "relaxed"
                            or data_confidence_policy.get("order_effect")
                            in {"allow_order", "reduce_quantity"}
                        ),
                        chart_blocked=(
                            signal.chart.blocked
                            if signal.chart is not None
                            else False
                        ),
                        market_mode_allowed=(
                            self.market_mode_settings.entry_allowed
                            or market_regime_playbook_policy.get("order_effect")
                            == "allow_reduced"
                        ),
                    )
                    if not safety.passed:
                        action = "skipped"
                        message = f"주문 직전 안전장치 차단: {safety.reason_text}"
                    else:
                        try:
                            execution = self.order_executor.submit(request)
                            action = "executed"
                            order_id = execution.internal_order_id
                            approved_lock_id = reentry_engine_execution_policy.get(
                                "approved_reentry_lock_id"
                            )
                            if approved_lock_id is not None:
                                self.database.release_reentry_lock(
                                    int(approved_lock_id),
                                    release_reason=(
                                        "approved_setup_buy_executed:"
                                        f"decision={decision_id}:order={order_id}"
                                    ),
                                    released_at=current.replace(
                                        tzinfo=None
                                    ).isoformat(timespec="seconds"),
                                )
                            if armed_policy.get("active"):
                                self.database.save_armed_candidate(
                                    account_id=account["account_id"],
                                    stock_code=stock_code,
                                    trading_day=str(armed_policy["trading_day"]),
                                    status="entered",
                                    observed_at=current.replace(
                                        tzinfo=None
                                    ).isoformat(timespec="seconds"),
                                    expires_at=str(armed_policy["expires_at"]),
                                    trigger_type=armed_policy.get("trigger_type"),
                                    triggered_at=current.replace(
                                        tzinfo=None
                                    ).isoformat(timespec="seconds"),
                                    evidence={
                                        **armed_policy,
                                        "status": "entered",
                                        "order_id": order_id,
                                    },
                                )
                            order_label = "추가 매수" if account["has_position"] else "매수"
                            message = (
                                f"모의 검증 진입 {quantity}주 {order_label}"
                                if paper_probe
                                else f"기본 돌파 전략 {quantity}주 {order_label}"
                            )
                        except OrderExecutionError as exc:
                            action = "failed"
                            message = str(exc)
            elif decision.action is DecisionAction.BUY:
                message = "모의 검증 진입 조건 충족" if paper_probe else "매수 신호 충족"

            stored_trade_plan = trade_plan if action != "skipped" else None
            if stored_trade_plan is not None:
                self.database.save_trade_plan(stored_trade_plan)

            stored = self._store_signal(
                account["account_id"],
                signal,
                action,
                order_id,
                message,
            )
            if decision_created:
                self.database.update_strategy_decision(
                    decision_id,
                    outcome=stored["action"],
                    message=stored["message"],
                    legacy_signal_id=stored["id"],
                )
                self._save_replay_snapshot(
                    decision_id,
                    signal,
                    decision,
                    replay_action=self._replay_action(
                        decision,
                        str(stored["action"]),
                    ),
                    replay_reason=str(stored["message"]),
                    trade_plan=trade_plan,
                )
            results.append(
                {
                    "stock_code": stock_code,
                    "passed": signal.passed,
                    "action": stored["action"],
                    "order_id": stored["order_id"],
                    "message": stored["message"],
                    "signal": signal,
                    "decision": decision,
                    "decision_id": decision_id,
                    "trade_plan": stored_trade_plan,
                    "session_profile": session_profile,
                    "strategy_mode": strategy_mode,
                }
            )
        return results

    def _session_profile(self, current: datetime) -> SessionProfile:
        if not self.strategy_settings.session_profile_enabled:
            return ALWAYS_ACTIVE_PROFILE
        return session_profile_for(current)

    def _strategy_mode(
        self,
        current: datetime,
        session_profile: SessionProfile,
        *,
        leader_score: dict[str, Any] | None,
        supply_score: dict[str, Any] | None,
    ) -> IntradayStrategyMode:
        return intraday_strategy_mode_for(
            current,
            session_profile,
            leader_score=leader_score,
            supply_score=supply_score,
            market_mode_enabled=self.market_mode_settings.enabled,
            market_mode=self.market_mode_settings.manual_mode,
            market_mode_reason=self.market_mode_settings.reason,
            market_mode_quantity_multiplier=(
                self.market_mode_settings.quantity_multiplier
                if self.market_mode_settings.order_effect == "paper_apply"
                else Decimal("1")
            ),
        )

    def _paper_probe_pass(
        self,
        signal: BreakoutSignal,
        decision: StrategyDecision,
        *,
        session_profile: SessionProfile,
        strategy_mode: IntradayStrategyMode,
    ) -> bool:
        if signal.chart is not None and signal.chart.blocked:
            return False
        if signal.bid_wall_risk:
            return False
        if signal.passed:
            return False
        if (
            not session_profile.paper_probe_allowed
            or not strategy_mode.paper_probe_allowed
        ):
            return False
        if not self.strategy_settings.paper_probe_enabled:
            return False
        if not isinstance(self.order_executor, PaperOrderExecutor):
            return False
        if not signal.chase_pass:
            return False
        if not signal.liquidity_pass:
            return False
        if decision.score < max(
            strategy_mode.minimum_score,
            session_profile.paper_probe_threshold(
                self.strategy_settings.paper_probe_min_score
            ),
        ):
            return False
        return (
            signal.breakout_hold_pass
            or signal.opening_range_pass
            or signal.pullback_pass
            or (
                signal.order_flow_pass
                and signal.vwap_pass
                and signal.moving_average_pass
            )
        )

    @staticmethod
    def _chart_overrides_session_score(signal: BreakoutSignal) -> bool:
        return (
            signal.chart is not None
            and signal.chart.force_entry
            and not signal.chart.blocked
            and signal.chart.total_score >= Decimal("62")
        )

    @staticmethod
    def _chart_overrides_session_entry_type(signal: BreakoutSignal) -> bool:
        return (
            signal.chart is not None
            and signal.chart.force_entry
            and not signal.chart.blocked
            and signal.chart.entry_type.startswith("chart_")
        )

    def _market_mode_gate_message(self) -> str:
        settings = self.market_mode_settings
        parts = [
            "시장 모드 게이트",
            f"적용={settings.manual_mode}",
            f"요청={settings.requested_mode}",
            f"source={settings.source}",
        ]
        if settings.reason:
            parts.append(settings.reason)
        parts.append("신규 진입 차단")
        return ": " + " · ".join(parts)

    def _entry_guard_policy(
        self,
        *,
        account_name: str,
        account_id: int,
        account_total_equity: int | None = None,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        now: datetime,
        has_position: bool,
        leader_score: dict[str, Any] | None,
        decision_score: Decimal,
        actual_leader_score: dict[str, Any] | None = None,
        market_rank_proxy: dict[str, Any] | None = None,
        small_account_aggressive_enabled_override: bool | None = None,
        reentry_policy: dict[str, Any] | None = None,
        armed_policy: dict[str, Any] | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        timestamp = now.replace(tzinfo=None)
        day = timestamp.date().isoformat()
        settings = self.strategy_settings
        if actual_leader_score is None and not bool((leader_score or {}).get("proxy")):
            actual_leader_score = leader_score
        market_rank_proxy = market_rank_proxy or self._market_rank_proxy(
            signal.stock_code,
            now,
        )
        grade_details = self._entry_grade(
            signal=signal,
            trade_plan=trade_plan,
            leader_score=leader_score,
            market_rank_proxy=market_rank_proxy,
            decision_score=decision_score,
        )
        entry_grade = str(grade_details["grade"])
        money_flow_policy = self._money_flow_policy(
            signal=signal,
            trade_plan=trade_plan,
            grade_details=grade_details,
            leader_score=actual_leader_score or leader_score,
            market_rank_proxy=market_rank_proxy,
            now=timestamp,
        )
        armed_policy = armed_policy or {}
        if armed_policy.get("trigger_ready"):
            money_flow_policy = {
                **money_flow_policy,
                "a_plus_qualified": True,
                "a_plus_qualified_source": "v0.12.92_armed_trigger",
                "armed_trigger_type": armed_policy.get("trigger_type"),
            }
        policy: dict[str, Any] = {
            "status": "allowed",
            "day": day,
            "has_position": has_position,
            "entry_type": trade_plan.entry_type,
            "entry_grade": entry_grade,
            "entry_grade_details": grade_details,
            "money_flow_policy": money_flow_policy,
            "a_plus_armed_policy": armed_policy,
            "risk_mode": "normal",
            "quantity_multiplier": "1",
            "target_position_rate": None,
            "per_trade_risk_rate": None,
            "max_quantity": None,
            "risk_override": False,
            "add_buy_policy": self._add_buy_policy(entry_grade),
            "reentry_policy": "allowed",
            "source_reentry_policy": reentry_policy or {},
            "stop_policy": self._stop_policy(entry_grade, signal),
            "data_availability": {},
            "entry_type_policy": {},
            "supply_block_policy": {},
            "same_level_failure_policy": {},
            "reentry_engine_policy": {},
            "small_account_aggressive": {},
            "morning_entry_guard_enabled": settings.morning_entry_guard_enabled,
            "morning_window": {
                "start_minutes": settings.morning_entry_limit_start_minutes,
                "end_minutes": settings.morning_entry_limit_end_minutes,
                "max_new_buys": settings.morning_max_new_buys,
                "max_a_grade_buys": settings.morning_max_a_grade_buys,
                "max_b_grade_buys": settings.morning_max_b_grade_buys,
            },
            "bucket": {
                "minutes": settings.entry_bucket_minutes,
                "max_new_buys": settings.max_new_buys_per_bucket,
            },
            "same_stock": {
                "max_buys_per_day": settings.max_same_stock_buys_per_day,
            },
            "risk": {
                "daily_max_loss_rate": str(self.risk_settings.daily_max_loss_rate),
                "max_daily_stop_losses": self.risk_settings.max_daily_stop_losses,
            },
        }

        def finalize_reentry_engine() -> None:
            policy["reentry_engine_policy"] = self._reentry_engine_policy(
                reentry_policy=policy.get("source_reentry_policy") or {},
                entry_type_policy=policy.get("entry_type_policy") or {},
                same_level_failure_policy=policy.get("same_level_failure_policy")
                or {},
                entry_guard_status=str(policy.get("status") or "allowed"),
            )

        data_policy = self._data_gap_policy(
            signal=signal,
            trade_plan=trade_plan,
            entry_grade=entry_grade,
            leader_score=actual_leader_score,
            market_rank_proxy=market_rank_proxy,
            decision_score=decision_score,
            money_flow_policy=money_flow_policy,
            now=timestamp,
        )
        policy["data_availability"] = data_policy
        small_account_policy = self._small_account_aggressive_policy(
            account_name=account_name,
            account_total_equity=account_total_equity,
            signal=signal,
            trade_plan=trade_plan,
            entry_grade=entry_grade,
            grade_details=grade_details,
            data_policy=data_policy,
            leader_score=actual_leader_score,
            money_flow_policy=money_flow_policy,
            now=timestamp,
            enabled_override=small_account_aggressive_enabled_override,
            armed_policy=armed_policy,
        )
        policy["small_account_aggressive"] = small_account_policy
        if small_account_policy.get("blocked"):
            policy["status"] = str(
                small_account_policy.get("status")
                or "blocked_small_account_aggressive"
            )
            finalize_reentry_engine()
            return (
                True,
                str(
                    small_account_policy.get("message")
                    or "small account aggressive blocked"
                ),
                policy,
            )
        if data_policy.get("add_buy_policy") == "blocked":
            policy["add_buy_policy"] = "blocked"
            policy["reentry_policy"] = "blocked_until_data_collected"
        if data_policy.get("minimum_probe"):
            current_multiplier = (
                _decimal_or_none(policy.get("quantity_multiplier")) or Decimal("1")
            )
            probe_multiplier = (
                _decimal_or_none(data_policy.get("quantity_multiplier"))
                or settings.data_gap_minimum_probe_quantity_multiplier
            )
            policy["quantity_multiplier"] = str(
                min(current_multiplier, probe_multiplier)
            )
            policy["max_quantity"] = data_policy.get("max_quantity")
            policy["status"] = "allowed_minimum_probe_data_gap"
        if data_policy.get("blocked"):
            policy["status"] = str(data_policy.get("status") or "blocked_data_gap")
            finalize_reentry_engine()
            return (
                True,
                str(data_policy.get("message") or "data gap blocked"),
                policy,
            )
        if small_account_policy.get("target_position_rate"):
            policy["status"] = str(
                small_account_policy.get("status")
                or "allowed_small_account_aggressive"
            )
            if small_account_policy.get("grade") == "A+":
                policy["entry_grade"] = "A+"
                policy["entry_grade_details"] = {
                    **grade_details,
                    "grade": "A+",
                    "small_account_grade": "A+",
                }
            policy["target_position_rate"] = str(
                small_account_policy["target_position_rate"]
            )
            policy["per_trade_risk_rate"] = str(
                small_account_policy["per_trade_risk_rate"]
            )
            policy["minimum_quantity_if_affordable"] = int(
                small_account_policy.get("minimum_quantity_if_affordable", 0)
                or 0
            )
            if small_account_policy.get("max_quantity") is not None:
                policy["max_quantity"] = small_account_policy.get("max_quantity")
        elif (
            entry_grade == "A"
            and money_flow_policy.get("a_plus_qualified")
            and self._a_plus_entry_type(trade_plan.entry_type)
        ):
            policy["entry_grade"] = "A+"
            policy["entry_grade_details"] = {
                **grade_details,
                "grade": "A+",
                "money_flow_grade": "A+",
            }
            policy["status"] = "allowed_a_plus_money_flow"
        with self.database.connect() as connection:
            daily_realized = connection.execute(
                """
                SELECT COALESCE(SUM(f.realized_pnl), 0)
                FROM paper_fills f
                JOIN paper_orders o ON o.id = f.order_id
                WHERE o.account_id = ?
                  AND f.side = 'sell'
                  AND substr(f.filled_at, 1, 10) = ?
                """,
                (account_id, day),
            ).fetchone()[0]
            account = connection.execute(
                "SELECT initial_cash FROM paper_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            initial_cash = int(account["initial_cash"]) if account is not None else 0
            stop_loss_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_risk_events
                WHERE account_id = ?
                  AND event_type = 'stop_loss'
                  AND status = 'executed'
                  AND substr(occurred_at, 1, 10) = ?
                """,
                (account_id, day),
            ).fetchone()[0]
            total_buy_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_orders
                WHERE account_id = ?
                  AND side = 'buy'
                  AND status = 'filled'
                  AND substr(requested_at, 1, 10) = ?
                """,
                (account_id, day),
            ).fetchone()[0]
            same_stock_buy_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_orders
                WHERE account_id = ?
                  AND stock_code = ?
                  AND side = 'buy'
                  AND status = 'filled'
                  AND substr(requested_at, 1, 10) = ?
                """,
                (account_id, signal.stock_code, day),
            ).fetchone()[0]
            daily_order_rows = connection.execute(
                """
                SELECT o.stock_code, o.requested_at, tp.setup_json
                FROM paper_orders o
                LEFT JOIN order_requests req
                  ON req.client_order_id = o.client_order_id
                LEFT JOIN trade_plans tp
                  ON tp.decision_id = req.decision_id
                WHERE o.account_id = ?
                  AND o.side = 'buy'
                  AND o.status = 'filled'
                  AND substr(o.requested_at, 1, 10) = ?
                """,
                (account_id, day),
            ).fetchall()
            data_gap_probe_attempt_count = (
                self._data_gap_probe_attempt_count(
                    connection,
                    account_id=account_id,
                    stock_code=signal.stock_code,
                    day=day,
                    entry_type=trade_plan.entry_type,
                )
            )

            minutes_since_open = (
                (timestamp.hour * 60 + timestamp.minute) - (9 * 60)
            )
            in_morning_window = (
                settings.morning_entry_guard_enabled
                and settings.morning_entry_limit_start_minutes
                <= minutes_since_open
                < settings.morning_entry_limit_end_minutes
            )
            bucket_start_minute = (
                (timestamp.hour * 60 + timestamp.minute)
                // settings.entry_bucket_minutes
                * settings.entry_bucket_minutes
            )
            bucket_start = timestamp.replace(
                hour=bucket_start_minute // 60,
                minute=bucket_start_minute % 60,
                second=0,
                microsecond=0,
            )
            bucket_end = bucket_start + timedelta(
                minutes=settings.entry_bucket_minutes
            )
            bucket_buy_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_orders
                WHERE account_id = ?
                  AND side = 'buy'
                  AND status = 'filled'
                  AND requested_at >= ?
                  AND requested_at < ?
                """,
                (
                    account_id,
                    bucket_start.isoformat(timespec="seconds"),
                    bucket_end.isoformat(timespec="seconds"),
                ),
            ).fetchone()[0]
            bucket_start_text = bucket_start.isoformat(timespec="seconds")
            bucket_end_text = bucket_end.isoformat(timespec="seconds")
            morning_start = timestamp.replace(
                hour=9,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(minutes=settings.morning_entry_limit_start_minutes)
            morning_end = timestamp.replace(
                hour=9,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(minutes=settings.morning_entry_limit_end_minutes)
            morning_start_text = morning_start.isoformat(timespec="seconds")
            morning_end_text = morning_end.isoformat(timespec="seconds")
            bucket_grade_counts = self._order_grade_counts(
                [
                    row
                    for row in daily_order_rows
                    if bucket_start_text <= str(row["requested_at"] or "") < bucket_end_text
                ]
            )
            morning_grade_counts = self._order_grade_counts(
                [
                    row
                    for row in daily_order_rows
                    if morning_start_text
                    <= str(row["requested_at"] or "")
                    < morning_end_text
                ]
            )
            partial_exit_row = connection.execute(
                """
                SELECT 1
                FROM trade_plans tp
                JOIN strategy_decisions d ON d.id = tp.decision_id
                WHERE d.account_id = ?
                  AND tp.stock_code = ?
                  AND tp.partial_exit_executed = 1
                  AND (
                    substr(tp.partial_exit_at, 1, 10) = ?
                    OR substr(d.decided_at, 1, 10) = ?
                  )
                ORDER BY tp.partial_exit_at DESC, tp.id DESC
                LIMIT 1
                """,
                (account_id, signal.stock_code, day, day),
            ).fetchone()

        loss_limit = int(Decimal(initial_cash) * self.risk_settings.daily_max_loss_rate)
        policy["risk"].update(
            {
                "daily_realized_pnl": int(daily_realized or 0),
                "daily_loss_limit": -loss_limit,
                "stop_loss_count": int(stop_loss_count),
            }
        )
        risk_mode = "normal"
        if loss_limit > 0 and int(daily_realized or 0) <= -loss_limit:
            risk_mode = "emergency"
        elif (
            self.risk_settings.max_daily_stop_losses > 0
            and stop_loss_count >= self.risk_settings.max_daily_stop_losses
        ):
            risk_mode = "cooldown"
        elif (
            loss_limit > 0
            and int(daily_realized or 0) <= -max(1, loss_limit // 2)
        ) or (
            self.risk_settings.max_daily_stop_losses > 1
            and stop_loss_count >= self.risk_settings.max_daily_stop_losses - 1
        ):
            risk_mode = "caution"
        policy["risk_mode"] = risk_mode
        policy["morning_window"].update(
            {
                "in_window": in_morning_window,
                "buy_count": int(total_buy_count),
                "grade_counts": morning_grade_counts,
            }
        )
        policy["bucket"].update(
            {
                "start": bucket_start.isoformat(timespec="seconds"),
                "end": bucket_end.isoformat(timespec="seconds"),
                "buy_count": int(bucket_buy_count),
                "grade_counts": bucket_grade_counts,
            }
        )
        partial_exit_done = partial_exit_row is not None
        new_high_breakout = (
            signal.previous_high is not None
            and signal.current_price > int(signal.previous_high)
        )
        vwap_reclaimed = (
            signal.vwap is not None
            and Decimal(signal.current_price) >= signal.vwap
        ) or signal.vwap_pass
        repeat_allowed = (
            entry_grade == "A"
            or (entry_grade == "B" and partial_exit_done)
            or (partial_exit_done and new_high_breakout and vwap_reclaimed)
        )
        policy["same_stock"].update(
            {
                "buy_count": int(same_stock_buy_count),
                "partial_exit_done": partial_exit_done,
                "new_high_breakout": new_high_breakout,
                "vwap_reclaimed": vwap_reclaimed,
                "repeat_allowed": repeat_allowed,
                "data_gap_probe_attempt_count": int(
                    data_gap_probe_attempt_count
                ),
            }
        )
        supply_block_policy = self._supply_block_policy(
            signal=signal,
            trade_plan=trade_plan,
            entry_grade=entry_grade,
            leader_score=actual_leader_score,
            same_stock_buy_count=int(same_stock_buy_count),
            has_position=has_position,
            decision_score=decision_score,
        )
        policy["supply_block_policy"] = supply_block_policy
        if supply_block_policy.get("quantity_multiplier"):
            current_multiplier = (
                _decimal_or_none(policy.get("quantity_multiplier")) or Decimal("1")
            )
            supply_multiplier = (
                _decimal_or_none(supply_block_policy.get("quantity_multiplier"))
                or settings.entry_type_minimum_probe_quantity_multiplier
            )
            policy["quantity_multiplier"] = str(
                min(current_multiplier, supply_multiplier)
            )
            policy["status"] = str(
                supply_block_policy.get("policy_status")
                or "allowed_supply_hard_block_reduced_probe"
            )
            policy["stop_policy"] = "supply_hard_block_fast_exit_watch"
        if supply_block_policy.get("blocked"):
            policy["status"] = str(
                supply_block_policy.get("status")
                or "blocked_supply_hard_block"
            )
            finalize_reentry_engine()
            return (
                True,
                str(supply_block_policy.get("message") or "supply blocked"),
                policy,
            )
        if (
            trade_plan.entry_type == "chart_breakout_hold"
            and data_policy.get("minimum_probe")
            and int(data_gap_probe_attempt_count) > 0
        ):
            policy["status"] = "blocked_chart_breakout_hold_data_gap_probe_once"
            finalize_reentry_engine()
            return (
                True,
                (
                    "chart_breakout_hold 데이터 공백 probe는 당일 1회만 허용: "
                    "수급/대장주 실점수 수집 전 반복 진입 금지"
                ),
                policy,
            )
        if policy.get("add_buy_policy") == "blocked" and int(same_stock_buy_count) > 0:
            policy["status"] = (
                "blocked_data_gap_add_buy_policy"
                if has_position
                else "blocked_data_gap_reentry_policy"
            )
            finalize_reentry_engine()
            return (
                True,
                (
                    "데이터 공백 최소수량 탐색 후 추가 진입 차단: "
                    "수급/대장주 점수 수집 전까지 같은 종목 추가매수·재진입 금지"
                ),
                policy,
            )
        entry_type_policy = self._entry_type_policy(
            signal=signal,
            trade_plan=trade_plan,
            entry_grade=entry_grade,
            grade_details=grade_details,
            market_rank_proxy=market_rank_proxy,
            in_morning_window=in_morning_window,
            now=timestamp,
            same_stock_buy_count=int(same_stock_buy_count),
            vwap_reclaimed=vwap_reclaimed,
        )
        policy["entry_type_policy"] = entry_type_policy
        if entry_type_policy.get("minimum_probe"):
            current_multiplier = (
                _decimal_or_none(policy.get("quantity_multiplier")) or Decimal("1")
            )
            type_multiplier = (
                _decimal_or_none(entry_type_policy.get("quantity_multiplier"))
                or settings.entry_type_minimum_probe_quantity_multiplier
            )
            policy["quantity_multiplier"] = str(
                min(current_multiplier, type_multiplier)
            )
            if entry_type_policy.get("max_quantity") is not None:
                policy["max_quantity"] = entry_type_policy.get("max_quantity")
            policy["status"] = str(
                entry_type_policy.get("status") or "allowed_minimum_probe_entry_type"
            )
        if entry_type_policy.get("blocked"):
            policy["status"] = str(
                entry_type_policy.get("status") or "blocked_entry_type_policy"
            )
            finalize_reentry_engine()
            return (
                True,
                str(entry_type_policy.get("message") or "entry type blocked"),
                policy,
            )

        same_level_failure_policy = self._same_level_failure_policy(
            account_id=account_id,
            signal=signal,
            as_of=timestamp,
        )
        policy["same_level_failure_policy"] = same_level_failure_policy
        finalize_reentry_engine()
        if same_level_failure_policy.get("blocked"):
            policy["status"] = str(
                same_level_failure_policy.get("status")
                or "blocked_same_level_failure_reentry"
            )
            finalize_reentry_engine()
            return (
                True,
                str(
                    same_level_failure_policy.get("message")
                    or "same level failed twice"
                ),
                policy,
            )

        if loss_limit > 0 and int(daily_realized or 0) <= -loss_limit:
            if entry_grade != "A":
                policy["status"] = "blocked_daily_loss_limit"
                finalize_reentry_engine()
                return (
                    True,
                    (
                        "일중 손실 제한으로 일반/약한 후보 신규 진입 차단: "
                        f"실현손익={int(daily_realized or 0):,}원, "
                        f"제한=-{loss_limit:,}원"
                    ),
                    policy,
                )
            policy["status"] = "allowed_a_grade_after_daily_loss_limit"
            current_multiplier = (
                _decimal_or_none(policy.get("quantity_multiplier")) or Decimal("1")
            )
            policy["quantity_multiplier"] = str(
                min(
                    current_multiplier,
                    settings.risk_loss_limit_a_grade_quantity_multiplier,
                )
            )
            policy["risk_override"] = True
        if (
            self.risk_settings.max_daily_stop_losses > 0
            and stop_loss_count >= self.risk_settings.max_daily_stop_losses
        ):
            cooldown_entry_allowed = (
                entry_grade == "A"
                and trade_plan.entry_type
                in {"chart_breakout_hold", "opening_range_breakout"}
            )
            if not cooldown_entry_allowed:
                policy["status"] = "blocked_daily_stop_loss_cooldown"
                finalize_reentry_engine()
                return (
                    True,
                    (
                        "손절 누적 cooldown으로 약한 진입 차단: "
                        f"손절={int(stop_loss_count)}회, "
                        f"제한={self.risk_settings.max_daily_stop_losses}회"
                    ),
                    policy,
                )
            current_multiplier = (
                _decimal_or_none(policy.get("quantity_multiplier")) or Decimal("1")
            )
            policy["status"] = "allowed_a_grade_cooldown_probe"
            policy["quantity_multiplier"] = str(
                min(
                    current_multiplier,
                    settings.risk_cooldown_a_grade_quantity_multiplier,
                )
            )
            policy["risk_override"] = True
        if (
            in_morning_window
            and settings.morning_max_new_buys > 0
            and total_buy_count >= settings.morning_max_new_buys
        ):
            policy["status"] = "blocked_morning_entry_limit"
            finalize_reentry_engine()
            return (
                True,
                (
                    "장초반 신규 진입 수 제한: "
                    f"매수={int(total_buy_count)}건, "
                    f"제한={settings.morning_max_new_buys}건"
                ),
                policy,
            )
        if in_morning_window:
            if entry_grade == "C":
                policy["status"] = "blocked_morning_c_grade"
                finalize_reentry_engine()
                return (
                    True,
                    "장초반 C급/데이터 부족 후보 신규 진입 차단",
                    policy,
                )
            if (
                entry_grade == "A"
                and settings.morning_max_a_grade_buys > 0
                and morning_grade_counts["A"] >= settings.morning_max_a_grade_buys
            ):
                policy["status"] = "blocked_morning_a_grade_slot"
                finalize_reentry_engine()
                return (
                    True,
                    (
                        "장초반 A급 전용 슬롯 초과: "
                        f"A급={morning_grade_counts['A']}건, "
                        f"제한={settings.morning_max_a_grade_buys}건"
                    ),
                    policy,
                )
            if (
                entry_grade == "B"
                and settings.morning_max_b_grade_buys > 0
                and morning_grade_counts["B"] >= settings.morning_max_b_grade_buys
            ):
                policy["status"] = "blocked_morning_b_grade_slot"
                finalize_reentry_engine()
                return (
                    True,
                    (
                        "장초반 B급 슬롯 초과: "
                        f"B급={morning_grade_counts['B']}건, "
                        f"제한={settings.morning_max_b_grade_buys}건"
                    ),
                    policy,
                )
        if (
            settings.max_new_buys_per_bucket > 0
            and bucket_buy_count >= settings.max_new_buys_per_bucket
        ):
            policy["status"] = "blocked_entry_bucket_limit"
            finalize_reentry_engine()
            return (
                True,
                (
                    "10분 구간 신규 진입 수 제한: "
                    f"매수={int(bucket_buy_count)}건, "
                    f"제한={settings.max_new_buys_per_bucket}건"
                ),
                policy,
            )
        if (
            settings.max_same_stock_buys_per_day > 0
            and same_stock_buy_count >= settings.max_same_stock_buys_per_day
            and not repeat_allowed
        ):
            policy["status"] = "blocked_same_stock_repeat"
            finalize_reentry_engine()
            return (
                True,
                (
                    "동일 종목 반복 진입 차단: "
                    f"오늘 매수={int(same_stock_buy_count)}건, "
                    "1차익절·신고가 재돌파·VWAP 재안착 전까지 추가 진입 금지"
                ),
                policy,
            )
        finalize_reentry_engine()
        return False, "", policy

    def _mode_shadow_compare(
        self,
        *,
        account_name: str,
        account_id: int,
        account_total_equity: int | None,
        cash_balance: int,
        position_value: int,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        now: datetime,
        has_position: bool,
        leader_score: dict[str, Any] | None,
        actual_leader_score: dict[str, Any] | None,
        market_rank_proxy: dict[str, Any],
        decision_score: Decimal,
        base_planned_quantity: int,
        reentry_blocked: bool,
        reentry_policy: dict[str, Any],
        actual_policy: dict[str, Any],
        armed_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actual_mode = (
            "small_account_aggressive"
            if self.strategy_settings.small_account_aggressive_enabled
            else "normal"
        )
        variants: dict[str, dict[str, Any]] = {}
        for name, enabled in (
            ("normal", False),
            ("small_account_aggressive", True),
        ):
            if name == actual_mode:
                message = ""
                policy = deepcopy(actual_policy)
                blocked = bool(
                    str(policy.get("status") or "").startswith("blocked")
                )
            else:
                blocked, message, policy = self._entry_guard_policy(
                    account_name=account_name,
                    account_id=account_id,
                    account_total_equity=account_total_equity,
                    signal=signal,
                    trade_plan=trade_plan,
                    now=now,
                    has_position=has_position,
                    leader_score=leader_score,
                    actual_leader_score=actual_leader_score,
                    market_rank_proxy=market_rank_proxy,
                    decision_score=decision_score,
                    small_account_aggressive_enabled_override=enabled,
                    armed_policy=armed_policy,
                )
                policy = deepcopy(policy)
            if not reentry_blocked:
                self._apply_reentry_quantity_policy(policy, reentry_policy)
            variants[name] = self._shadow_variant_summary(
                policy=policy,
                blocked=blocked or reentry_blocked,
                message=message if blocked else "",
                account_total_equity=account_total_equity or 0,
                cash_balance=cash_balance,
                position_value=position_value,
                signal=signal,
                decision_score=decision_score,
                trade_plan=trade_plan,
                base_planned_quantity=base_planned_quantity,
            )

        normal = variants["normal"]
        aggressive = variants["small_account_aggressive"]
        return {
            "version": "v0.12.65",
            "actual_mode": actual_mode,
            "shadow_only": True,
            "normal": normal,
            "small_account_aggressive": aggressive,
            "diff": {
                "action_diff": normal["would_enter"] != aggressive["would_enter"],
                "grade_diff": normal["entry_grade"] != aggressive["entry_grade"],
                "quantity_diff": (
                    int(aggressive["planned_quantity"])
                    - int(normal["planned_quantity"])
                ),
                "a_plus_only_in_aggressive": (
                    normal["entry_grade"] != "A+"
                    and aggressive["entry_grade"] == "A+"
                ),
                "aggressive_blocks_normal_entry": (
                    normal["would_enter"] and not aggressive["would_enter"]
                ),
            },
        }

    def _strategy_ab_compare(
        self,
        *,
        account_total_equity: int | None,
        cash_balance: int,
        position_value: int,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        decision_score: Decimal,
        base_planned_quantity: int,
        reentry_blocked: bool,
        entry_guard_blocked: bool,
        reentry_policy: dict[str, Any],
        entry_guard_policy: dict[str, Any],
        router_execution_policy: dict[str, Any],
        legacy_entry_policy: dict[str, Any],
        entry_candle_quality_policy: dict[str, Any],
        strategy_playbook: dict[str, Any],
    ) -> dict[str, Any]:
        active_mode = (
            self.strategy_settings.strategy_ab_active_mode
            if self.strategy_settings.strategy_ab_test_enabled
            else "remake"
        )
        if active_mode not in {"normal", "remake"}:
            active_mode = "remake"
        normal_blocked = bool(reentry_blocked or entry_guard_blocked)
        router_order_effect = str(
            router_execution_policy.get("order_effect") or "not_recorded"
        )
        remake_blocked = bool(
            normal_blocked or router_order_effect == "shadow_only"
        )
        normal_policy = deepcopy(entry_guard_policy)
        remake_policy = deepcopy(entry_guard_policy)
        if router_order_effect == "allow_minimum_probe":
            current_max = remake_policy.get("max_quantity")
            router_max = router_execution_policy.get("max_quantity")
            if router_max is not None:
                remake_policy["max_quantity"] = (
                    min(int(current_max), int(router_max))
                    if current_max is not None
                    else int(router_max)
                )
        normal = self._shadow_variant_summary(
            policy=normal_policy,
            blocked=normal_blocked,
            message=(
                str(reentry_policy.get("status") or "reentry blocked")
                if reentry_blocked
                else (
                    str(entry_guard_policy.get("status") or "entry guard blocked")
                    if entry_guard_blocked
                    else ""
                )
            ),
            account_total_equity=account_total_equity or 0,
            cash_balance=cash_balance,
            position_value=position_value,
            signal=signal,
            decision_score=decision_score,
            trade_plan=trade_plan,
            base_planned_quantity=base_planned_quantity,
        )
        remake = self._shadow_variant_summary(
            policy=remake_policy,
            blocked=remake_blocked,
            message=(
                str(router_execution_policy.get("message") or "router shadow-only")
                if router_order_effect == "shadow_only"
                else normal["message"]
            ),
            account_total_equity=account_total_equity or 0,
            cash_balance=cash_balance,
            position_value=position_value,
            signal=signal,
            decision_score=decision_score,
            trade_plan=trade_plan,
            base_planned_quantity=base_planned_quantity,
        )
        normal.update(
            {
                "mode": "normal",
                "routing": "baseline_entry_guard_before_remake_router",
                "order_effect": "block" if normal_blocked else "allow_order",
                "legacy_old_logic_would_buy": bool(
                    legacy_entry_policy.get("old_logic_would_buy")
                ),
                "legacy_route": legacy_entry_policy.get("legacy_route"),
                "playbook": strategy_playbook.get("name"),
            }
        )
        remake.update(
            {
                "mode": "remake",
                "routing": "three_playbook_router",
                "order_effect": (
                    "block" if normal_blocked else router_order_effect
                ),
                "router_status": router_execution_policy.get("status"),
                "router_message": router_execution_policy.get("message"),
                "entry_candle_quality_status": entry_candle_quality_policy.get(
                    "status"
                ),
                "playbook": strategy_playbook.get("name"),
            }
        )
        selected = normal if active_mode == "normal" else remake
        shadow = remake if active_mode == "normal" else normal
        return {
            "version": "v0.12.89",
            "enabled": bool(self.strategy_settings.strategy_ab_test_enabled),
            "actual_mode": active_mode,
            "shadow_mode": "remake" if active_mode == "normal" else "normal",
            "actual_orders_follow_selected_mode_only": True,
            "normal": normal,
            "remake": remake,
            "selected": selected,
            "shadow": shadow,
            "diff": {
                "action_diff": normal["would_enter"] != remake["would_enter"],
                "quantity_diff": (
                    int(remake["planned_quantity"])
                    - int(normal["planned_quantity"])
                ),
                "normal_only_entry": (
                    normal["would_enter"] and not remake["would_enter"]
                ),
                "remake_only_entry": (
                    remake["would_enter"] and not normal["would_enter"]
                ),
                "selected_would_enter": bool(selected["would_enter"]),
                "shadow_would_enter": bool(shadow["would_enter"]),
            },
        }

    def _shadow_variant_summary(
        self,
        *,
        policy: dict[str, Any],
        blocked: bool,
        message: str,
        account_total_equity: int,
        cash_balance: int,
        position_value: int,
        signal: BreakoutSignal,
        decision_score: Decimal,
        trade_plan: TradePlan,
        base_planned_quantity: int,
    ) -> dict[str, Any]:
        quantity = 0
        if not blocked:
            quantity_multiplier = _decimal_or_none(
                policy.get("quantity_multiplier")
            )
            target_position_rate = _decimal_or_none(
                policy.get("target_position_rate")
            )
            if target_position_rate is not None:
                effective_multiplier = quantity_multiplier or Decimal("1")
                per_trade_risk_rate = _decimal_or_none(
                    policy.get("per_trade_risk_rate")
                )
                if per_trade_risk_rate is not None:
                    per_trade_risk_rate *= effective_multiplier
                quantity = self._calculate_quantity(
                    account_total_equity,
                    cash_balance,
                    signal.current_price,
                    decision_score,
                    position_value,
                    stop_loss_price=trade_plan.stop_loss_price,
                    target_position_rate=(
                        target_position_rate * effective_multiplier
                    ),
                    per_trade_risk_rate=per_trade_risk_rate,
                    minimum_quantity_if_affordable=int(
                        policy.get("minimum_quantity_if_affordable", 0) or 0
                    ),
                )
                if policy.get("max_quantity") is not None:
                    quantity = min(quantity, int(policy["max_quantity"]))
            elif quantity_multiplier is not None:
                quantity = self._apply_entry_guard_quantity(
                    base_planned_quantity,
                    policy,
                )
            else:
                quantity = base_planned_quantity
        small_policy = policy.get("small_account_aggressive") or {}
        return {
            "would_enter": bool(not blocked and quantity >= 1),
            "blocked": bool(blocked),
            "message": message,
            "status": policy.get("status"),
            "entry_grade": policy.get("entry_grade"),
            "target_position_rate": policy.get("target_position_rate"),
            "per_trade_risk_rate": policy.get("per_trade_risk_rate"),
            "planned_quantity": int(quantity),
            "small_account_active": bool(small_policy.get("active")),
            "a_plus_conditions": small_policy.get("a_plus_conditions") or {},
        }

    @staticmethod
    def _a_plus_entry_type(entry_type: str) -> bool:
        return entry_type in {
            "opening_range_breakout",
            "chart_breakout_hold",
            "volume_breakout",
            "breakout_probe",
        }

    @staticmethod
    def _money_flow_policy(
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        grade_details: dict[str, Any],
        leader_score: dict[str, Any] | None,
        market_rank_proxy: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        market_rank_proxy = market_rank_proxy or {}
        leader_score = leader_score or {}
        components = leader_score.get("components") or {}
        ranks = components.get("ranks") or {}

        def _rank(name: str, proxy_key: str | None = None) -> int | None:
            source = ranks.get(name) or {}
            value = source.get("rank")
            if value is None and proxy_key:
                value = (market_rank_proxy.get(proxy_key) or {}).get("rank")
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _rank_points(rank: int | None, top: Decimal) -> Decimal:
            if rank is None:
                return Decimal("0")
            if rank <= 10:
                return top
            if rank <= 30:
                return top * Decimal("0.70")
            if rank <= 60:
                return top * Decimal("0.35")
            return Decimal("0")

        trade_amount_rank = _rank("trade_amount", "trade_amount")
        realtime_rank = _rank("realtime_inquiry", "realtime")
        change_rate_rank = _rank("change_rate", None)
        trade_amount_score = _decimal_or_none(leader_score.get("trade_amount_score"))
        rank_score = _decimal_or_none(leader_score.get("rank_score"))
        theme_score = _decimal_or_none(leader_score.get("theme_score"))
        leader_role = str(leader_score.get("role") or "")
        projected_trade_amount = signal.projected_trade_amount or 0
        trade_amount_points = max(
            _rank_points(trade_amount_rank, Decimal("18")),
            min(
                Decimal("18"),
                (trade_amount_score or Decimal("0")) * Decimal("0.18"),
            ),
            Decimal("14") if projected_trade_amount >= 50_000_000_000 else Decimal("0"),
            Decimal("8") if projected_trade_amount >= 5_000_000_000 else Decimal("0"),
        )
        realtime_points = max(
            _rank_points(realtime_rank, Decimal("12")),
            min(Decimal("12"), (rank_score or Decimal("0")) * Decimal("0.12")),
        )
        change_rate_points = _rank_points(change_rate_rank, Decimal("8"))
        if signal.day_gain_rate is not None:
            if signal.day_gain_rate >= Decimal("8"):
                change_rate_points = max(change_rate_points, Decimal("8"))
            elif signal.day_gain_rate >= Decimal("3"):
                change_rate_points = max(change_rate_points, Decimal("5"))
        volume_ratio = signal.projected_volume_ratio or signal.volume_ratio
        volume_points = Decimal("0")
        if volume_ratio is not None:
            if volume_ratio >= Decimal("3"):
                volume_points = Decimal("12")
            elif volume_ratio >= Decimal("2"):
                volume_points = Decimal("9")
            elif volume_ratio >= Decimal("1.5"):
                volume_points = Decimal("6")
        theme_points = min(Decimal("12"), (theme_score or Decimal("0")) * Decimal("0.16"))
        vwap_hold = bool(
            signal.vwap_pass
            or (
                signal.vwap is not None
                and Decimal(signal.current_price) >= signal.vwap
            )
        )
        vwap_points = Decimal("10") if vwap_hold else Decimal("0")
        new_high = bool(
            grade_details.get("daily_breakout_ok")
            or signal.opening_range_pass
            or (
                signal.intraday_high is not None
                and signal.current_price > int(signal.intraday_high)
            )
            or (
                signal.previous_high is not None
                and signal.current_price > int(signal.previous_high)
            )
        )
        new_high_points = Decimal("10") if new_high else Decimal("0")
        stock_name = str(leader_score.get("stock_name") or "").strip() or None
        stock_category = stock_category_payload(signal.stock_code, stock_name)
        normal_stock = stock_category["key"] == COMMON_STOCK
        category_points = Decimal("6") if normal_stock else Decimal("-30")
        supply = signal.supply
        supply_missing = supply is None
        supply_blocked = bool((supply or {}).get("blocked"))
        confirmed_net_sell = (
            BreakoutStrategy._confirmed_supply_net_sell(supply)
            if supply
            else False
        )
        supply_total = _decimal_or_none((supply or {}).get("total_score"))
        supply_points = Decimal("0")
        if supply_missing:
            supply_points = Decimal("-18")
        elif supply_blocked or confirmed_net_sell:
            supply_points = Decimal("-35")
        elif supply_total is None or supply_total >= Decimal("60"):
            supply_points = Decimal("12")
        elif supply_total >= Decimal("45"):
            supply_points = Decimal("6")
        current_time = now.time().replace(tzinfo=None)
        opening_window = clock_time(9, 0) <= current_time < clock_time(9, 20)
        total = (
            trade_amount_points
            + realtime_points
            + change_rate_points
            + volume_points
            + theme_points
            + vwap_points
            + new_high_points
            + category_points
            + supply_points
        )
        money_flow_top = bool(
            trade_amount_rank is not None
            and trade_amount_rank <= 30
            or trade_amount_score is not None
            and trade_amount_score >= Decimal("70")
            or projected_trade_amount >= 5_000_000_000
        )
        realtime_rank_top = bool(
            realtime_rank is not None
            and realtime_rank <= 30
            or rank_score is not None
            and rank_score >= Decimal("70")
            or bool(market_rank_proxy.get("usable"))
            or leader_role in {"leader", "second"}
        )
        a_plus_qualified = bool(
            opening_window
            and money_flow_top
            and realtime_rank_top
            and vwap_hold
            and new_high
            and normal_stock
            and total >= Decimal("55")
        )
        leader_or_rank_confirmed = bool(
            leader_role in {"leader", "second"}
            or trade_amount_rank is not None
            and trade_amount_rank <= 20
            and realtime_rank is not None
            and realtime_rank <= 20
        )
        a_plus_arm_qualified = bool(
            opening_window
            and money_flow_top
            and realtime_rank_top
            and leader_or_rank_confirmed
            and vwap_hold
            and normal_stock
            and total >= Decimal("50")
        )
        strong_probe_qualified = bool(
            money_flow_top
            and realtime_rank_top
            and vwap_hold
            and new_high
            and normal_stock
            and total >= Decimal("50")
        )
        return {
            "version": "v0.12.82",
            "total_score": str(total.quantize(Decimal("0.01"))),
            "a_plus_qualified": a_plus_qualified,
            "a_plus_arm_qualified": a_plus_arm_qualified,
            "leader_or_rank_confirmed": leader_or_rank_confirmed,
            "strong_probe_qualified": strong_probe_qualified,
            "money_flow_top": money_flow_top,
            "realtime_rank_top": realtime_rank_top,
            "opening_window": opening_window,
            "vwap_hold": vwap_hold,
            "new_high": new_high,
            "normal_stock": normal_stock,
            "supply_missing": supply_missing,
            "supply_blocked": supply_blocked,
            "confirmed_net_sell": confirmed_net_sell,
            "stock_category": stock_category,
            "entry_type": trade_plan.entry_type,
            "components": {
                "trade_amount_rank": trade_amount_rank,
                "trade_amount_score": (
                    str(trade_amount_score)
                    if trade_amount_score is not None
                    else None
                ),
                "projected_trade_amount": projected_trade_amount,
                "realtime_rank": realtime_rank,
                "rank_score": str(rank_score) if rank_score is not None else None,
                "change_rate_rank": change_rate_rank,
                "day_gain_rate": (
                    str(signal.day_gain_rate)
                    if signal.day_gain_rate is not None
                    else None
                ),
                "volume_ratio": str(volume_ratio) if volume_ratio is not None else None,
                "theme_score": (
                    str(theme_score) if theme_score is not None else None
                ),
                "supply_total_score": (
                    str(supply_total) if supply_total is not None else None
                ),
            },
            "points": {
                "trade_amount": str(trade_amount_points),
                "realtime_rank": str(realtime_points),
                "change_rate": str(change_rate_points),
                "volume_growth": str(volume_points),
                "theme": str(theme_points),
                "vwap_hold": str(vwap_points),
                "new_high": str(new_high_points),
                "stock_category": str(category_points),
                "supply": str(supply_points),
            },
        }

    def _a_plus_armed_policy(
        self,
        *,
        account_id: int,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        grade_details: dict[str, Any],
        money_flow_policy: dict[str, Any],
        leader_score: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        current = now.replace(tzinfo=None)
        trading_day = current.date().isoformat()
        expiry = current.replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(
            minutes=self.strategy_settings.a_plus_armed_expiry_minutes_after_open
        )
        existing = self.database.armed_candidate(
            account_id=account_id,
            stock_code=signal.stock_code,
            trading_day=trading_day,
        )
        leader_role = str((leader_score or {}).get("role") or "").lower()
        leader_total = _decimal_or_none((leader_score or {}).get("total_score"))
        money_flow_score = (
            _decimal_or_none(money_flow_policy.get("total_score"))
            or Decimal("0")
        )
        chart_blocked = bool(signal.chart is not None and signal.chart.blocked)
        quality = signal.entry_candle_quality or {}
        quality_blocked = bool(quality.get("blocked"))
        supply_ok = bool(
            signal.supply is not None
            and not bool((signal.supply or {}).get("blocked"))
            and not bool(money_flow_policy.get("confirmed_net_sell"))
        )
        leader_maintained = bool(
            leader_role in {"leader", "second"}
            and (leader_total is None or leader_total >= Decimal("65"))
            or money_flow_policy.get("leader_or_rank_confirmed")
        )
        arm_qualified = bool(
            self.strategy_settings.a_plus_armed_enabled
            and money_flow_policy.get("a_plus_arm_qualified")
            and money_flow_score
            >= self.strategy_settings.a_plus_armed_min_money_flow_score
            and str(grade_details.get("grade") or "") == "A"
            and not signal.information_blocked
            and not chart_blocked
        )
        maintained = bool(
            money_flow_policy.get("money_flow_top")
            and money_flow_policy.get("realtime_rank_top")
            and money_flow_policy.get("vwap_hold")
            and money_flow_policy.get("normal_stock")
            and supply_ok
            and leader_maintained
            and not signal.information_blocked
            and not chart_blocked
        )
        existing_status = str((existing or {}).get("status") or "none")
        active = existing_status == "armed"
        status = "not_qualified"
        reason = "opening money-flow A+ arm conditions not met"

        evidence = {
            "version": "v0.12.92",
            "entry_type": trade_plan.entry_type,
            "entry_grade": grade_details.get("grade"),
            "leader_role": leader_role or None,
            "leader_total_score": (
                str(leader_total) if leader_total is not None else None
            ),
            "money_flow_score": str(money_flow_score),
            "money_flow_top": bool(money_flow_policy.get("money_flow_top")),
            "realtime_rank_top": bool(
                money_flow_policy.get("realtime_rank_top")
            ),
            "vwap_hold": bool(money_flow_policy.get("vwap_hold")),
            "supply_ok": supply_ok,
            "leader_maintained": leader_maintained,
            "arm_qualified": arm_qualified,
            "maintained": maintained,
        }

        if active and current >= datetime.fromisoformat(str(existing["expires_at"])):
            active = False
            status = "expired"
            reason = "armed candidate validity window expired"
            self.database.save_armed_candidate(
                account_id=account_id,
                stock_code=signal.stock_code,
                trading_day=trading_day,
                status="expired",
                observed_at=current.isoformat(timespec="seconds"),
                expires_at=expiry.isoformat(timespec="seconds"),
                evidence={**evidence, "status": status, "reason": reason},
            )
        elif active and not maintained:
            active = False
            status = "disarmed"
            reason = "money-flow, VWAP, supply, or leadership no longer maintained"
            self.database.save_armed_candidate(
                account_id=account_id,
                stock_code=signal.stock_code,
                trading_day=trading_day,
                status="disarmed",
                observed_at=current.isoformat(timespec="seconds"),
                expires_at=expiry.isoformat(timespec="seconds"),
                evidence={**evidence, "status": status, "reason": reason},
            )
        elif active:
            status = "armed_waiting_trigger"
            reason = "A+ candidate remains armed while leadership is maintained"
        elif existing_status == "entered":
            status = "already_entered"
            reason = "armed candidate already produced an executed entry"
        elif arm_qualified and current < expiry:
            active = True
            status = "newly_armed"
            reason = "opening money-flow A+ candidate armed"

        trigger_type = None
        if active and not quality_blocked:
            if signal.opening_range_pass:
                trigger_type = "opening_range_breakout"
            elif (
                signal.pullback_pass
                and signal.pullback_support_pass
                and signal.vwap_pass
            ):
                trigger_type = "vwap_pullback"
            elif (
                signal.breakout_hold_pass
                and bool(money_flow_policy.get("new_high"))
            ):
                trigger_type = "new_high_rebreakout"
        trigger_ready = trigger_type is not None
        if trigger_ready:
            status = "armed_trigger_ready"
            reason = f"armed A+ trigger ready: {trigger_type}"

        if active:
            self.database.save_armed_candidate(
                account_id=account_id,
                stock_code=signal.stock_code,
                trading_day=trading_day,
                status="armed",
                observed_at=current.isoformat(timespec="seconds"),
                expires_at=expiry.isoformat(timespec="seconds"),
                trigger_type=trigger_type,
                triggered_at=(
                    current.isoformat(timespec="seconds")
                    if trigger_ready
                    else None
                ),
                evidence={
                    **evidence,
                    "status": status,
                    "reason": reason,
                    "trigger_type": trigger_type,
                    "trigger_ready": trigger_ready,
                    "entry_candle_quality_blocked": quality_blocked,
                },
            )

        return {
            **evidence,
            "enabled": self.strategy_settings.a_plus_armed_enabled,
            "status": status,
            "reason": reason,
            "active": active,
            "trigger_ready": trigger_ready,
            "trigger_type": trigger_type,
            "trading_day": trading_day,
            "expires_at": expiry.isoformat(timespec="seconds"),
            "existing_status": existing_status,
            "entry_candle_quality_blocked": quality_blocked,
        }

    @staticmethod
    def _a_plus_armed_decision(
        base: StrategyDecision,
        armed_policy: dict[str, Any],
    ) -> StrategyDecision:
        return StrategyDecision(
            strategy_name=base.strategy_name,
            stock_code=base.stock_code,
            decided_at=base.decided_at,
            action=DecisionAction.BUY,
            score=base.score,
            confidence=Decimal("0.90"),
            reason=(
                "A+ armed 진입 트리거: "
                f"{armed_policy.get('trigger_type') or 'unknown'}"
            ),
            components=base.components,
        )

    @staticmethod
    def _strategy_playbook_policy(
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        entry_guard_policy: dict[str, Any],
        reentry_policy: dict[str, Any],
    ) -> dict[str, Any]:
        entry_grade = str(entry_guard_policy.get("entry_grade") or "")
        entry_type = trade_plan.entry_type
        small_account = entry_guard_policy.get("small_account_aggressive") or {}
        money_flow = entry_guard_policy.get("money_flow_policy") or {}
        money_flow_qualified = (
            bool(money_flow.get("a_plus_qualified")) if money_flow else True
        )
        entry_guard_status = str(entry_guard_policy.get("status") or "")
        money_flow_probe_status = entry_guard_status in {
            "allowed_small_account_b_money_flow_probe",
            "allowed_small_account_money_flow_data_gap_probe",
            "allowed_minimum_probe_data_gap",
        } and bool(
            (entry_guard_policy.get("data_availability") or {}).get("status")
            == "minimum_probe_money_flow_data_gap"
            or small_account.get("money_flow_probe")
        )
        money_flow_probe_route = bool(
            money_flow_probe_status
            or entry_grade == "A"
            and bool(money_flow.get("strong_probe_qualified"))
        )
        grade_details = entry_guard_policy.get("entry_grade_details") or {}
        leader_role = str(
            grade_details.get("leader_role")
            or grade_details.get("role")
            or ""
        ).lower()
        money_flow_probe_order_qualified = bool(
            entry_grade == "A"
            and leader_role in {"leader", "second"}
            and not bool(money_flow.get("supply_missing"))
            and not bool(money_flow.get("supply_blocked"))
            and not bool(money_flow.get("confirmed_net_sell"))
            and (
                signal.opening_range_pass
                or signal.vwap_pass
                and signal.breakout_hold_pass
            )
        )
        if (
            entry_grade == "A+"
            and money_flow_qualified
            and BreakoutStrategy._a_plus_entry_type(entry_type)
        ):
            return {
                "name": "a_plus_trend_day_runner",
                "priority": 1,
                "entry_focus": (
                    "09:00~09:20 money-flow/theme/new-high/VWAP/ORB leader"
                ),
                "positioning": (
                    "large_a_plus"
                    if bool(small_account.get("active"))
                    else "core_a_plus"
                ),
                "first_exit": "small_partial_profit_lock",
                "fast_partial_profit_rate": "0.02",
                "fast_partial_min_r": "0.30",
                "residual_exit_rule": "structure_break_runner",
                "residual_protection": (
                    "hold_until_vwap_5m_previous_low_or_volume_bear_break"
                ),
                "percent_exit_as_primary": False,
            }
        if (
            entry_grade in {"A", "B"}
            and bool(money_flow.get("strong_probe_qualified"))
            and money_flow_probe_route
            and BreakoutStrategy._a_plus_entry_type(entry_type)
        ):
            return {
                "name": "money_flow_strong_probe",
                "priority": 4,
                "entry_focus": "strong money-flow minimum probe",
                "positioning": "one_share_probe",
                "first_exit": "quick_probe_profit_or_structure_hold",
                "residual_exit_rule": trade_plan.setup.get("residual_exit_rule"),
                "residual_protection": "vwap_or_structure_break_fast_watch",
                "max_quantity": 1,
                "money_flow_score": money_flow.get("total_score"),
                "data_gap_probe": bool(money_flow.get("supply_missing")),
                "entry_grade": entry_grade,
                "leader_role": leader_role or None,
                "real_order_qualified": money_flow_probe_order_qualified,
            }
        if (
            entry_type == "vwap_pullback"
            and signal.vwap_pass
            and signal.pullback_pass
            and signal.pullback_support_pass
        ):
            return {
                "name": "vwap_pullback_reentry",
                "priority": 2,
                "entry_focus": "post-spike VWAP pullback recovery",
                "positioning": "scaled_reentry",
                "first_exit": "standard_partial_profit_lock",
                "residual_exit_rule": trade_plan.setup.get("residual_exit_rule"),
                "residual_protection": "vwap_5m_previous_low_structure",
                "chase_after_spike": False,
            }
        reentry_status = str(reentry_policy.get("status") or "")
        if (
            reentry_status
            in {
                "allowed_after_partial_exit_reassessment",
                "allowed_after_strong_reassessment",
            }
            and bool(reentry_policy.get("new_high_breakout"))
            and bool(reentry_policy.get("vwap_reanchored"))
            and bool(reentry_policy.get("volume_recovered"))
        ):
            return {
                "name": "new_high_rebreakout_reentry_probe",
                "priority": 3,
                "entry_focus": "new-high rebreakout after profit protection",
                "positioning": "minimum_or_reduced_size",
                "first_exit": "quick_probe_profit_or_structure_hold",
                "residual_exit_rule": trade_plan.setup.get("residual_exit_rule"),
                "reentry_quantity_multiplier": reentry_policy.get(
                    "quantity_multiplier"
                ),
                "requires_profit_protect_previous_exit": bool(
                    reentry_policy.get("previous_exit_was_profit_protect")
                ),
            }
        return {
            "name": "standard_breakout",
            "priority": 0,
            "entry_focus": entry_type,
            "positioning": "standard",
            "first_exit": "trade_plan_default",
            "residual_exit_rule": trade_plan.setup.get("residual_exit_rule"),
        }

    def _data_confidence_policy(
        self,
        *,
        signal: BreakoutSignal,
        entry_guard_policy: dict[str, Any],
    ) -> dict[str, Any]:
        score = Decimal("100")
        deductions: list[dict[str, Any]] = []
        money_flow = entry_guard_policy.get("money_flow_policy") or {}
        money_flow_components = money_flow.get("components") or {}
        data_availability = entry_guard_policy.get("data_availability") or {}
        supply_policy = entry_guard_policy.get("supply_block_policy") or {}

        def deduct(source: str, points: Decimal, reason: str) -> None:
            nonlocal score
            score -= points
            deductions.append(
                {
                    "source": source,
                    "points": str(points),
                    "reason": reason,
                }
            )

        supply = signal.supply or {}
        supply_missing = not bool(signal.supply)
        supply_blocked = bool(supply.get("blocked"))
        confirmed_net_sell = bool(
            money_flow.get("confirmed_net_sell")
            or supply_policy.get("confirmed_net_sell")
        )
        if supply_missing:
            deduct("supply", Decimal("25"), "supply data missing")
        elif supply_blocked:
            deduct("supply", Decimal("25"), "supply risk signal")
        if confirmed_net_sell:
            deduct("supply", Decimal("20"), "confirmed net selling")

        if signal.information_blocked:
            deduct("information", Decimal("35"), "negative news/disclosure signal")
        elif signal.information_score < 0:
            deduct(
                "information",
                min(Decimal("20"), abs(signal.information_score)),
                "negative information score",
            )

        theme_score = _decimal_or_none(money_flow_components.get("theme_score"))
        if theme_score is None:
            deduct("theme", Decimal("10"), "theme confirmation missing")
        elif theme_score <= 0:
            deduct("theme", Decimal("15"), "theme participation not confirmed")
        elif theme_score < Decimal("40"):
            deduct("theme", Decimal("8"), "theme participation weak")

        data_status = str(data_availability.get("status") or "available")
        if data_availability.get("blocked"):
            deduct("availability", Decimal("20"), data_status)
        elif data_availability.get("minimum_probe"):
            deduct("availability", Decimal("10"), data_status)

        market_data_unavailable = bool(
            self.market_mode_settings.enabled
            and (
                self.market_mode_settings.order_effect == "fail_closed"
                or self.market_mode_settings.source == "auto_unavailable"
            )
        )
        if market_data_unavailable:
            deduct("market", Decimal("50"), "market regime data unavailable")

        score = max(Decimal("0"), min(Decimal("100"), score))
        if score >= Decimal("80"):
            band = "high"
            order_effect = "allow_order"
            quantity_multiplier = Decimal("1")
            max_quantity = None
        elif score >= Decimal("55"):
            band = "medium"
            order_effect = "reduce_quantity"
            quantity_multiplier = Decimal("0.65")
            max_quantity = None
        elif score >= Decimal("40"):
            band = "low"
            order_effect = "reduce_quantity"
            quantity_multiplier = Decimal("0.25")
            max_quantity = 1
        else:
            band = "poor"
            order_effect = "shadow_only"
            quantity_multiplier = Decimal("0")
            max_quantity = 0
        return {
            "version": MARKET_REGIME_PLAYBOOK_VERSION,
            "score": str(score.quantize(Decimal("0.1"))),
            "band": band,
            "order_effect": order_effect,
            "quantity_multiplier": str(quantity_multiplier),
            "max_quantity": max_quantity,
            "individual_hard_blocks": False,
            "inputs": {
                "supply_missing": supply_missing,
                "supply_blocked": supply_blocked,
                "confirmed_net_sell": confirmed_net_sell,
                "information_blocked": signal.information_blocked,
                "information_score": str(signal.information_score),
                "theme_score": str(theme_score) if theme_score is not None else None,
                "data_availability_status": data_status,
                "market_data_unavailable": market_data_unavailable,
            },
            "deductions": deductions,
        }

    def _market_regime_playbook_policy(
        self,
        *,
        strategy_playbook: dict[str, Any],
        entry_guard_policy: dict[str, Any],
        data_confidence_policy: dict[str, Any],
    ) -> dict[str, Any]:
        playbook = str(strategy_playbook.get("name") or "none")
        entry_grade = str(entry_guard_policy.get("entry_grade") or "")
        settings = self.market_mode_settings
        confidence_score = _decimal_or_none(data_confidence_policy.get("score")) or Decimal("0")
        confidence_effect = str(
            data_confidence_policy.get("order_effect") or "shadow_only"
        )
        observed_mode = settings.shadow_mode or settings.manual_mode
        preferred_by_mode = {
            "risk_on": "a_plus_trend_day_runner",
            "neutral": "vwap_pullback_reentry",
            "risk_off": None,
        }
        policy: dict[str, Any] = {
            "version": MARKET_REGIME_PLAYBOOK_VERSION,
            "market_mode": settings.manual_mode,
            "observed_mode": observed_mode,
            "market_mode_source": settings.source,
            "market_mode_enabled": settings.enabled,
            "playbook": playbook,
            "entry_grade": entry_grade,
            "preferred_playbook": preferred_by_mode.get(settings.manual_mode),
            "order_effect": "allow_order",
            "quantity_multiplier": "1",
            "max_quantity": None,
            "status": "allowed_unclassified_regime",
            "data_confidence_band": data_confidence_policy.get("band"),
            "data_confidence_score": data_confidence_policy.get("score"),
        }

        if confidence_effect == "shadow_only":
            policy.update(
                {
                    "status": "shadow_only_poor_data_confidence",
                    "order_effect": "shadow_only",
                    "quantity_multiplier": "0",
                    "max_quantity": 0,
                    "message": "poor data confidence; keep candidate in shadow",
                }
            )
            return policy
        if not settings.enabled:
            policy.update(
                {
                    "status": "allowed_market_regime_gate_disabled",
                    "order_effect": (
                        "allow_reduced"
                        if confidence_effect == "reduce_quantity"
                        else "allow_order"
                    ),
                    "quantity_multiplier": data_confidence_policy.get(
                        "quantity_multiplier", "1"
                    ),
                    "max_quantity": data_confidence_policy.get("max_quantity"),
                    "message": "market regime gate disabled; data confidence only",
                }
            )
            return policy
        if settings.order_effect == "shadow_only":
            policy.update(
                {
                    "status": "allowed_auto_market_mode_shadow_observation",
                    "order_effect": (
                        "allow_reduced"
                        if confidence_effect == "reduce_quantity"
                        else "allow_order"
                    ),
                    "quantity_multiplier": data_confidence_policy.get(
                        "quantity_multiplier", "1"
                    ),
                    "max_quantity": data_confidence_policy.get("max_quantity"),
                    "shadow_preferred_playbook": preferred_by_mode.get(observed_mode),
                    "message": "auto market mode is observation-only",
                }
            )
            return policy
        if (
            settings.order_effect == "fail_closed"
            or settings.source == "auto_unavailable"
        ):
            policy.update(
                {
                    "status": "shadow_only_market_data_unavailable",
                    "order_effect": "shadow_only",
                    "quantity_multiplier": "0",
                    "max_quantity": 0,
                    "message": "market regime data unavailable; fail closed to shadow",
                }
            )
            return policy

        core_playbooks = {
            "a_plus_trend_day_runner",
            "vwap_pullback_reentry",
            "new_high_rebreakout_reentry_probe",
        }
        if playbook not in core_playbooks:
            policy.update(
                {
                    "status": "defer_non_core_to_strategy_router",
                    "message": "non-core playbook remains controlled by strategy router",
                }
            )
            return policy

        mode = settings.manual_mode
        matched = playbook == preferred_by_mode.get(mode)
        reacceleration = playbook == "new_high_rebreakout_reentry_probe"
        if mode in {"risk_on", "neutral"} and (matched or reacceleration):
            policy.update(
                {
                    "status": (
                        "allowed_strong_stock_reacceleration"
                        if reacceleration
                        else f"allowed_{mode}_preferred_playbook"
                    ),
                    "order_effect": (
                        "allow_reduced"
                        if confidence_effect == "reduce_quantity"
                        else "allow_order"
                    ),
                    "quantity_multiplier": data_confidence_policy.get(
                        "quantity_multiplier", "1"
                    ),
                    "max_quantity": data_confidence_policy.get("max_quantity"),
                    "message": "playbook matches market regime",
                }
            )
            return policy
        if (
            mode == "risk_off"
            and playbook in core_playbooks
            and entry_grade in {"A", "A+"}
            and confidence_score >= Decimal("55")
        ):
            policy.update(
                {
                    "status": "allowed_risk_off_reduced_probe",
                    "order_effect": "allow_reduced",
                    "quantity_multiplier": str(
                        min(
                            Decimal("0.20"),
                            _decimal_or_none(
                                data_confidence_policy.get("quantity_multiplier")
                            )
                            or Decimal("1"),
                        )
                    ),
                    "max_quantity": 1,
                    "message": "risk-off permits only reduced A-grade core probe",
                }
            )
            return policy
        policy.update(
            {
                "status": "shadow_only_regime_playbook_mismatch",
                "order_effect": "shadow_only",
                "quantity_multiplier": "0",
                "max_quantity": 0,
                "message": "playbook does not match current market regime",
            }
        )
        return policy

    @staticmethod
    def _data_confidence_guard_status(status: Any) -> bool:
        value = str(status or "")
        return any(
            token in value
            for token in (
                "supply",
                "data_gap",
                "missing_leader",
                "missing_supply",
            )
        )

    @staticmethod
    def _apply_regime_quantity_policy(
        *,
        entry_guard_policy: dict[str, Any],
        data_confidence_policy: dict[str, Any],
        market_regime_playbook_policy: dict[str, Any],
    ) -> dict[str, Any]:
        policy = dict(entry_guard_policy)
        multipliers = [
            _decimal_or_none(policy.get("quantity_multiplier")) or Decimal("1")
        ]
        if data_confidence_policy.get("order_effect") == "reduce_quantity":
            multipliers.append(
                _decimal_or_none(data_confidence_policy.get("quantity_multiplier"))
                or Decimal("1")
            )
        if market_regime_playbook_policy.get("order_effect") == "allow_reduced":
            multipliers.append(
                _decimal_or_none(
                    market_regime_playbook_policy.get("quantity_multiplier")
                )
                or Decimal("1")
            )
        policy["quantity_multiplier"] = str(min(multipliers))
        max_quantities = [
            int(value)
            for value in (
                policy.get("max_quantity"),
                data_confidence_policy.get("max_quantity"),
                market_regime_playbook_policy.get("max_quantity"),
            )
            if value is not None
        ]
        if max_quantities:
            policy["max_quantity"] = min(max_quantities)
        policy["data_confidence_policy"] = data_confidence_policy
        policy["market_regime_playbook_policy"] = market_regime_playbook_policy
        return policy

    def _position_build_policy(
        self,
        *,
        signal: BreakoutSignal,
        strategy_playbook: dict[str, Any],
        entry_guard_policy: dict[str, Any],
        has_position: bool,
        position_value: int,
        total_equity: int,
    ) -> dict[str, Any]:
        settings = self.strategy_settings
        playbook = str(strategy_playbook.get("name") or "none")
        core_playbooks = {
            "a_plus_trend_day_runner",
            "vwap_pullback_reentry",
            "new_high_rebreakout_reentry_probe",
        }
        ceiling = _decimal_or_none(entry_guard_policy.get("target_position_rate"))
        if ceiling is None:
            ceiling = self.risk_settings.max_position_rate
        current_rate = (
            Decimal(position_value) / Decimal(total_equity)
            if total_equity > 0
            else Decimal("0")
        )
        policy: dict[str, Any] = {
            "version": "v0.12.93",
            "enabled": settings.staged_position_build_enabled,
            "active": False,
            "playbook_name": playbook,
            "stage": "not_applicable",
            "status": "not_applicable",
            "has_position": has_position,
            "current_position_rate": str(current_rate.quantize(Decimal("0.0001"))),
            "position_ceiling_rate": str(ceiling),
            "target_position_rate": None,
            "confirmation": {
                "breakout_hold": signal.breakout_hold_pass,
                "volume": signal.volume_pass,
                "new_high_or_orb": bool(
                    signal.opening_range_pass
                    or signal.intraday_high is not None
                    and signal.current_price > signal.intraday_high
                ),
                "higher_low": signal.higher_low_pass,
                "vwap": signal.vwap_pass,
                "moving_average": signal.moving_average_pass,
            },
        }
        if not settings.staged_position_build_enabled or playbook not in core_playbooks:
            return policy

        policy["active"] = True
        breakout_confirmed = bool(
            signal.breakout_hold_pass
            and signal.volume_pass
            and (
                signal.opening_range_pass
                or signal.intraday_high is not None
                and signal.current_price > signal.intraday_high
            )
        )
        trend_confirmed = bool(
            signal.higher_low_pass
            and signal.vwap_pass
            and signal.moving_average_pass
        )
        if playbook == "new_high_rebreakout_reentry_probe":
            stage = "reentry_probe"
            target = min(ceiling, settings.staged_position_reentry_probe_rate)
        elif not has_position:
            stage = "scout"
            target = min(ceiling, settings.staged_position_scout_rate)
        elif current_rate < min(
            ceiling,
            settings.staged_position_confirmation_rate,
        ) and (breakout_confirmed or trend_confirmed):
            stage = "breakout_confirmed"
            target = min(ceiling, settings.staged_position_confirmation_rate)
        elif trend_confirmed:
            stage = "trend_confirmed"
            target = ceiling
        else:
            policy.update(
                {
                    "stage": "waiting_confirmation",
                    "status": "shadow_only_waiting_position_confirmation",
                    "order_effect": "shadow_only",
                    "message": "기존 보유분 이후 새 돌파 또는 higher-low 확인 전 추가매수 대기",
                }
            )
            return policy

        policy.update(
            {
                "stage": stage,
                "target_position_rate": str(target),
                "status": f"allowed_{stage}",
                "order_effect": "allow_order",
                "message": f"단계적 포지션 구축 {stage}: 누적 목표 {target:.1%}",
            }
        )
        if has_position and current_rate >= target:
            policy.update(
                {
                    "status": "shadow_only_stage_target_already_reached",
                    "order_effect": "shadow_only",
                    "message": f"{stage} 누적 목표 {target:.1%} 이미 충족",
                }
            )
        return policy

    @staticmethod
    def _entry_candle_quality_policy(
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        strategy_playbook: dict[str, Any],
    ) -> dict[str, Any]:
        raw_policy = dict(signal.entry_candle_quality or {})
        entry_type = str(trade_plan.entry_type or "")
        playbook_name = str(strategy_playbook.get("name") or "none")
        breakout_entry_types = {
            "opening_range_breakout",
            "chart_breakout_hold",
            "volume_breakout",
            "breakout_probe",
            "paper_probe",
        }
        breakout_playbooks = {
            "a_plus_trend_day_runner",
            "new_high_rebreakout_reentry_probe",
            "money_flow_strong_probe",
        }
        applies = bool(
            entry_type in breakout_entry_types
            or playbook_name in breakout_playbooks
        )
        if not raw_policy:
            raw_policy = {
                "version": "v0.12.85",
                "evaluated": False,
                "status": "not_recorded",
                "blocked": False,
                "score": "0",
                "threshold": "60",
                "components": [],
                "block_reasons": [],
            }
        return {
            **raw_policy,
            "version": "v0.12.85",
            "applies_to_entry": applies,
            "entry_type": entry_type,
            "playbook_name": playbook_name,
            "order_effect_when_blocked": (
                "shadow_only" if applies else "not_applicable"
            ),
        }

    @staticmethod
    def _legacy_entry_policy(
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        entry_guard_policy: dict[str, Any],
        strategy_playbook: dict[str, Any],
    ) -> dict[str, Any]:
        entry_type = str(trade_plan.entry_type or "")
        playbook_name = str(strategy_playbook.get("name") or "none")
        core_playbooks = {
            "a_plus_trend_day_runner",
            "vwap_pullback_reentry",
            "new_high_rebreakout_reentry_probe",
            "money_flow_strong_probe",
        }
        money_flow = entry_guard_policy.get("money_flow_policy") or {}
        grade_details = entry_guard_policy.get("entry_grade_details") or {}
        leader_role = str(
            grade_details.get("leader_role")
            or grade_details.get("role")
            or ""
        ).lower()
        theme_score = _decimal_or_none(
            (money_flow.get("components") or {}).get("theme_score")
        )
        leader_or_theme_confirmed = any(
            [
                leader_role in {"leader", "second", "top"},
                bool(money_flow.get("money_flow_top")),
                bool(money_flow.get("realtime_rank_top")),
                theme_score is not None and theme_score > 0,
            ]
        )
        vwap_confirmed = bool(signal.vwap_pass or money_flow.get("vwap_hold"))
        money_flow_confirmed = bool(
            money_flow.get("a_plus_qualified")
            or money_flow.get("strong_probe_qualified")
        )
        reasons: list[str] = []

        if playbook_name not in core_playbooks:
            if entry_type in {"volume_breakout", "paper_probe", "order_flow_momentum"}:
                reasons.append("simple_volume_breakout")
            if entry_type == "breakout_probe":
                reasons.append("simple_breakout_probe")
            if entry_type in {
                "chart_pattern_entry",
                "chart_long_bullish",
                "chart_cup_handle",
                "chart_ascending_triangle",
            }:
                reasons.append("broad_chart_pattern_entry")
            if (
                entry_type.startswith("chart_")
                and not leader_or_theme_confirmed
                and not vwap_confirmed
            ):
                reasons.append("unconfirmed_pattern_entry")
            if entry_type in {
                "chart_vwap_reclaim",
                "chart_order_block_reentry",
                "vwap_pullback",
            } or any(token in entry_type for token in ("rebound", "reclaim")):
                reasons.append("mean_reversion_rebound")

        blocked_status = str(entry_guard_policy.get("status") or "").startswith(
            "blocked"
        )
        legacy_candidate = bool(reasons)
        return {
            "version": "v0.12.84",
            "legacy_candidate": legacy_candidate,
            "legacy_route": reasons[0] if reasons else "none",
            "legacy_reasons": reasons,
            "entry_type": entry_type,
            "playbook_name": playbook_name,
            "entry_grade": entry_guard_policy.get("entry_grade"),
            "leader_or_theme_confirmed": leader_or_theme_confirmed,
            "vwap_confirmed": vwap_confirmed,
            "money_flow_confirmed": money_flow_confirmed,
            "old_logic_would_buy": legacy_candidate and not blocked_status,
            "recommended_order_effect": (
                "shadow_only" if legacy_candidate else "follow_router"
            ),
            "new_logic_order_effect": "pending_router",
            "new_logic_would_buy": False,
            "removed_from_order_influence": False,
            "downgraded_to_minimum_probe": False,
        }

    @staticmethod
    def _router_execution_policy(
        *,
        trade_plan: TradePlan,
        strategy_playbook: dict[str, Any],
        legacy_entry_policy: dict[str, Any] | None = None,
        entry_candle_quality_policy: dict[str, Any] | None = None,
        position_build_policy: dict[str, Any] | None = None,
        market_regime_playbook_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        core_playbooks = {
            "a_plus_trend_day_runner",
            "vwap_pullback_reentry",
            "new_high_rebreakout_reentry_probe",
        }
        minimum_probe_playbooks = {"money_flow_strong_probe"}
        playbook_name = str(strategy_playbook.get("name") or "none")
        policy = {
            "version": "v0.12.81",
            "status": "allowed_core_playbook",
            "order_effect": "allow_order",
            "playbook_name": playbook_name,
            "entry_type": trade_plan.entry_type,
            "core_playbooks": sorted(core_playbooks),
            "shadow_result_saved": False,
            "message": "",
        }
        quality_policy = entry_candle_quality_policy or {}
        structural_stop_policy = trade_plan.setup.get("structural_stop_policy") or {}
        if (
            playbook_name in core_playbooks | minimum_probe_playbooks
            and structural_stop_policy.get("enabled")
            and not structural_stop_policy.get("valid")
        ):
            policy.update(
                {
                    "version": "v0.12.94",
                    "status": "shadow_only_invalid_structural_stop",
                    "order_effect": "shadow_only",
                    "shadow_result_saved": True,
                    "would_order_without_router": True,
                    "structural_stop_status": structural_stop_policy.get("status"),
                    "message": "유효한 구조 손절선이 없어 핵심 주문을 shadow 처리",
                }
            )
            return policy
        build_policy = position_build_policy or {}
        if (
            playbook_name in core_playbooks
            and build_policy.get("active")
            and build_policy.get("order_effect") == "shadow_only"
        ):
            policy.update(
                {
                    "version": "v0.12.93",
                    "status": build_policy.get("status"),
                    "order_effect": "shadow_only",
                    "shadow_result_saved": True,
                    "would_order_without_router": True,
                    "position_build_stage": build_policy.get("stage"),
                    "message": build_policy.get("message"),
                }
            )
            return policy
        quality_blocks_order = bool(
            quality_policy.get("applies_to_entry")
            and quality_policy.get("blocked")
            and playbook_name in core_playbooks | minimum_probe_playbooks
        )
        if quality_blocks_order:
            policy.update(
                {
                    "version": "v0.12.85",
                    "status": "shadow_only_bad_entry_candle_quality",
                    "order_effect": "shadow_only",
                    "shadow_result_saved": True,
                    "would_order_without_router": True,
                    "entry_candle_quality_status": quality_policy.get("status"),
                    "entry_candle_quality_score": quality_policy.get("score"),
                    "entry_candle_quality_block_reasons": (
                        quality_policy.get("block_reasons") or []
                    ),
                    "message": (
                        "bad breakout candle quality is observed only by v0.12.85 router"
                    ),
                }
            )
            return policy
        regime_policy = market_regime_playbook_policy or {}
        if regime_policy.get("order_effect") == "shadow_only":
            policy.update(
                {
                    "version": MARKET_REGIME_PLAYBOOK_VERSION,
                    "status": regime_policy.get("status"),
                    "order_effect": "shadow_only",
                    "shadow_result_saved": True,
                    "would_order_without_router": True,
                    "market_regime_playbook_policy": regime_policy,
                    "message": regime_policy.get("message"),
                }
            )
            return policy
        if regime_policy.get("order_effect") == "allow_reduced":
            policy.update(
                {
                    "version": MARKET_REGIME_PLAYBOOK_VERSION,
                    "status": regime_policy.get("status"),
                    "order_effect": "allow_minimum_probe",
                    "max_quantity": regime_policy.get("max_quantity"),
                    "shadow_result_saved": True,
                    "market_regime_playbook_policy": regime_policy,
                    "message": regime_policy.get("message"),
                }
            )
        if playbook_name in core_playbooks:
            if not policy.get("message"):
                policy["message"] = "core playbook route can affect orders"
            return policy
        if playbook_name in minimum_probe_playbooks:
            if not bool(strategy_playbook.get("real_order_qualified")):
                policy.update(
                    {
                        "version": "v0.12.91",
                        "status": "shadow_only_unqualified_money_flow_probe",
                        "order_effect": "shadow_only",
                        "shadow_result_saved": True,
                        "would_order_without_router": True,
                        "message": (
                            "B-grade/follower/data-gap money-flow probe is shadow only"
                        ),
                    }
                )
                return policy
            policy.update(
                {
                    "version": "v0.12.91",
                    "status": "allowed_a_leader_money_flow_minimum_probe",
                    "order_effect": "allow_minimum_probe",
                    "max_quantity": 1,
                    "shadow_result_saved": True,
                    "message": (
                        "A-grade leader/second money-flow probe can affect orders with max one share"
                    ),
                }
            )
            return policy
        if bool((legacy_entry_policy or {}).get("legacy_candidate")):
            policy.update(
                {
                    "version": "v0.12.84",
                    "status": "shadow_only_legacy_entry_route",
                    "order_effect": "shadow_only",
                    "shadow_result_saved": True,
                    "would_order_without_router": bool(
                        (legacy_entry_policy or {}).get("old_logic_would_buy")
                    ),
                    "legacy_route": (legacy_entry_policy or {}).get(
                        "legacy_route"
                    ),
                    "legacy_reasons": (legacy_entry_policy or {}).get(
                        "legacy_reasons"
                    )
                    or [],
                    "message": (
                        "legacy entry route is observed only by v0.12.84 router"
                    ),
                }
            )
            return policy
        policy.update(
            {
                "status": "shadow_only_non_core_playbook",
                "order_effect": "shadow_only",
                "shadow_result_saved": True,
                "would_order_without_router": True,
                "message": (
                    "non-core playbook is observed only by v0.12.81 router"
                ),
            }
        )
        return policy

    @staticmethod
    def _data_gap_probe_attempt_count(
        connection: Any,
        *,
        account_id: int,
        stock_code: str,
        day: str,
        entry_type: str,
        ) -> int:
        rows = connection.execute(
            """
            SELECT tp.setup_json
            FROM trade_plans tp
            JOIN strategy_decisions d ON d.id = tp.decision_id
            WHERE d.account_id = ?
              AND tp.stock_code = ?
              AND tp.entry_type = ?
              AND d.action = 'buy'
              AND substr(d.decided_at, 1, 10) = ?
            ORDER BY d.decided_at DESC, d.id DESC
            """,
            (account_id, stock_code, entry_type, day),
        ).fetchall()
        count = 0
        for row in rows:
            try:
                setup = json.loads(row["setup_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            data_policy = (
                setup.get("data_availability_policy")
                or (setup.get("entry_guard_policy") or {}).get(
                    "data_availability"
                )
                or {}
            )
            entry_status = str(
                (setup.get("entry_guard_policy") or {}).get("status") or ""
            )
            if data_policy.get("minimum_probe") or entry_status.startswith(
                "allowed_minimum_probe_data_gap"
            ):
                count += 1
        return count

    def _small_account_aggressive_policy(
        self,
        *,
        account_name: str,
        account_total_equity: int | None,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        entry_grade: str,
        grade_details: dict[str, Any],
        data_policy: dict[str, Any],
        leader_score: dict[str, Any] | None,
        money_flow_policy: dict[str, Any],
        now: datetime,
        enabled_override: bool | None = None,
        armed_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = self.strategy_settings
        armed_policy = armed_policy or {}
        armed_trigger_ready = bool(armed_policy.get("trigger_ready"))
        enabled = (
            settings.small_account_aggressive_enabled
            if enabled_override is None
            else enabled_override
        )
        active = (
            enabled
            and isinstance(self.order_executor, PaperOrderExecutor)
            and (account_total_equity or 0) > 0
            and (account_total_equity or 0) <= settings.small_account_max_equity
        )
        policy: dict[str, Any] = {
            "enabled": enabled,
            "active": active,
            "account_name": account_name,
            "account_total_equity": account_total_equity,
            "max_equity": settings.small_account_max_equity,
            "paper_only": True,
            "profile": "small_account_aggressive_1m",
            "grade": entry_grade,
            "blocked": False,
            "status": "disabled" if not active else "evaluating",
            "message": "",
            "target_position_rate": None,
            "per_trade_risk_rate": None,
            "minimum_quantity_if_affordable": 0,
            "feature_bundle": "v0.12.63_64",
            "a_plus_conditions": {},
            "money_flow_policy": money_flow_policy,
            "a_plus_armed_policy": armed_policy,
        }
        if not active:
            return policy

        supply_total = _decimal_or_none((signal.supply or {}).get("total_score"))
        supply_ok = (
            signal.supply is not None
            and not bool(signal.supply.get("blocked"))
            and (supply_total is None or supply_total >= Decimal("60"))
        )
        current_time = now.time().replace(tzinfo=None)
        in_opening_leader_window = clock_time(9, 0) <= current_time < clock_time(9, 20)
        leader_role = str((leader_score or {}).get("role") or "")
        leader_total = _decimal_or_none((leader_score or {}).get("total_score"))
        leader_ok = (
            leader_role in {"leader", "second"}
            and not bool((leader_score or {}).get("blocked"))
            and (leader_total is None or leader_total >= Decimal("70"))
        )
        leader_trade_amount_score = _decimal_or_none(
            (leader_score or {}).get("trade_amount_score")
        )
        leader_rank_score = _decimal_or_none((leader_score or {}).get("rank_score"))
        rank_ok = bool(
            grade_details.get("market_rank_top")
            or grade_details.get("trade_amount_top")
        )
        trade_amount_top = bool(grade_details.get("trade_amount_top"))
        money_flow_top = bool(
            trade_amount_top
            or (
                leader_trade_amount_score is not None
                and leader_trade_amount_score >= Decimal("70")
            )
            or (
                signal.projected_trade_amount is not None
                and signal.projected_trade_amount >= 5_000_000_000
            )
        )
        day_leadership_ok = bool(
            leader_ok
            or rank_ok
            or (
                leader_rank_score is not None
                and leader_rank_score >= Decimal("70")
            )
        )
        new_high_ok = bool(
            grade_details.get("daily_breakout_ok")
            or signal.opening_range_pass
            or (
                signal.intraday_high is not None
                and signal.current_price > int(signal.intraday_high)
            )
        )
        vwap_or_orb_ok = (
            signal.opening_range_pass
            or (signal.breakout_hold_pass and signal.vwap_pass)
            or (
                signal.vwap is not None
                and Decimal(signal.current_price) >= signal.vwap
            )
        )
        chart_score = _decimal_or_none(grade_details.get("chart_score")) or Decimal("0")
        stock_name = str((leader_score or {}).get("stock_name") or "").strip() or None
        stock_category = stock_category_payload(signal.stock_code, stock_name)
        normal_stock = stock_category["key"] == COMMON_STOCK
        a_plus_conditions = {
            "opening_window": in_opening_leader_window,
            "armed_trigger_ready": armed_trigger_ready,
            "armed_trigger_type": armed_policy.get("trigger_type"),
            "trade_amount_top": trade_amount_top,
            "money_flow_top": money_flow_top,
            "money_flow_score": money_flow_policy.get("total_score"),
            "money_flow_a_plus_qualified": bool(
                money_flow_policy.get("a_plus_qualified")
            ),
            "realtime_rank_top": bool(
                money_flow_policy.get("realtime_rank_top")
            ),
            "day_leadership_ok": day_leadership_ok,
            "new_high_ok": new_high_ok,
            "orb_or_vwap_hold": vwap_or_orb_ok,
            "supply_ok": supply_ok,
            "supply_missing": bool(money_flow_policy.get("supply_missing")),
            "confirmed_net_sell": bool(
                money_flow_policy.get("confirmed_net_sell")
            ),
            "normal_stock": normal_stock,
            "stock_category": stock_category,
            "leader_role": leader_role or None,
            "leader_total_score": (
                str(leader_total) if leader_total is not None else None
            ),
            "leader_trade_amount_score": (
                str(leader_trade_amount_score)
                if leader_trade_amount_score is not None
                else None
            ),
            "projected_trade_amount": signal.projected_trade_amount,
            "chart_score": str(chart_score),
        }
        policy["a_plus_conditions"] = a_plus_conditions
        a_plus = (
            entry_grade == "A"
            and (in_opening_leader_window or armed_trigger_ready)
            and bool(money_flow_policy.get("a_plus_qualified"))
            and BreakoutStrategy._a_plus_entry_type(trade_plan.entry_type)
            and chart_score >= Decimal("65")
        )
        money_flow_data_gap_probe_allowed = bool(
            data_policy.get("minimum_probe")
            and data_policy.get("status") == "minimum_probe_money_flow_data_gap"
            and money_flow_policy.get("strong_probe_qualified")
        )
        a_plus_data_gap_probe_allowed = bool(
            a_plus
            and data_policy.get("minimum_probe")
            and data_policy.get("status") == "minimum_probe_missing_leader_with_rank_proxy"
            and data_policy.get("supply_status") == "available"
            and data_policy.get("leader_status") == "market_rank_proxy"
        )

        if settings.small_account_block_data_gap and (
            data_policy.get("blocked")
            or data_policy.get("minimum_probe")
            or data_policy.get("supply_status") == "missing"
            or data_policy.get("leader_status") == "missing"
        ) and not a_plus_data_gap_probe_allowed and not money_flow_data_gap_probe_allowed:
            policy.update(
                {
                    "blocked": True,
                    "status": "blocked_small_account_data_gap",
                    "message": (
                        "소액 공격 모드: 데이터 공백/최소탐색 후보는 신규 진입 차단"
                    ),
                }
            )
            return policy
        if (
            settings.small_account_block_supply_missing
            and signal.supply is None
            and not money_flow_data_gap_probe_allowed
        ):
            policy.update(
                {
                    "blocked": True,
                    "status": "blocked_small_account_supply_missing",
                    "message": "소액 공격 모드: 수급 공백 후보 신규 진입 차단",
                }
            )
            return policy
        if settings.small_account_block_c_grade and entry_grade == "C":
            policy.update(
                {
                    "blocked": True,
                    "status": "blocked_small_account_c_grade",
                    "message": "소액 공격 모드: C급 후보 신규 진입 차단",
                }
            )
            return policy
        if money_flow_data_gap_probe_allowed:
            policy.update(
                {
                    "status": "allowed_small_account_money_flow_data_gap_probe",
                    "grade": entry_grade,
                    "minimum_quantity_if_affordable": (
                        settings.small_account_minimum_quantity_if_affordable
                    ),
                    "max_quantity": settings.data_gap_minimum_probe_max_quantity,
                    "message": (
                        "소액 공격 모드: 데이터 공백이지만 strong money-flow라 "
                        "최소수량 탐색 허용"
                    ),
                    "data_gap_probe": True,
                    "money_flow_probe": True,
                }
            )
            return policy

        supply_total = _decimal_or_none((signal.supply or {}).get("total_score"))
        supply_ok = (
            signal.supply is not None
            and not bool(signal.supply.get("blocked"))
            and (supply_total is None or supply_total >= Decimal("60"))
        )
        current_time = now.time().replace(tzinfo=None)
        in_opening_leader_window = clock_time(9, 0) <= current_time < clock_time(9, 20)
        leader_role = str((leader_score or {}).get("role") or "")
        leader_total = _decimal_or_none((leader_score or {}).get("total_score"))
        leader_ok = (
            leader_role in {"leader", "second"}
            and not bool((leader_score or {}).get("blocked"))
            and (leader_total is None or leader_total >= Decimal("70"))
        )
        leader_trade_amount_score = _decimal_or_none(
            (leader_score or {}).get("trade_amount_score")
        )
        leader_rank_score = _decimal_or_none((leader_score or {}).get("rank_score"))
        rank_ok = bool(
            grade_details.get("market_rank_top")
            or grade_details.get("trade_amount_top")
        )
        trade_amount_top = bool(grade_details.get("trade_amount_top"))
        money_flow_top = bool(
            trade_amount_top
            or (
                leader_trade_amount_score is not None
                and leader_trade_amount_score >= Decimal("70")
            )
            or (
                signal.projected_trade_amount is not None
                and signal.projected_trade_amount >= 5_000_000_000
            )
        )
        day_leadership_ok = bool(
            leader_ok
            or rank_ok
            or (
                leader_rank_score is not None
                and leader_rank_score >= Decimal("70")
            )
        )
        new_high_ok = bool(
            grade_details.get("daily_breakout_ok")
            or signal.opening_range_pass
            or (
                signal.intraday_high is not None
                and signal.current_price > int(signal.intraday_high)
            )
        )
        vwap_or_orb_ok = (
            signal.opening_range_pass
            or (signal.breakout_hold_pass and signal.vwap_pass)
            or (
                signal.vwap is not None
                and Decimal(signal.current_price) >= signal.vwap
            )
        )
        chart_score = _decimal_or_none(grade_details.get("chart_score")) or Decimal("0")
        stock_name = str((leader_score or {}).get("stock_name") or "").strip() or None
        stock_category = stock_category_payload(signal.stock_code, stock_name)
        normal_stock = stock_category["key"] == COMMON_STOCK
        a_plus_conditions = {
            "opening_window": in_opening_leader_window,
            "armed_trigger_ready": armed_trigger_ready,
            "armed_trigger_type": armed_policy.get("trigger_type"),
            "trade_amount_top": trade_amount_top,
            "money_flow_top": money_flow_top,
            "day_leadership_ok": day_leadership_ok,
            "new_high_ok": new_high_ok,
            "orb_or_vwap_hold": vwap_or_orb_ok,
            "supply_ok": supply_ok,
            "normal_stock": normal_stock,
            "stock_category": stock_category,
            "leader_role": leader_role or None,
            "leader_total_score": (
                str(leader_total) if leader_total is not None else None
            ),
            "leader_trade_amount_score": (
                str(leader_trade_amount_score)
                if leader_trade_amount_score is not None
                else None
            ),
            "projected_trade_amount": signal.projected_trade_amount,
            "chart_score": str(chart_score),
        }
        policy["a_plus_conditions"] = a_plus_conditions
        a_plus = (
            entry_grade == "A"
            and (in_opening_leader_window or armed_trigger_ready)
            and supply_ok
            and money_flow_top
            and day_leadership_ok
            and new_high_ok
            and vwap_or_orb_ok
            and normal_stock
            and trade_plan.entry_type
            in {
                "opening_range_breakout",
                "chart_breakout_hold",
                "volume_breakout",
                "breakout_probe",
            }
            and chart_score >= Decimal("65")
        )
        if a_plus:
            policy.update(
                {
                    "status": (
                        "allowed_small_account_a_plus_data_gap_probe"
                        if a_plus_data_gap_probe_allowed
                        else "allowed_small_account_a_plus"
                    ),
                    "grade": "A+",
                    "target_position_rate": str(
                        settings.small_account_a_plus_position_rate
                    ),
                    "per_trade_risk_rate": (
                        str(settings.small_account_a_plus_per_trade_risk_rate)
                    ),
                    "minimum_quantity_if_affordable": (
                        settings.small_account_minimum_quantity_if_affordable
                    ),
                    "message": (
                        "소액 공격 모드 A+: 장초반 money-flow·실시간 순위·"
                        "신고가·VWAP·수급 조건 충족"
                    ),
                    "data_gap_probe": a_plus_data_gap_probe_allowed,
                    "exit_policy": (
                        "aggressive_a_plus_fast_partial_and_structure_exit"
                    ),
                }
            )
            return policy
        if entry_grade == "A":
            policy.update(
                {
                    "status": "allowed_small_account_a",
                    "target_position_rate": str(
                        settings.small_account_a_position_rate
                    ),
                    "per_trade_risk_rate": str(
                        settings.small_account_a_per_trade_risk_rate
                    ),
                    "minimum_quantity_if_affordable": (
                        settings.small_account_minimum_quantity_if_affordable
                    ),
                    "message": "소액 공격 모드 A급: 계좌 20~35% 구간 목표",
                }
            )
            return policy
        if entry_grade == "B":
            if (
                money_flow_policy.get("strong_probe_qualified")
                and BreakoutStrategy._a_plus_entry_type(trade_plan.entry_type)
                and signal.supply is not None
                and not bool((signal.supply or {}).get("blocked"))
            ):
                policy.update(
                    {
                        "status": "allowed_small_account_b_money_flow_probe",
                        "target_position_rate": str(
                            settings.small_account_b_position_rate
                        ),
                        "per_trade_risk_rate": str(
                            settings.small_account_b_per_trade_risk_rate
                        ),
                        "minimum_quantity_if_affordable": (
                            settings.small_account_minimum_quantity_if_affordable
                        ),
                        "max_quantity": 1,
                        "message": (
                            "소액 공격 모드 B급 strong money-flow: 1주 탐색 허용"
                        ),
                        "money_flow_probe": True,
                    }
                )
                return policy
            policy.update(
                {
                    "status": "allowed_small_account_b_reduced",
                    "target_position_rate": str(
                        settings.small_account_b_position_rate
                    ),
                    "per_trade_risk_rate": str(
                        settings.small_account_b_per_trade_risk_rate
                    ),
                    "minimum_quantity_if_affordable": (
                        settings.small_account_minimum_quantity_if_affordable
                    ),
                    "message": "소액 공격 모드 B급: 축소 수량만 허용",
                }
            )
            return policy
        return policy

    def _entry_type_policy(
        self,
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        entry_grade: str,
        grade_details: dict[str, Any],
        market_rank_proxy: dict[str, Any],
        in_morning_window: bool,
        now: datetime,
        same_stock_buy_count: int,
        vwap_reclaimed: bool,
    ) -> dict[str, Any]:
        settings = self.strategy_settings
        entry_type = trade_plan.entry_type
        leader_or_rank_top = bool(
            grade_details.get("leader_role") in {"leader", "second"}
            or grade_details.get("market_rank_top")
            or grade_details.get("trade_amount_top")
        )

        policy: dict[str, Any] = {
            "status": "allowed",
            "entry_type": entry_type,
            "entry_grade": entry_grade,
            "minimum_probe": False,
            "quantity_multiplier": "1",
            "max_quantity": None,
            "blocked": False,
            "message": "",
            "vwap_reclaimed": vwap_reclaimed,
            "same_stock_buy_count": same_stock_buy_count,
        }

        def minimum_probe(
            status: str,
            message: str,
            *,
            max_quantity: int | None = None,
        ) -> dict[str, Any]:
            policy.update(
                {
                    "status": status,
                    "minimum_probe": True,
                    "quantity_multiplier": str(
                        settings.entry_type_minimum_probe_quantity_multiplier
                    ),
                    "message": message,
                }
            )
            if max_quantity is not None:
                policy["max_quantity"] = max_quantity
            return policy

        def block(status: str, message: str) -> dict[str, Any]:
            policy.update(
                {
                    "status": status,
                    "blocked": True,
                    "message": message,
                }
            )
            return policy

        if entry_type == "chart_order_block_reentry":
            if entry_grade != "A":
                return block(
                    "blocked_order_block_reentry_not_a_grade",
                    "오더블럭 재진입은 A급이 아니면 차단",
                )
            return minimum_probe(
                "minimum_probe_order_block_reentry_a_grade",
                "A급 오더블럭 재진입은 최소수량만 허용",
            )

        if entry_type == "chart_vwap_reclaim":
            if entry_grade != "A":
                return block(
                    "blocked_vwap_reclaim_not_a_grade",
                    "VWAP 재안착 진입은 A급이 아니면 차단",
                )
            if not vwap_reclaimed:
                return block(
                    "blocked_vwap_reclaim_without_confirmation",
                    "VWAP 재안착 확인 전 진입 차단",
                )
            return minimum_probe(
                "minimum_probe_vwap_reclaim_a_grade",
                "A급 VWAP 재안착 진입은 최소수량만 허용",
            )

        if entry_type == "chart_pattern_entry" and entry_grade == "C":
            return block(
                "blocked_low_score_chart_pattern_entry",
                "낮은 점수의 차트 패턴 진입 차단",
            )

        current_time = now.time().replace(tzinfo=None)
        midday_chase_window = (
            clock_time(10, 0) <= current_time < REENTRY_CUTOFF
        )
        intraday_high = signal.intraday_high
        near_intraday_high = bool(
            intraday_high is not None
            and Decimal(signal.current_price)
            >= Decimal(int(intraday_high)) * Decimal("0.985")
        )
        support_confirmed = bool(
            signal.vwap_pass or signal.pullback_pass or signal.moving_average_pass
        )
        b_grade_reentry_window = current_time >= clock_time(10, 0)
        if (
            entry_grade == "B"
            and same_stock_buy_count > 0
            and b_grade_reentry_window
            and entry_type
            in {
                "chart_breakout_hold",
                "volume_breakout",
                "breakout_probe",
                "chart_pattern_entry",
                "vwap_pullback",
            }
        ):
            policy["b_grade_reentry_limit"] = {
                "window": "10:00~close",
                "same_stock_buy_count": same_stock_buy_count,
                "near_intraday_high": near_intraday_high,
                "support_confirmed": support_confirmed,
                "vwap_pass": signal.vwap_pass,
                "pullback_pass": signal.pullback_pass,
                "moving_average_pass": signal.moving_average_pass,
                "intraday_high": intraday_high,
                "current_price": signal.current_price,
            }
            if near_intraday_high and not support_confirmed:
                return block(
                    "blocked_b_grade_midday_high_chase_reentry",
                    "B grade repeated entry near intraday high is blocked without VWAP/pullback/MA support",
                )
            return minimum_probe(
                "minimum_probe_b_grade_reentry",
                "B grade repeated entry after mid-session is limited to one-share probe",
                max_quantity=1,
            )
        if (
            entry_grade == "B"
            and midday_chase_window
            and entry_type
            in {
                "chart_breakout_hold",
                "volume_breakout",
                "breakout_probe",
                "chart_pattern_entry",
            }
            and near_intraday_high
            and not support_confirmed
        ):
            chart = signal.chart
            supply_clear = signal.supply is not None and not bool(
                signal.supply.get("blocked")
            )
            policy["midday_b_chase"] = {
                "window": "10:00~14:30",
                "near_intraday_high": near_intraday_high,
                "support_confirmed": support_confirmed,
                "vwap_pass": signal.vwap_pass,
                "pullback_pass": signal.pullback_pass,
                "moving_average_pass": signal.moving_average_pass,
                "intraday_high": intraday_high,
                "current_price": signal.current_price,
                "leader_or_rank_top": leader_or_rank_top,
                "supply_clear": supply_clear,
                "chart_score": (
                    str(chart.total_score) if chart is not None else None
                ),
            }
            if (
                leader_or_rank_top
                and supply_clear
                and chart is not None
                and not chart.blocked
                and chart.total_score >= Decimal("75")
            ):
                return minimum_probe(
                    "minimum_probe_midday_b_grade_chase",
                    "B grade midday chase near high is limited to one-share probe until VWAP/pullback/MA support confirms",
                    max_quantity=1,
                )
            return block(
                "blocked_midday_b_grade_chase_without_support",
                "B grade midday chase near high is blocked without VWAP/pullback/MA support",
            )

        if entry_type == "chart_breakout_hold" and in_morning_window:
            if not leader_or_rank_top:
                return block(
                    "blocked_morning_breakout_without_leader_or_rank",
                    "장초반 돌파 유지 진입은 대장주 또는 거래대금 상위 확인 필요",
                )

        if (
            entry_type == "opening_range_breakout"
            and same_stock_buy_count > 0
            and not vwap_reclaimed
        ):
            return block(
                "blocked_orb_reentry_without_vwap_reclaim",
                "ORB 첫 진입 이후 재진입은 VWAP 재회복 확인 필요",
            )

        return policy

    def _same_level_failure_policy(
        self,
        *,
        account_id: int,
        signal: BreakoutSignal,
        as_of: datetime,
    ) -> dict[str, Any]:
        tolerance_rate = Decimal("0.005")
        failure_limit = 2
        current_anchors = self._signal_level_anchors(signal)
        policy: dict[str, Any] = {
            "status": "allowed",
            "blocked": False,
            "failure_count": 0,
            "failure_limit": failure_limit,
            "level_tolerance_rate": str(tolerance_rate),
            "current_anchors": [
                self._serializable_anchor(anchor) for anchor in current_anchors
            ],
            "matched_failures": [],
            "unlocked_by_new_high": False,
            "unlocked_by_new_base": False,
            "message": "",
        }
        if not current_anchors:
            return policy

        day_start = datetime.combine(as_of.date(), clock_time.min).isoformat(
            timespec="seconds"
        )
        day_end = datetime.combine(as_of.date(), clock_time.max).isoformat(
            timespec="seconds"
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.id, e.event_type, e.exit_class, e.exit_reason_class,
                       e.setup_invalidated, e.occurred_at,
                       (
                         SELECT tp.setup_json
                         FROM trade_plans tp
                         JOIN strategy_decisions d ON d.id = tp.decision_id
                         WHERE d.account_id = e.account_id
                           AND d.stock_code = e.stock_code
                           AND d.decided_at <= e.occurred_at
                         ORDER BY d.decided_at DESC, tp.id DESC
                         LIMIT 1
                       ) AS setup_json
                FROM paper_risk_events e
                WHERE e.account_id = ?
                  AND e.stock_code = ?
                  AND e.status = 'executed'
                  AND e.occurred_at >= ?
                  AND e.occurred_at <= ?
                  AND (
                    e.setup_invalidated = 1
                    OR e.exit_class IN (
                      'loss_stop', 'trailing_loss_stop', 'partial_loss_exit'
                    )
                    OR e.exit_reason_class IN (
                      'structure_loss_stop', 'noise_stop',
                      'news_or_supply_stop', 'market_break_stop'
                    )
                  )
                ORDER BY e.occurred_at DESC, e.id DESC
                """,
                (account_id, signal.stock_code, day_start, day_end),
            ).fetchall()

        matched_failures: list[dict[str, Any]] = []
        failed_highs: list[Decimal] = []
        failed_breakouts: list[Decimal] = []
        for row in rows:
            setup = self._json_object(row["setup_json"])
            failed_anchors = self._setup_level_anchors(setup)
            if not failed_anchors:
                continue
            for anchor in failed_anchors:
                if anchor["kind"] == "high":
                    failed_highs.append(anchor["value"])
                if anchor["kind"] == "breakout":
                    failed_breakouts.append(anchor["value"])
            matches = [
                {
                    "current": self._serializable_anchor(current_anchor),
                    "failed": self._serializable_anchor(failed_anchor),
                }
                for current_anchor in current_anchors
                for failed_anchor in failed_anchors
                if self._levels_match(
                    current_anchor,
                    failed_anchor,
                    tolerance_rate=tolerance_rate,
                )
            ]
            if not matches:
                continue
            matched_failures.append(
                {
                    "event_id": int(row["id"]),
                    "event_type": row["event_type"],
                    "exit_class": row["exit_class"],
                    "exit_reason_class": row["exit_reason_class"],
                    "setup_invalidated": bool(row["setup_invalidated"]),
                    "occurred_at": row["occurred_at"],
                    "matches": matches,
                }
            )

        policy["failure_count"] = len(matched_failures)
        policy["matched_failures"] = matched_failures[:failure_limit]
        if len(matched_failures) < failure_limit:
            return policy

        current_highs = [
            anchor["value"] for anchor in current_anchors if anchor["kind"] == "high"
        ]
        current_breakouts = [
            anchor["value"]
            for anchor in current_anchors
            if anchor["kind"] == "breakout"
        ]
        matched_kinds = {
            match["current"]["kind"]
            for failure in matched_failures
            for match in failure["matches"]
        }
        if (
            failed_breakouts
            and current_breakouts
            and max(current_breakouts)
            > max(failed_breakouts) * (Decimal("1") + tolerance_rate)
            and "breakout" not in matched_kinds
        ):
            policy["unlocked_by_new_base"] = True
            policy["status"] = "allowed_new_base_after_same_level_failures"
            return policy

        policy.update(
            {
                "status": "blocked_same_level_failure_reentry",
                "blocked": True,
                "message": "same level failed twice; wait for a new base",
            }
        )
        return policy

    @staticmethod
    def _signal_level_anchors(signal: BreakoutSignal) -> list[dict[str, Any]]:
        anchors: list[dict[str, Any]] = []
        for name, kind in (
            ("breakout_level", "breakout"),
            ("opening_range_high", "breakout"),
            ("vwap", "vwap"),
            ("previous_high", "high"),
            ("intraday_high", "high"),
        ):
            value = _decimal_or_none(getattr(signal, name, None))
            if value is None or value <= 0:
                continue
            anchors.append({"name": name, "kind": kind, "value": value})
        return anchors

    @staticmethod
    def _setup_level_anchors(setup: dict[str, Any]) -> list[dict[str, Any]]:
        anchors: list[dict[str, Any]] = []
        for name, kind in (
            ("breakout_level", "breakout"),
            ("opening_range_high", "breakout"),
            ("vwap", "vwap"),
            ("previous_high", "high"),
            ("intraday_high", "high"),
        ):
            value = _decimal_or_none(setup.get(name))
            if value is None or value <= 0:
                continue
            anchors.append({"name": name, "kind": kind, "value": value})
        return anchors

    @staticmethod
    def _levels_match(
        current_anchor: dict[str, Any],
        failed_anchor: dict[str, Any],
        *,
        tolerance_rate: Decimal,
    ) -> bool:
        if current_anchor.get("kind") != failed_anchor.get("kind"):
            return False
        current_value = _decimal_or_none(current_anchor.get("value"))
        failed_value = _decimal_or_none(failed_anchor.get("value"))
        if current_value is None or failed_value is None or failed_value <= 0:
            return False
        return abs(current_value - failed_value) <= failed_value * tolerance_rate

    @staticmethod
    def _serializable_anchor(anchor: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": anchor.get("name"),
            "kind": anchor.get("kind"),
            "value": str(anchor.get("value")),
        }

    @staticmethod
    def _json_object(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if not payload:
            return {}
        try:
            value = json.loads(str(payload))
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _data_gap_policy(
        self,
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        entry_grade: str,
        leader_score: dict[str, Any] | None,
        market_rank_proxy: dict[str, Any],
        decision_score: Decimal,
        money_flow_policy: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        settings = self.strategy_settings
        money_flow_policy = money_flow_policy or {}
        chart = signal.chart
        force_entry = bool(chart is not None and chart.force_entry)
        supply_missing = signal.supply is None
        leader_missing = leader_score is None
        proxy_usable = bool(market_rank_proxy.get("usable"))
        policy: dict[str, Any] = {
            "status": "available",
            "supply_status": "missing" if supply_missing else "available",
            "leader_status": (
                "market_rank_proxy"
                if leader_missing and proxy_usable
                else ("missing" if leader_missing else "available")
            ),
            "market_rank_proxy": market_rank_proxy,
            "force_entry": force_entry,
            "minimum_probe": False,
            "quantity_multiplier": "1",
            "add_buy_policy": "allowed",
            "reentry_policy": "allowed",
            "blocked": False,
            "message": "",
        }
        if supply_missing:
            policy["add_buy_policy"] = "blocked"
            policy["reentry_policy"] = "blocked_until_supply_collected"

        def minimum_probe(status: str, message: str) -> dict[str, Any]:
            policy.update(
                {
                    "status": status,
                    "minimum_probe": True,
                    "quantity_multiplier": str(
                        settings.data_gap_minimum_probe_quantity_multiplier
                    ),
                    "max_quantity": settings.data_gap_minimum_probe_max_quantity,
                    "message": message,
                }
            )
            return policy

        def block(status: str, message: str) -> dict[str, Any]:
            policy.update(
                {
                    "status": status,
                    "blocked": True,
                    "message": message,
                }
            )
            return policy

        def strong_missing_supply_b_probe_allowed() -> bool:
            current_time = (now or datetime.now()).time().replace(tzinfo=None)
            if not clock_time(9, 10) <= current_time < clock_time(12, 0):
                return False
            if self.market_mode_settings.enabled and (
                self.market_mode_settings.manual_mode == "risk_off"
            ):
                return False
            if entry_grade != "B":
                return False
            if decision_score < Decimal("76"):
                return False
            if trade_plan.entry_type != "chart_breakout_hold":
                return False
            if not proxy_usable:
                return False
            if chart is None or chart.blocked:
                return False
            if chart.total_score < Decimal("75"):
                return False
            return bool(force_entry or signal.breakout_hold_pass)

        def strong_money_flow_data_gap_probe_allowed() -> bool:
            if not settings.data_gap_probe_enabled:
                return False
            if not bool(money_flow_policy.get("strong_probe_qualified")):
                return False
            if entry_grade not in {"A", "B"}:
                return False
            if not BreakoutStrategy._a_plus_entry_type(trade_plan.entry_type):
                return False
            if bool(money_flow_policy.get("supply_blocked")):
                return False
            if bool(money_flow_policy.get("confirmed_net_sell")):
                return False
            if chart is None or chart.blocked:
                return False
            if chart.total_score < Decimal("65"):
                return False
            return bool(
                signal.vwap_pass
                and money_flow_policy.get("new_high")
                and money_flow_policy.get("money_flow_top")
            )

        if supply_missing:
            if strong_money_flow_data_gap_probe_allowed():
                return minimum_probe(
                    "minimum_probe_money_flow_data_gap",
                    "수급/대장주 데이터 공백: strong money-flow 후보는 1주 탐색만 허용",
                )
            if leader_missing and not proxy_usable:
                return block(
                    "blocked_missing_supply_leader",
                    "수급/대장주 점수 없음: 시장순위 프록시도 없어 신규 진입 차단",
                )
            if strong_missing_supply_b_probe_allowed():
                return minimum_probe(
                    "minimum_probe_missing_supply_strong_b_grade_breakout",
                    "수급 없음: 강한 B급 돌파 후보는 오전 1주 탐색만 허용",
                )
            if entry_grade != "A":
                return block(
                    "blocked_missing_supply_non_a_grade",
                    "수급 없음: B/C급 후보 신규 진입 차단",
                )
            if not settings.data_gap_probe_enabled:
                return block(
                    "blocked_missing_supply_probe_disabled",
                    "수급 없음: 최소수량 탐색 비활성으로 신규 진입 차단",
                )
            if leader_missing:
                return minimum_probe(
                    "minimum_probe_missing_supply_leader_with_rank_proxy",
                    "수급/대장주 점수 없음: A급+0187/0198 상위권만 1주 탐색 허용",
                )
            return minimum_probe(
                "minimum_probe_missing_supply_a_grade",
                "수급 없음: A급 후보만 1주 탐색 허용, 추가매수/재진입 금지",
            )

        if not settings.data_gap_probe_enabled:
            return policy

        if force_entry and leader_missing:
            if proxy_usable:
                return minimum_probe(
                    "minimum_probe_missing_leader_with_rank_proxy",
                    "대장주 점수 없음: 0187/0198 상위권 프록시로 최소수량만 허용",
                )
            return block(
                "blocked_missing_leader_force_entry",
                "대장주 점수 없음: 시장순위 프록시도 없어 차트 강제진입 차단",
            )

        return policy

    @staticmethod
    def _supply_block_relaxed_by_chart(signal: BreakoutSignal) -> bool:
        if not bool((signal.supply or {}).get("blocked")):
            return False
        chart = signal.chart
        if chart is None or chart.blocked:
            return False
        return (
            chart.force_entry
            and chart.total_score >= Decimal("75")
            and (
                signal.breakout_hold_pass
                or signal.opening_range_pass
                or chart.entry_type
                in {
                    "chart_breakout_hold",
                    "opening_range_breakout",
                    "volume_breakout",
                    "order_flow_momentum",
                }
            )
        )

    def _supply_block_policy(
        self,
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        entry_grade: str,
        leader_score: dict[str, Any] | None,
        same_stock_buy_count: int,
        has_position: bool,
        decision_score: Decimal,
    ) -> dict[str, Any]:
        if not bool((signal.supply or {}).get("blocked")):
            return {"status": "clear", "blocked": False}
        supply = signal.supply or {}
        confirmed_net_sell = self._confirmed_supply_net_sell(supply)
        leader_role = str((leader_score or {}).get("role") or "")
        leader_total = _decimal_or_none((leader_score or {}).get("total_score"))
        leader_maintained = (
            leader_role in {"leader", "second"}
            and (leader_total is None or leader_total >= Decimal("70"))
            and not bool((leader_score or {}).get("blocked"))
        )
        chart = signal.chart
        chart_ok = (
            chart is not None
            and not chart.blocked
            and chart.force_entry
            and chart.total_score >= Decimal("75")
            and trade_plan.entry_type
            in {
                "chart_breakout_hold",
                "opening_range_breakout",
                "volume_breakout",
                "order_flow_momentum",
            }
        )
        strong_allowed = (
            entry_grade == "A"
            and decision_score >= Decimal("70")
            and leader_maintained
            and chart_ok
            and (
                signal.breakout_hold_pass
                or signal.opening_range_pass
                or signal.order_flow_pass
            )
        )
        if same_stock_buy_count > 0 or has_position:
            return {
                "status": "blocked_supply_confirmed_followup",
                "blocked": True,
                "message": (
                    "수급 차단 확인 후 같은 종목 후속 진입 차단: "
                    f"{supply.get('blocked_reason') or '-'}"
                ),
                "confirmed_net_sell": confirmed_net_sell,
                "same_stock_buy_count": same_stock_buy_count,
                "has_position": has_position,
            }
        if not strong_allowed:
            return {
                "status": "blocked_supply_hard_block_weak_entry",
                "blocked": True,
                "message": (
                    "수급 차단: 강한 대장주·차트 재평가 조건 미충족, "
                    f"{supply.get('blocked_reason') or '-'}"
                ),
                "confirmed_net_sell": confirmed_net_sell,
                "leader_maintained": leader_maintained,
                "chart_ok": chart_ok,
            }
        return {
            "status": "relaxed",
            "policy_status": "allowed_supply_hard_block_reduced_probe",
            "blocked": False,
            "reason": supply.get("blocked_reason"),
            "confirmed_net_sell": confirmed_net_sell,
            "leader_maintained": leader_maintained,
            "chart_ok": chart_ok,
            "quantity_multiplier": str(
                self.strategy_settings.entry_type_minimum_probe_quantity_multiplier
            ),
            "fast_profit_lock": True,
            "exit_watch": "confirmed_net_sell_fast_exit",
            "message": "수급 hard block 완화: 강한 대장주·차트, 축소 수량과 빠른 청산 감시",
        }

    @staticmethod
    def _confirmed_supply_net_sell(supply: dict[str, Any]) -> bool:
        components = supply.get("components") or {}
        program = components.get("program") or {}
        trend = components.get("trend") or {}
        return bool(
            program.get("hard_block")
            or program.get("policy_state") == "block"
            or int(program.get("consecutive_net_sell_count") or 0) >= 3
            or int(trend.get("consecutive_net_sell_count") or 0) >= 3
            or _float_value(supply.get("total_score"), 50.0) <= 20.0
        )

    @staticmethod
    def _apply_entry_guard_quantity(
        planned_quantity: int,
        policy: dict[str, Any],
    ) -> int:
        if planned_quantity < 1:
            return 0
        multiplier = _decimal_or_none(policy.get("quantity_multiplier"))
        adjusted = planned_quantity
        if multiplier is not None and multiplier < Decimal("1"):
            adjusted = max(
                1,
                int(
                    (Decimal(planned_quantity) * multiplier).to_integral_value(
                        rounding=ROUND_FLOOR
                    )
                ),
            )
        max_quantity = policy.get("max_quantity")
        if max_quantity is not None:
            adjusted = min(adjusted, max(1, int(max_quantity)))
        return adjusted

    def _market_rank_proxy(
        self,
        stock_code: str,
        now: datetime,
    ) -> dict[str, Any]:
        current = now.astimezone(SEOUL_TIMEZONE) if now.tzinfo else now
        current = current.replace(tzinfo=None)
        settings = self.strategy_settings
        with self.database.connect() as connection:
            market_row = connection.execute(
                """
                SELECT s.observed_at, i.rank_type, i.rank, i.trade_amount,
                       i.collected_at
                FROM market_rank_items i
                JOIN market_rank_snapshots s ON s.id = i.snapshot_id
                WHERE i.stock_code = ?
                  AND i.rank_type = 'trade_amount'
                ORDER BY i.collected_at DESC, i.rowid DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
            realtime_row = connection.execute(
                """
                SELECT query_type, big_data_rank, observed_at, collected_at
                FROM realtime_item_rankings
                WHERE stock_code = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()

        market = self._rank_proxy_row(
            row=dict(market_row) if market_row is not None else None,
            rank_key="rank",
            now=current,
            max_age_minutes=settings.data_gap_market_rank_max_age_minutes,
            rank_limit=settings.data_gap_trade_amount_rank_limit,
        )
        realtime = self._rank_proxy_row(
            row=dict(realtime_row) if realtime_row is not None else None,
            rank_key="big_data_rank",
            now=current,
            max_age_minutes=settings.data_gap_market_rank_max_age_minutes,
            rank_limit=settings.data_gap_realtime_rank_limit,
        )
        usable = bool(market.get("usable") or realtime.get("usable"))
        source = None
        if market.get("usable"):
            source = "0187_trade_amount"
        elif realtime.get("usable"):
            source = "0198_realtime_rank"
        return {
            "usable": usable,
            "source": source,
            "trade_amount": market,
            "realtime": realtime,
            "max_age_minutes": settings.data_gap_market_rank_max_age_minutes,
            "trade_amount_rank_limit": settings.data_gap_trade_amount_rank_limit,
            "realtime_rank_limit": settings.data_gap_realtime_rank_limit,
        }

    @staticmethod
    def _rank_proxy_row(
        *,
        row: dict[str, Any] | None,
        rank_key: str,
        now: datetime,
        max_age_minutes: int,
        rank_limit: int,
    ) -> dict[str, Any]:
        if row is None:
            return {
                "available": False,
                "fresh": False,
                "usable": False,
                "rank": None,
                "age_minutes": None,
            }
        observed = BreakoutStrategy._local_datetime(
            row.get("observed_at") or row.get("collected_at")
        )
        reference_now = (
            now.astimezone(SEOUL_TIMEZONE).replace(tzinfo=None)
            if now.tzinfo is not None
            else now
        )
        age_minutes = None
        fresh = False
        if observed is not None:
            age_minutes = max(0.0, (reference_now - observed).total_seconds() / 60)
            fresh = age_minutes <= max_age_minutes
        rank_value = row.get(rank_key)
        try:
            rank = int(rank_value) if rank_value is not None else None
        except (TypeError, ValueError):
            rank = None
        usable = bool(fresh and rank is not None and rank <= rank_limit)
        return {
            "available": True,
            "fresh": fresh,
            "usable": usable,
            "rank": rank,
            "age_minutes": age_minutes,
            "observed_at": row.get("observed_at"),
            "collected_at": row.get("collected_at"),
        }

    @staticmethod
    def _local_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(SEOUL_TIMEZONE)
        return parsed.replace(tzinfo=None)

    @staticmethod
    def _proxy_leader_score(
        *,
        stock_code: str,
        market_rank_proxy: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "stock_code": stock_code,
            "role": "leader",
            "total_score": 70.0,
            "blocked": False,
            "source": "market_rank_proxy",
            "proxy": True,
            "market_rank_proxy": market_rank_proxy,
        }

    def _entry_grade(
        self,
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan,
        leader_score: dict[str, Any] | None,
        decision_score: Decimal,
        market_rank_proxy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chart = signal.chart
        chart_score = chart.total_score if chart is not None else Decimal("0")
        daily_setup = chart.daily_setup_name if chart is not None else ""
        entry_type = trade_plan.entry_type
        leader_role = str((leader_score or {}).get("role") or "")
        leader_total = _decimal_or_none((leader_score or {}).get("total_score"))
        leader_blocked = bool((leader_score or {}).get("blocked"))
        leader_ok = (
            leader_role in {"leader", "second"}
            and not leader_blocked
            and (leader_total is None or leader_total >= Decimal("65"))
        )
        market_rank_proxy = market_rank_proxy or {}
        trade_amount_top = bool(
            ((market_rank_proxy.get("trade_amount") or {}).get("usable"))
        )
        market_rank_top = trade_amount_top or bool(market_rank_proxy.get("usable"))
        leader_or_rank_ok = leader_ok or market_rank_top
        strong_entry_types = {
            "chart_breakout_hold",
            "opening_range_breakout",
            "vwap_pullback",
            "volume_breakout",
            "breakout_probe",
        }
        weak_reentry_types = {
            "chart_order_block_reentry",
            "chart_vwap_reclaim",
        }
        strong_daily_setups = {
            "daily_new_high",
            "daily_box_breakout",
            "daily_resistance_breakout",
            "daily_amount_trend_turn",
            "daily_cup_handle",
            "daily_ascending_triangle",
        }
        daily_breakout_ok = (
            daily_setup in strong_daily_setups
            or (
                signal.previous_high is not None
                and signal.current_price > int(signal.previous_high)
            )
        )
        strong_vwap_pullback = (
            entry_type == "vwap_pullback"
            and signal.vwap_pass
            and signal.pullback_pass
            and decision_score >= Decimal("78")
        )
        good_chart = (
            chart is not None
            and not chart.blocked
            and chart_score >= Decimal("75")
            and (
                entry_type in {"chart_breakout_hold", "opening_range_breakout"}
                or strong_vwap_pullback
            )
            and daily_breakout_ok
        )
        exceptional_weak_reentry = (
            entry_type in weak_reentry_types
            and chart is not None
            and not chart.blocked
            and chart_score >= Decimal("85")
            and daily_setup in strong_daily_setups
            and leader_or_rank_ok
        )
        pattern_b_grade = (
            entry_type == "chart_pattern_entry"
            and chart is not None
            and not chart.blocked
            and chart_score >= Decimal("75")
            and decision_score >= Decimal("70")
            and (leader_or_rank_ok or signal.supply is not None)
        )
        if (
            leader_or_rank_ok
            and (
                good_chart
                or exceptional_weak_reentry
                or entry_type == "opening_range_breakout"
            )
            and entry_type not in {"chart_pattern_entry"}
        ):
            grade = "A"
            reason = (
                "exceptional_weak_reentry"
                if exceptional_weak_reentry
                else "leader_or_rank_with_strong_chart"
            )
        elif (
            (leader_or_rank_ok and chart_score >= Decimal("65"))
            or pattern_b_grade
            or (
                entry_type in strong_entry_types
                and decision_score >= Decimal("70")
                and entry_type not in weak_reentry_types
            )
        ):
            grade = "B"
            reason = "conditional_leader_or_good_chart"
        else:
            grade = "C"
            reason = "weak_or_unconfirmed_entry"
        return {
            "grade": grade,
            "reason": reason,
            "leader_role": leader_role or None,
            "leader_total_score": str(leader_total) if leader_total is not None else None,
            "leader_blocked": leader_blocked,
            "market_rank_top": market_rank_top,
            "trade_amount_top": trade_amount_top,
            "chart_score": str(chart_score),
            "daily_setup": daily_setup or None,
            "daily_breakout_ok": daily_breakout_ok,
            "entry_type": entry_type,
            "decision_score": str(decision_score),
        }

    @staticmethod
    def _add_buy_policy(entry_grade: str) -> str:
        if entry_grade == "A":
            return "allowed"
        if entry_grade == "B":
            return "conditional"
        return "blocked"

    @staticmethod
    def _stop_policy(entry_grade: str, signal: BreakoutSignal) -> str:
        if entry_grade == "A":
            return "shakeout_watch"
        if entry_grade == "B":
            return "chart_invalidation" if signal.chart is not None else "fixed_percent"
        return "fixed_percent"

    @staticmethod
    def _order_grade_counts(rows: list[Any]) -> dict[str, int]:
        counts = {"A+": 0, "A": 0, "B": 0, "C": 0, "unknown": 0}
        for row in rows:
            setup = {}
            raw = row["setup_json"] if "setup_json" in row.keys() else None
            if raw:
                try:
                    setup = json.loads(raw)
                except Exception:
                    setup = {}
            grade = (
                setup.get("entry_grade")
                or (setup.get("entry_guard_policy") or {}).get("entry_grade")
                or "unknown"
            )
            if grade == "A+":
                counts["A+"] += 1
                counts["A"] += 1
            elif grade in {"A", "B", "C"}:
                counts[str(grade)] += 1
            else:
                counts["unknown"] += 1
                counts["C"] += 1
        return counts

    def _reentry_policy(
        self,
        *,
        signal: BreakoutSignal,
        leader_score: dict[str, Any] | None,
        now: datetime,
        trade_plan: TradePlan | None = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        timestamp = now.replace(tzinfo=None).isoformat(timespec="seconds")
        lock = self.database.pending_reentry_lock(
            signal.stock_code,
            as_of=timestamp,
        )
        if lock is None:
            policy = {
                "status": "allowed",
                "reason": "no pending reentry setup",
                "setup_version": REENTRY_ENGINE_VERSION,
            }
            policy["reentry_engine_policy"] = self._reentry_engine_policy(
                reentry_policy=policy,
                entry_type_policy={},
                same_level_failure_policy={},
                entry_guard_status="allowed",
            )
            return False, "", policy
        reason = str(lock.get("reason") or "")
        full_exit_lock = reason.startswith("exit_reentry_block:")
        hard_block = any(
            marker in reason
            for marker in (
                "vwap_actual_exit",
                "supply_exit",
                "stop_loss",
                "breakeven",
            )
        ) or full_exit_lock
        near_close = now.time().replace(tzinfo=None) >= REENTRY_CUTOFF
        leader_role = str((leader_score or {}).get("role") or "")
        leader_total = _decimal_or_none((leader_score or {}).get("total_score"))
        leader_maintained = (
            leader_role in {"leader", "second"}
            and (leader_total is None or leader_total >= Decimal("65"))
            and not bool((leader_score or {}).get("blocked"))
        )
        supply_total = _decimal_or_none((signal.supply or {}).get("total_score"))
        supply_maintained = (
            bool(signal.supply)
            and not bool((signal.supply or {}).get("blocked"))
            and (supply_total is None or supply_total >= Decimal("45"))
        )
        planned_profit_lock = reason.startswith("partial_take_profit")
        reassessable_hard_block = any(
            marker in reason
            for marker in (
                "vwap_actual_exit",
                "stop_loss",
                "breakeven",
            )
        ) or full_exit_lock
        reassessment = self._reentry_reassessment(
            signal=signal,
            leader_maintained=leader_maintained,
        )
        minimum_wait_elapsed = True
        if reassessable_hard_block:
            created_at = lock.get("created_at")
            try:
                created = datetime.fromisoformat(str(created_at))
                minimum_wait_elapsed = (
                    now.replace(tzinfo=None) - created.replace(tzinfo=None)
                ) >= timedelta(minutes=self.risk_settings.stop_loss_cooldown_minutes)
            except (TypeError, ValueError):
                minimum_wait_elapsed = False
        recovery_history_ok = (
            "vwap_actual_exit" not in reason
            or bool(reassessment["prior_vwap_failure_observed"])
        )
        exit_context = self._latest_reentry_exit_context(
            signal.stock_code,
            lock=lock,
            as_of=timestamp,
        )
        previous_exit_profit_protect = (
            bool(exit_context.get("profit_protect"))
            if planned_profit_lock
            else bool(exit_context.get("profit_protect"))
        )
        previous_exit_reassessment_allowed = bool(
            str(exit_context.get("exit_reason_class") or "")
            not in {
                "news_or_supply_stop",
                "market_break_stop",
                "system_or_fill_error",
            }
        )
        quality = self._reentry_quality_score(
            signal=signal,
            trade_plan=trade_plan,
            reassessment=reassessment,
            exit_context=exit_context,
            leader_maintained=leader_maintained,
            supply_maintained=supply_maintained,
        )
        reward_risk_policy = self._reentry_reward_risk_policy(
            signal=signal,
            trade_plan=trade_plan,
        )
        pre_reward_conditions = (
            (planned_profit_lock or reassessable_hard_block)
            and (not hard_block or reassessable_hard_block)
            and not near_close
            and minimum_wait_elapsed
            and recovery_history_ok
            and previous_exit_reassessment_allowed
            and bool(quality["passed"])
            and supply_maintained
            and reassessment["valid_rebreakout"]
        )
        allowed = pre_reward_conditions and bool(reward_risk_policy["passed"])
        quantity_multiplier = (
            str(self.strategy_settings.entry_type_minimum_probe_quantity_multiplier)
            if allowed and planned_profit_lock
            else "1"
        )
        policy = {
            "status": (
                "allowed_after_partial_exit_reassessment"
                if allowed and planned_profit_lock
                else (
                    "allowed_after_strong_reassessment"
                    if allowed
                    else (
                        "blocked_reentry_reward_risk"
                        if pre_reward_conditions
                        and not bool(reward_risk_policy["passed"])
                        else "blocked"
                    )
                )
            ),
            "lock_reason": reason,
            "lock_id": int(lock["id"]),
            "lock_setup_version": lock.get("setup_version"),
            "lock_setup": lock.get("setup") or {},
            "setup_version": REENTRY_ENGINE_VERSION,
            "lock_until": lock.get("lock_until"),
            "leader_maintained": leader_maintained,
            "supply_maintained": supply_maintained,
            "previous_exit_was_planned_take_profit": planned_profit_lock,
            "previous_exit_was_profit_protect": previous_exit_profit_protect,
            "previous_exit_reassessment_allowed": (
                previous_exit_reassessment_allowed
            ),
            "previous_exit": exit_context,
            "reassessable_hard_block": reassessable_hard_block,
            "hard_block": hard_block,
            "near_close": near_close,
            "minimum_wait_elapsed": minimum_wait_elapsed,
            "recovery_history_ok": recovery_history_ok,
            "reentry_quality_score": quality["score"],
            "reentry_quality_threshold": quality["threshold"],
            "reentry_quality_passed": quality["passed"],
            "reentry_quality_grade": quality["grade"],
            "reentry_quality_components": quality["components"],
            "reentry_reward_risk_ratio": quality["reward_risk_ratio"],
            "reentry_reward_risk_passed": reward_risk_policy["passed"],
            "reentry_reward_risk_policy": reward_risk_policy,
            "minimum_reentry_probe": allowed and planned_profit_lock,
            "quantity_multiplier": quantity_multiplier,
            **reassessment,
        }
        policy["reentry_engine_policy"] = self._reentry_engine_policy(
            reentry_policy=policy,
            entry_type_policy={},
            same_level_failure_policy={},
            entry_guard_status=str(policy["status"]),
        )
        if allowed:
            return False, "", policy
        message = (
            "재진입 정책 차단: "
            f"{'마감 전 신규 제한 구간' if near_close else reason}"
        )
        return True, message, policy

    def _latest_reentry_exit_context(
        self,
        stock_code: str,
        *,
        lock: dict[str, Any],
        as_of: str,
    ) -> dict[str, Any]:
        created_at = str(lock.get("created_at") or "")
        source_decision_id = lock.get("source_decision_id")
        filters = [
            "e.stock_code = ?",
            "e.status = 'executed'",
            "e.occurred_at <= ?",
        ]
        parameters: list[Any] = [stock_code, as_of]
        if created_at:
            filters.append("e.occurred_at >= ?")
            parameters.append(created_at)
        if source_decision_id is not None:
            filters.append(
                """
                (
                  e.order_id IN (
                    SELECT p.order_id
                    FROM partial_exit_fills p
                    JOIN trade_plans tp ON tp.id = p.trade_plan_id
                    WHERE tp.decision_id = ?
                  )
                  OR e.order_id IN (
                    SELECT pf.order_id
                    FROM paper_fills pf
                    JOIN paper_orders po ON po.id = pf.order_id
                    JOIN order_requests req
                      ON req.client_order_id = po.client_order_id
                    WHERE req.decision_id = ?
                  )
                )
                """
            )
            parameters.extend([source_decision_id, source_decision_id])
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT e.event_type, e.exit_class, e.exit_reason_class,
                       e.setup_invalidated, e.order_id, e.occurred_at,
                       e.classification_json,
                       COALESCE(SUM(f.realized_pnl), 0) AS realized_pnl,
                       COUNT(f.id) AS fill_count
                FROM paper_risk_events e
                LEFT JOIN paper_fills f
                  ON f.order_id = e.order_id
                 AND f.side = 'sell'
                WHERE {" AND ".join(filters)}
                GROUP BY e.id
                ORDER BY e.occurred_at DESC, e.id DESC
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
        if row is None:
            return {
                "event_type": None,
                "exit_class": "unknown",
                "exit_reason_class": "unknown",
                "setup_invalidated": False,
                "profit_protect": False,
                "realized_pnl": None,
                "matched_sell": False,
                "setup_version": None,
                "reentry_setup": {},
            }
        realized_pnl = int(row["realized_pnl"] or 0)
        event_type = str(row["event_type"] or "")
        exit_class = str(
            row["exit_class"] or self._risk_exit_class(event_type, realized_pnl)
        )
        setup_invalidated = bool(row["setup_invalidated"])
        classification = self._json_object(row["classification_json"])
        reentry_setup = classification.get("reentry_setup") or {}
        return {
            "event_type": event_type,
            "exit_class": exit_class,
            "exit_reason_class": row["exit_reason_class"] or exit_class,
            "setup_invalidated": setup_invalidated,
            "profit_protect": exit_class
            in {
                "partial_take_profit",
                "profit_protect_stop",
                "trailing_profit_lock",
            }
            and not setup_invalidated,
            "realized_pnl": realized_pnl,
            "order_id": row["order_id"],
            "occurred_at": row["occurred_at"],
            "matched_sell": int(row["fill_count"] or 0) > 0,
            "setup_version": reentry_setup.get("version"),
            "reentry_setup": reentry_setup,
        }

    @staticmethod
    def _risk_exit_class(event_type: str, realized_pnl: int) -> str:
        if event_type == "partial_take_profit":
            return "partial_take_profit" if realized_pnl >= 0 else "partial_loss_exit"
        if event_type == "trailing_stop":
            return "trailing_profit_lock" if realized_pnl >= 0 else "trailing_loss_stop"
        if event_type == "stop_loss":
            return "profit_protect_stop" if realized_pnl >= 0 else "loss_stop"
        return event_type or "unknown"

    @staticmethod
    def _reentry_engine_policy(
        *,
        reentry_policy: dict[str, Any],
        entry_type_policy: dict[str, Any],
        same_level_failure_policy: dict[str, Any],
        entry_guard_status: str,
    ) -> dict[str, Any]:
        reentry_status = str(reentry_policy.get("status") or "not_checked")
        entry_type_status = str(entry_type_policy.get("status") or "not_checked")
        same_level_status = str(
            same_level_failure_policy.get("status") or "not_checked"
        )
        reward_risk_policy = reentry_policy.get("reentry_reward_risk_policy") or {}
        b_grade_policy = entry_type_policy.get("b_grade_reentry_limit") or {}
        active_lock = bool(reentry_policy.get("lock_reason"))
        quality_checked = "reentry_quality_passed" in reentry_policy
        quality_passed = (
            bool(reentry_policy.get("reentry_quality_passed"))
            if quality_checked
            else None
        )
        reward_risk_checked = "reentry_reward_risk_passed" in reentry_policy
        reward_risk_passed = (
            bool(reentry_policy.get("reentry_reward_risk_passed"))
            if reward_risk_checked
            else None
        )
        same_level_checked = bool(same_level_failure_policy)
        same_level_passed = not bool(same_level_failure_policy.get("blocked"))
        b_grade_checked = bool(b_grade_policy)
        b_grade_blocked = bool(entry_type_policy.get("blocked")) and (
            entry_type_status == "blocked_b_grade_midday_high_chase_reentry"
        )
        b_grade_minimum_probe = bool(entry_type_policy.get("minimum_probe")) and (
            entry_type_status == "minimum_probe_b_grade_reentry"
        )
        new_setup_detected = bool(
            reentry_policy.get("valid_rebreakout")
            and reentry_policy.get("new_high_breakout")
            and reentry_policy.get("vwap_reanchored")
            and reentry_policy.get("volume_recovered")
            and reentry_policy.get("supply_maintained")
        )
        strict_setup_passed = bool(
            new_setup_detected
            and reward_risk_checked
            and reward_risk_passed
        )
        allowed_reentry_statuses = {
            "allowed_after_partial_exit_reassessment",
            "allowed_after_strong_reassessment",
        }

        status = "allowed_non_reentry"
        order_effect = "follow_entry_guard"
        if same_level_checked and not same_level_passed:
            status = "blocked_same_failure_level"
            order_effect = "block"
        elif b_grade_blocked:
            status = "blocked_b_grade_reentry"
            order_effect = "block"
        elif reward_risk_checked and not reward_risk_passed:
            status = "blocked_reward_risk"
            order_effect = "block"
        elif quality_checked and not quality_passed:
            status = "blocked_reentry_quality"
            order_effect = "block"
        elif active_lock and not strict_setup_passed:
            status = "blocked_incomplete_new_setup"
            order_effect = "block"
        elif reentry_status in allowed_reentry_statuses and strict_setup_passed:
            status = "allowed_new_setup"
            order_effect = (
                "allow_minimum_probe"
                if reentry_policy.get("minimum_reentry_probe")
                else "allow"
            )
        elif active_lock:
            status = "blocked_waiting_new_setup"
            order_effect = "block"
        elif b_grade_minimum_probe:
            status = "minimum_probe_b_grade_reentry"
            order_effect = "allow_minimum_probe"
        elif reentry_status == "allowed":
            status = "allowed_no_active_lock"

        return {
            "version": REENTRY_ENGINE_VERSION,
            "status": status,
            "order_effect": order_effect,
            "decision_basis": "new_setup_same_level_reward_risk",
            "entry_guard_status": entry_guard_status,
            "source_statuses": {
                "reentry": reentry_status,
                "entry_type": entry_type_status,
                "same_level_failure": same_level_status,
            },
            "active_reentry_lock": active_lock,
            "new_setup_detected": new_setup_detected,
            "strict_setup_passed": strict_setup_passed,
            "approved_reentry_lock_id": (
                int(reentry_policy["lock_id"])
                if status == "allowed_new_setup" and reentry_policy.get("lock_id")
                else None
            ),
            "time_gate_role": (
                "supporting_only"
                if "minimum_wait_elapsed" in reentry_policy
                else "not_used"
            ),
            "criteria": {
                "exit_taxonomy": {
                    "checked": bool(reentry_policy.get("previous_exit")),
                    "passed": bool(
                        reentry_policy.get("previous_exit_was_profit_protect")
                    )
                    or not active_lock,
                    "previous_exit": reentry_policy.get("previous_exit"),
                    "setup_invalidated": bool(
                        (reentry_policy.get("previous_exit") or {}).get(
                            "setup_invalidated"
                        )
                    ),
                },
                "quality": {
                    "checked": quality_checked,
                    "passed": quality_passed,
                    "score": reentry_policy.get("reentry_quality_score"),
                    "threshold": reentry_policy.get("reentry_quality_threshold"),
                    "grade": reentry_policy.get("reentry_quality_grade"),
                    "components": reentry_policy.get(
                        "reentry_quality_components",
                        [],
                    ),
                },
                "b_grade_repeat_limit": {
                    "checked": b_grade_checked,
                    "passed": not b_grade_blocked,
                    "minimum_probe": b_grade_minimum_probe,
                    "policy": b_grade_policy,
                },
                "same_level_failure": {
                    "checked": same_level_checked,
                    "passed": same_level_passed,
                    "failure_count": same_level_failure_policy.get(
                        "failure_count",
                    ),
                    "current_level_key": same_level_failure_policy.get(
                        "current_level_key",
                    ),
                    "unlock_reason": same_level_failure_policy.get(
                        "unlock_reason",
                    ),
                    "policy": same_level_failure_policy,
                },
                "reward_risk": {
                    "checked": reward_risk_checked,
                    "passed": reward_risk_passed,
                    "ratio": reentry_policy.get("reentry_reward_risk_ratio"),
                    "policy": reward_risk_policy,
                },
                "new_setup": {
                    "passed": new_setup_detected,
                    "valid_rebreakout": bool(
                        reentry_policy.get("valid_rebreakout")
                    ),
                    "new_high_breakout": bool(
                        reentry_policy.get("new_high_breakout")
                    ),
                    "vwap_reanchored": bool(
                        reentry_policy.get("vwap_reanchored")
                    ),
                    "volume_recovered": bool(
                        reentry_policy.get("volume_recovered")
                    ),
                    "leader_maintained": bool(
                        reentry_policy.get("leader_maintained")
                    ),
                    "supply_maintained": bool(
                        reentry_policy.get("supply_maintained")
                    ),
                    "all_required_conditions_passed": strict_setup_passed,
                },
                "time_gate": {
                    "role": (
                        "supporting_only"
                        if "minimum_wait_elapsed" in reentry_policy
                        else "not_used"
                    ),
                    "minimum_wait_elapsed": reentry_policy.get(
                        "minimum_wait_elapsed"
                    ),
                    "near_close": reentry_policy.get("near_close"),
                    "recovery_history_ok": reentry_policy.get(
                        "recovery_history_ok"
                    ),
                },
            },
        }

    @staticmethod
    def _apply_reentry_quantity_policy(
        entry_guard_policy: dict[str, Any],
        reentry_policy: dict[str, Any],
    ) -> None:
        if not reentry_policy.get("minimum_reentry_probe"):
            return
        current_multiplier = (
            _decimal_or_none(entry_guard_policy.get("quantity_multiplier"))
            or Decimal("1")
        )
        reentry_multiplier = (
            _decimal_or_none(reentry_policy.get("quantity_multiplier"))
            or Decimal("1")
        )
        entry_guard_policy["quantity_multiplier"] = str(
            min(current_multiplier, reentry_multiplier)
        )
        entry_guard_policy["reentry_quantity_policy"] = {
            "minimum_probe": True,
            "source": "partial_take_profit_reentry_block",
            "quantity_multiplier": str(reentry_multiplier),
        }

    def _reentry_quality_score(
        self,
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan | None,
        reassessment: dict[str, Any],
        exit_context: dict[str, Any],
        leader_maintained: bool,
        supply_maintained: bool,
    ) -> dict[str, Any]:
        components: list[dict[str, Any]] = []

        def add(name: str, points: int, passed: bool, reason: str) -> None:
            components.append(
                {
                    "name": name,
                    "points": int(points),
                    "passed": bool(passed),
                    "reason": reason,
                }
            )

        if bool(reassessment.get("vwap_reanchored")):
            add("vwap_recovery", 20, True, "two_confirmed_closes_above_vwap")
        else:
            add("vwap_recovery", -20, False, "vwap_reanchor_not_confirmed")

        if bool(reassessment.get("new_high_breakout")):
            add("new_high_rebreakout", 20, True, "price_and_close_above_prior_high")
        else:
            add("new_high_rebreakout", -20, False, "new_high_rebreakout_missing")

        if bool(reassessment.get("volume_recovered")):
            add(
                "volume_recovery",
                15,
                True,
                f"ratio={reassessment.get('volume_recovery_ratio')}",
            )
        else:
            add("volume_recovery", -15, False, "volume_recovery_below_1.30x")

        supply = signal.supply or {}
        if supply_maintained:
            add("supply", 15, True, "supply_score_maintained")
        elif supply.get("blocked"):
            add("supply", -20, False, "supply_blocked")
        elif supply:
            add("supply", -8, False, "supply_score_weak")
        else:
            add("supply", 0, False, "supply_missing")

        grade = self._reentry_quality_grade(
            signal=signal,
            trade_plan=trade_plan,
            leader_maintained=leader_maintained,
            supply_maintained=supply_maintained,
        )
        grade_points = {"A+": 15, "A": 12, "B": 6, "C": -15}.get(grade, 0)
        add("entry_grade", grade_points, grade in {"A+", "A", "B"}, grade)

        exit_reason_class = str(exit_context.get("exit_reason_class") or "")
        exit_class = str(exit_context.get("exit_class") or "")
        setup_invalidated = bool(exit_context.get("setup_invalidated"))
        previous_exit_points = 0
        previous_exit_ok = False
        if setup_invalidated or exit_reason_class in {
            "structure_loss_stop",
            "news_or_supply_stop",
            "market_break_stop",
            "system_or_fill_error",
        }:
            previous_exit_points = -35
            previous_exit_ok = False
        elif exit_class in {
            "partial_take_profit",
            "profit_protect_stop",
            "trailing_profit_lock",
        }:
            previous_exit_points = 10
            previous_exit_ok = True
        elif exit_reason_class == "noise_stop":
            previous_exit_points = 4
            previous_exit_ok = True
        elif exit_class in {"loss_stop", "trailing_loss_stop", "partial_loss_exit"}:
            previous_exit_points = -25
            previous_exit_ok = False
        add(
            "previous_exit",
            previous_exit_points,
            previous_exit_ok,
            exit_reason_class or exit_class or "unknown",
        )

        reward_risk_ratio = self._reentry_reward_risk_ratio(
            signal=signal,
            trade_plan=trade_plan,
        )
        if reward_risk_ratio is None:
            add("reward_risk", 0, False, "unknown")
        elif reward_risk_ratio >= Decimal("2"):
            add("reward_risk", 10, True, f"rr={reward_risk_ratio}")
        elif reward_risk_ratio >= Decimal("1.5"):
            add("reward_risk", 6, True, f"rr={reward_risk_ratio}")
        elif reward_risk_ratio >= Decimal("1.2"):
            add("reward_risk", 3, True, f"rr={reward_risk_ratio}")
        elif reward_risk_ratio >= Decimal("1"):
            add("reward_risk", 0, True, f"rr={reward_risk_ratio}")
        else:
            add("reward_risk", -15, False, f"rr={reward_risk_ratio}")

        raw_score = sum(int(item["points"]) for item in components)
        score = max(0, min(100, raw_score))
        threshold = {"A+": 70, "A": 72, "B": 82, "C": 101}.get(grade, 82)
        return {
            "score": score,
            "threshold": threshold,
            "passed": score >= threshold,
            "grade": grade,
            "reward_risk_ratio": (
                str(reward_risk_ratio) if reward_risk_ratio is not None else None
            ),
            "components": components,
        }

    @staticmethod
    def _reentry_quality_grade(
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan | None,
        leader_maintained: bool,
        supply_maintained: bool,
    ) -> str:
        setup = trade_plan.setup if trade_plan is not None else {}
        guard = setup.get("entry_guard_policy") or {}
        grade = setup.get("entry_grade") or guard.get("entry_grade")
        if grade in {"A+", "A", "B", "C"}:
            return str(grade)
        if leader_maintained and signal.opening_range_pass:
            return "A+"
        if (
            leader_maintained
            and signal.previous_high is not None
            and signal.current_price > int(signal.previous_high)
            and signal.vwap_pass
        ):
            return "A"
        if leader_maintained or supply_maintained:
            return "B"
        return "C"

    @staticmethod
    def _reentry_reward_risk_ratio(
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan | None,
    ) -> Decimal | None:
        if trade_plan is not None:
            ratio = _decimal_or_none(trade_plan.reward_risk_ratio)
            if ratio is not None:
                return ratio
            risk = signal.current_price - int(trade_plan.stop_loss_price)
            reward = int(trade_plan.first_take_profit_price) - signal.current_price
            if risk > 0:
                return Decimal(max(0, reward)) / Decimal(risk)
        return None

    @staticmethod
    def _reentry_reward_risk_policy(
        *,
        signal: BreakoutSignal,
        trade_plan: TradePlan | None,
    ) -> dict[str, Any]:
        minimum_ratio = Decimal("1")
        policy: dict[str, Any] = {
            "status": "unknown",
            "passed": False,
            "minimum_ratio": str(minimum_ratio),
            "entry_price": int(signal.current_price),
            "stop_loss_price": None,
            "first_take_profit_price": None,
            "risk_per_share": None,
            "expected_upside": None,
            "ratio": None,
            "message": "",
        }
        if trade_plan is None:
            policy["message"] = "trade plan missing"
            return policy

        entry_price = int(signal.current_price)
        stop_loss_price = int(trade_plan.stop_loss_price)
        first_take_profit_price = int(trade_plan.first_take_profit_price)
        risk_per_share = entry_price - stop_loss_price
        expected_upside = first_take_profit_price - entry_price
        policy.update(
            {
                "stop_loss_price": stop_loss_price,
                "first_take_profit_price": first_take_profit_price,
                "risk_per_share": risk_per_share,
                "expected_upside": expected_upside,
            }
        )
        if risk_per_share <= 0:
            policy.update(
                {
                    "status": "blocked_invalid_reentry_stop",
                    "message": "stop loss must be below reentry price",
                }
            )
            return policy
        if expected_upside <= 0:
            policy.update(
                {
                    "status": "blocked_no_expected_reentry_upside",
                    "message": "expected upside must be above reentry price",
                }
            )
            return policy

        ratio = Decimal(expected_upside) / Decimal(risk_per_share)
        policy["ratio"] = str(ratio)
        if ratio < minimum_ratio:
            policy.update(
                {
                    "status": "blocked_reentry_reward_risk",
                    "message": "expected upside is smaller than stop distance",
                }
            )
            return policy
        policy.update(
            {
                "status": "passed",
                "passed": True,
                "message": "reward risk is acceptable",
            }
        )
        return policy

    def _reentry_reassessment(
        self,
        *,
        signal: BreakoutSignal,
        leader_maintained: bool,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT close_price, volume, candle_time
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'minute'
                  AND interval_minutes = ?
                  AND close_price IS NOT NULL
                ORDER BY candle_time DESC, id DESC
                LIMIT 6
                """,
                (
                    signal.stock_code,
                    self.strategy_settings.minute_interval,
                ),
            ).fetchall()
        closes = [Decimal(row["close_price"]) for row in rows]
        volumes = [
            Decimal(row["volume"])
            for row in rows
            if row["volume"] not in (None, 0)
        ]
        vwap = signal.vwap
        two_closes_above_vwap = (
            vwap is not None
            and len(closes) >= 2
            and closes[0] >= vwap
            and closes[1] >= vwap
        )
        prior_vwap_failure_observed = (
            vwap is not None
            and any(close < vwap for close in closes[2:])
        )
        latest_close = closes[0] if closes else None
        new_high_rebreakout = (
            signal.previous_high is not None
            and signal.current_price > int(signal.previous_high)
            and latest_close is not None
            and latest_close > Decimal(signal.previous_high)
        )
        volume_recovered = False
        volume_recovery_ratio: Decimal | None = None
        if len(volumes) >= 4:
            prior_average = sum(volumes[1:4]) / Decimal("3")
            if prior_average > 0:
                volume_recovery_ratio = volumes[0] / prior_average
                volume_recovered = volume_recovery_ratio >= Decimal("1.30")
        valid_rebreakout = (
            two_closes_above_vwap
            and new_high_rebreakout
            and volume_recovered
            and leader_maintained
        )
        return {
            "valid_rebreakout": valid_rebreakout,
            "vwap_reanchored": two_closes_above_vwap,
            "prior_vwap_failure_observed": prior_vwap_failure_observed,
            "new_high_breakout": new_high_rebreakout,
            "volume_recovered": volume_recovered,
            "volume_recovery_ratio": (
                str(volume_recovery_ratio)
                if volume_recovery_ratio is not None
                else None
            ),
            "latest_confirmed_close": (
                int(latest_close) if latest_close is not None else None
            ),
            "reassessment_reason": (
                "신고가 재돌파·VWAP 2개 봉 재안착·거래량 회복·대장주 유지"
                if valid_rebreakout
                else "단순 반등 또는 재돌파 확인 부족"
            ),
        }

    @staticmethod
    def _opening_range_component(signal: BreakoutSignal) -> ScoreComponent:
        if signal.opening_range_pass:
            score = Decimal("100")
            reason = (
                "장초반 ORB 상단 돌파 "
                f"{signal.opening_range_breakout_rate:.2%}"
                if signal.opening_range_breakout_rate is not None
                else "장초반 ORB 상단 돌파"
            )
        elif signal.opening_range_high is None:
            score = Decimal("0")
            reason = "장초반 ORB 범위 데이터 부족"
        elif signal.opening_range_breakout_rate is None:
            score = Decimal("25")
            reason = "장초반 ORB 상단 미돌파"
        else:
            score = Decimal("50")
            reason = (
                f"장초반 ORB 돌파폭 {signal.opening_range_breakout_rate:.2%}, "
                "확인 조건 부족"
            )
        if signal.opening_range_high is not None:
            reason += (
                f" · range={signal.opening_range_low:,}"
                f"~{signal.opening_range_high:,}"
            )
        return ScoreComponent(
            name="opening_range",
            score=score,
            weight=Decimal("0"),
            reason=reason,
        )

    @staticmethod
    def _pullback_component(signal: BreakoutSignal) -> ScoreComponent:
        if signal.pullback_pass:
            score = Decimal("100")
            depth = (
                f"{signal.pullback_depth_rate:.2%}"
                if signal.pullback_depth_rate is not None
                else "-"
            )
            support = (
                f"{signal.pullback_support_level:.2f}"
                if signal.pullback_support_level is not None
                else "-"
            )
            reason = f"VWAP·이평·중심선 지지 눌림목, 고점 대비 {depth}, 지지선={support}"
        elif signal.pullback_depth_rate is None:
            score = Decimal("0")
            reason = "눌림목 구간 아님"
        elif signal.deep_pullback_blocked:
            score = Decimal("0")
            reason = f"눌림 깊이 {signal.pullback_depth_rate:.2%}, 최대 허용 초과"
        elif not signal.pullback_support_pass:
            score = Decimal("25")
            reason = f"눌림 깊이 {signal.pullback_depth_rate:.2%}, 지지 회복 부족"
        else:
            score = Decimal("25")
            reason = f"눌림 깊이 {signal.pullback_depth_rate:.2%}, 지지 조건 부족"
        return ScoreComponent(
            name="pullback",
            score=score,
            weight=Decimal("0.08"),
            reason=reason,
        )

    @staticmethod
    def _order_flow_component(signal: BreakoutSignal) -> ScoreComponent:
        score = _order_flow_signal_score(signal)
        spread = (
            f"{signal.order_book_spread_rate:.2%}"
            if signal.order_book_spread_rate is not None
            else "-"
        )
        bid_ask = (
            f"{signal.order_book_bid_ask_ratio:.2f}"
            if signal.order_book_bid_ask_ratio is not None
            else "-"
        )
        strength = (
            f"{signal.trade_strength:.1f}" if signal.trade_strength is not None else "-"
        )
        change = []
        if signal.ask_quantity_change_rate is not None:
            change.append(f"매도잔량 {signal.ask_quantity_change_rate:.1%}")
        if signal.bid_quantity_change_rate is not None:
            change.append(f"매수잔량 {signal.bid_quantity_change_rate:.1%}")
        flags = []
        if signal.ask_wall_absorption:
            flags.append("매도벽 흡수")
        if signal.bid_wall_risk:
            flags.append("매수벽 위험")
        details = " · ".join((*change, *flags, *signal.order_flow_reasons))
        reason = (
            f"호가/체결 수급: 스프레드={spread}, "
            f"매수/매도잔량비={bid_ask}, 체결강도={strength}"
            f"{' · ' + details if details else ''}"
        )
        return ScoreComponent(
            name="order_flow",
            score=score,
            weight=Decimal("0.07"),
            reason=reason,
        )

    @staticmethod
    def _pattern_component(signal: BreakoutSignal) -> ScoreComponent:
        score = _pattern_signal_score(signal.pattern_score, signal.patterns)
        reason = (
            f"패턴 가점 {signal.pattern_score:+.2f}: "
            + ", ".join(signal.patterns[:3])
            if signal.patterns
            else "최근 후보 패턴 없음"
        )
        return ScoreComponent(
            name="pattern",
            score=score,
            weight=Decimal("0.06"),
            reason=reason,
        )

    @staticmethod
    def _information_component(signal: BreakoutSignal) -> ScoreComponent:
        score = _information_signal_score(
            signal.information_score,
            signal.information_blocked,
        )
        if signal.information_blocked:
            reason = "뉴스·공시 위험: data confidence 감점"
        elif signal.information_score > 0:
            reason = "뉴스·공시 긍정 가점 미반영"
        elif signal.information_score:
            reason = f"뉴스·공시 점수 {signal.information_score:+.2f}"
        else:
            reason = "뉴스·공시 중립"
        return ScoreComponent(
            name="information",
            score=score,
            weight=Decimal("0.02"),
            reason=reason,
        )

    @staticmethod
    def _chart_component(signal: BreakoutSignal) -> ScoreComponent:
        if signal.chart is None:
            return ScoreComponent(
                name="chart_trade",
                score=Decimal("0"),
                weight=Decimal("0"),
                reason="차트 기반 매매 판단 없음",
            )
        if signal.chart.blocked:
            score = Decimal("-100")
            reason = f"차트 위험 차단: {signal.chart.block_reason}"
        else:
            score = signal.chart.total_score
            if signal.chart.daily_setup_name == "daily_unconfirmed":
                score = min(score, Decimal("35"))
            elif (
                not signal.chart.force_entry
                and signal.chart.intraday_trigger_name == "intraday_unconfirmed"
            ):
                score = min(score, Decimal("45"))
            reason = (
                f"{signal.chart.daily_setup_name} / "
                f"{signal.chart.intraday_trigger_name} "
                f"{signal.chart.total_score:.1f}점"
            )
            if signal.chart.force_entry:
                reason = "강제 진입 " + reason
        return ScoreComponent(
            name="chart_trade",
            score=_clamp_score(score),
            weight=Decimal("0.18"),
            reason=reason,
        )

    @staticmethod
    def _supply_component(signal: BreakoutSignal) -> ScoreComponent:
        if not signal.supply:
            return ScoreComponent(
                name="supply_flow",
                score=Decimal("50"),
                weight=Decimal("0"),
                reason="수급 점수 없음",
            )
        total = _float_value(signal.supply.get("total_score"), 50.0)
        broker = _float_value(signal.supply.get("broker_score"), 0.0)
        foreign = _float_value(signal.supply.get("foreign_broker_score"), 0.0)
        investor = _float_value(signal.supply.get("investor_score"), 0.0)
        signed_score = max(-100.0, min(100.0, (total - 50.0) * 2.0))
        score = Decimal(str(signed_score))
        if signal.supply.get("blocked"):
            score = Decimal("-100")
            reason = (
                "수급 위험: data confidence 감점 - "
                f"{signal.supply.get('blocked_reason') or '-'}"
            )
        else:
            reason = (
                f"수급 {total:.1f}점 "
                f"(거래원 {broker:.1f}, 외국계 {foreign:.1f}, "
                f"투자자 {investor:.1f})"
            )
        return ScoreComponent(
            name="supply_flow",
            score=_clamp_score(score),
            weight=Decimal("0.04"),
            reason=reason,
        )

    def _paper_probe_decision(
        self,
        signal: BreakoutSignal,
        base: StrategyDecision,
    ) -> StrategyDecision:
        return StrategyDecision(
            strategy_name=base.strategy_name,
            stock_code=base.stock_code,
            decided_at=base.decided_at,
            action=DecisionAction.BUY,
            score=base.score,
            confidence=Decimal("0.70"),
            reason=(
                "모의 검증 진입: 돌파·눌림목·호가 수급 중 보조 조건이 맞고 "
                f"전략 점수 {base.score:.1f}점으로 주문·체결 흐름 검증"
            ),
            components=base.components,
        )

    def _build_trade_plan(
        self,
        *,
        signal: BreakoutSignal,
        decision: StrategyDecision,
        decision_id: int,
        planned_quantity: int,
        paper_probe: bool,
        session_profile: SessionProfile,
        strategy_mode: IntradayStrategyMode,
    ) -> TradePlan:
        entry_type, tags = self._entry_type(signal, paper_probe)
        stop_loss_price, stop_loss_reason = self._initial_stop(signal, entry_type)
        if stop_loss_price >= signal.current_price:
            stop_loss_price, stop_loss_reason = self._fallback_stop(signal.current_price)
        structural_stop_policy = self._structural_stop_policy(
            signal=signal,
            stop_loss_price=stop_loss_price,
            stop_loss_reason=stop_loss_reason,
        )
        risk_per_share = max(1, signal.current_price - stop_loss_price)
        first_take_profit_price, first_take_profit_reason = (
            self._first_take_profit(signal, entry_type, risk_per_share)
        )
        partial_exit_rate, partial_exit_strategy, residual_exit_rule, trailing_rule = (
            self._partial_exit_policy(signal, entry_type)
        )
        opening_leader_mode = self._opening_leader_mode_policy(strategy_mode)
        reward = max(0, first_take_profit_price - signal.current_price)
        reward_risk_ratio = Decimal(reward) / Decimal(risk_per_share)
        return TradePlan(
            decision_id=decision_id,
            stock_code=signal.stock_code,
            entry_type=entry_type,
            entry_tags=tags,
            entry_price=signal.current_price,
            planned_quantity=planned_quantity,
            stop_loss_price=stop_loss_price,
            stop_loss_reason=stop_loss_reason,
            first_take_profit_price=first_take_profit_price,
            first_take_profit_reason=first_take_profit_reason,
            partial_exit_rate=partial_exit_rate,
            breakeven_price=signal.current_price,
            breakeven_trigger_price=first_take_profit_price,
            trailing_rule=(
                "1차 익절 후 잔여 수량은 본절 이동, 이후 VWAP 또는 "
                "5분봉20MA 이탈과 동적 트레일링으로 최종 청산"
            ),
            invalidation_reason=stop_loss_reason,
            reward_risk_ratio=reward_risk_ratio,
            setup={
                "strategy_score": str(decision.score),
                "session_profile": session_profile.name,
                "session_profile_label": session_profile.label,
                "session_quantity_multiplier": str(
                    strategy_mode.quantity_multiplier
                ),
                "session_minimum_score": str(strategy_mode.minimum_score),
                "intraday_strategy_mode": strategy_mode.as_dict(),
                "strategy_mode": strategy_mode.name,
                "strategy_mode_label": strategy_mode.label,
                "strategy_mode_reason": strategy_mode.reason,
                "leader_mode_pass": strategy_mode.leader_pass,
                "supply_mode_pass": strategy_mode.supply_pass,
                "market_mode_pass": strategy_mode.market_mode_pass,
                "market_mode_gate_enabled": self.market_mode_settings.enabled,
                "market_mode": self.market_mode_settings.manual_mode,
                "market_mode_source": self.market_mode_settings.source,
                "market_mode_requested": self.market_mode_settings.requested_mode,
                "market_mode_reason": self.market_mode_settings.reason,
                "market_mode_order_effect": self.market_mode_settings.order_effect,
                "market_mode_quantity_multiplier": str(
                    self.market_mode_settings.quantity_multiplier
                ),
                "market_mode_allowed_entry_grades": (
                    self.market_mode_settings.allowed_entry_grades
                ),
                "market_mode_shadow": self.market_mode_settings.shadow_mode,
                "opening_leader_mode": opening_leader_mode,
                "paper_probe": paper_probe,
                "passed": signal.passed,
                "volume_pass": signal.volume_pass,
                "liquidity_pass": signal.liquidity_pass,
                "breakout_pass": signal.breakout_pass,
                "breakout_hold_pass": signal.breakout_hold_pass,
                "pullback_pass": signal.pullback_pass,
                "pullback_support_pass": signal.pullback_support_pass,
                "deep_pullback_blocked": signal.deep_pullback_blocked,
                "opening_range_pass": signal.opening_range_pass,
                "opening_range_high": signal.opening_range_high,
                "opening_range_low": signal.opening_range_low,
                "opening_range_volume": signal.opening_range_volume,
                "opening_range_start": signal.opening_range_start,
                "opening_range_end": signal.opening_range_end,
                "opening_range_breakout_rate": (
                    str(signal.opening_range_breakout_rate)
                    if signal.opening_range_breakout_rate is not None
                    else None
                ),
                "order_flow_pass": signal.order_flow_pass,
                "order_flow_score": (
                    str(signal.order_flow_score)
                    if signal.order_flow_score is not None
                    else None
                ),
                "ask_wall_absorption": signal.ask_wall_absorption,
                "bid_wall_risk": signal.bid_wall_risk,
                "ask_quantity_change_rate": (
                    str(signal.ask_quantity_change_rate)
                    if signal.ask_quantity_change_rate is not None
                    else None
                ),
                "bid_quantity_change_rate": (
                    str(signal.bid_quantity_change_rate)
                    if signal.bid_quantity_change_rate is not None
                    else None
                ),
                "order_flow_reasons": list(signal.order_flow_reasons),
                "vwap": str(signal.vwap) if signal.vwap is not None else None,
                "moving_average": (
                    str(signal.moving_average)
                    if signal.moving_average is not None
                    else None
                ),
                "intraday_midline": (
                    str(signal.intraday_midline)
                    if signal.intraday_midline is not None
                    else None
                ),
                "intraday_high": signal.intraday_high,
                "breakout_level": signal.breakout_level,
                "recent_swing_low": signal.recent_swing_low,
                "higher_low_pass": signal.higher_low_pass,
                "minute_atr": (
                    str(signal.minute_atr) if signal.minute_atr is not None else None
                ),
                "recent_candle_range": (
                    str(signal.recent_candle_range)
                    if signal.recent_candle_range is not None
                    else None
                ),
                "structural_stop_policy": structural_stop_policy,
                "entry_candle_quality": signal.entry_candle_quality or {},
                "pullback_support_level": (
                    str(signal.pullback_support_level)
                    if signal.pullback_support_level is not None
                    else None
                ),
                "previous_high": signal.previous_high,
                "projected_trade_amount": signal.projected_trade_amount,
                "day_gain_rate": (
                    str(signal.day_gain_rate)
                    if signal.day_gain_rate is not None
                    else None
                ),
                "chart_trade": (
                    signal.chart.as_dict() if signal.chart is not None else None
                ),
                "chart_daily_setup": (
                    signal.chart.daily_setup_name if signal.chart is not None else None
                ),
                "chart_intraday_trigger": (
                    signal.chart.intraday_trigger_name
                    if signal.chart is not None
                    else None
                ),
                "chart_risk_blocked": (
                    signal.chart.blocked if signal.chart is not None else False
                ),
                "chart_block_reason": (
                    signal.chart.block_reason if signal.chart is not None else None
                ),
                "supply_flow": signal.supply,
                "supply_score": (
                    signal.supply.get("total_score") if signal.supply else None
                ),
                "supply_blocked": (
                    bool(signal.supply.get("blocked")) if signal.supply else False
                ),
                "supply_block_reason": (
                    signal.supply.get("blocked_reason") if signal.supply else None
                ),
                "partial_exit_strategy": partial_exit_strategy,
                "residual_exit_rule": residual_exit_rule,
                "partial_trailing_rule": trailing_rule,
                "first_exit_rate": str(partial_exit_rate),
                "invalidation_price": stop_loss_price,
                "second_take_profit_price": (
                    signal.chart.second_take_profit_price
                    if signal.chart is not None
                    else None
                ),
            },
        )

    @staticmethod
    def _opening_leader_mode_policy(
        strategy_mode: IntradayStrategyMode,
    ) -> dict[str, Any]:
        active = strategy_mode.name == "opening_leader_core"
        return {
            "active": active,
            "mode": strategy_mode.name,
            "window": "09:00~09:20",
            "entry_focus": "leader_selection",
            "profit_plan": (
                "fast_partial_take_profit"
                if active
                else "standard_partial_take_profit"
            ),
            "fast_partial_profit_rate": "0.03" if active else None,
            "fast_partial_min_r": "0.35" if active else None,
            "residual_protection": (
                "breakeven_then_vwap_or_5m20ma_break"
                if active
                else "trade_plan_default"
            ),
            "loss_control": (
                "account_10pct_kill_switch_and_structure_break"
                if active
                else "standard_risk_policy"
            ),
            "new_entry_limit_control": False,
        }

    def _entry_type(
        self,
        signal: BreakoutSignal,
        paper_probe: bool,
    ) -> tuple[str, tuple[str, ...]]:
        if signal.chart is not None and signal.chart.force_entry:
            return signal.chart.entry_type, signal.chart.entry_tags
        tags = []
        if paper_probe:
            tags.append("paper_probe")
        if signal.pullback_pass:
            tags.append("vwap_pullback")
        if signal.pullback_support_pass:
            tags.append("support_recovery")
        if signal.breakout_pass:
            tags.append("breakout")
        if signal.breakout_hold_pass:
            tags.append("breakout_hold")
        if signal.opening_range_pass:
            tags.append("opening_range_breakout")
        if signal.order_flow_pass:
            tags.append("order_flow")
        if signal.vwap_pass:
            tags.append("vwap_support")
        if signal.moving_average_pass:
            tags.append("moving_average_support")
        if (
            signal.intraday_midline is not None
            and Decimal(signal.current_price) > signal.intraday_midline
        ):
            tags.append("midline_support")

        if signal.opening_range_pass:
            entry_type = "opening_range_breakout"
        elif signal.pullback_pass:
            entry_type = "vwap_pullback"
        elif signal.passed and signal.breakout_pass:
            entry_type = "volume_breakout"
        elif signal.breakout_pass:
            entry_type = "breakout_probe"
        elif signal.order_flow_pass:
            entry_type = "order_flow_momentum"
        else:
            entry_type = "paper_probe"
        return entry_type, tuple(dict.fromkeys(tags or [entry_type]))

    def _initial_stop(
        self,
        signal: BreakoutSignal,
        entry_type: str,
    ) -> tuple[int, str]:
        if (
            signal.chart is not None
            and entry_type.startswith("chart_")
            and signal.chart.invalidation_price is not None
            and signal.chart.invalidation_price < signal.current_price
        ):
            return signal.chart.invalidation_price, "차트 무효화 가격 이탈"
        if entry_type == "vwap_pullback":
            if (
                signal.recent_swing_low is not None
                and signal.recent_swing_low < signal.current_price
            ):
                return (
                    self._buffered_structural_stop(signal.recent_swing_low, signal),
                    "최근 5분 higher-low·눌림 저점 이탈",
                )
            supports = [
                value
                for value in (
                    signal.vwap,
                    signal.moving_average,
                    signal.intraday_midline,
                )
                if value is not None and value < Decimal(signal.current_price)
            ]
            if supports:
                return (
                    self._buffered_structural_stop(min(supports), signal),
                    "VWAP·5분봉20MA·중심선 지지 구간 이탈",
                )
        if entry_type in {"volume_breakout", "breakout_probe"}:
            if (
                signal.recent_swing_low is not None
                and signal.recent_swing_low < signal.current_price
            ):
                return (
                    self._buffered_structural_stop(signal.recent_swing_low, signal),
                    "돌파 전 최근 5분 스윙 저점 이탈",
                )
            breakout_levels = [
                level
                for level in (signal.previous_high, signal.intraday_high)
                if level is not None and level < signal.current_price
            ]
            if breakout_levels:
                level = max(breakout_levels)
                return (
                    self._buffered_structural_stop(level, signal),
                    "돌파 기준선 하단 구조 이탈",
                )
        if entry_type == "opening_range_breakout":
            if (
                signal.opening_range_low is not None
                and signal.opening_range_low < signal.current_price
            ):
                return (
                    self._buffered_structural_stop(signal.opening_range_low, signal),
                    "장초반 ORB 하단 구조 이탈",
                )
        if entry_type == "order_flow_momentum":
            if (
                signal.recent_swing_low is not None
                and signal.recent_swing_low < signal.current_price
            ):
                return (
                    self._buffered_structural_stop(signal.recent_swing_low, signal),
                    "호가 수급 진입 후 최근 5분 스윙 저점 이탈",
                )
            supports = [
                value
                for value in (signal.vwap, signal.moving_average)
                if value is not None and value < Decimal(signal.current_price)
            ]
            if supports:
                return (
                    self._buffered_structural_stop(max(supports), signal),
                    "호가 수급 진입 후 VWAP·이평 지지 이탈",
                )
        return self._fallback_stop(signal.current_price)

    def _buffered_structural_stop(
        self,
        level: int | Decimal,
        signal: BreakoutSignal,
    ) -> int:
        raw_level = Decimal(level)
        atr_buffer = (
            signal.minute_atr * self.strategy_settings.structural_stop_buffer_atr_rate
            if signal.minute_atr is not None
            else Decimal("0")
        )
        spread_buffer = (
            Decimal(signal.current_price) * signal.order_book_spread_rate
            if signal.order_book_spread_rate is not None
            else Decimal("0")
        )
        buffer = max(Decimal("1"), atr_buffer, spread_buffer)
        return max(
            1,
            int((raw_level - buffer).to_integral_value(rounding=ROUND_FLOOR)),
        )

    def _structural_stop_policy(
        self,
        *,
        signal: BreakoutSignal,
        stop_loss_price: int,
        stop_loss_reason: str,
    ) -> dict[str, Any]:
        settings = self.strategy_settings
        distance = max(0, signal.current_price - stop_loss_price)
        atr_noise = (
            signal.minute_atr * settings.structural_stop_noise_atr_rate
            if signal.minute_atr is not None
            else Decimal("0")
        )
        range_noise = (
            signal.recent_candle_range * settings.structural_stop_noise_range_rate
            if signal.recent_candle_range is not None
            else Decimal("0")
        )
        spread_noise = (
            Decimal(signal.current_price) * signal.order_book_spread_rate
            if signal.order_book_spread_rate is not None
            else Decimal("0")
        )
        noise_floor = max(Decimal("1"), atr_noise, range_noise, spread_noise)
        has_structure = "최후 계획 손절" not in stop_loss_reason
        valid = bool(
            not settings.structural_initial_stop_enabled
            or has_structure
            and stop_loss_price < signal.current_price
            and Decimal(distance) >= noise_floor
        )
        if not settings.structural_initial_stop_enabled:
            status = "disabled"
        elif not has_structure:
            status = "invalid_missing_structure"
        elif Decimal(distance) < noise_floor:
            status = "invalid_inside_normal_noise"
        else:
            status = "valid_structural_stop"
        return {
            "version": "v0.12.94",
            "enabled": settings.structural_initial_stop_enabled,
            "valid": valid,
            "status": status,
            "stop_loss_price": stop_loss_price,
            "stop_loss_reason": stop_loss_reason,
            "risk_per_share": distance,
            "risk_rate": str(
                Decimal(distance) / Decimal(signal.current_price)
                if signal.current_price > 0
                else Decimal("0")
            ),
            "normal_noise_floor": str(noise_floor),
            "minute_atr": str(signal.minute_atr) if signal.minute_atr is not None else None,
            "recent_candle_range": (
                str(signal.recent_candle_range)
                if signal.recent_candle_range is not None
                else None
            ),
            "spread_rate": (
                str(signal.order_book_spread_rate)
                if signal.order_book_spread_rate is not None
                else None
            ),
        }

    def _fallback_stop(self, current_price: int) -> tuple[int, str]:
        price = Decimal(current_price) * (
            Decimal("1") - self.strategy_settings.plan_fallback_stop_rate
        )
        return (
            max(1, int(price.to_integral_value(rounding=ROUND_FLOOR))),
            "차트 기준 부족 시 최후 계획 손절",
        )

    @staticmethod
    def _first_take_profit(
        signal: BreakoutSignal,
        entry_type: str,
        risk_per_share: int,
    ) -> tuple[int, str]:
        if (
            signal.chart is not None
            and entry_type.startswith("chart_")
            and signal.chart.first_take_profit_price is not None
            and signal.chart.first_take_profit_price > signal.current_price
        ):
            return signal.chart.first_take_profit_price, "차트 계획 1차 익절"
        if (
            entry_type == "vwap_pullback"
            and signal.intraday_high is not None
            and signal.intraday_high > signal.current_price
        ):
            return signal.intraday_high, "직전 장중 고점에서 1차 반익절"
        return (
            signal.current_price + risk_per_share,
            "1R 도달 시 1차 반익절",
        )

    @staticmethod
    def _partial_exit_policy(
        signal: BreakoutSignal,
        entry_type: str,
    ) -> tuple[Decimal, str, str, str]:
        chart = signal.chart
        daily_setup = chart.daily_setup_name if chart is not None else ""
        trigger = chart.intraday_trigger_name if chart is not None else ""
        chart_score = chart.total_score if chart is not None else Decimal("0")
        strong_leader_trend = (
            chart_score >= Decimal("85")
            and daily_setup
            in {
                "daily_new_high",
                "daily_box_breakout",
                "daily_resistance_breakout",
                "daily_amount_trend_turn",
            }
            and trigger
            in {
                "intraday_breakout_hold",
                "intraday_amount_reentry",
                "intraday_order_flow",
                "intraday_trade_strength",
            }
        )
        if strong_leader_trend:
            return (
                Decimal("0.30"),
                "strong_leader_trend",
                "leader_trailing",
                "30% first take profit, 70% trailing with VWAP/5m20MA trend protection",
            )
        if entry_type == "chart_cup_handle" or daily_setup == "daily_cup_handle":
            return (
                Decimal("0.40"),
                "cup_handle_target_hold",
                "hold_to_second_target",
                "40% first take profit, residual held toward second target",
            )
        if entry_type == "chart_order_block_reentry" or "order_block" in trigger:
            return (
                Decimal("0.50"),
                "order_block_quick_partial",
                "prior_high_or_trailing",
                "50% quick first take profit, residual toward prior high with tight invalidation",
            )
        if entry_type in {
            "chart_long_bullish",
            "chart_breakout_hold",
            "volume_breakout",
            "opening_range_breakout",
            "breakout_probe",
        } or trigger in {
            "intraday_long_bullish",
            "intraday_breakout_hold",
            "intraday_previous_high_break",
        }:
            return (
                Decimal("0.50"),
                "long_bullish_breakout",
                "vwap_or_5m20ma_break",
                "50% first take profit, residual protected at breakeven then VWAP/5m20MA break",
            )
        return (
            Decimal("0.50"),
            "standard_partial_trailing",
            "breakeven_then_trailing",
            "50% first take profit, residual protected at breakeven then trailing",
        )

    def _account_and_position(self, account_name: str, stock_code: str) -> dict:
        summary = self.paper_engine.account_summary(account_name)
        with self.database.connect() as connection:
            account = connection.execute(
                "SELECT id FROM paper_accounts WHERE name = ?",
                (account_name,),
            ).fetchone()
            position = connection.execute(
                """
                SELECT p.quantity, p.cost_basis, ps.current_price
                FROM paper_positions p
                JOIN paper_accounts a ON a.id = p.account_id
                LEFT JOIN price_snapshots ps ON ps.id = (
                    SELECT id
                    FROM price_snapshots
                    WHERE stock_code = p.stock_code
                    ORDER BY collected_at DESC, id DESC
                    LIMIT 1
                )
                WHERE a.name = ? AND p.stock_code = ?
                """,
                (account_name, stock_code),
            ).fetchone()
        position_value = 0
        if position is not None:
            position_value = (
                int(position["quantity"]) * int(position["current_price"])
                if position["current_price"] is not None
                else int(position["cost_basis"])
            )
        return {
            "account_id": account["id"],
            "has_position": position is not None,
            "total_equity": summary["total_equity"],
            "cash_balance": summary["cash_balance"],
            "position_value": position_value,
        }

    def _order_flow_context(
        self,
        *,
        current_price: int,
        order_books: object,
        trade_strength_rows: object,
    ) -> dict:
        order_book_rows = list(order_books)
        trade_rows = list(trade_strength_rows)
        order_book = order_book_rows[0] if order_book_rows else None
        trade_strength_row = trade_rows[0] if trade_rows else None
        spread_rate = None
        bid_ask_ratio = None
        latest_ask_quantity = None
        latest_bid_quantity = None
        if order_book is not None:
            best_ask = _optional_int(order_book["best_ask_price"])
            best_bid = _optional_int(order_book["best_bid_price"])
            if best_ask is not None and best_bid is not None and current_price > 0:
                spread = max(0, best_ask - best_bid)
                spread_rate = Decimal(spread) / Decimal(current_price)

            ask_quantity = _optional_int(order_book["total_ask_quantity"])
            bid_quantity = _optional_int(order_book["total_bid_quantity"])
            if ask_quantity in (None, 0) or bid_quantity is None:
                ask_quantity = _optional_int(order_book["best_ask_quantity"])
                bid_quantity = _optional_int(order_book["best_bid_quantity"])
            latest_ask_quantity = ask_quantity
            latest_bid_quantity = bid_quantity
            if ask_quantity not in (None, 0) and bid_quantity is not None:
                bid_ask_ratio = Decimal(bid_quantity) / Decimal(ask_quantity)

        trade_strength = None
        if trade_strength_row is not None and trade_strength_row["strength"] is not None:
            trade_strength = Decimal(str(trade_strength_row["strength"]))

        ask_quantity_change_rate = self._quantity_change_rate(
            order_book_rows,
            "total_ask_quantity",
            "best_ask_quantity",
        )
        bid_quantity_change_rate = self._quantity_change_rate(
            order_book_rows,
            "total_bid_quantity",
            "best_bid_quantity",
        )
        ask_wall_absorption = (
            ask_quantity_change_rate is not None
            and ask_quantity_change_rate
            <= -self.strategy_settings.order_flow_ask_absorption_drop_rate
            and trade_strength is not None
            and trade_strength >= self.strategy_settings.order_flow_min_strength
        )
        bid_wall_risk = (
            bid_ask_ratio is not None
            and bid_ask_ratio
            >= self.strategy_settings.order_flow_bid_wall_risk_ratio
            and not ask_wall_absorption
            and (
                trade_strength is None
                or trade_strength
                < self.strategy_settings.order_flow_min_strength
            )
        )
        score, reasons = self._score_order_flow(
            spread_rate=spread_rate,
            bid_ask_ratio=bid_ask_ratio,
            trade_strength=trade_strength,
            ask_wall_absorption=ask_wall_absorption,
            bid_wall_risk=bid_wall_risk,
            ask_quantity_change_rate=ask_quantity_change_rate,
            bid_quantity_change_rate=bid_quantity_change_rate,
            latest_ask_quantity=latest_ask_quantity,
            latest_bid_quantity=latest_bid_quantity,
        )

        return {
            "spread_rate": spread_rate,
            "bid_ask_ratio": bid_ask_ratio,
            "trade_strength": trade_strength,
            "score": score,
            "ask_wall_absorption": ask_wall_absorption,
            "bid_wall_risk": bid_wall_risk,
            "ask_quantity_change_rate": ask_quantity_change_rate,
            "bid_quantity_change_rate": bid_quantity_change_rate,
            "reasons": reasons,
        }

    def _order_flow_pass(self, order_flow: dict) -> bool:
        spread_rate = order_flow["spread_rate"]
        bid_ask_ratio = order_flow["bid_ask_ratio"]
        trade_strength = order_flow["trade_strength"]
        score = order_flow["score"]
        if order_flow["bid_wall_risk"]:
            return False
        spread_stable = (
            spread_rate is None
            or spread_rate <= self.strategy_settings.order_flow_max_spread_rate
        )
        balance_strong = (
            bid_ask_ratio is not None
            and bid_ask_ratio >= self.strategy_settings.order_flow_min_bid_ask_ratio
        )
        strength_strong = (
            trade_strength is not None
            and trade_strength >= self.strategy_settings.order_flow_min_strength
        )
        flow_score_strong = score is not None and score >= Decimal("70")
        return spread_stable and (
            balance_strong
            or strength_strong
            or order_flow["ask_wall_absorption"]
            or flow_score_strong
        )

    @staticmethod
    def _quantity_change_rate(
        rows: list[object],
        total_key: str,
        best_key: str,
    ) -> Optional[Decimal]:
        if len(rows) < 2:
            return None
        latest = _optional_int(rows[0][total_key])
        oldest = _optional_int(rows[-1][total_key])
        if latest in (None, 0) or oldest in (None, 0):
            latest = _optional_int(rows[0][best_key])
            oldest = _optional_int(rows[-1][best_key])
        if latest is None or oldest in (None, 0):
            return None
        return Decimal(latest - oldest) / Decimal(oldest)

    def _score_order_flow(
        self,
        *,
        spread_rate: Optional[Decimal],
        bid_ask_ratio: Optional[Decimal],
        trade_strength: Optional[Decimal],
        ask_wall_absorption: bool,
        bid_wall_risk: bool,
        ask_quantity_change_rate: Optional[Decimal],
        bid_quantity_change_rate: Optional[Decimal],
        latest_ask_quantity: Optional[int],
        latest_bid_quantity: Optional[int],
    ) -> tuple[Decimal, list[str]]:
        score = Decimal("50")
        reasons: list[str] = []
        if spread_rate is None:
            reasons.append("스프레드 미수집")
        elif spread_rate <= self.strategy_settings.order_flow_max_spread_rate:
            score += Decimal("10")
            reasons.append("스프레드 안정")
        else:
            score -= Decimal("30")
            reasons.append("스프레드 과다")

        if trade_strength is None:
            reasons.append("체결강도 미수집")
        elif trade_strength >= self.strategy_settings.order_flow_min_strength:
            score += Decimal("20")
            reasons.append(f"체결강도 {trade_strength:.1f}")
        elif trade_strength < Decimal("100"):
            score -= Decimal("10")
            reasons.append(f"체결강도 약함 {trade_strength:.1f}")

        if ask_wall_absorption:
            score += Decimal("20")
            reasons.append(
                "매도잔량 흡수 "
                f"{ask_quantity_change_rate:.1%}"
                if ask_quantity_change_rate is not None
                else "매도잔량 흡수"
            )
        elif (
            ask_quantity_change_rate is not None
            and ask_quantity_change_rate > Decimal("0.20")
        ):
            score -= Decimal("8")
            reasons.append(f"매도잔량 증가 {ask_quantity_change_rate:.1%}")

        if bid_wall_risk:
            score -= Decimal("25")
            reasons.append("매수잔량 과잉 위험")
        elif (
            bid_ask_ratio is not None
            and bid_ask_ratio >= self.strategy_settings.order_flow_min_bid_ask_ratio
            and trade_strength is not None
            and trade_strength >= self.strategy_settings.order_flow_min_strength
        ):
            score += Decimal("20")
            reasons.append("잔량 우위와 체결강도 동반")
        elif (
            bid_quantity_change_rate is not None
            and bid_quantity_change_rate > Decimal("0.20")
        ):
            score += Decimal("5")
            reasons.append(f"매수잔량 증가 {bid_quantity_change_rate:.1%}")

        if (
            latest_ask_quantity is not None
            and latest_bid_quantity is not None
            and latest_ask_quantity > 0
        ):
            reasons.append(
                "호가잔량 "
                f"매수/매도 {Decimal(latest_bid_quantity) / Decimal(latest_ask_quantity):.2f}"
            )
        return _clamp_score(score), reasons

    def _calculate_quantity(
        self,
        total_equity: int,
        cash_balance: int,
        market_price: int,
        score: Decimal,
        current_position_value: int = 0,
        stop_loss_price: Optional[int] = None,
        quantity_multiplier: Decimal = Decimal("1"),
        target_position_rate: Optional[Decimal] = None,
        per_trade_risk_rate: Optional[Decimal] = None,
        minimum_quantity_if_affordable: int = 0,
    ) -> int:
        if target_position_rate is None:
            target_rate = min(
                self._score_position_rate(score),
                self.risk_settings.max_position_rate,
            ) * quantity_multiplier
        else:
            target_rate = min(target_position_rate, Decimal("1"))
        budget = (
            Decimal(total_equity) * target_rate
            - Decimal(current_position_value)
        )
        if budget <= 0:
            return 0
        budget = min(budget, Decimal(cash_balance))
        if budget <= 0:
            return 0
        estimated_price = Decimal(market_price) * (
            Decimal("1") + self.paper_settings.slippage_rate
        )
        cost_per_share = estimated_price * (
            Decimal("1") + self.paper_settings.commission_rate
        )
        quantity_by_budget = int(
            (budget / cost_per_share).to_integral_value(rounding=ROUND_FLOOR)
        )
        if quantity_by_budget < 1:
            return self._minimum_affordable_quantity(
                total_equity=total_equity,
                cash_balance=cash_balance,
                cost_per_share=cost_per_share,
                market_price=market_price,
                stop_loss_price=stop_loss_price,
                minimum_quantity=minimum_quantity_if_affordable,
            )
        if stop_loss_price is None or stop_loss_price >= market_price:
            return quantity_by_budget
        risk_per_share = Decimal(market_price - stop_loss_price)
        if risk_per_share <= 0:
            return quantity_by_budget
        risk_budget = Decimal(total_equity) * (
            per_trade_risk_rate or self.risk_settings.per_trade_risk_rate
        )
        quantity_by_risk = int(
            (risk_budget / risk_per_share).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        quantity = min(quantity_by_budget, quantity_by_risk)
        if quantity >= 1:
            return quantity
        return self._minimum_affordable_quantity(
            total_equity=total_equity,
            cash_balance=cash_balance,
            cost_per_share=cost_per_share,
            market_price=market_price,
            stop_loss_price=stop_loss_price,
            minimum_quantity=minimum_quantity_if_affordable,
        )

    def _minimum_buy_diagnostics(
        self,
        *,
        account: dict[str, Any],
        signal: BreakoutSignal,
        trade_plan: Optional[TradePlan],
    ) -> dict[str, Any]:
        market_price = int(signal.current_price)
        stop_loss_price = (
            int(trade_plan.stop_loss_price)
            if trade_plan is not None and trade_plan.stop_loss_price
            else None
        )
        estimated_price = Decimal(market_price) * (
            Decimal("1") + self.paper_settings.slippage_rate
        )
        cost_per_share = estimated_price * (
            Decimal("1") + self.paper_settings.commission_rate
        )
        cash_balance = int(account.get("cash_balance") or 0)
        total_equity = int(account.get("total_equity") or 0)
        risk_per_share = (
            market_price - stop_loss_price
            if stop_loss_price is not None and stop_loss_price < market_price
            else None
        )
        daily_risk_budget = int(
            (
                Decimal(total_equity) * self.risk_settings.daily_max_loss_rate
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        price_references = self._latest_price_references(signal.stock_code)
        reference_prices = [
            int(item["price"])
            for item in price_references
            if item.get("price") is not None and int(item["price"]) > 0
        ]
        price_ratio = None
        if len(reference_prices) >= 2:
            low = min(reference_prices)
            high = max(reference_prices)
            if low > 0:
                price_ratio = float(Decimal(high) / Decimal(low))

        reason = "quantity_budget_or_risk_limit"
        if cost_per_share > Decimal(cash_balance):
            reason = "one_share_cash_shortfall"
            if (
                len(reference_prices) >= 2
                and price_ratio is not None
                and price_ratio <= 1.05
            ):
                reason = "verified_high_price_unaffordable"
        if price_ratio is not None and price_ratio >= 5:
            reason = "price_scale_mismatch"
        if (
            risk_per_share is not None
            and daily_risk_budget > 0
            and risk_per_share > daily_risk_budget
        ):
            reason = "one_share_stop_risk_exceeds_daily_limit"

        return {
            "version": "v0.12.67",
            "reason": reason,
            "market_price": market_price,
            "cash_balance": cash_balance,
            "total_equity": total_equity,
            "estimated_one_share_cost": int(
                cost_per_share.to_integral_value(rounding=ROUND_FLOOR)
            ),
            "stop_loss_price": stop_loss_price,
            "risk_per_share": risk_per_share,
            "daily_risk_budget": daily_risk_budget,
            "price_reference_count": len(reference_prices),
            "price_reference_ratio": price_ratio,
            "price_references": price_references,
        }

    def _latest_price_references(self, stock_code: str) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            for row in connection.execute(
                """
                SELECT 'price_snapshot' AS source, current_price AS price,
                       collected_at AS observed_at
                FROM price_snapshots
                WHERE stock_code = ? AND current_price IS NOT NULL
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchall():
                references.append(dict(row))
            for row in connection.execute(
                """
                SELECT 'minute_candle' AS source, close_price AS price,
                       candle_time AS observed_at
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'minute'
                  AND close_price IS NOT NULL
                ORDER BY candle_time DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchall():
                references.append(dict(row))
            for row in connection.execute(
                """
                SELECT 'market_rank' AS source, current_price AS price,
                       collected_at AS observed_at
                FROM market_rank_items
                WHERE stock_code = ? AND current_price IS NOT NULL
                ORDER BY collected_at DESC, snapshot_id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchall():
                references.append(dict(row))
        return references

    @staticmethod
    def _minimum_buy_message(diagnostics: dict[str, Any]) -> str:
        reason = diagnostics.get("reason")
        if reason == "verified_high_price_unaffordable":
            return (
                "1-share unavailable: verified high-price stock, not price scale error"
            )
        if reason == "price_scale_mismatch":
            return "1-share unavailable: price scale mismatch requires review"
        if reason == "one_share_stop_risk_exceeds_daily_limit":
            return "1-share unavailable: one-share stop risk exceeds daily limit"
        if reason == "one_share_cash_shortfall":
            return "1-share unavailable: cash is below one-share estimated cost"
        return "1-share unavailable: score/cash/stop-risk sizing returned zero"

    def _minimum_affordable_quantity(
        self,
        *,
        total_equity: int,
        cash_balance: int,
        cost_per_share: Decimal,
        market_price: int,
        stop_loss_price: Optional[int],
        minimum_quantity: int,
    ) -> int:
        if minimum_quantity < 1 or cost_per_share <= 0 or cash_balance <= 0:
            return 0
        affordable_quantity = int(
            (Decimal(cash_balance) / cost_per_share).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        quantity = min(minimum_quantity, affordable_quantity)
        if quantity < 1:
            return 0
        if stop_loss_price is not None and stop_loss_price < market_price:
            risk_per_share = Decimal(market_price - stop_loss_price)
            daily_risk_budget = Decimal(total_equity) * (
                self.risk_settings.daily_max_loss_rate
            )
            if risk_per_share * Decimal(quantity) > daily_risk_budget:
                return 0
        return quantity

    @staticmethod
    def _score_position_rate(score: Decimal) -> Decimal:
        if score >= Decimal("80"):
            return Decimal("0.25")
        if score >= Decimal("70"):
            return Decimal("0.20")
        if score >= Decimal("60"):
            return Decimal("0.15")
        if score >= Decimal("50"):
            return Decimal("0.10")
        return Decimal("0")

    @staticmethod
    def _project_volume(
        *,
        signal_time: str,
        current_volume: Optional[int],
    ) -> Optional[int]:
        if current_volume is None:
            return None
        timestamp = datetime.fromisoformat(signal_time)
        session_start = timestamp.replace(hour=9, minute=0, second=0, microsecond=0)
        session_end = timestamp.replace(hour=15, minute=30, second=0, microsecond=0)
        elapsed = (timestamp - session_start).total_seconds()
        total = (session_end - session_start).total_seconds()
        if elapsed <= 0 or total <= 0:
            return None
        elapsed_rate = min(Decimal("1"), Decimal(str(elapsed / total)))
        if elapsed_rate <= 0:
            return None
        projected = Decimal(current_volume) / elapsed_rate
        return int(projected.to_integral_value(rounding=ROUND_FLOOR))

    @staticmethod
    def _active_session_pass(signal_time: str) -> bool:
        timestamp = datetime.fromisoformat(signal_time)
        current = timestamp.time().replace(tzinfo=None)
        return (
            clock_time(9, 0) <= current <= clock_time(11, 0)
            or clock_time(14, 0) <= current <= clock_time(15, 0)
        )

    def _opening_range_context(
        self,
        *,
        signal_time: str,
        current_price: int,
        minute_rows: object,
        volume_pass: bool,
        liquidity_pass: bool,
        vwap_pass: bool,
        chase_pass: bool,
    ) -> dict:
        timestamp = datetime.fromisoformat(signal_time)
        session_start = timestamp.replace(hour=9, minute=0, second=0, microsecond=0)
        range_end = session_start + timedelta(
            minutes=self.strategy_settings.opening_range_minutes
        )
        entry_end = session_start + timedelta(
            minutes=self.strategy_settings.opening_range_entry_end_minutes
        )
        rows = []
        for row in minute_rows:
            try:
                candle_time = datetime.fromisoformat(row["candle_time"])
            except (TypeError, ValueError):
                continue
            if session_start <= candle_time < range_end:
                rows.append(row)
        highs = [
            int(row["high_price"])
            for row in rows
            if row["high_price"] is not None
        ]
        lows = [
            int(row["low_price"])
            for row in rows
            if row["low_price"] is not None
        ]
        volumes = [
            int(row["volume"])
            for row in rows
            if row["volume"] is not None
        ]
        high = max(highs) if highs else None
        low = min(lows) if lows else None
        volume = sum(volumes) if volumes else None
        breakout_rate = (
            Decimal(current_price) / Decimal(high) - Decimal("1")
            if high not in (None, 0) and current_price > high
            else None
        )
        required_breakout = (
            self.strategy_settings.opening_range_breakout_buffer_rate
        )
        passed = (
            high is not None
            and low is not None
            and timestamp >= range_end
            and timestamp <= entry_end
            and breakout_rate is not None
            and breakout_rate >= required_breakout
            and volume_pass
            and liquidity_pass
            and vwap_pass
            and chase_pass
        )
        return {
            "high": high,
            "low": low,
            "volume": volume,
            "start": session_start.isoformat(timespec="seconds"),
            "end": range_end.isoformat(timespec="seconds"),
            "breakout_rate": breakout_rate,
            "passed": passed,
        }

    @staticmethod
    def _calculate_vwap(rows: object) -> Optional[Decimal]:
        cumulative_value = Decimal("0")
        cumulative_volume = Decimal("0")
        for row in rows:
            volume = row["volume"]
            high = row["high_price"]
            low = row["low_price"]
            close = row["close_price"]
            if volume in (None, 0) or high is None or low is None or close is None:
                continue
            typical_price = (
                Decimal(high) + Decimal(low) + Decimal(close)
            ) / Decimal("3")
            cumulative_value += typical_price * Decimal(volume)
            cumulative_volume += Decimal(volume)
        if cumulative_volume <= 0:
            return None
        return cumulative_value / cumulative_volume

    @staticmethod
    def _calculate_intraday_midline(
        rows: object,
        lookback: int,
    ) -> Optional[Decimal]:
        recent = list(rows)[:lookback]
        highs = [
            Decimal(row["high_price"])
            for row in recent
            if row["high_price"] is not None
        ]
        lows = [
            Decimal(row["low_price"])
            for row in recent
            if row["low_price"] is not None
        ]
        if not highs or not lows:
            return None
        return (max(highs) + min(lows)) / Decimal("2")

    @staticmethod
    def _breakout_level(
        *,
        current_price: int,
        previous_high: Optional[int],
        intraday_high: Optional[int],
    ) -> Optional[int]:
        levels = [
            level
            for level in (previous_high, intraday_high)
            if level is not None and current_price > level
        ]
        return max(levels) if levels else None

    def _breakout_hold_pass(
        self,
        *,
        current_price: int,
        breakout_level: Optional[int],
        latest_minute: object | None,
    ) -> bool:
        if breakout_level is None:
            return False
        level = Decimal(breakout_level)
        buffer = self.strategy_settings.intraday_breakout_hold_buffer_rate
        if Decimal(current_price) >= level * (Decimal("1") + buffer):
            return True
        if latest_minute is None or latest_minute["close_price"] is None:
            return False
        latest_close = Decimal(latest_minute["close_price"])
        return latest_close >= level

    @staticmethod
    def _entry_candle_quality_context(
        *,
        current: object,
        minute_rows: object,
        breakout_level: Optional[int],
        vwap: Optional[Decimal],
        volume_pass: bool,
    ) -> dict[str, Any]:
        rows = list(minute_rows)
        current_price = _decimal_or_none(current["current_price"])
        if current_price is None:
            return {
                "version": "v0.12.85",
                "evaluated": False,
                "status": "not_evaluated_no_price",
                "blocked": False,
                "score": "0",
                "threshold": "60",
                "components": [],
                "block_reasons": [],
            }
        current_high = _decimal_or_none(current["high_price"])
        current_low = _decimal_or_none(current["low_price"])
        current_open = _decimal_or_none(current["open_price"])
        latest = rows[0] if rows else None
        if current_high is None and latest is not None:
            current_high = _decimal_or_none(latest["high_price"])
        if current_low is None and latest is not None:
            current_low = _decimal_or_none(latest["low_price"])
        if current_open is None and latest is not None:
            current_open = _decimal_or_none(latest["open_price"])
        current_high = max(current_high or current_price, current_price)
        current_low = min(current_low or current_price, current_price)
        candle_range = max(current_high - current_low, Decimal("1"))
        close_position = (current_price - current_low) / candle_range
        upper_wick_rate = (current_high - current_price) / candle_range
        upper_wick_price_rate = (current_high - current_price) / max(
            current_price,
            Decimal("1"),
        )

        def _range_rate(row: Any) -> Decimal | None:
            high = _decimal_or_none(row["high_price"])
            low = _decimal_or_none(row["low_price"])
            close = _decimal_or_none(row["close_price"])
            if high is None or low is None or close in (None, Decimal("0")):
                return None
            return (high - low) / max(close, Decimal("1"))

        recent_ranges = [
            value
            for value in (_range_rate(row) for row in rows[1:4])
            if value is not None
        ]
        baseline_ranges = [
            value
            for value in (_range_rate(row) for row in rows[4:10])
            if value is not None
        ]
        recent_range_avg = (
            sum(recent_ranges) / Decimal(len(recent_ranges))
            if recent_ranges
            else None
        )
        baseline_range_avg = (
            sum(baseline_ranges) / Decimal(len(baseline_ranges))
            if baseline_ranges
            else None
        )
        volatility_contraction = False
        if recent_range_avg is not None and baseline_range_avg is not None:
            volatility_contraction = (
                recent_range_avg <= baseline_range_avg * Decimal("0.85")
            )
        elif recent_range_avg is not None:
            volatility_contraction = recent_range_avg <= Decimal("0.012")

        latest_volume = (
            int(latest["volume"])
            if latest is not None and latest["volume"] is not None
            else None
        )
        previous_volumes = [
            int(row["volume"])
            for row in rows[1:7]
            if row["volume"] is not None and int(row["volume"]) > 0
        ]
        previous_volume_avg = (
            Decimal(sum(previous_volumes)) / Decimal(len(previous_volumes))
            if previous_volumes
            else None
        )
        breakout_volume_expansion = bool(volume_pass)
        if (
            latest_volume is not None
            and previous_volume_avg is not None
            and Decimal(latest_volume) >= previous_volume_avg * Decimal("1.5")
        ):
            breakout_volume_expansion = True

        latest_close = (
            _decimal_or_none(latest["close_price"]) if latest is not None else None
        )
        vwap_hold_after_breakout = bool(
            vwap is not None
            and current_price >= vwap
            and (
                latest_close is None
                or latest_close >= vwap * Decimal("0.998")
            )
        )
        close_near_high = close_position >= Decimal("0.70")
        upper_wick_controlled = (
            upper_wick_rate <= Decimal("0.35")
            and upper_wick_price_rate <= Decimal("0.03")
        )
        components = [
            {
                "name": "close_near_high",
                "passed": close_near_high,
                "points": 25 if close_near_high else 0,
                "value": f"{close_position:.4f}",
            },
            {
                "name": "upper_wick_controlled",
                "passed": upper_wick_controlled,
                "points": 20 if upper_wick_controlled else 0,
                "value": f"{upper_wick_rate:.4f}",
            },
            {
                "name": "pre_breakout_volatility_contraction",
                "passed": volatility_contraction,
                "points": 15 if volatility_contraction else 0,
                "value": (
                    f"{recent_range_avg:.4f}"
                    if recent_range_avg is not None
                    else None
                ),
            },
            {
                "name": "breakout_volume_expansion",
                "passed": breakout_volume_expansion,
                "points": 25 if breakout_volume_expansion else 0,
                "value": str(latest_volume) if latest_volume is not None else None,
            },
            {
                "name": "vwap_hold_after_breakout",
                "passed": vwap_hold_after_breakout,
                "points": 15 if vwap_hold_after_breakout else 0,
                "value": str(vwap) if vwap is not None else None,
            },
        ]
        score = sum(int(item["points"]) for item in components)
        block_reasons = []
        if not close_near_high:
            block_reasons.append("breakout_close_not_near_high")
        if not upper_wick_controlled:
            block_reasons.append("excessive_upper_wick")
        if not vwap_hold_after_breakout:
            block_reasons.append("failed_to_hold_vwap_after_breakout")
        if score < 60:
            block_reasons.append("entry_candle_quality_score_too_low")
        evaluated = breakout_level is not None
        blocked = bool(evaluated and block_reasons)
        return {
            "version": "v0.12.85",
            "evaluated": evaluated,
            "status": (
                "blocked_bad_entry_candle_quality"
                if blocked
                else "allowed_entry_candle_quality"
                if evaluated
                else "not_evaluated_no_breakout_level"
            ),
            "blocked": blocked,
            "score": str(score),
            "threshold": "60",
            "block_reasons": block_reasons,
            "breakout_level": breakout_level,
            "open": str(current_open) if current_open is not None else None,
            "high": str(current_high),
            "low": str(current_low),
            "close": str(current_price),
            "close_position": f"{close_position:.4f}",
            "upper_wick_rate": f"{upper_wick_rate:.4f}",
            "upper_wick_price_rate": f"{upper_wick_price_rate:.4f}",
            "recent_range_avg": (
                f"{recent_range_avg:.4f}"
                if recent_range_avg is not None
                else None
            ),
            "baseline_range_avg": (
                f"{baseline_range_avg:.4f}"
                if baseline_range_avg is not None
                else None
            ),
            "latest_volume": latest_volume,
            "previous_volume_avg": (
                f"{previous_volume_avg:.2f}"
                if previous_volume_avg is not None
                else None
            ),
            "components": components,
        }

    @staticmethod
    def _pullback_support_level(
        *,
        current_price: int,
        vwap: Optional[Decimal],
        moving_average: Optional[Decimal],
        intraday_midline: Optional[Decimal],
    ) -> Optional[Decimal]:
        current = Decimal(current_price)
        supports = [
            value
            for value in (vwap, moving_average, intraday_midline)
            if value is not None and value < current
        ]
        return max(supports) if supports else None

    def _pullback_support_pass(
        self,
        *,
        current_price: int,
        support_level: Optional[Decimal],
        recent_minutes: object,
    ) -> bool:
        if support_level is None:
            return False
        current = Decimal(current_price)
        if current < support_level:
            return False
        rows = list(recent_minutes)
        if not rows:
            return False
        touch_limit = support_level * (
            Decimal("1") + self.strategy_settings.pullback_support_touch_buffer_rate
        )
        return any(
            row["low_price"] is not None
            and Decimal(row["low_price"]) <= touch_limit
            for row in rows
        )

    @staticmethod
    def _minute_structure_context(minute_rows: object) -> dict[str, Any]:
        rows = list(minute_rows)
        recent = [
            row
            for row in rows[:6]
            if row["high_price"] is not None
            and row["low_price"] is not None
            and row["close_price"] is not None
        ]
        recent_swing_low = (
            min(int(row["low_price"]) for row in recent[:3])
            if recent
            else None
        )
        higher_low_pass = False
        if len(recent) >= 3:
            latest_low = int(recent[0]["low_price"])
            earlier_lows = [int(row["low_price"]) for row in recent[1:3]]
            latest_close = int(recent[0]["close_price"])
            previous_close = int(recent[1]["close_price"])
            higher_low_pass = bool(
                latest_low > max(earlier_lows)
                and latest_close >= previous_close
            )

        ranges = [
            Decimal(int(row["high_price"]) - int(row["low_price"]))
            for row in recent[:5]
        ]
        recent_candle_range = (
            sum(ranges, Decimal("0")) / Decimal(len(ranges))
            if ranges
            else None
        )

        chronological = list(reversed(rows[:14]))
        true_ranges: list[Decimal] = []
        previous_close: Optional[int] = None
        for row in chronological:
            if row["high_price"] is None or row["low_price"] is None:
                continue
            high = int(row["high_price"])
            low = int(row["low_price"])
            values = [Decimal(high - low)]
            if previous_close is not None:
                values.extend(
                    [
                        Decimal(abs(high - previous_close)),
                        Decimal(abs(low - previous_close)),
                    ]
                )
            true_ranges.append(max(values))
            if row["close_price"] is not None:
                previous_close = int(row["close_price"])
        minute_atr = (
            sum(true_ranges, Decimal("0")) / Decimal(len(true_ranges))
            if true_ranges
            else None
        )
        return {
            "recent_swing_low": recent_swing_low,
            "higher_low_pass": higher_low_pass,
            "minute_atr": minute_atr,
            "recent_candle_range": recent_candle_range,
        }

    @staticmethod
    def _signal_reason(signal: BreakoutSignal) -> str:
        volume_ratio = (
            f"{signal.volume_ratio:.2f}" if signal.volume_ratio is not None else "-"
        )
        projected_volume_ratio = (
            f"{signal.projected_volume_ratio:.2f}"
            if signal.projected_volume_ratio is not None
            else "-"
        )
        projected_trade_amount = (
            f"{signal.projected_trade_amount:,}"
            if signal.projected_trade_amount is not None
            else "-"
        )
        previous_high = (
            f"{signal.previous_high:,}" if signal.previous_high is not None else "-"
        )
        intraday_high = (
            f"{signal.intraday_high:,}" if signal.intraday_high is not None else "-"
        )
        moving_average = (
            f"{signal.moving_average:.2f}" if signal.moving_average is not None else "-"
        )
        vwap = f"{signal.vwap:.2f}" if signal.vwap is not None else "-"
        intraday_midline = (
            f"{signal.intraday_midline:.2f}"
            if signal.intraday_midline is not None
            else "-"
        )
        breakout_level = (
            f"{signal.breakout_level:,}" if signal.breakout_level is not None else "-"
        )
        opening_range = (
            f"{signal.opening_range_low:,}~{signal.opening_range_high:,}"
            if (
                signal.opening_range_low is not None
                and signal.opening_range_high is not None
            )
            else "-"
        )
        opening_range_breakout = (
            f"{signal.opening_range_breakout_rate:.2%}"
            if signal.opening_range_breakout_rate is not None
            else "-"
        )
        pullback_support_level = (
            f"{signal.pullback_support_level:.2f}"
            if signal.pullback_support_level is not None
            else "-"
        )
        day_gain_rate = (
            f"{signal.day_gain_rate:.2%}" if signal.day_gain_rate is not None else "-"
        )
        return (
            f"기본 돌파 전략: 거래량비={volume_ratio}, "
            f"예상거래량비={projected_volume_ratio}, "
            f"예상거래대금={projected_trade_amount}, "
            f"전일고가={previous_high}, 장중고점={intraday_high}, "
            f"돌파유지선={breakout_level}, VWAP={vwap}, "
            f"5분봉20MA={moving_average}, 중심선={intraday_midline}, "
            f"ORB={opening_range}, ORB돌파폭={opening_range_breakout}, "
            f"눌림지지선={pullback_support_level}, "
            f"당일상승률={day_gain_rate}"
        )

    def _store_signal(
        self,
        account_id: int,
        signal: BreakoutSignal,
        action: str,
        order_id: Optional[int],
        message: str,
    ) -> dict:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_signals(
                    account_id, stock_code, signal_time, current_price,
                    previous_high, previous_close, current_volume,
                    previous_volume, volume_ratio, moving_average,
                    day_gain_rate, volume_pass, breakout_pass,
                    moving_average_pass, chase_pass, signal_pass,
                    action, order_id, message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, stock_code, signal_time) DO UPDATE SET
                    action = CASE
                        WHEN strategy_signals.action = 'executed'
                            THEN strategy_signals.action
                        ELSE excluded.action
                    END,
                    order_id = COALESCE(strategy_signals.order_id, excluded.order_id),
                    message = CASE
                        WHEN strategy_signals.action = 'executed'
                            THEN strategy_signals.message
                        ELSE excluded.message
                    END
                """,
                (
                    account_id,
                    signal.stock_code,
                    signal.signal_time,
                    signal.current_price,
                    signal.previous_high,
                    signal.previous_close,
                    signal.current_volume,
                    signal.previous_volume,
                    float(signal.volume_ratio) if signal.volume_ratio is not None else None,
                    float(signal.moving_average) if signal.moving_average is not None else None,
                    float(signal.day_gain_rate) if signal.day_gain_rate is not None else None,
                    int(signal.volume_pass),
                    int(signal.breakout_pass),
                    int(signal.moving_average_pass),
                    int(signal.chase_pass),
                    int(signal.passed),
                    action,
                    order_id,
                    message,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT id, action, order_id, message
                FROM strategy_signals
                WHERE account_id = ? AND stock_code = ? AND signal_time = ?
                """,
                (account_id, signal.stock_code, signal.signal_time),
            ).fetchone()
            return dict(row)

    def _existing_signal(
        self,
        account_name: str,
        signal: BreakoutSignal,
    ) -> Optional[dict]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT s.id, s.action, s.order_id, s.message
                FROM strategy_signals s
                JOIN paper_accounts a ON a.id = s.account_id
                WHERE a.name = ? AND s.stock_code = ? AND s.signal_time = ?
                """,
                (account_name, signal.stock_code, signal.signal_time),
            ).fetchone()
            return dict(row) if row is not None else None

    def _save_replay_snapshot(
        self,
        decision_id: int,
        signal: BreakoutSignal,
        decision: StrategyDecision,
        *,
        replay_action: Optional[str] = None,
        replay_reason: Optional[str] = None,
        trade_plan: Optional[TradePlan] = None,
    ) -> None:
        leader = self._latest_leader_score(signal.stock_code)
        candles = self._replay_candles(signal.stock_code, signal.signal_time)
        block_reasons = []
        if signal.information_blocked:
            block_reasons.append("뉴스·공시 진입 차단")
        if signal.chart is not None and signal.chart.blocked:
            block_reasons.append(signal.chart.block_reason or "차트 위험 패턴")
        if signal.supply and signal.supply.get("blocked"):
            block_reasons.append(signal.supply.get("blocked_reason") or "수급 차단")
        setup = trade_plan.setup if trade_plan is not None else {}
        data_policy = (
            setup.get("data_availability_policy")
            or (setup.get("entry_guard_policy") or {}).get("data_availability")
            or {}
        )
        if data_policy.get("blocked"):
            block_reasons.append(data_policy.get("message") or "data gap blocked")
        chart_details = signal.chart.as_dict() if signal.chart is not None else {}
        self.database.save_strategy_replay_snapshot(
            decision_id=decision_id,
            stock_code=signal.stock_code,
            evaluated_at=signal.signal_time,
            action=replay_action or decision.action.value,
            score=float(decision.score),
            reason=replay_reason or decision.reason,
            leader_score=leader,
            supply_score=signal.supply,
            chart_setup=(chart_details.get("details") or {}).get("daily") or {},
            intraday_trigger=(chart_details.get("details") or {}).get("trigger") or {},
            block_reasons=block_reasons,
            details={
                "passed": signal.passed,
                "volume_pass": signal.volume_pass,
                "liquidity_pass": signal.liquidity_pass,
                "breakout_pass": signal.breakout_pass,
                "breakout_hold_pass": signal.breakout_hold_pass,
                "vwap_pass": signal.vwap_pass,
                "moving_average_pass": signal.moving_average_pass,
                "pullback_pass": signal.pullback_pass,
                "order_flow_pass": signal.order_flow_pass,
                "chart": chart_details,
                "trade_plan_setup": setup,
                "entry_guard_policy": setup.get("entry_guard_policy") or {},
                "router_execution_policy": (
                    setup.get("router_execution_policy") or {}
                ),
                "legacy_entry_policy": setup.get("legacy_entry_policy") or {},
                "entry_candle_quality_policy": (
                    setup.get("entry_candle_quality_policy") or {}
                ),
                "mode_shadow_compare": setup.get("mode_shadow_compare") or {},
                "strategy_ab_compare": setup.get("strategy_ab_compare") or {},
                "data_availability_policy": data_policy,
                "strategy_action": decision.action.value,
                "runtime_action": replay_action or decision.action.value,
                "runtime_reason": replay_reason or decision.reason,
            },
            candles=candles,
        )

    @staticmethod
    def _replay_action(decision: StrategyDecision, runtime_action: str) -> str:
        normalized = runtime_action.strip().lower()
        if normalized in {"skipped", "failed"}:
            return normalized
        return decision.action.value

    def _latest_leader_score(self, stock_code: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                {Database.leader_stock_scores_latest_cte()}
                SELECT *
                FROM latest_leader_stock_scores
                WHERE stock_code = ?
                ORDER BY trade_date DESC, scored_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        for source, target, fallback in (
            ("reasons_json", "reasons", []),
            ("components_json", "components", {}),
        ):
            try:
                item[target] = json.loads(item.pop(source) or json.dumps(fallback))
            except (TypeError, ValueError, json.JSONDecodeError):
                item[target] = fallback
        item["blocked"] = bool(item.get("blocked"))
        return item

    def _replay_candles(
        self,
        stock_code: str,
        signal_time: str,
    ) -> list[dict[str, Any]]:
        signal_day = signal_time[:10]
        with self.database.connect() as connection:
            daily = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'daily'
                  AND candle_time <= ?
                ORDER BY candle_time DESC
                LIMIT 5
                """,
                (stock_code, signal_day),
            ).fetchall()
            minute = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume, interval_minutes
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'minute'
                  AND interval_minutes = ?
                  AND candle_time <= ?
                ORDER BY candle_time DESC
                LIMIT 30
                """,
                (
                    stock_code,
                    self.strategy_settings.minute_interval,
                    signal_time,
                ),
            ).fetchall()
            minute_after = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume, interval_minutes
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'minute'
                  AND interval_minutes = ?
                  AND candle_time > ?
                ORDER BY candle_time
                LIMIT 12
                """,
                (
                    stock_code,
                    self.strategy_settings.minute_interval,
                    signal_time,
                ),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(daily):
            item = dict(row)
            item["candle_type"] = "daily"
            item["interval_minutes"] = 0
            item["relative_position"] = "daily_context"
            result.append(item)
        for row in reversed(minute):
            item = dict(row)
            item["candle_type"] = "minute"
            item["relative_position"] = (
                "entry" if item["candle_time"] == signal_time else "before"
            )
            result.append(item)
        for row in minute_after:
            item = dict(row)
            item["candle_type"] = "minute"
            item["relative_position"] = "after"
            result.append(item)
        return result

    def history(self, account_name: str, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.stock_code, s.signal_time, s.current_price,
                       s.volume_ratio, s.moving_average, s.day_gain_rate,
                       s.signal_pass, s.action, s.order_id, s.message
                FROM strategy_signals s
                JOIN paper_accounts a ON a.id = s.account_id
                WHERE a.name = ?
                ORDER BY s.id DESC
                LIMIT ?
                """,
                (account_name, limit),
            ).fetchall()
            return [dict(row) for row in rows]


def _candidate_context(row: object | None) -> dict:
    if row is None:
        return {
            "information_blocked": False,
            "information_score": Decimal("0"),
            "pattern_score": Decimal("0"),
            "patterns": (),
            "features": {},
        }
    try:
        patterns = tuple(json.loads(row["patterns_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        patterns = ()
    try:
        features = json.loads(row["features_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        features = {}
    return {
        "information_blocked": bool(row["information_blocked"]),
        "information_score": Decimal(str(row["information_score"] or 0)),
        "pattern_score": Decimal(str(row["pattern_score"] or 0)),
        "patterns": patterns,
        "features": features if isinstance(features, dict) else {},
    }


def _supply_context(row: object | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for source, target, fallback in (
        ("reasons_json", "reasons", []),
        ("components_json", "components", {}),
    ):
        try:
            item[target] = json.loads(item.pop(source) or json.dumps(fallback))
        except (TypeError, ValueError, json.JSONDecodeError):
            item[target] = fallback
    item["blocked"] = bool(item.get("blocked"))
    return item


def _json_intraday_pattern(row: object | None) -> dict[str, object] | None:
    if row is None:
        return None
    item = dict(row)
    for source, target in (
        ("details_json", "details"),
        ("reasons_json", "reasons"),
    ):
        try:
            item[target] = json.loads(item.pop(source) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item[target] = {} if target == "details" else []
    return item


def _pattern_signal_score(
    pattern_score: Decimal,
    patterns: tuple[str, ...],
) -> Decimal:
    if not patterns:
        return Decimal("50")
    return _clamp_score(Decimal("50") + pattern_score)


def _information_signal_score(
    information_score: Decimal,
    information_blocked: bool,
) -> Decimal:
    if information_blocked:
        return Decimal("-100")
    if information_score > 0:
        return Decimal("50")
    return _clamp_score(Decimal("50") + information_score * Decimal("2.5"))


def _order_flow_signal_score(signal: BreakoutSignal) -> Decimal:
    if signal.order_flow_score is not None:
        return signal.order_flow_score
    if (
        signal.order_book_spread_rate is None
        and signal.order_book_bid_ask_ratio is None
        and signal.trade_strength is None
    ):
        return Decimal("50")
    if signal.order_book_spread_rate is not None and (
        signal.order_book_spread_rate
        > Decimal("0.02")
    ):
        return Decimal("0")
    score = Decimal("25")
    if signal.order_flow_pass:
        score = Decimal("75")
    if (
        signal.order_flow_pass
        and signal.order_book_bid_ask_ratio is not None
        and signal.order_book_bid_ask_ratio >= Decimal("1.2")
        and signal.trade_strength is not None
        and signal.trade_strength >= Decimal("150")
    ):
        score = Decimal("100")
    return score


def _clamp_score(value: Decimal) -> Decimal:
    return max(Decimal("-100"), min(Decimal("100"), value))


def _float_value(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _optional_int(value: object) -> Optional[int]:
    return int(value) if value is not None else None
