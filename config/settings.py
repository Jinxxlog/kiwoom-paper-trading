import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional


BASE_DIR = Path(__file__).resolve().parent.parent


def _read_required_secret(env_name: str) -> str:
    value = os.getenv(env_name)
    if value:
        return value.strip()
    raise RuntimeError(f"{env_name} 환경변수를 설정하세요.")


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_windows_user_env(name: str) -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value).strip() if value else None


def _read_env_or_user(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value.strip()
    return _read_windows_user_env(name) or ""


@dataclass(frozen=True)
class Settings:
    app_key: str
    secret_key: str
    use_mock: bool
    database_path: Path
    token_cache_path: Optional[Path]
    request_timeout: float


@dataclass(frozen=True)
class PaperTradingSettings:
    initial_cash: int
    commission_rate: Decimal
    sell_tax_rate: Decimal
    slippage_rate: Decimal


@dataclass(frozen=True)
class RiskSettings:
    stop_loss_rate: Decimal
    take_profit_rate: Decimal
    trailing_activation_rate: Decimal
    trailing_drawdown_rate: Decimal
    daily_max_loss_rate: Decimal
    max_consecutive_losses: int
    max_daily_stop_losses: int = 0
    take_profit_enabled: bool = True
    per_trade_risk_rate: Decimal = Decimal("0.005")
    stop_loss_cooldown_minutes: int = 0
    max_position_rate: Decimal = Decimal("1.0")
    max_positions: int = 10
    max_entry_gain_rate: Decimal = Decimal("0")
    atr_period: int = 14
    atr_stop_multiple: Decimal = Decimal("2.0")
    breakeven_activation_rate: Decimal = Decimal("0.02")
    breakeven_buffer_rate: Decimal = Decimal("0")
    strong_trend_take_profit_rate: Decimal = Decimal("0.08")
    strong_trend_trailing_drawdown_rate: Decimal = Decimal("0.025")
    weak_trend_trailing_drawdown_rate: Decimal = Decimal("0.01")
    volume_dry_up_ratio: Decimal = Decimal("0.40")


@dataclass(frozen=True)
class StrategySettings:
    volume_multiple: Decimal
    max_day_gain_rate: Decimal
    moving_average_period: int
    minute_interval: int
    intraday_breakout_lookback: int = 20
    paper_probe_enabled: bool = True
    paper_probe_min_score: Decimal = Decimal("65")
    minimum_projected_trade_amount: int = 50_000_000_000
    candidate_context_max_age_minutes: int = 120
    pullback_min_depth_rate: Decimal = Decimal("0.005")
    pullback_max_depth_rate: Decimal = Decimal("0.05")
    intraday_midline_lookback: int = 20
    intraday_breakout_hold_buffer_rate: Decimal = Decimal("0.003")
    pullback_support_touch_buffer_rate: Decimal = Decimal("0.005")
    order_flow_max_spread_rate: Decimal = Decimal("0.01")
    order_flow_min_bid_ask_ratio: Decimal = Decimal("1.2")
    order_flow_min_strength: Decimal = Decimal("150")
    order_flow_history_limit: int = 5
    order_flow_ask_absorption_drop_rate: Decimal = Decimal("0.15")
    order_flow_bid_wall_risk_ratio: Decimal = Decimal("2.0")
    opening_range_minutes: int = 10
    opening_range_entry_end_minutes: int = 60
    opening_range_breakout_buffer_rate: Decimal = Decimal("0.002")
    plan_fallback_stop_rate: Decimal = Decimal("0.035")
    session_profile_enabled: bool = True
    morning_entry_guard_enabled: bool = False
    morning_entry_limit_start_minutes: int = 10
    morning_entry_limit_end_minutes: int = 30
    morning_max_new_buys: int = 0
    morning_max_a_grade_buys: int = 0
    morning_max_b_grade_buys: int = 0
    entry_bucket_minutes: int = 10
    max_new_buys_per_bucket: int = 0
    max_same_stock_buys_per_day: int = 0
    risk_loss_limit_a_grade_quantity_multiplier: Decimal = Decimal("0.40")
    risk_cooldown_a_grade_quantity_multiplier: Decimal = Decimal("0.35")
    data_gap_probe_enabled: bool = True
    data_gap_market_rank_max_age_minutes: int = 15
    data_gap_trade_amount_rank_limit: int = 20
    data_gap_realtime_rank_limit: int = 20
    data_gap_minimum_probe_quantity_multiplier: Decimal = Decimal("0.20")
    data_gap_minimum_probe_max_quantity: int = 1
    entry_type_minimum_probe_quantity_multiplier: Decimal = Decimal("0.25")
    small_account_aggressive_enabled: bool = False
    small_account_max_equity: int = 1_500_000
    small_account_a_plus_position_rate: Decimal = Decimal("0.60")
    small_account_a_position_rate: Decimal = Decimal("0.30")
    small_account_b_position_rate: Decimal = Decimal("0.08")
    small_account_a_plus_per_trade_risk_rate: Decimal = Decimal("0.030")
    small_account_a_per_trade_risk_rate: Decimal = Decimal("0.018")
    small_account_b_per_trade_risk_rate: Decimal = Decimal("0.006")
    small_account_minimum_quantity_if_affordable: int = 1
    small_account_block_c_grade: bool = True
    small_account_block_data_gap: bool = True
    small_account_block_supply_missing: bool = True
    strategy_ab_test_enabled: bool = True
    strategy_ab_active_mode: str = "remake"
    a_plus_armed_enabled: bool = True
    a_plus_armed_expiry_minutes_after_open: int = 90
    a_plus_armed_min_money_flow_score: Decimal = Decimal("50")
    staged_position_build_enabled: bool = True
    staged_position_scout_rate: Decimal = Decimal("0.25")
    staged_position_confirmation_rate: Decimal = Decimal("0.45")
    staged_position_reentry_probe_rate: Decimal = Decimal("0.20")
    structural_initial_stop_enabled: bool = True
    structural_stop_buffer_atr_rate: Decimal = Decimal("0.10")
    structural_stop_noise_atr_rate: Decimal = Decimal("0.60")
    structural_stop_noise_range_rate: Decimal = Decimal("0.50")


@dataclass(frozen=True)
class AccountDataSettings:
    retention_days: int


@dataclass(frozen=True)
class MarketDataSettings:
    max_price_age_seconds: int
    current_price_critical_interval_seconds: int = 30
    current_price_high_interval_seconds: int = 60
    current_price_normal_interval_seconds: int = 180
    current_price_low_interval_seconds: int = 300
    current_price_max_codes_per_cycle: int = 12
    current_price_call_spacing_seconds: float = 0.25
    current_price_pre_entry_max_age_minutes: int = 10
    detail_collection_interval_minutes: int = 5
    detail_collection_max_codes: int = 5
    detail_failure_cooldown_minutes: int = 15
    detail_abort_on_rate_limit: bool = True


@dataclass(frozen=True)
class OperationsAlertSettings:
    collection_delay_seconds: int
    consecutive_failures: int


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool
    bot_token: str
    chat_id: str
    request_timeout: float = 10.0


@dataclass(frozen=True)
class CandidateSettings:
    minimum_history_bars: int = 60
    minimum_price: int = 1_000
    minimum_average_volume: int = 100_000
    minimum_average_trade_amount: int = 500_000_000
    maximum_daily_volatility: Decimal = Decimal("0.15")
    maximum_order_book_spread_rate: Decimal = Decimal("0.02")
    selected_count: int = 20
    theme_score_limit: Decimal = Decimal("12")
    theme_max_age_minutes: int = 240
    theme_collection_top_groups: int = 10
    theme_collection_interval_minutes: int = 15
    realtime_rank_query_type: str = "1"
    realtime_rank_collection_interval_minutes: int = 5
    realtime_rank_max_pages: int = 1
    maximum_open_gap_rate: Decimal = Decimal("0.10")
    maximum_day_gain_rate: Decimal = Decimal("0.18")


@dataclass(frozen=True)
class MarketModeGateSettings:
    enabled: bool
    manual_mode: str
    source: str = "manual_env"
    requested_mode: str = ""
    reason: str = ""
    order_effect: str = "paper_apply"
    quantity_multiplier: Decimal = Decimal("1")
    allowed_entry_grades: str = "A/B"
    shadow_mode: str = ""

    def __post_init__(self) -> None:
        mode = _normalize_market_mode(self.manual_mode)
        if mode not in {"risk_on", "neutral", "risk_off"}:
            raise ValueError(
                "manual_mode은 risk_on, neutral, risk_off 중 하나여야 합니다."
            )
        requested = _normalize_market_mode(self.requested_mode or mode)
        if requested not in {"risk_on", "neutral", "risk_off", "auto"}:
            raise ValueError(
                "requested_mode은 risk_on, neutral, risk_off, auto 중 하나여야 합니다."
            )
        object.__setattr__(self, "manual_mode", mode)
        object.__setattr__(self, "requested_mode", requested)
        shadow = _normalize_market_mode(self.shadow_mode) if self.shadow_mode else ""
        if shadow and shadow not in {"risk_on", "neutral", "risk_off"}:
            raise ValueError(
                "shadow_mode은 risk_on, neutral, risk_off 중 하나여야 합니다."
            )
        object.__setattr__(self, "shadow_mode", shadow)

    @property
    def entry_allowed(self) -> bool:
        return (not self.enabled) or self.manual_mode != "risk_off"

    @property
    def entry_state(self) -> str:
        if not self.enabled:
            return "disabled"
        return "allowed" if self.entry_allowed else "blocked"


@dataclass(frozen=True)
class InformationSettings:
    naver_client_id: str
    naver_client_secret: str
    dart_api_key: str
    request_timeout: float = 10.0
    news_half_life_hours: float = 72.0
    disclosure_half_life_hours: float = 168.0
    candidate_score_limit: float = 20.0


@dataclass(frozen=True)
class CompositeScoreSettings:
    technical_weight: Decimal = Decimal("0.25")
    pattern_weight: Decimal = Decimal("0.25")
    relative_strength_weight: Decimal = Decimal("0.15")
    liquidity_weight: Decimal = Decimal("0.15")
    information_weight: Decimal = Decimal("0.05")
    market_weight: Decimal = Decimal("0.15")
    buy_threshold: Decimal = Decimal("70")
    observe_threshold: Decimal = Decimal("55")
    minimum_confidence: Decimal = Decimal("0.70")


@dataclass(frozen=True)
class AiShadowSettings:
    enabled: bool
    provider: str
    model: str
    prompt_version: str
    candidate_limit: int
    interval_minutes: int
    request_timeout: float
    max_output_tokens: int
    openai_api_key: str
    anthropic_api_key: str
    input_cost_per_million: Decimal | None
    output_cost_per_million: Decimal | None


def load_database_path() -> Path:
    data_dir = Path(os.getenv("AUTO_TRADER_DATA_DIR", BASE_DIR / "runtime"))
    return Path(os.getenv("AUTO_TRADER_DB_PATH", data_dir / "auto_trader.db"))


def load_settings() -> Settings:
    token_cache = os.getenv("KIWOOM_TOKEN_CACHE", "").strip()
    return Settings(
        app_key=_read_required_secret("KIWOOM_APP_KEY"),
        secret_key=_read_required_secret("KIWOOM_SECRET_KEY"),
        use_mock=_read_bool("KIWOOM_USE_MOCK", True),
        database_path=load_database_path(),
        token_cache_path=Path(token_cache) if token_cache else None,
        request_timeout=float(os.getenv("KIWOOM_REQUEST_TIMEOUT", "10")),
    )


def load_paper_trading_settings() -> PaperTradingSettings:
    return PaperTradingSettings(
        initial_cash=int(os.getenv("PAPER_INITIAL_CASH", "10000000")),
        commission_rate=Decimal(os.getenv("PAPER_COMMISSION_RATE", "0.00015")),
        sell_tax_rate=Decimal(os.getenv("PAPER_SELL_TAX_RATE", "0.002")),
        slippage_rate=Decimal(os.getenv("PAPER_SLIPPAGE_RATE", "0.0005")),
    )


def load_risk_settings() -> RiskSettings:
    return RiskSettings(
        stop_loss_rate=Decimal(os.getenv("RISK_STOP_LOSS_RATE", "0.02")),
        take_profit_rate=Decimal(os.getenv("RISK_TAKE_PROFIT_RATE", "0.04")),
        take_profit_enabled=_read_bool("RISK_TAKE_PROFIT_ENABLED", False),
        trailing_activation_rate=Decimal(
            os.getenv("RISK_TRAILING_ACTIVATION_RATE", "0.03")
        ),
        trailing_drawdown_rate=Decimal(
            os.getenv("RISK_TRAILING_DRAWDOWN_RATE", "0.015")
        ),
        daily_max_loss_rate=Decimal(
            os.getenv("RISK_DAILY_MAX_LOSS_RATE", "0.10")
        ),
        max_consecutive_losses=int(
            os.getenv("RISK_MAX_CONSECUTIVE_LOSSES", "0")
        ),
        max_daily_stop_losses=int(
            os.getenv("RISK_MAX_DAILY_STOP_LOSSES", "0")
        ),
        per_trade_risk_rate=Decimal(
            os.getenv("RISK_PER_TRADE_RATE", "0.005")
        ),
        stop_loss_cooldown_minutes=int(
            os.getenv("RISK_STOP_LOSS_COOLDOWN_MINUTES", "0")
        ),
        max_position_rate=Decimal(
            os.getenv("RISK_MAX_POSITION_RATE", "1.0")
        ),
        max_positions=int(os.getenv("RISK_MAX_POSITIONS", "10")),
        max_entry_gain_rate=Decimal(
            os.getenv("RISK_MAX_ENTRY_GAIN_RATE", "0")
        ),
        atr_period=int(os.getenv("RISK_ATR_PERIOD", "14")),
        atr_stop_multiple=Decimal(
            os.getenv("RISK_ATR_STOP_MULTIPLE", "2.0")
        ),
        breakeven_activation_rate=Decimal(
            os.getenv("RISK_BREAKEVEN_ACTIVATION_RATE", "0.02")
        ),
        breakeven_buffer_rate=Decimal(
            os.getenv("RISK_BREAKEVEN_BUFFER_RATE", "0")
        ),
        strong_trend_take_profit_rate=Decimal(
            os.getenv("RISK_STRONG_TREND_TAKE_PROFIT_RATE", "0.08")
        ),
        strong_trend_trailing_drawdown_rate=Decimal(
            os.getenv(
                "RISK_STRONG_TREND_TRAILING_DRAWDOWN_RATE",
                "0.025",
            )
        ),
        weak_trend_trailing_drawdown_rate=Decimal(
            os.getenv(
                "RISK_WEAK_TREND_TRAILING_DRAWDOWN_RATE",
                "0.01",
            )
        ),
        volume_dry_up_ratio=Decimal(
            os.getenv("RISK_VOLUME_DRY_UP_RATIO", "0.40")
        ),
    )


def load_strategy_settings() -> StrategySettings:
    return StrategySettings(
        volume_multiple=Decimal(
            os.getenv("STRATEGY_VOLUME_MULTIPLE", "1.3")
        ),
        max_day_gain_rate=Decimal(
            os.getenv("STRATEGY_MAX_DAY_GAIN_RATE", "0")
        ),
        moving_average_period=int(
            os.getenv("STRATEGY_MOVING_AVERAGE_PERIOD", "20")
        ),
        minute_interval=int(os.getenv("STRATEGY_MINUTE_INTERVAL", "5")),
        intraday_breakout_lookback=int(
            os.getenv("STRATEGY_INTRADAY_BREAKOUT_LOOKBACK", "20")
        ),
        paper_probe_enabled=_read_bool("STRATEGY_PAPER_PROBE_ENABLED", True),
        paper_probe_min_score=Decimal(
            os.getenv("STRATEGY_PAPER_PROBE_MIN_SCORE", "65")
        ),
        minimum_projected_trade_amount=int(
            os.getenv(
                "STRATEGY_MINIMUM_PROJECTED_TRADE_AMOUNT",
                "50000000000",
            )
        ),
        candidate_context_max_age_minutes=int(
            os.getenv("STRATEGY_CANDIDATE_CONTEXT_MAX_AGE_MINUTES", "120")
        ),
        pullback_min_depth_rate=Decimal(
            os.getenv("STRATEGY_PULLBACK_MIN_DEPTH_RATE", "0.005")
        ),
        pullback_max_depth_rate=Decimal(
            os.getenv("STRATEGY_PULLBACK_MAX_DEPTH_RATE", "0.05")
        ),
        intraday_midline_lookback=int(
            os.getenv("STRATEGY_INTRADAY_MIDLINE_LOOKBACK", "20")
        ),
        intraday_breakout_hold_buffer_rate=Decimal(
            os.getenv("STRATEGY_INTRADAY_BREAKOUT_HOLD_BUFFER_RATE", "0.003")
        ),
        pullback_support_touch_buffer_rate=Decimal(
            os.getenv("STRATEGY_PULLBACK_SUPPORT_TOUCH_BUFFER_RATE", "0.005")
        ),
        order_flow_max_spread_rate=Decimal(
            os.getenv("STRATEGY_ORDER_FLOW_MAX_SPREAD_RATE", "0.01")
        ),
        order_flow_min_bid_ask_ratio=Decimal(
            os.getenv("STRATEGY_ORDER_FLOW_MIN_BID_ASK_RATIO", "1.2")
        ),
        order_flow_min_strength=Decimal(
            os.getenv("STRATEGY_ORDER_FLOW_MIN_STRENGTH", "150")
        ),
        order_flow_history_limit=int(
            os.getenv("STRATEGY_ORDER_FLOW_HISTORY_LIMIT", "5")
        ),
        order_flow_ask_absorption_drop_rate=Decimal(
            os.getenv("STRATEGY_ORDER_FLOW_ASK_ABSORPTION_DROP_RATE", "0.15")
        ),
        order_flow_bid_wall_risk_ratio=Decimal(
            os.getenv("STRATEGY_ORDER_FLOW_BID_WALL_RISK_RATIO", "2.0")
        ),
        opening_range_minutes=int(
            os.getenv("STRATEGY_OPENING_RANGE_MINUTES", "10")
        ),
        opening_range_entry_end_minutes=int(
            os.getenv("STRATEGY_OPENING_RANGE_ENTRY_END_MINUTES", "60")
        ),
        opening_range_breakout_buffer_rate=Decimal(
            os.getenv("STRATEGY_OPENING_RANGE_BREAKOUT_BUFFER_RATE", "0.002")
        ),
        plan_fallback_stop_rate=Decimal(
            os.getenv("STRATEGY_PLAN_FALLBACK_STOP_RATE", "0.035")
        ),
        session_profile_enabled=_read_bool(
            "STRATEGY_SESSION_PROFILE_ENABLED",
            True,
        ),
        morning_entry_guard_enabled=_read_bool(
            "STRATEGY_MORNING_ENTRY_GUARD_ENABLED",
            False,
        ),
        morning_entry_limit_start_minutes=int(
            os.getenv("STRATEGY_MORNING_ENTRY_LIMIT_START_MINUTES", "10")
        ),
        morning_entry_limit_end_minutes=int(
            os.getenv("STRATEGY_MORNING_ENTRY_LIMIT_END_MINUTES", "30")
        ),
        morning_max_new_buys=int(
            os.getenv("STRATEGY_MORNING_MAX_NEW_BUYS", "0")
        ),
        morning_max_a_grade_buys=int(
            os.getenv("STRATEGY_MORNING_MAX_A_GRADE_BUYS", "0")
        ),
        morning_max_b_grade_buys=int(
            os.getenv("STRATEGY_MORNING_MAX_B_GRADE_BUYS", "0")
        ),
        entry_bucket_minutes=int(
            os.getenv("STRATEGY_ENTRY_BUCKET_MINUTES", "10")
        ),
        max_new_buys_per_bucket=int(
            os.getenv("STRATEGY_MAX_NEW_BUYS_PER_BUCKET", "0")
        ),
        max_same_stock_buys_per_day=int(
            os.getenv("STRATEGY_MAX_SAME_STOCK_BUYS_PER_DAY", "0")
        ),
        risk_loss_limit_a_grade_quantity_multiplier=Decimal(
            os.getenv("STRATEGY_RISK_LOSS_LIMIT_A_GRADE_QUANTITY_MULTIPLIER", "0.40")
        ),
        risk_cooldown_a_grade_quantity_multiplier=Decimal(
            os.getenv("STRATEGY_RISK_COOLDOWN_A_GRADE_QUANTITY_MULTIPLIER", "0.35")
        ),
        data_gap_probe_enabled=_read_bool(
            "STRATEGY_DATA_GAP_PROBE_ENABLED",
            True,
        ),
        data_gap_market_rank_max_age_minutes=int(
            os.getenv("STRATEGY_DATA_GAP_MARKET_RANK_MAX_AGE_MINUTES", "15")
        ),
        data_gap_trade_amount_rank_limit=int(
            os.getenv("STRATEGY_DATA_GAP_TRADE_AMOUNT_RANK_LIMIT", "20")
        ),
        data_gap_realtime_rank_limit=int(
            os.getenv("STRATEGY_DATA_GAP_REALTIME_RANK_LIMIT", "20")
        ),
        data_gap_minimum_probe_quantity_multiplier=Decimal(
            os.getenv("STRATEGY_DATA_GAP_MINIMUM_PROBE_QUANTITY_MULTIPLIER", "0.20")
        ),
        data_gap_minimum_probe_max_quantity=int(
            os.getenv("STRATEGY_DATA_GAP_MINIMUM_PROBE_MAX_QUANTITY", "1")
        ),
        entry_type_minimum_probe_quantity_multiplier=Decimal(
            os.getenv("STRATEGY_ENTRY_TYPE_MINIMUM_PROBE_QUANTITY_MULTIPLIER", "0.25")
        ),
        small_account_aggressive_enabled=_read_bool(
            "STRATEGY_SMALL_ACCOUNT_AGGRESSIVE_ENABLED",
            False,
        ),
        small_account_max_equity=int(
            os.getenv("STRATEGY_SMALL_ACCOUNT_MAX_EQUITY", "1500000")
        ),
        small_account_a_plus_position_rate=Decimal(
            os.getenv("STRATEGY_SMALL_ACCOUNT_A_PLUS_POSITION_RATE", "0.60")
        ),
        small_account_a_position_rate=Decimal(
            os.getenv("STRATEGY_SMALL_ACCOUNT_A_POSITION_RATE", "0.30")
        ),
        small_account_b_position_rate=Decimal(
            os.getenv("STRATEGY_SMALL_ACCOUNT_B_POSITION_RATE", "0.08")
        ),
        small_account_a_plus_per_trade_risk_rate=Decimal(
            os.getenv("STRATEGY_SMALL_ACCOUNT_A_PLUS_PER_TRADE_RISK_RATE", "0.030")
        ),
        small_account_a_per_trade_risk_rate=Decimal(
            os.getenv("STRATEGY_SMALL_ACCOUNT_A_PER_TRADE_RISK_RATE", "0.018")
        ),
        small_account_b_per_trade_risk_rate=Decimal(
            os.getenv("STRATEGY_SMALL_ACCOUNT_B_PER_TRADE_RISK_RATE", "0.006")
        ),
        small_account_minimum_quantity_if_affordable=int(
            os.getenv("STRATEGY_SMALL_ACCOUNT_MINIMUM_QUANTITY_IF_AFFORDABLE", "1")
        ),
        small_account_block_c_grade=_read_bool(
            "STRATEGY_SMALL_ACCOUNT_BLOCK_C_GRADE",
            True,
        ),
        small_account_block_data_gap=_read_bool(
            "STRATEGY_SMALL_ACCOUNT_BLOCK_DATA_GAP",
            True,
        ),
        small_account_block_supply_missing=_read_bool(
            "STRATEGY_SMALL_ACCOUNT_BLOCK_SUPPLY_MISSING",
            True,
        ),
        strategy_ab_test_enabled=_read_bool(
            "STRATEGY_AB_TEST_ENABLED",
            True,
        ),
        strategy_ab_active_mode=os.getenv(
            "STRATEGY_AB_ACTIVE_MODE",
            "remake",
        ).strip().lower(),
        a_plus_armed_enabled=_read_bool(
            "STRATEGY_A_PLUS_ARMED_ENABLED",
            True,
        ),
        a_plus_armed_expiry_minutes_after_open=int(
            os.getenv(
                "STRATEGY_A_PLUS_ARMED_EXPIRY_MINUTES_AFTER_OPEN",
                "90",
            )
        ),
        a_plus_armed_min_money_flow_score=Decimal(
            os.getenv("STRATEGY_A_PLUS_ARMED_MIN_MONEY_FLOW_SCORE", "50")
        ),
        staged_position_build_enabled=_read_bool(
            "STRATEGY_STAGED_POSITION_BUILD_ENABLED",
            True,
        ),
        staged_position_scout_rate=Decimal(
            os.getenv("STRATEGY_STAGED_POSITION_SCOUT_RATE", "0.25")
        ),
        staged_position_confirmation_rate=Decimal(
            os.getenv("STRATEGY_STAGED_POSITION_CONFIRMATION_RATE", "0.45")
        ),
        staged_position_reentry_probe_rate=Decimal(
            os.getenv("STRATEGY_STAGED_POSITION_REENTRY_PROBE_RATE", "0.20")
        ),
        structural_initial_stop_enabled=_read_bool(
            "STRATEGY_STRUCTURAL_INITIAL_STOP_ENABLED",
            True,
        ),
        structural_stop_buffer_atr_rate=Decimal(
            os.getenv("STRATEGY_STRUCTURAL_STOP_BUFFER_ATR_RATE", "0.10")
        ),
        structural_stop_noise_atr_rate=Decimal(
            os.getenv("STRATEGY_STRUCTURAL_STOP_NOISE_ATR_RATE", "0.60")
        ),
        structural_stop_noise_range_rate=Decimal(
            os.getenv("STRATEGY_STRUCTURAL_STOP_NOISE_RANGE_RATE", "0.50")
        ),
    )


def load_candidate_settings() -> CandidateSettings:
    return CandidateSettings(
        minimum_history_bars=int(
            os.getenv("CANDIDATE_MINIMUM_HISTORY_BARS", "60")
        ),
        minimum_price=int(os.getenv("CANDIDATE_MINIMUM_PRICE", "1000")),
        minimum_average_volume=int(
            os.getenv("CANDIDATE_MINIMUM_AVERAGE_VOLUME", "100000")
        ),
        minimum_average_trade_amount=int(
            os.getenv(
                "CANDIDATE_MINIMUM_AVERAGE_TRADE_AMOUNT",
                "500000000",
            )
        ),
        maximum_daily_volatility=Decimal(
            os.getenv("CANDIDATE_MAXIMUM_DAILY_VOLATILITY", "0.15")
        ),
        maximum_order_book_spread_rate=Decimal(
            os.getenv("CANDIDATE_MAXIMUM_ORDER_BOOK_SPREAD_RATE", "0.02")
        ),
        selected_count=int(os.getenv("CANDIDATE_SELECTED_COUNT", "20")),
        theme_score_limit=Decimal(
            os.getenv("CANDIDATE_THEME_SCORE_LIMIT", "12")
        ),
        theme_max_age_minutes=int(
            os.getenv("CANDIDATE_THEME_MAX_AGE_MINUTES", "240")
        ),
        theme_collection_top_groups=int(
            os.getenv("CANDIDATE_THEME_COLLECTION_TOP_GROUPS", "10")
        ),
        theme_collection_interval_minutes=int(
            os.getenv("CANDIDATE_THEME_COLLECTION_INTERVAL_MINUTES", "15")
        ),
        realtime_rank_query_type=os.getenv(
            "CANDIDATE_REALTIME_RANK_QUERY_TYPE",
            "1",
        ).strip(),
        realtime_rank_collection_interval_minutes=int(
            os.getenv(
                "CANDIDATE_REALTIME_RANK_COLLECTION_INTERVAL_MINUTES",
                "5",
            )
        ),
        realtime_rank_max_pages=int(
            os.getenv("CANDIDATE_REALTIME_RANK_MAX_PAGES", "1")
        ),
        maximum_open_gap_rate=Decimal(
            os.getenv("CANDIDATE_MAXIMUM_OPEN_GAP_RATE", "0.10")
        ),
        maximum_day_gain_rate=Decimal(
            os.getenv("CANDIDATE_MAXIMUM_DAY_GAIN_RATE", "0.18")
        ),
    )


def _normalize_market_mode(value: str) -> str:
    mode = value.strip().lower().replace("-", "_")
    aliases = {
        "on": "risk_on",
        "allow": "risk_on",
        "allowed": "risk_on",
        "open": "risk_on",
        "off": "risk_off",
        "block": "risk_off",
        "blocked": "risk_off",
        "close": "risk_off",
        "closed": "risk_off",
        "safe": "risk_off",
        "watch": "neutral",
        "observe": "neutral",
    }
    return aliases.get(mode, mode)


def load_market_mode_gate_settings() -> MarketModeGateSettings:
    requested_mode = _normalize_market_mode(
        os.getenv("MARKET_MODE_MANUAL_MODE", "neutral")
    )
    if requested_mode == "auto":
        fallback_mode = _normalize_market_mode(
            os.getenv("MARKET_MODE_AUTO_FALLBACK_MODE", "risk_off")
        )
        if fallback_mode not in {"risk_on", "neutral", "risk_off"}:
            raise ValueError(
                "MARKET_MODE_AUTO_FALLBACK_MODE은 risk_on, neutral, risk_off 중 하나여야 합니다."
            )
        return MarketModeGateSettings(
            enabled=_read_bool("MARKET_MODE_GATE_ENABLED", True),
            manual_mode=fallback_mode,
            source="auto_unavailable",
            requested_mode="auto",
            reason="자동 시장 모드 데이터가 아직 연결되지 않아 fallback 모드 적용",
        )
    if requested_mode not in {"risk_on", "neutral", "risk_off"}:
        raise ValueError(
            "MARKET_MODE_MANUAL_MODE은 risk_on, neutral, risk_off, auto 중 하나여야 합니다."
        )
    return MarketModeGateSettings(
        enabled=_read_bool("MARKET_MODE_GATE_ENABLED", True),
        manual_mode=requested_mode,
        requested_mode=requested_mode,
        reason="수동 환경변수 기준",
    )


def load_information_settings(*, required: bool = True) -> InformationSettings:
    values = {
        "NAVER_CLIENT_ID": os.getenv("NAVER_CLIENT_ID", "").strip(),
        "NAVER_CLIENT_SECRET": os.getenv("NAVER_CLIENT_SECRET", "").strip(),
        "DART_API_KEY": os.getenv("DART_API_KEY", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if required and missing:
        raise RuntimeError(
            "뉴스·공시 환경변수를 설정하세요: " + ", ".join(missing)
        )
    return InformationSettings(
        naver_client_id=values["NAVER_CLIENT_ID"],
        naver_client_secret=values["NAVER_CLIENT_SECRET"],
        dart_api_key=values["DART_API_KEY"],
        request_timeout=float(os.getenv("INFORMATION_REQUEST_TIMEOUT", "10")),
        news_half_life_hours=float(
            os.getenv("NEWS_SCORE_HALF_LIFE_HOURS", "72")
        ),
        disclosure_half_life_hours=float(
            os.getenv("DART_SCORE_HALF_LIFE_HOURS", "168")
        ),
        candidate_score_limit=float(
            os.getenv("CANDIDATE_INFORMATION_SCORE_LIMIT", "20")
        ),
    )


def load_composite_score_settings() -> CompositeScoreSettings:
    settings = CompositeScoreSettings(
        technical_weight=Decimal(
            os.getenv("COMPOSITE_TECHNICAL_WEIGHT", "0.25")
        ),
        pattern_weight=Decimal(
            os.getenv("COMPOSITE_PATTERN_WEIGHT", "0.25")
        ),
        relative_strength_weight=Decimal(
            os.getenv("COMPOSITE_RELATIVE_STRENGTH_WEIGHT", "0.15")
        ),
        liquidity_weight=Decimal(
            os.getenv("COMPOSITE_LIQUIDITY_WEIGHT", "0.15")
        ),
        information_weight=Decimal(
            os.getenv("COMPOSITE_INFORMATION_WEIGHT", "0.05")
        ),
        market_weight=Decimal(
            os.getenv("COMPOSITE_MARKET_WEIGHT", "0.15")
        ),
        buy_threshold=Decimal(
            os.getenv("COMPOSITE_BUY_THRESHOLD", "70")
        ),
        observe_threshold=Decimal(
            os.getenv("COMPOSITE_OBSERVE_THRESHOLD", "55")
        ),
        minimum_confidence=Decimal(
            os.getenv("COMPOSITE_MINIMUM_CONFIDENCE", "0.70")
        ),
    )
    weights = (
        settings.technical_weight,
        settings.pattern_weight,
        settings.relative_strength_weight,
        settings.liquidity_weight,
        settings.information_weight,
        settings.market_weight,
    )
    if any(weight < 0 or weight > 1 for weight in weights):
        raise ValueError("종합 점수 가중치는 0 이상 1 이하여야 합니다.")
    if sum(weights) != Decimal("1"):
        raise ValueError("종합 점수 가중치 합은 1이어야 합니다.")
    if not (
        Decimal("0")
        <= settings.observe_threshold
        <= settings.buy_threshold
        <= Decimal("100")
    ):
        raise ValueError("종합 점수 기준값 순서가 올바르지 않습니다.")
    if not Decimal("0") <= settings.minimum_confidence <= Decimal("1"):
        raise ValueError("종합 점수 최소 신뢰도는 0 이상 1 이하여야 합니다.")
    return settings


def load_ai_shadow_settings() -> AiShadowSettings:
    provider = os.getenv("AI_SHADOW_PROVIDER", "openai").strip().lower()
    if provider not in {"openai", "anthropic"}:
        raise ValueError("AI_SHADOW_PROVIDER는 openai 또는 anthropic이어야 합니다.")
    default_model = (
        "gpt-5.4-mini"
        if provider == "openai"
        else "claude-sonnet-4-6"
    )
    input_cost = os.getenv("AI_INPUT_COST_PER_MILLION", "").strip()
    output_cost = os.getenv("AI_OUTPUT_COST_PER_MILLION", "").strip()
    settings = AiShadowSettings(
        enabled=_read_bool("AI_SHADOW_ENABLED", False),
        provider=provider,
        model=os.getenv("AI_SHADOW_MODEL", default_model).strip(),
        prompt_version=os.getenv("AI_PROMPT_VERSION", "1.0.0").strip(),
        candidate_limit=int(os.getenv("AI_SHADOW_CANDIDATE_LIMIT", "3")),
        interval_minutes=int(os.getenv("AI_SHADOW_INTERVAL_MINUTES", "60")),
        request_timeout=float(os.getenv("AI_REQUEST_TIMEOUT", "45")),
        max_output_tokens=int(os.getenv("AI_MAX_OUTPUT_TOKENS", "700")),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        input_cost_per_million=Decimal(input_cost) if input_cost else None,
        output_cost_per_million=Decimal(output_cost) if output_cost else None,
    )
    if not settings.model or not settings.prompt_version:
        raise ValueError("AI 모델과 프롬프트 버전은 비어 있을 수 없습니다.")
    if settings.candidate_limit < 1 or settings.candidate_limit > 20:
        raise ValueError("AI 분석 후보 수는 1~20이어야 합니다.")
    if settings.interval_minutes < 5:
        raise ValueError("AI 분석 간격은 5분 이상이어야 합니다.")
    if settings.request_timeout <= 0 or settings.max_output_tokens < 100:
        raise ValueError("AI 요청 제한값이 올바르지 않습니다.")
    return settings


def load_account_data_settings() -> AccountDataSettings:
    retention_days = int(os.getenv("ACCOUNT_DATA_RETENTION_DAYS", "90"))
    if retention_days < 1:
        raise ValueError("ACCOUNT_DATA_RETENTION_DAYS는 1 이상이어야 합니다.")
    return AccountDataSettings(retention_days=retention_days)


def load_market_data_settings() -> MarketDataSettings:
    max_price_age_seconds = int(
        os.getenv("MARKET_MAX_PRICE_AGE_SECONDS", "180")
    )
    detail_collection_interval_minutes = int(
        os.getenv("MARKET_DETAIL_COLLECTION_INTERVAL_MINUTES", "5")
    )
    detail_collection_max_codes = int(
        os.getenv("MARKET_DETAIL_MAX_CODES", "5")
    )
    detail_failure_cooldown_minutes = int(
        os.getenv("MARKET_DETAIL_FAILURE_COOLDOWN_MINUTES", "15")
    )
    current_price_intervals = {
        "critical": int(
            os.getenv("MARKET_CURRENT_PRICE_CRITICAL_INTERVAL_SECONDS", "30")
        ),
        "high": int(
            os.getenv("MARKET_CURRENT_PRICE_HIGH_INTERVAL_SECONDS", "60")
        ),
        "normal": int(
            os.getenv("MARKET_CURRENT_PRICE_NORMAL_INTERVAL_SECONDS", "180")
        ),
        "low": int(
            os.getenv("MARKET_CURRENT_PRICE_LOW_INTERVAL_SECONDS", "300")
        ),
    }
    current_price_max_codes_per_cycle = int(
        os.getenv("MARKET_CURRENT_PRICE_MAX_CODES_PER_CYCLE", "12")
    )
    current_price_call_spacing_seconds = float(
        os.getenv("MARKET_CURRENT_PRICE_CALL_SPACING_SECONDS", "0.25")
    )
    current_price_pre_entry_max_age_minutes = int(
        os.getenv("MARKET_CURRENT_PRICE_PRE_ENTRY_MAX_AGE_MINUTES", "10")
    )
    if max_price_age_seconds < 1:
        raise ValueError("MARKET_MAX_PRICE_AGE_SECONDS는 1 이상이어야 합니다.")
    if detail_collection_interval_minutes < 1:
        raise ValueError(
            "MARKET_DETAIL_COLLECTION_INTERVAL_MINUTES는 1 이상이어야 합니다."
        )
    if detail_collection_max_codes < 0:
        raise ValueError("MARKET_DETAIL_MAX_CODES는 0 이상이어야 합니다.")
    if detail_failure_cooldown_minutes < 0:
        raise ValueError(
            "MARKET_DETAIL_FAILURE_COOLDOWN_MINUTES는 0 이상이어야 합니다."
        )
    if any(value < 0 for value in current_price_intervals.values()):
        raise ValueError("현재가 우선순위별 갱신 간격은 0 이상이어야 합니다.")
    if current_price_max_codes_per_cycle < 1:
        raise ValueError("MARKET_CURRENT_PRICE_MAX_CODES_PER_CYCLE은 1 이상이어야 합니다.")
    if current_price_call_spacing_seconds < 0:
        raise ValueError("MARKET_CURRENT_PRICE_CALL_SPACING_SECONDS는 0 이상이어야 합니다.")
    if current_price_pre_entry_max_age_minutes < 1:
        raise ValueError(
            "MARKET_CURRENT_PRICE_PRE_ENTRY_MAX_AGE_MINUTES는 1 이상이어야 합니다."
        )
    return MarketDataSettings(
        max_price_age_seconds=max_price_age_seconds,
        current_price_critical_interval_seconds=current_price_intervals["critical"],
        current_price_high_interval_seconds=current_price_intervals["high"],
        current_price_normal_interval_seconds=current_price_intervals["normal"],
        current_price_low_interval_seconds=current_price_intervals["low"],
        current_price_max_codes_per_cycle=current_price_max_codes_per_cycle,
        current_price_call_spacing_seconds=current_price_call_spacing_seconds,
        current_price_pre_entry_max_age_minutes=(
            current_price_pre_entry_max_age_minutes
        ),
        detail_collection_interval_minutes=detail_collection_interval_minutes,
        detail_collection_max_codes=detail_collection_max_codes,
        detail_failure_cooldown_minutes=detail_failure_cooldown_minutes,
        detail_abort_on_rate_limit=_read_bool("MARKET_DETAIL_ABORT_ON_RATE_LIMIT", True),
    )


def load_operations_alert_settings() -> OperationsAlertSettings:
    collection_delay_seconds = int(
        os.getenv("OPS_ALERT_COLLECTION_DELAY_SECONDS", "180")
    )
    consecutive_failures = int(
        os.getenv("OPS_ALERT_CONSECUTIVE_FAILURES", "3")
    )
    if collection_delay_seconds < 1:
        raise ValueError("OPS_ALERT_COLLECTION_DELAY_SECONDS는 1 이상이어야 합니다.")
    if consecutive_failures < 1:
        raise ValueError("OPS_ALERT_CONSECUTIVE_FAILURES는 1 이상이어야 합니다.")
    return OperationsAlertSettings(
        collection_delay_seconds=collection_delay_seconds,
        consecutive_failures=consecutive_failures,
    )


def load_telegram_settings() -> TelegramSettings:
    bot_token = _read_env_or_user("TELEGRAM_BOT_TOKEN")
    chat_id = _read_env_or_user("TELEGRAM_CHAT_ID")
    enabled = _read_bool("TELEGRAM_ENABLED", True)
    request_timeout = float(os.getenv("TELEGRAM_REQUEST_TIMEOUT", "10"))
    if request_timeout <= 0:
        raise ValueError("TELEGRAM_REQUEST_TIMEOUT은 0보다 커야 합니다.")
    return TelegramSettings(
        enabled=enabled,
        bot_token=bot_token,
        chat_id=chat_id,
        request_timeout=request_timeout,
    )
