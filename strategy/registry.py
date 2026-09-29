from dataclasses import asdict
from typing import Any

from config.settings import (
    CompositeScoreSettings,
    PaperTradingSettings,
    RiskSettings,
    StrategySettings,
)
from db.database import Database


BREAKOUT_STRATEGY_NAME = "breakout"
BREAKOUT_STRATEGY_VERSION = "1.9.0"
RISK_STRATEGY_NAME = "risk"
RISK_STRATEGY_VERSION = "1.1.0"
COMPOSITE_STRATEGY_NAME = "composite"
COMPOSITE_STRATEGY_VERSION = "1.0.0"


def register_breakout_strategy(
    database: Database,
    strategy_settings: StrategySettings,
    paper_settings: PaperTradingSettings,
    risk_settings: RiskSettings,
) -> tuple[int, int]:
    config: dict[str, Any] = {
        "strategy": asdict(strategy_settings),
        "paper": asdict(paper_settings),
        "risk": asdict(risk_settings),
    }
    version_id, config_id = database.register_strategy(
        BREAKOUT_STRATEGY_NAME,
        BREAKOUT_STRATEGY_VERSION,
        config,
        description=(
            "예상거래량·500억 유동성·VWAP·돌파·장초반 ORB·눌림목·호가 수급과 "
            "후보 패턴·시장 국면별 playbook·통합 data confidence·시간대별 "
            "SessionProfile을 반영하고 진입 유형·매매 계획을 기록하는 단타 전략"
        ),
    )
    database.backfill_legacy_strategy_signals(
        strategy_version_id=version_id,
        strategy_config_id=config_id,
    )
    return version_id, config_id


def register_risk_strategy(
    database: Database,
    risk_settings: RiskSettings,
) -> tuple[int, int]:
    return database.register_strategy(
        RISK_STRATEGY_NAME,
        RISK_STRATEGY_VERSION,
        {"risk": asdict(risk_settings)},
        description="손절·익절·트레일링·장마감 청산 전략",
    )


def register_composite_strategy(
    database: Database,
    settings: CompositeScoreSettings,
) -> tuple[int, int]:
    return database.register_strategy(
        COMPOSITE_STRATEGY_NAME,
        COMPOSITE_STRATEGY_VERSION,
        {"composite": asdict(settings), "mode": "shadow"},
        description="기술·패턴·상대강도·유동성·뉴스·시장 종합 점수 관찰 전략",
    )
