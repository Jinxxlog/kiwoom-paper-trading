import argparse
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ai_shadow.service import AiShadowService
from backtest.engine import BacktestConfig, BreakoutBacktester
from backtest.comparison import BacktestComparisonService
from backtest.exporter import BacktestReportExporter
from backtest.report import BacktestReportService
from backtest.repository import BacktestRepository
from backtest.validation import BacktestDataValidator, DataValidationResult
from config.settings import (
    load_ai_shadow_settings,
    load_candidate_settings,
    load_database_path,
    load_information_settings,
    load_account_data_settings,
    load_market_mode_gate_settings,
    load_market_data_settings,
    load_operations_alert_settings,
    load_paper_trading_settings,
    load_risk_settings,
    load_settings,
    load_strategy_settings,
)
from data.account_collector import AccountDataCollector
from data.api_budget import ApiBudgetManager, ApiCallPriority
from data.collector import MarketDataCollector
from data.candle_freshness import CandleFreshness, CandleRefreshService
from data.market_calendar import MarketCalendar
from data.scheduler import (
    SEOUL_TIMEZONE,
    PeriodicCollector,
    ProcessLock,
)
from data.watchlist import WatchlistService, normalize_stock_code
from data.universe_collection import UniverseDailyCollector
from data.universe_sync import UniverseSyncService
from db.database import Database
from execution.paper import PaperOrderExecutor
from kiwoom_rest_client import KiwoomRestClient
from information.service import InformationService
from paper.engine import PaperTradingEngine, PaperTradingError
from operations.alerts import OperationsAlertService
from operations.approval import PaperOperationApprovalService
from operations.daily_check import DailyOperationsChecker
from operations.daily_report import DailyOperationsReport
from operations.market_mode import MarketModeService
from operations.replay_outcomes import ReplayOutcomeService
from operations.runtime_control import CollectorRuntimeControl
from operations.universe_runtime import UniverseCollectionRuntime
from operations.service import CollectorOperationsTracker, OperationsService
from notifications.telegram import TelegramNotifier
from reports.trading_review import TradingReviewReport
from risk.manager import RiskManager
from screening.engine import CandidateScreeningEngine, StockUniverseService
from screening.intraday import IntradayVwapService
from screening.leader import LeaderStockScoringEngine
from screening.leader_learning import (
    LeaderOutcomeLearningEngine,
    LeaderOutcomeReportService,
)
from screening.market_product import is_market_product_name
from screening.performance import PatternPerformanceService
from screening.supply import SupplyScoringEngine
from strategy.breakout import BreakoutStrategy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="키움 시세 수집기")
    subparsers = parser.add_subparsers(dest="command", required=True)

    watchlist = subparsers.add_parser("watchlist", help="관심 종목 관리")
    watchlist_commands = watchlist.add_subparsers(
        dest="watchlist_command",
        required=True,
    )
    add = watchlist_commands.add_parser("add", help="관심 종목 추가")
    add.add_argument("stock_code")
    add.add_argument("--name", help="종목명")
    remove = watchlist_commands.add_parser("remove", help="관심 종목 비활성화")
    remove.add_argument("stock_code")
    list_command = watchlist_commands.add_parser("list", help="관심 종목 조회")
    list_command.add_argument(
        "--all",
        action="store_true",
        help="비활성화된 종목도 표시",
    )

    universe = subparsers.add_parser("universe", help="전체 종목 유니버스 관리")
    universe_commands = universe.add_subparsers(
        dest="universe_command",
        required=True,
    )
    universe_add = universe_commands.add_parser("add", help="유니버스 종목 등록")
    universe_add.add_argument("stock_code")
    universe_add.add_argument("--name")
    universe_add.add_argument("--market")
    universe_add.add_argument("--sector")
    universe_add.add_argument("--not-tradable", action="store_true")
    universe_add.add_argument("--management", action="store_true")
    universe_add.add_argument("--halted", action="store_true")
    universe_add.add_argument("--overheated", action="store_true")
    universe_import = universe_commands.add_parser(
        "import",
        help="UTF-8 CSV 종목 유니버스 가져오기",
    )
    universe_import.add_argument("path", type=Path)
    universe_commands.add_parser("list", help="유니버스와 일봉 보유 현황")
    universe_commands.add_parser("sync", help="KIND 코스피·코스닥 종목 자동 등록")
    universe_collect = universe_commands.add_parser(
        "collect",
        help="유니버스 과거 일봉 일괄 수집",
    )
    universe_collect.add_argument("--target-bars", type=int, default=200)
    universe_collect.add_argument("--pages", type=int, default=1)
    universe_collect.add_argument("--limit", type=int)
    universe_collect.add_argument("--delay", type=float, default=0.5)
    universe_complete = universe_commands.add_parser(
        "complete",
        help="미수집·부족 종목을 저속 배치로 끝까지 수집 후 후보 재계산",
    )
    universe_complete.add_argument("--target-bars", type=int, default=200)
    universe_complete.add_argument("--pages", type=int, default=3)
    universe_complete.add_argument("--batch-size", type=int, default=10)
    universe_complete.add_argument("--delay", type=float, default=1.0)
    universe_complete.add_argument("--round-delay", type=float, default=10.0)
    universe_complete.add_argument("--max-rounds", type=int, default=0)
    universe_complete.add_argument("--no-scan", action="store_true")
    universe_complete.add_argument("--sync-first", action="store_true", help=argparse.SUPPRESS)
    universe_sync_collect = universe_commands.add_parser(
        "sync-collect",
        help=argparse.SUPPRESS,
    )
    universe_sync_collect.add_argument("--target-bars", type=int, default=200)
    universe_sync_collect.add_argument("--pages", type=int, default=1)
    universe_sync_collect.add_argument("--limit", type=int)
    universe_sync_collect.add_argument("--delay", type=float, default=0.5)

    candidates = subparsers.add_parser(
        "candidates",
        help="기술 특징·패턴 기반 후보 검색",
    )
    candidate_commands = candidates.add_subparsers(
        dest="candidate_command",
        required=True,
    )
    candidate_scan = candidate_commands.add_parser("scan", help="후보 점수 계산")
    candidate_scan.add_argument("--as-of", help="평가 기준일 YYYY-MM-DD")
    candidate_scan.add_argument("--limit", type=int)
    candidate_scan.add_argument(
        "--all",
        action="store_true",
        help="선정 후보뿐 아니라 제외 종목도 출력",
    )
    candidate_history = candidate_commands.add_parser(
        "history",
        help="최근 후보 점수 조회",
    )
    candidate_history.add_argument("--all", action="store_true")
    candidate_themes = candidate_commands.add_parser(
        "collect-themes",
        help="키움 테마 그룹과 구성종목 수집",
    )
    candidate_themes.add_argument("--top", type=int, default=10)
    candidate_themes.add_argument("--date-type", default="10")
    candidate_themes.add_argument("--component-date-type", default="2")
    candidate_themes.add_argument("--max-pages", type=int, default=1)
    candidate_stock_themes = candidate_commands.add_parser(
        "collect-stock-themes",
        help="ka90001 종목검색으로 특정 종목의 소속 테마 수집",
    )
    candidate_stock_themes.add_argument("stock_codes", nargs="+")
    candidate_stock_themes.add_argument("--date-type", default="10")
    candidate_stock_themes.add_argument("--component-date-type", default="10")
    candidate_stock_themes.add_argument("--max-pages", type=int, default=1)
    candidate_stock_themes.add_argument(
        "--no-components",
        action="store_true",
        help="ka90002 구성종목 확인은 생략",
    )
    candidate_ranks = candidate_commands.add_parser(
        "collect-realtime-ranks",
        help="ka00198 실시간 조회종목 순위 수집",
    )
    candidate_ranks.add_argument("--query-type", default="1")
    candidate_ranks.add_argument("--max-pages", type=int, default=1)
    candidate_market_ranks = candidate_commands.add_parser(
        "collect-market-ranks",
        help="0198/0183/0187/0181 시장 순위 공통 수집",
    )
    candidate_market_ranks.add_argument(
        "--types",
        nargs="+",
        choices=[
            "realtime_inquiry",
            "expected_change",
            "trade_amount",
            "change_rate",
        ],
        default=[
            "realtime_inquiry",
            "expected_change",
            "trade_amount",
            "change_rate",
        ],
        help="수집할 순위 유형",
    )
    candidate_market_ranks.add_argument("--market-type", default="000")
    candidate_market_ranks.add_argument("--max-pages", type=int, default=1)
    candidate_leader_score = candidate_commands.add_parser(
        "leader-score",
        help="대장주 후보 점수 계산",
    )
    candidate_leader_score.add_argument("--date", help="평가 기준일 YYYY-MM-DD")
    candidate_leader_score.add_argument("--limit", type=int, default=50)
    candidate_leader_outcomes = candidate_commands.add_parser(
        "leader-outcomes",
        help="대장주 TOP 후보 당일/익일 성과 학습",
    )
    candidate_leader_outcomes.add_argument("--date", help="평가 기준일 YYYY-MM-DD")
    candidate_leader_outcomes.add_argument("--limit", type=int, default=20)
    candidate_leader_report = candidate_commands.add_parser(
        "leader-report",
        help="대장주 학습 리포트 집계",
    )
    candidate_leader_report.add_argument("--date", help="평가 기준일 YYYY-MM-DD")
    candidate_leader_report.add_argument("--days", type=int, default=30)
    candidate_leader_report.add_argument("--limit", type=int, default=10)
    candidate_supply = candidate_commands.add_parser(
        "collect-supply",
        help="거래원·외국계·투자자별 수급 원천 수집",
    )
    candidate_supply.add_argument("stock_codes", nargs="+")
    candidate_supply.add_argument("--start-date", help="투자자 합계 시작일 YYYYMMDD")
    candidate_supply.add_argument("--end-date", help="투자자 합계 종료일 YYYYMMDD")
    candidate_supply.add_argument("--market-type", default="000")
    candidate_supply.add_argument("--max-pages", type=int, default=1)
    candidate_supply.add_argument(
        "--skip-foreign-rank",
        action="store_true",
        help="ka10037 외국계 창구 상위 수집 생략",
    )
    candidate_supply_score = candidate_commands.add_parser(
        "supply-score",
        help="저장된 수급 원천으로 관측 점수 계산",
    )
    candidate_supply_score.add_argument("stock_codes", nargs="*")
    candidate_theme_journal = candidate_commands.add_parser(
        "theme-journal",
        help="날짜별 강한 테마와 구성 종목 수동 기록",
    )
    candidate_theme_journal.add_argument("--date", required=True)
    candidate_theme_journal.add_argument("--theme", required=True)
    candidate_theme_journal.add_argument(
        "--strength",
        choices=["strong", "medium", "weak", "none"],
        default="strong",
    )
    candidate_theme_journal.add_argument("--notes")
    candidate_theme_journal.add_argument(
        "--member",
        action="append",
        default=[],
        help="종목코드:역할[:종목명] 형식. 역할은 leader,second,follower,watch,blocked",
    )
    candidate_performance = candidate_commands.add_parser(
        "performance",
        help="패턴별 전방 수익률과 기여도 평가",
    )
    candidate_performance.add_argument("--start", required=True)
    candidate_performance.add_argument("--end", required=True)
    candidate_performance.add_argument("--horizon", type=int, default=5)

    information = subparsers.add_parser(
        "information",
        help="뉴스·공시 수집과 점수",
    )
    information_commands = information.add_subparsers(
        dest="information_command",
        required=True,
    )
    information_commands.add_parser(
        "sync-companies",
        help="OpenDART 기업 고유번호와 종목코드 동기화",
    )
    dart_collect = information_commands.add_parser(
        "collect-dart",
        help="OpenDART 공시 수집",
    )
    dart_collect.add_argument("stock_codes", nargs="*")
    dart_collect.add_argument("--start")
    dart_collect.add_argument("--end")
    naver_collect = information_commands.add_parser(
        "collect-naver",
        help="네이버 뉴스 수집",
    )
    naver_collect.add_argument("stock_codes", nargs="*")
    naver_collect.add_argument("--display", type=int, default=20)
    information_score = information_commands.add_parser(
        "score",
        help="기준시각 뉴스·공시 점수 계산",
    )
    information_score.add_argument("stock_codes", nargs="*")
    information_score.add_argument("--as-of")
    information_recent = information_commands.add_parser(
        "recent",
        help="최근 뉴스·공시 조회",
    )
    information_recent.add_argument("--limit", type=int, default=30)

    ai = subparsers.add_parser("ai", help="GPT·Claude shadow 분석")
    ai_commands = ai.add_subparsers(dest="ai_command", required=True)
    ai_analyze = ai_commands.add_parser(
        "analyze",
        help="최근 상위 후보 AI 분석",
    )
    ai_analyze.add_argument("--limit", type=int)
    ai_recent = ai_commands.add_parser("recent", help="최근 AI 판단 조회")
    ai_recent.add_argument("--limit", type=int, default=30)
    ai_evaluate = ai_commands.add_parser(
        "evaluate",
        help="AI 판단 사후 성과 계산",
    )
    ai_evaluate.add_argument("--horizon", type=int, default=5)

    replay = subparsers.add_parser(
        "replay",
        help="전략 판단 전방성과 일괄 계산",
    )
    replay_commands = replay.add_subparsers(
        dest="replay_command",
        required=True,
    )
    replay_commands.add_parser(
        "outcomes-today",
        help="한국시간 오늘 판단의 30/60/120분 전방성과 계산",
    )
    replay_date = replay_commands.add_parser(
        "outcomes-date",
        help="지정일 판단의 30/60/120분 전방성과 계산",
    )
    replay_date.add_argument("target_date", help="YYYY-MM-DD")

    collect = subparsers.add_parser("collect", help="현재가 1회 수집")
    collect.add_argument(
        "stock_codes",
        nargs="*",
        help="생략하면 활성 관심 종목 전체를 수집",
    )

    run = subparsers.add_parser("run", help="현재가 주기 수집")
    run.add_argument(
        "--interval",
        type=int,
        default=60,
        help="수집 간격(초, 기본값: 60)",
    )
    run.add_argument(
        "--all-hours",
        action="store_true",
        help="정규장 시간 외에도 수집",
    )
    run.add_argument(
        "--auto-strategy",
        action="store_true",
        help="수집 후 기본 전략 신호로 모의 자동 매수",
    )
    run.add_argument(
        "--account",
        default="default",
        help="자동 전략을 실행할 모의계좌",
    )
    run.add_argument(
        "--with-depth",
        action="store_true",
        help="현재가 수집 후 호가 10단계도 저장",
    )
    run.add_argument(
        "--with-trades",
        action="store_true",
        help="현재가 수집 후 체결강도 이력도 저장",
    )
    run.add_argument(
        "--trade-pages",
        type=int,
        default=1,
        help="체결강도 최대 연속조회 페이지 수",
    )
    run.add_argument(
        "--unattended",
        action="store_true",
        help="후보 종목으로 운전하고 15:35 자동 종료·리포트 생성",
    )
    run.add_argument(
        "--preopen-scan",
        action="store_true",
        help="08:50~09:00 장전 후보·뉴스·공시를 갱신하고 주문 없이 대기",
    )
    run.add_argument(
        "--candidate-limit",
        type=int,
        default=20,
        help="무인 운전 시 장중 수집할 상위 후보 수",
    )

    dashboard = subparsers.add_parser(
        "dashboard",
        help="로컬 웹 운영 대시보드 실행",
    )
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument(
        "--no-browser",
        action="store_true",
        help="시작할 때 웹 브라우저를 자동으로 열지 않음",
    )

    candles = subparsers.add_parser("candles", help="일봉·분봉 수집")
    candle_commands = candles.add_subparsers(
        dest="candle_command",
        required=True,
    )
    daily = candle_commands.add_parser("daily", help="일봉 수집")
    daily.add_argument("stock_codes", nargs="*")
    daily.add_argument("--base-date", help="기준일자 YYYYMMDD, 기본값: 오늘")
    daily.add_argument("--pages", type=int, default=1, help="최대 연속조회 페이지 수")

    minute = candle_commands.add_parser("minute", help="분봉 수집")
    minute.add_argument("stock_codes", nargs="*")
    minute.add_argument(
        "--interval",
        type=int,
        choices=[1, 3, 5, 10, 15, 30, 45, 60],
        default=5,
        help="분봉 간격(기본값: 5)",
    )
    minute.add_argument("--base-date", help="기준일자 YYYYMMDD, 기본값: 오늘")
    minute.add_argument("--pages", type=int, default=1, help="최대 연속조회 페이지 수")

    market = subparsers.add_parser("market", help="호가·체결강도 수집")
    market_commands = market.add_subparsers(dest="market_command", required=True)
    depth = market_commands.add_parser("depth", help="호가 10단계 스냅샷 저장")
    depth.add_argument("stock_codes", nargs="*")
    trades = market_commands.add_parser("trades", help="체결강도 이력 저장")
    trades.add_argument("stock_codes", nargs="*")
    trades.add_argument("--pages", type=int, default=1, help="최대 연속조회 페이지 수")
    market_mode = market_commands.add_parser(
        "mode",
        help="코스닥 지수·거래대금 기반 자동 시장 모드 조회/수집",
    )
    market_mode_sub = market_mode.add_subparsers(
        dest="market_mode_command",
        required=True,
    )
    market_mode_collect = market_mode_sub.add_parser(
        "collect",
        help="코스닥 시장 모드 스냅샷 수집",
    )
    market_mode_collect.add_argument("--base-date", help="기준일자 YYYYMMDD")
    market_mode_collect.add_argument("--pages", type=int, default=1)
    market_mode_sub.add_parser("status", help="최신 코스닥 시장 모드 조회")

    account = subparsers.add_parser("account", help="실계좌 조회 데이터 저장")
    account_commands = account.add_subparsers(
        dest="account_command",
        required=True,
    )
    account_sync = account_commands.add_parser(
        "sync",
        help="잔고·보유 종목·주문 체결 내역 1회 저장",
    )
    account_sync.add_argument("--date", default="", help="주문일자 YYYYMMDD")
    account_sync.add_argument("--pages", type=int, default=1)
    account_run = account_commands.add_parser(
        "run",
        help="실계좌 조회 데이터를 주기적으로 저장",
    )
    account_run.add_argument("--interval", type=int, default=300)
    account_run.add_argument("--pages", type=int, default=1)
    account_run.add_argument(
        "--all-hours",
        action="store_true",
        help="정규장 시간이 아니어도 저장",
    )
    account_prune = account_commands.add_parser(
        "prune",
        help="보존 기간이 지난 민감 계좌 데이터 삭제",
    )
    account_prune.add_argument("--days", type=int)

    paper = subparsers.add_parser("paper", help="모의투자")
    paper_commands = paper.add_subparsers(dest="paper_command", required=True)

    paper_init = paper_commands.add_parser("init", help="모의계좌 생성")
    paper_init.add_argument("--account", default="default", help="계좌 이름")
    paper_init.add_argument("--cash", type=int, help="초기 자금(원)")

    paper_account = paper_commands.add_parser("account", help="계좌와 포지션 조회")
    paper_account.add_argument("--account", default="default", help="계좌 이름")

    for command, help_text in (("buy", "시장가 모의 매수"), ("sell", "시장가 모의 매도")):
        order = paper_commands.add_parser(command, help=help_text)
        order.add_argument("stock_code")
        order.add_argument("quantity", type=int)
        order.add_argument("--account", default="default", help="계좌 이름")
        order.add_argument("--reason", help="매매 이유")
        order.add_argument("--client-order-id", help="중복 주문 방지 ID")

    paper_orders = paper_commands.add_parser("orders", help="주문 내역 조회")
    paper_orders.add_argument("--account", default="default", help="계좌 이름")
    paper_orders.add_argument("--limit", type=int, default=20, help="조회 건수")

    paper_fills = paper_commands.add_parser("fills", help="체결 내역 조회")
    paper_fills.add_argument("--account", default="default", help="계좌 이름")
    paper_fills.add_argument("--limit", type=int, default=20, help="조회 건수")

    paper_review_today = paper_commands.add_parser(
        "review-today",
        help="오늘 모의투자 복기 리포트 생성",
    )
    paper_review_today.add_argument("--output", type=Path, help="JSON 저장 경로")
    paper_review_today.add_argument(
        "--no-export",
        action="store_true",
        help="JSON 파일 저장 없이 콘솔 요약만 출력",
    )

    paper_review_date = paper_commands.add_parser(
        "review-date",
        help="지정일 모의투자 복기 리포트 생성",
    )
    paper_review_date.add_argument("date", help="복기 대상일 YYYY-MM-DD")
    paper_review_date.add_argument("--output", type=Path, help="JSON 저장 경로")
    paper_review_date.add_argument(
        "--no-export",
        action="store_true",
        help="JSON 파일 저장 없이 콘솔 요약만 출력",
    )

    risk = subparsers.add_parser("risk", help="모의투자 리스크 관리")
    risk_commands = risk.add_subparsers(dest="risk_command", required=True)

    risk_status = risk_commands.add_parser("status", help="포지션 리스크 상태 조회")
    risk_status.add_argument("--account", default="default", help="계좌 이름")

    risk_set = risk_commands.add_parser("set", help="포지션 리스크 비율 설정")
    risk_set.add_argument("stock_code")
    risk_set.add_argument("--account", default="default", help="계좌 이름")
    risk_set.add_argument("--stop-loss", type=Decimal, help="손절률, 예: 0.02")
    risk_set.add_argument("--take-profit", type=Decimal, help="익절률, 예: 0.04")
    risk_set.add_argument(
        "--trailing-activation",
        type=Decimal,
        help="트레일링 활성 수익률, 예: 0.03",
    )
    risk_set.add_argument(
        "--trailing-drawdown",
        type=Decimal,
        help="고점 대비 하락률, 예: 0.015",
    )

    risk_check = risk_commands.add_parser("check", help="리스크 평가 및 자동 매도")
    risk_check.add_argument("--account", help="생략하면 모든 모의계좌 평가")

    risk_events = risk_commands.add_parser("events", help="리스크 이벤트 조회")
    risk_events.add_argument("--account", default="default", help="계좌 이름")
    risk_events.add_argument("--limit", type=int, default=20, help="조회 건수")

    risk_close = risk_commands.add_parser("close", help="보유 포지션 전량 청산")
    risk_close.add_argument("--account", help="생략하면 모든 모의계좌")
    risk_close.add_argument(
        "--force",
        action="store_true",
        help="15:20 이전에도 강제 청산",
    )

    strategy = subparsers.add_parser("strategy", help="기본 돌파 전략")
    strategy_commands = strategy.add_subparsers(
        dest="strategy_command",
        required=True,
    )
    for command, help_text in (
        ("check", "전략 신호만 평가"),
        ("run", "전략 신호 평가 후 모의 매수"),
    ):
        strategy_action = strategy_commands.add_parser(command, help=help_text)
        strategy_action.add_argument("stock_codes", nargs="*")
        strategy_action.add_argument(
            "--account",
            default="default",
            help="모의계좌 이름",
        )
    strategy_history = strategy_commands.add_parser(
        "history",
        help="전략 평가 이력",
    )
    strategy_history.add_argument("--account", default="default", help="모의계좌 이름")
    strategy_history.add_argument("--limit", type=int, default=20, help="조회 건수")

    backtest = subparsers.add_parser("backtest", help="저장된 분봉 기반 백테스트")
    backtest_commands = backtest.add_subparsers(
        dest="backtest_command",
        required=True,
    )
    backtest_run = backtest_commands.add_parser("run", help="기본 돌파 전략 재생")
    backtest_run.add_argument("stock_codes", nargs="*")
    backtest_run.add_argument("--start", required=True, help="시작일 YYYY-MM-DD")
    backtest_run.add_argument("--end", required=True, help="종료일 YYYY-MM-DD")
    backtest_run.add_argument("--cash", type=int, help="초기자금")
    backtest_run.add_argument(
        "--interval",
        type=int,
        default=5,
        help="분봉 간격(기본값: 5)",
    )
    backtest_run.add_argument(
        "--trades",
        action="store_true",
        help="개별 거래 결과도 출력",
    )
    backtest_history = backtest_commands.add_parser(
        "history",
        help="저장된 백테스트 실행 이력",
    )
    backtest_history.add_argument("--limit", type=int, default=20)
    backtest_show = backtest_commands.add_parser(
        "show",
        help="저장된 백테스트 상세 조회",
    )
    backtest_show.add_argument("run_id", type=int)
    backtest_show.add_argument(
        "--trades",
        action="store_true",
        help="저장된 거래 내역 출력",
    )
    backtest_show.add_argument(
        "--equity",
        type=int,
        metavar="LIMIT",
        help="자산곡선 앞부분을 지정 건수만큼 출력",
    )
    backtest_report = backtest_commands.add_parser(
        "report",
        help="저장된 백테스트 상세 성과 집계",
    )
    backtest_report.add_argument("run_id", type=int)
    backtest_export = backtest_commands.add_parser(
        "export",
        help="저장된 백테스트 리포트 파일 내보내기",
    )
    backtest_export.add_argument("run_id", type=int)
    backtest_export.add_argument(
        "--format",
        choices=["json", "csv"],
        required=True,
        dest="export_format",
    )
    backtest_export.add_argument(
        "--output",
        type=Path,
        help="JSON 파일 경로 또는 CSV 출력 디렉터리",
    )
    backtest_compare = backtest_commands.add_parser(
        "compare",
        help="저장된 백테스트 실행 간 성과 비교",
    )
    backtest_compare.add_argument("run_ids", nargs="+", type=int)
    backtest_validate = backtest_commands.add_parser(
        "validate",
        help="백테스트 캔들 데이터 충분성 검사",
    )
    backtest_validate.add_argument("stock_codes", nargs="*")
    backtest_validate.add_argument("--start", required=True, help="시작일 YYYY-MM-DD")
    backtest_validate.add_argument("--end", required=True, help="종료일 YYYY-MM-DD")
    backtest_validate.add_argument(
        "--interval",
        type=int,
        default=5,
        help="분봉 간격(기본값: 5)",
    )

    system = subparsers.add_parser("system", help="운영 상태와 이벤트")
    system_commands = system.add_subparsers(
        dest="system_command",
        required=True,
    )
    system_status = system_commands.add_parser(
        "status",
        help="모듈 상태와 최근 실행 조회",
    )
    system_status.add_argument("--limit", type=int, default=10)
    system_events = system_commands.add_parser(
        "events",
        help="운영 이벤트 조회",
    )
    system_events.add_argument("--limit", type=int, default=50)
    system_events.add_argument("--module")
    system_events.add_argument(
        "--severity",
        choices=["info", "warning", "error"],
    )
    system_health = system_commands.add_parser(
        "health",
        help="운영 모듈 건강 상태 확인",
    )
    system_health.add_argument(
        "--stale-minutes",
        type=int,
        default=10,
        help="마지막 성공 시각의 신선도 기준",
    )
    system_recover = system_commands.add_parser(
        "recover",
        help="중단된 실행과 오래된 잠금 복구",
    )
    system_recover.add_argument(
        "--stale-minutes",
        type=int,
        default=10,
        help="실행 중 상태를 중단으로 판단할 기준",
    )
    system_prices = system_commands.add_parser(
        "prices",
        help="관심종목 최신 가격 신선도 확인",
    )
    system_prices.add_argument(
        "--max-age-seconds",
        type=int,
        default=None,
        help="오래된 가격 판단 기준, 기본값은 환경설정",
    )
    system_daily_check = system_commands.add_parser(
        "daily-check",
        help="장 시작부터 마감까지 일일 모의운전 검증",
    )
    system_daily_check.add_argument(
        "--date",
        help="검증일 YYYY-MM-DD, 기본값은 한국시간 오늘",
    )
    system_daily_check.add_argument(
        "--interval",
        type=int,
        help="예상 수집 간격(초), 생략하면 실행 설정에서 추론",
    )
    system_daily_check.add_argument(
        "--fail-on-error",
        action="store_true",
        help="검증 실패 시 종료 코드 1 반환",
    )
    system_daily_report = system_commands.add_parser(
        "daily-report",
        help="운영 이벤트 기반 일일 요약 리포트",
    )
    system_daily_report.add_argument(
        "--date",
        help="리포트 날짜 YYYY-MM-DD, 기본값은 한국시간 오늘",
    )
    system_daily_report.add_argument(
        "--interval",
        type=int,
        help="검증용 예상 수집 간격(초)",
    )
    system_daily_report.add_argument(
        "--json",
        action="store_true",
        help="기본 reports 경로에 JSON 저장",
    )
    system_daily_report.add_argument(
        "--output",
        type=Path,
        help="JSON 출력 파일 경로",
    )
    system_alerts = system_commands.add_parser(
        "alerts",
        help="수집 지연·연속 실패 경보 평가와 조회",
    )
    system_alerts.add_argument(
        "--interval",
        type=int,
        default=60,
        help="예상 주기 수집 간격(초)",
    )
    system_alerts.add_argument(
        "--delay-seconds",
        type=int,
        help="수집 지연 경보 최소 기준(초)",
    )
    system_alerts.add_argument(
        "--consecutive",
        type=int,
        help="연속 실패 경보 기준 횟수",
    )
    system_alerts.add_argument(
        "--all",
        action="store_true",
        help="해제된 경보도 표시",
    )
    system_approval = system_commands.add_parser(
        "approval",
        help="실제 주문 전 모의 운전 승인 조건 검사",
    )
    system_approval.add_argument(
        "--days",
        type=int,
        default=5,
        help="필수 통과 거래일 수",
    )
    system_approval.add_argument(
        "--interval",
        type=int,
        help="일일 검증용 예상 수집 간격(초)",
    )
    system_approval.add_argument(
        "--round-trips",
        type=int,
        default=1,
        help="필수 자동 매수·매도 전체 경로 횟수",
    )
    system_approval.add_argument(
        "--json",
        action="store_true",
        help="기본 reports 경로에 JSON 증빙 저장",
    )
    system_approval.add_argument(
        "--output",
        type=Path,
        help="JSON 출력 파일 경로",
    )
    system_approval.add_argument(
        "--fail-on-reject",
        action="store_true",
        help="기술 승인 조건 미충족 시 종료 코드 1 반환",
    )

    calendar = subparsers.add_parser("calendar", help="거래일 예외 관리")
    calendar_commands = calendar.add_subparsers(
        dest="calendar_command",
        required=True,
    )
    calendar_check = calendar_commands.add_parser(
        "check",
        help="지정 날짜의 거래일 여부 확인",
    )
    calendar_check.add_argument(
        "market_date",
        nargs="?",
        help="YYYY-MM-DD, 생략하면 한국시간 오늘",
    )
    calendar_list = calendar_commands.add_parser(
        "list",
        help="연간 휴장·예외 목록",
    )
    calendar_list.add_argument(
        "--year",
        type=int,
        default=datetime.now(SEOUL_TIMEZONE).year,
    )
    calendar_set = calendar_commands.add_parser(
        "set",
        help="임시 휴장 또는 예외 개장 등록",
    )
    calendar_set.add_argument("market_date", help="YYYY-MM-DD")
    calendar_set.add_argument(
        "--status",
        choices=["open", "closed"],
        required=True,
    )
    calendar_set.add_argument("--name", help="예외 사유")
    calendar_set.add_argument("--source", default="manual")
    calendar_remove = calendar_commands.add_parser(
        "remove",
        help="수동 거래일 예외 삭제",
    )
    calendar_remove.add_argument("market_date", help="YYYY-MM-DD")
    return parser


def send_unattended_start_notification(
    notifier: TelegramNotifier,
    *,
    account: str,
    interval: int,
    candidate_limit: int,
) -> None:
    notifier.send(
        "\n".join(
            [
                "[무인 모의투자 시작]",
                f"계좌: {account}",
                f"수집 간격: {interval}초",
                f"후보 수: {candidate_limit}",
                "주문: SQLite 모의계좌만",
            ]
        )
    )


def send_daily_report_notification(
    notifier: TelegramNotifier,
    report_path: Path,
) -> None:
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        notifier.send(
            "\n".join(
                [
                    "[무인 모의투자 종료]",
                    f"보고서: {report_path}",
                    f"요약 실패: {exc}",
                ]
            )
        )
        return
    validation = payload.get("validation", {})
    collection = payload.get("collection", {}).get("summary", {})
    strategy = payload.get("strategy", {}).get("summary", {})
    forward = payload.get("forward_outcomes", {})
    trading = payload.get("trading", {})
    accounts = trading.get("accounts") or []
    account = accounts[0] if accounts else {}
    notifier.send(
        "\n".join(
            [
                "[무인 모의투자 종료]",
                f"날짜: {payload.get('report_date')}",
                f"검증: {validation.get('status')}",
                (
                    "수집: "
                    f"{collection.get('run_count', 0)}회 "
                    f"(부분실패 {collection.get('partial_runs', 0)}, "
                    f"실패 {collection.get('failed_runs', 0)})"
                ),
                (
                    "전략: "
                    f"평가 {strategy.get('signal_count', 0)}건, "
                    f"통과 {strategy.get('passed_count', 0)}건, "
                    f"실행 {strategy.get('executed_count', 0)}건"
                ),
                (
                    "전방성과: "
                    f"저장 {forward.get('saved', 0)}건, "
                    f"보류 {forward.get('pending', 0)}건, "
                    f"캔들부족 {forward.get('unavailable', 0)}건"
                ),
                (
                    "주문: "
                    f"{account.get('order_count', 0)}건, "
                    f"체결 {account.get('filled_orders', 0)}건, "
                    f"손익 {int(account.get('realized_pnl') or 0):,}원"
                ),
                f"보고서: {report_path}",
            ]
        )
    )


def create_database() -> Database:
    database = Database(load_database_path())
    database.initialize()
    return database


def create_market_collector(database: Database) -> MarketDataCollector:
    return MarketDataCollector(create_kiwoom_client(database), database)


def create_kiwoom_client(database: Database | None = None) -> KiwoomRestClient:
    settings = load_settings()
    return KiwoomRestClient(
        app_key=settings.app_key,
        secret_key=settings.secret_key,
        use_mock=settings.use_mock,
        token_cache=settings.token_cache_path,
        timeout=settings.request_timeout,
        initial_token=os.getenv("KIWOOM_BOOTSTRAP_TOKEN"),
        initial_expires_dt=os.getenv("KIWOOM_BOOTSTRAP_EXPIRES_DT"),
        api_budget_manager=ApiBudgetManager(database) if database is not None else None,
    )


def create_account_collector(database: Database) -> AccountDataCollector:
    return AccountDataCollector(create_kiwoom_client(database), database)


def create_periodic_collector(
    database: Database,
    stock_codes: Sequence[str] | None = None,
    *,
    auto_strategy: bool = False,
    account_name: str = "default",
    with_depth: bool = False,
    with_trades: bool = False,
    trade_pages: int = 1,
    operations_tracker: CollectorOperationsTracker | None = None,
    candidate_mode: bool = False,
    candidate_limit: int = 20,
    preopen_scan: bool = False,
) -> PeriodicCollector:
    collector = create_market_collector(database)
    watchlist = WatchlistService(database)
    risk_manager = create_risk_manager(database)
    if stock_codes is not None:
        base_provider = lambda: list(stock_codes)
    elif candidate_mode:
        base_provider = lambda: _candidate_collection_codes(
            database,
            candidate_limit,
        )
    else:
        base_provider = watchlist.active_codes
    current_price_priorities: dict[str, ApiCallPriority] = {}

    def current_price_provider() -> list[str]:
        settings = load_market_data_settings()
        plan = _current_price_collection_plan(
            database,
            base_provider(),
            current=datetime.now(SEOUL_TIMEZONE),
            settings=settings,
        )
        current_price_priorities.clear()
        current_price_priorities.update(
            {
                str(item["stock_code"]): item["priority"]
                for item in plan
            }
        )
        return [str(item["stock_code"]) for item in plan]
    strategy = create_breakout_strategy(database)
    intraday_vwap = IntradayVwapService(database)
    market_calendar = MarketCalendar(database)
    last_minute_bucket: str | None = None
    last_theme_bucket: str | None = None
    last_realtime_rank_bucket: str | None = None
    last_market_rank_bucket: str | None = None
    last_information_bucket: str | None = None
    last_detail_bucket: str | None = None
    last_supply_leader_bucket: str | None = None
    detail_cooldown_until: dict[str, datetime] = {}
    last_ai_bucket: str | None = None
    ai_lock = threading.Lock()

    def refresh_candidates_and_information(
        current: datetime,
        *,
        component: str,
        event_type: str,
        success_message: str,
        collect_themes: bool = False,
        collect_information: bool = True,
    ) -> None:
        candidate_settings = load_candidate_settings()
        details: dict[str, object] = {}
        if collect_themes:
            try:
                theme_result = collector.collect_theme_snapshot(
                    top_groups=candidate_settings.theme_collection_top_groups,
                )
                details["themes"] = {
                    "group_count": theme_result.group_count,
                    "saved_group_count": theme_result.saved_group_count,
                    "component_count": theme_result.component_count,
                    "saved_component_count": theme_result.saved_component_count,
                    "page_count": theme_result.page_count,
                }
            except Exception as exc:
                logging.exception("테마 주기 수집 실패")
                details["theme_error"] = str(exc)
                if operations_tracker is not None:
                    operations_tracker.record(
                        "theme",
                        "theme_collection_failed",
                        "warning",
                        str(exc),
                    )
        candidate_run = CandidateScreeningEngine(
            database,
            candidate_settings,
        ).scan(limit=candidate_limit)
        codes = _candidate_collection_codes(database, candidate_limit)
        details.update(
            {
                "candidate_run_id": candidate_run.run_id,
                "universe_count": candidate_run.universe_count,
                "eligible_count": candidate_run.eligible_count,
                "selected_count": candidate_run.selected_count,
                "stock_count": len(codes),
            }
        )
        info_settings = load_information_settings(required=False)
        if (
            collect_information
            and
            codes
            and info_settings.naver_client_id
            and info_settings.naver_client_secret
            and info_settings.dart_api_key
        ):
            information = InformationService(database, info_settings)
            naver_result = information.collect_naver(codes, display=10)
            dart_result = information.collect_dart(
                codes,
                begin_date=current.date(),
                end_date=current.date(),
            )
            refreshed_run = CandidateScreeningEngine(
                database,
                load_candidate_settings(),
            ).scan(limit=candidate_limit)
            details.update(
                {
                    "naver": naver_result,
                    "dart": dart_result,
                    "refreshed_run_id": refreshed_run.run_id,
                    "refreshed_selected_count": refreshed_run.selected_count,
                }
            )
        else:
            details["information_skipped"] = True
        if operations_tracker is not None:
            operations_tracker.record(
                component,
                event_type,
                "info",
                success_message,
                details=details,
            )

    def pre_market_scan() -> None:
        # Historical bootstrap's completed flag must not suppress daily updates.
        refresh = CandleRefreshService(database, collector)
        for code in base_provider():
            try:
                result = refresh.daily(code, datetime.now(SEOUL_TIMEZONE))
                if not result["ready"]:
                    logging.warning("%s 장전 일봉 최신성 부족: %s", code, result)
                if result.get("action") == "refreshed":
                    time.sleep(0.7)
            except Exception:
                logging.exception("%s 장전 일봉 갱신 실패; 최신성 게이트로 신규 진입 제외", code)
        refresh_candidates_and_information(
            datetime.now(SEOUL_TIMEZONE),
            component="preopen",
            event_type="preopen_candidate_scan_completed",
            success_message="장전 테마·후보·뉴스·공시 갱신 성공",
            collect_themes=True,
            collect_information=True,
        )

    def after_collection() -> None:
        nonlocal last_minute_bucket, last_theme_bucket, last_realtime_rank_bucket
        nonlocal last_market_rank_bucket, last_supply_leader_bucket
        nonlocal last_information_bucket, last_detail_bucket, last_ai_bucket
        codes = list(base_provider())
        current = datetime.now(SEOUL_TIMEZONE)
        market_data_settings = load_market_data_settings()
        candidate_settings = load_candidate_settings()
        minute_bucket = (
            f"{current:%Y%m%d%H}{current.minute // 5:02d}"
        )
        if (candidate_mode or auto_strategy) and minute_bucket != last_minute_bucket:
            minute_plan = _minute_collection_codes(
                database,
                codes,
                max_codes=max(
                    1,
                    min(
                        len(codes),
                        max(
                            5,
                            int(
                                getattr(
                                    market_data_settings,
                                    "detail_collection_max_codes",
                                    5,
                                )
                            )
                            * 2,
                        ),
                    ),
                ),
            )
            for item in minute_plan:
                stock_code = str(item["stock_code"])
                # A daily refresh failure must not prevent held-position minute updates.
                try:
                    # New intraday candidates may not have been in the preopen set.
                    daily_result = CandleRefreshService(database, collector).daily(
                        stock_code, current)
                    if not daily_result["ready"]:
                        logging.warning("%s 일봉 최신성 부족: %s", stock_code, daily_result)
                except Exception:
                    logging.exception("%s 일봉 갱신 실패; 분봉 수집은 계속", stock_code)
                try:
                    _collect_minute_candles_with_priority(
                        collector,
                        stock_code,
                        current.strftime("%Y%m%d"),
                        load_strategy_settings().minute_interval,
                        max_pages=1,
                        priority=_current_price_priority(database, stock_code),
                    )
                    intraday_vwap.evaluate(
                        stock_code,
                        interval_minutes=load_strategy_settings().minute_interval,
                    )
                except Exception as exc:
                    logging.exception("%s 분봉 수집 실패", stock_code)
                    if operations_tracker is not None:
                        operations_tracker.record(
                            "market_detail",
                            "minute_candle_failed",
                            "error",
                            str(exc),
                            stock_code=stock_code,
                    )
            last_minute_bucket = minute_bucket
        theme_interval = candidate_settings.theme_collection_interval_minutes
        theme_bucket = (
            f"{current:%Y%m%d%H}{current.minute // theme_interval:02d}"
            if theme_interval > 0
            else None
        )
        if (
            candidate_mode
            and theme_bucket is not None
            and theme_bucket != last_theme_bucket
        ):
            try:
                refresh_candidates_and_information(
                    current,
                    component="theme",
                    event_type="theme_candidate_scan_completed",
                    success_message="테마 수집과 후보 점수 갱신 성공",
                    collect_themes=True,
                    collect_information=False,
                )
            except Exception as exc:
                logging.exception("테마 기반 후보 갱신 실패")
                if operations_tracker is not None:
                    operations_tracker.record(
                        "theme",
                        "theme_candidate_scan_failed",
                        "warning",
                        str(exc),
                    )
            last_theme_bucket = theme_bucket
        rank_interval = candidate_settings.realtime_rank_collection_interval_minutes
        rank_bucket = (
            f"{current:%Y%m%d%H}{current.minute // rank_interval:02d}"
            if rank_interval > 0
            else None
        )
        if (
            candidate_mode
            and rank_bucket is not None
            and rank_bucket != last_market_rank_bucket
        ):
            try:
                trade_amount_rank = collector.collect_market_ranking(
                    "trade_amount",
                    max_pages=candidate_settings.realtime_rank_max_pages,
                )
                refreshed_run = CandidateScreeningEngine(
                    database,
                    load_candidate_settings(),
                ).scan(limit=candidate_limit)
                if operations_tracker is not None:
                    latest_trade_amount = [
                        dict(row)
                        for row in database.latest_market_rank_items(
                            rank_type="trade_amount",
                            limit=5,
                        )
                    ]
                    operations_tracker.record(
                        "market_rank",
                        "trade_amount_rank_collected",
                        "info",
                        "0187 거래대금 상위 수집과 후보 점수 갱신 성공",
                        details={
                            "rank_type": trade_amount_rank.rank_type,
                            "api_id": trade_amount_rank.api_id,
                            "received_count": trade_amount_rank.received_count,
                            "saved_count": trade_amount_rank.saved_count,
                            "page_count": trade_amount_rank.page_count,
                            "candidate_run_id": refreshed_run.run_id,
                            "selected_count": refreshed_run.selected_count,
                            "top": latest_trade_amount,
                            "scoring_effect": "candidate_main",
                        },
                    )
            except Exception as exc:
                logging.exception("0187 거래대금 상위 수집 실패")
                if operations_tracker is not None:
                    operations_tracker.record(
                        "market_rank",
                        "trade_amount_rank_failed",
                        "warning",
                        str(exc),
                    )
            last_market_rank_bucket = rank_bucket
        if (
            candidate_mode
            and rank_bucket is not None
            and rank_bucket != last_realtime_rank_bucket
        ):
            try:
                rank_result = collector.collect_realtime_item_ranking(
                    query_type=candidate_settings.realtime_rank_query_type,
                    max_pages=candidate_settings.realtime_rank_max_pages,
                )
                if operations_tracker is not None:
                    latest_rankings = [
                        dict(row)
                        for row in database.latest_realtime_item_rankings(
                            query_type=candidate_settings.realtime_rank_query_type,
                            limit=5,
                        )
                    ]
                    operations_tracker.record(
                        "realtime_rank",
                        "realtime_rank_collected",
                        "info",
                        "실시간 조회종목 순위 수집 성공",
                        details={
                            "query_type": rank_result.query_type,
                            "received_count": rank_result.received_count,
                            "saved_count": rank_result.saved_count,
                            "page_count": rank_result.page_count,
                            "top": latest_rankings,
                            "scoring_effect": "none",
                        },
                    )
            except Exception as exc:
                logging.exception("실시간 조회종목 순위 수집 실패")
                if operations_tracker is not None:
                    operations_tracker.record(
                        "realtime_rank",
                        "realtime_rank_failed",
                        "warning",
                        str(exc),
                    )
            last_realtime_rank_bucket = rank_bucket
        if (
            candidate_mode
            and rank_bucket is not None
            and rank_bucket != last_supply_leader_bucket
        ):
            scoring_candidates = _candidate_collection_codes(database, candidate_limit)
            supply_codes = [
                str(item["stock_code"])
                for item in _market_detail_collection_codes(
                    database,
                    scoring_candidates,
                    current=current,
                    cooldown_until=detail_cooldown_until,
                    max_codes=max(1, min(3, candidate_limit)),
                    strict_top_only=True,
                )
            ]
            if not supply_codes:
                supply_codes = scoring_candidates[: max(1, min(3, candidate_limit))]
            supply_saved = 0
            supply_scored = 0
            program_validation: dict[str, object] = {}
            supply_errors: list[dict[str, str]] = []
            for index, stock_code in enumerate(supply_codes):
                try:
                    collector.collect_supply_sources(
                        stock_code,
                        start_date=current.strftime("%Y%m%d"),
                        end_date=current.strftime("%Y%m%d"),
                        max_pages=1,
                        include_foreign_rank=index == 0,
                    )
                except Exception as exc:
                    logging.exception("%s 수급 원천 수집 실패", stock_code)
                    supply_errors.append(
                        {"stock_code": stock_code, "error": str(exc)}
                    )
                    if _is_rate_limit_error(exc):
                        break
            try:
                supply_result = SupplyScoringEngine(database).score(supply_codes)
                supply_saved = supply_result.saved_count
                supply_scored = supply_result.scored_count
                program_validation = getattr(
                    supply_result,
                    "program_validation",
                    {},
                )
            except Exception as exc:
                logging.exception("수급 점수 저장 실패")
                supply_errors.append({"stock_code": "*", "error": str(exc)})
            try:
                leader_result = LeaderStockScoringEngine(database).score(
                    trade_date=current.date(),
                    limit=max(20, candidate_limit),
                )
                leader_event_type = "leader_supply_scores_updated"
                leader_severity = "info"
                leader_message = "수급·대장주 점수 저장 성공"
                leader_details: dict[str, object] = {
                    "supply_requested_count": len(supply_codes),
                    "supply_codes": supply_codes,
                    "supply_scored_count": supply_scored,
                    "supply_saved_count": supply_saved,
                    "supply_errors": supply_errors,
                    "program_validation": program_validation,
                    "leader_scored_count": leader_result.scored_count,
                    "leader_saved_count": leader_result.saved_count,
                    "trade_date": leader_result.trade_date,
                }
                if supply_saved < 1:
                    leader_severity = "warning"
                    leader_message = (
                        "대장주 점수는 저장했지만 수급 점수 원천이 부족합니다"
                    )
                    leader_details["supply_warning"] = "supply_scores_not_saved"
                if operations_tracker is not None:
                    operations_tracker.record(
                        "leader_supply",
                        leader_event_type,
                        leader_severity,
                        leader_message,
                        details=leader_details,
                    )
            except Exception as exc:
                logging.exception("대장주 점수 저장 실패")
                if operations_tracker is not None:
                    operations_tracker.record(
                        "leader_supply",
                        "leader_scores_failed",
                        "warning",
                        str(exc),
                        details={
                            "supply_requested_count": len(supply_codes),
                            "supply_saved_count": supply_saved,
                            "supply_errors": supply_errors,
                        },
                    )
            last_supply_leader_bucket = rank_bucket
        information_bucket = f"{current:%Y%m%d}{current.hour // 2:02d}"
        if (
            candidate_mode
            and information_bucket != last_information_bucket
        ):
            try:
                refresh_candidates_and_information(
                    current,
                    component="information",
                    event_type="news_disclosure_collected",
                    success_message="뉴스·공시 수집과 후보 점수 갱신 성공",
                )
            except Exception as exc:
                logging.exception("뉴스·공시 주기 수집 실패")
                if operations_tracker is not None:
                    operations_tracker.record(
                        "information",
                        "news_disclosure_failed",
                        "warning",
                        str(exc),
                    )
            last_information_bucket = information_bucket
        ai_settings = load_ai_shadow_settings()
        ai_bucket = (
            f"{current:%Y%m%d%H}"
            f"{current.minute // ai_settings.interval_minutes:02d}"
        )
        if (
            candidate_mode
            and ai_settings.enabled
            and ai_bucket != last_ai_bucket
            and not ai_lock.locked()
        ):
            last_ai_bucket = ai_bucket

            def run_ai_shadow() -> None:
                with ai_lock:
                    try:
                        result = AiShadowService(
                            database,
                            ai_settings,
                        ).analyze_latest()
                        if operations_tracker is not None:
                            operations_tracker.record(
                                "ai_shadow",
                                "ai_analysis_completed",
                                "info",
                                "AI shadow 분석 완료",
                                details=result,
                            )
                    except Exception as exc:
                        logging.exception("AI shadow 분석 실패")
                        if operations_tracker is not None:
                            operations_tracker.record(
                                "ai_shadow",
                                "ai_analysis_failed",
                                "warning",
                                str(exc),
                            )

            threading.Thread(
                target=run_ai_shadow,
                name="ai-shadow-analysis",
                daemon=True,
            ).start()
        detail_interval = market_data_settings.detail_collection_interval_minutes
        detail_bucket = f"{current:%Y%m%d%H}{current.minute // detail_interval:02d}"
        should_collect_detail = (
            (with_depth or with_trades)
            and (not candidate_mode or detail_bucket != last_detail_bucket)
        )
        if should_collect_detail:
            detail_plan = _market_detail_collection_codes(
                database,
                codes,
                current=current,
                cooldown_until=detail_cooldown_until,
                max_codes=market_data_settings.detail_collection_max_codes,
                strict_top_only=candidate_mode,
            )
            if operations_tracker is not None:
                operations_tracker.record(
                    "market_detail",
                    "market_detail_collection_planned",
                    "info",
                    "호가·체결강도 상세 수집 대상 조정",
                    details={
                        "requested_count": len(codes),
                        "selected_count": len(detail_plan),
                        "max_codes": market_data_settings.detail_collection_max_codes,
                        "selected": [
                            {
                                "stock_code": item["stock_code"],
                                "priority": item["priority"].name,
                                "reason": item["reason"],
                            }
                            for item in detail_plan
                        ],
                        "cooldown_count": sum(
                            1
                            for until in detail_cooldown_until.values()
                            if until > current
                        ),
                        "cooldown_minutes": (
                            market_data_settings.detail_failure_cooldown_minutes
                        ),
                    },
                )
            rate_limited = False
            for index, item in enumerate(detail_plan):
                stock_code = str(item["stock_code"])
                priority = item["priority"]
                try:
                    if with_depth:
                        depth_result = _collect_order_book_with_priority(
                            collector,
                            stock_code,
                            priority,
                        )
                        if operations_tracker is not None:
                            operations_tracker.record(
                                "market_detail",
                                "order_book_collected",
                                "info",
                                f"{stock_code} 호가 저장 성공",
                                stock_code=stock_code,
                                details={
                                    "database_id": depth_result.database_id,
                                    "level_count": depth_result.level_count,
                                    "priority": priority.name,
                                    "priority_reason": item["reason"],
                                },
                            )
                    if with_trades:
                        trade_result = _collect_trade_strength_with_priority(
                            collector,
                            stock_code,
                            max_pages=trade_pages,
                            priority=priority,
                        )
                        if operations_tracker is not None:
                            operations_tracker.record(
                                "market_detail",
                                "trade_strength_collected",
                                "info",
                                f"{stock_code} 체결강도 저장 성공",
                                stock_code=stock_code,
                                details={
                                    "saved_count": trade_result.saved_count,
                                    "page_count": trade_result.page_count,
                                    "priority": priority.name,
                                    "priority_reason": item["reason"],
                                },
                            )
                except Exception as exc:
                    logging.exception("%s 호가·체결강도 수집 실패", stock_code)
                    detail_cooldown_until[stock_code] = current + timedelta(
                        minutes=market_data_settings.detail_failure_cooldown_minutes
                    )
                    rate_limited = _is_rate_limit_error(exc)
                    if operations_tracker is not None:
                        operations_tracker.record(
                            "market_detail",
                            "market_detail_failed",
                            "warning" if rate_limited else "error",
                            str(exc),
                            stock_code=stock_code,
                            details={
                                "cooldown_until": detail_cooldown_until[
                                    stock_code
                                ].isoformat(timespec="seconds"),
                                "rate_limited": rate_limited,
                                "priority": priority.name,
                                "priority_reason": item["reason"],
                            },
                        )
                    if (
                        rate_limited
                        and market_data_settings.detail_abort_on_rate_limit
                    ):
                        if operations_tracker is not None:
                            operations_tracker.record(
                                "market_detail",
                                "market_detail_rate_limited",
                                "warning",
                                "API 429 감지로 같은 주기 남은 상세 수집 중단",
                                stock_code=stock_code,
                                details={
                                    "remaining_count": max(
                                        0,
                                        len(detail_plan) - index - 1,
                                    ),
                                    "budget_manager": "central",
                                },
                            )
                        break
            last_detail_bucket = detail_bucket
        close_results = risk_manager.liquidate_market_close()
        risk_results = risk_manager.evaluate_all_accounts()
        if operations_tracker is not None:
            operations_tracker.record(
                "risk",
                "risk_evaluated",
                "info",
                "리스크 평가 성공",
                details={
                    "market_close_count": len(close_results),
                    "evaluation_count": len(risk_results),
                },
            )
        if auto_strategy:
            strategy_results = strategy.run(
                account_name,
                codes,
                execute=True,
            )
            if operations_tracker is not None:
                operations_tracker.record(
                    "strategy",
                    "strategy_evaluated",
                    "info",
                    "자동 전략 평가 성공",
                    details={
                        "result_count": len(strategy_results),
                        "executed_count": sum(
                            result["action"] == "executed"
                            for result in strategy_results
                        ),
                        "failed_count": sum(
                            result["action"] == "failed"
                            for result in strategy_results
                        ),
                    },
                )

    return PeriodicCollector(
        collector,
        current_price_provider,
        after_collection=after_collection,
        event_callback=(
            operations_tracker.handle
            if operations_tracker is not None
            else None
        ),
        current_price_priority_provider=(
            lambda stock_code: current_price_priorities.get(
                normalize_stock_code(stock_code),
                ApiCallPriority.NORMAL,
            )
        ),
        current_price_call_spacing_seconds=(
            getattr(
                load_market_data_settings(),
                "current_price_call_spacing_seconds",
                0.25,
            )
        ),
        market_open_check=market_calendar.is_market_open,
        pre_market_callback=(
            pre_market_scan
            if candidate_mode and preopen_scan
            else None
        ),
    )


def _current_price_collection_plan(
    database: Database,
    codes: Sequence[str],
    *,
    current: datetime,
    settings: object,
) -> list[dict[str, object]]:
    seen: set[str] = set()
    normalized_codes: list[str] = []
    for stock_code in codes:
        normalized = normalize_stock_code(stock_code)
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_codes.append(normalized)
    if not normalized_codes:
        return []

    context = _api_priority_context(
        database,
        normalized_codes,
        current=current,
        pre_entry_max_age_minutes=int(
            getattr(settings, "current_price_pre_entry_max_age_minutes", 10)
        ),
    )
    placeholders = ",".join("?" for _ in normalized_codes)
    latest_prices: dict[str, str] = {}
    with database.connect() as connection:
        rows = connection.execute(
            f"""
            SELECT p.stock_code, MAX(p.collected_at) AS collected_at
            FROM price_snapshots p
            WHERE p.stock_code IN ({placeholders})
            GROUP BY p.stock_code
            """,
            normalized_codes,
        ).fetchall()
        latest_prices = {
            str(row["stock_code"]): str(row["collected_at"])
            for row in rows
            if row["collected_at"]
        }

    window = database.get_api_budget_window("current_price")
    backoff_until = _datetime_or_none(
        window["backoff_until"] if window is not None else None
    )
    local_current = current.astimezone(SEOUL_TIMEZONE) if current.tzinfo else current
    local_current = local_current.replace(tzinfo=None)
    backoff_active = (
        backoff_until is not None
        and _as_naive_datetime(backoff_until) > local_current
    )
    intervals = {
        ApiCallPriority.CRITICAL: int(
            getattr(settings, "current_price_critical_interval_seconds", 30)
        ),
        ApiCallPriority.HIGH: int(
            getattr(settings, "current_price_high_interval_seconds", 60)
        ),
        ApiCallPriority.NORMAL: int(
            getattr(settings, "current_price_normal_interval_seconds", 180)
        ),
        ApiCallPriority.LOW: int(
            getattr(settings, "current_price_low_interval_seconds", 300)
        ),
    }
    ranked = sorted(
        (
            _detail_priority_item(stock_code, index, context)
            for index, stock_code in enumerate(normalized_codes)
        ),
        key=lambda item: (
            -int(item["priority_value"]),
            int(item["rank"]),
            int(item["input_order"]),
            str(item["stock_code"]),
        ),
    )
    due: list[dict[str, object]] = []
    for item in ranked:
        priority = item["priority"]
        if (
            backoff_active
            and priority in {ApiCallPriority.NORMAL, ApiCallPriority.LOW}
        ):
            continue
        latest_at = _datetime_or_none(latest_prices.get(str(item["stock_code"])))
        age_seconds = None
        if latest_at is not None:
            age_seconds = max(
                0,
                int(
                    (
                        local_current - _as_naive_datetime(latest_at)
                    ).total_seconds()
                ),
            )
        minimum_age = intervals[priority]  # type: ignore[index]
        if age_seconds is not None and age_seconds < minimum_age:
            continue
        due.append(
            {
                **item,
                "latest_price_at": (
                    latest_at.isoformat(timespec="seconds")
                    if latest_at is not None
                    else None
                ),
                "age_seconds": age_seconds,
                "minimum_age_seconds": minimum_age,
                "backoff_active": backoff_active,
            }
        )

    max_codes = max(
        1,
        int(getattr(settings, "current_price_max_codes_per_cycle", 12)),
    )
    critical = [
        item for item in due if item["priority"] is ApiCallPriority.CRITICAL
    ]
    others = [
        item for item in due if item["priority"] is not ApiCallPriority.CRITICAL
    ]
    remaining = max(0, max_codes - len(critical))
    return critical + others[:remaining]


def _datetime_or_none(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _as_naive_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(SEOUL_TIMEZONE).replace(tzinfo=None)


def _market_detail_collection_codes(
    database: Database,
    codes: Sequence[str],
    *,
    current: datetime,
    cooldown_until: dict[str, datetime],
    max_codes: int,
    strict_top_only: bool = False,
) -> list[dict[str, object]]:
    if max_codes < 0:
        raise ValueError("상세 수집 최대 종목 수는 0 이상이어야 합니다.")
    if max_codes == 0:
        return []
    context = _api_priority_context(database, codes)
    selected: list[dict[str, object]] = []
    seen: set[str] = set()
    normalized_codes = []
    for stock_code in codes:
        normalized = normalize_stock_code(stock_code)
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_codes.append(normalized)

    ranked = sorted(
        (
            _detail_priority_item(stock_code, index, context)
            for index, stock_code in enumerate(normalized_codes)
        ),
        key=lambda item: (
            -int(item["priority_value"]),
            int(item["rank"]),
            int(item["input_order"]),
            str(item["stock_code"]),
        ),
    )
    for item in ranked:
        if (
            strict_top_only
            and item["priority"] is not ApiCallPriority.CRITICAL
            and item["priority"] is not ApiCallPriority.HIGH
        ):
            continue
        normalized = str(item["stock_code"])
        cooldown = cooldown_until.get(normalized)
        if cooldown is not None and cooldown > current:
            continue
        if cooldown is not None:
            cooldown_until.pop(normalized, None)
        selected.append(item)
        if len(selected) >= max_codes:
            break
    return selected


def _minute_collection_codes(
    database: Database,
    codes: Sequence[str],
    *,
    max_codes: int,
) -> list[dict[str, object]]:
    if max_codes < 1:
        return []
    context = _api_priority_context(database, codes)
    seen: set[str] = set()
    normalized_codes: list[str] = []
    for stock_code in codes:
        normalized = normalize_stock_code(stock_code)
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_codes.append(normalized)
    ranked = sorted(
        (
            _detail_priority_item(stock_code, index, context)
            for index, stock_code in enumerate(normalized_codes)
        ),
        key=lambda item: (
            -int(item["priority_value"]),
            int(item["rank"]),
            int(item["input_order"]),
            str(item["stock_code"]),
        ),
    )
    selected = [
        item
        for item in ranked
        if item["priority"] in {ApiCallPriority.CRITICAL, ApiCallPriority.HIGH}
    ]
    return selected[:max_codes]


def _api_priority_context(
    database: Database,
    codes: Sequence[str],
    *,
    current: datetime | None = None,
    pre_entry_max_age_minutes: int = 10,
) -> dict[str, dict[str, object]]:
    normalized_codes = list(dict.fromkeys(normalize_stock_code(code) for code in codes))
    if not normalized_codes:
        return {}
    placeholders = ",".join("?" for _ in normalized_codes)
    context = {
        code: {
            "candidate_rank": None,
            "candidate_score": None,
            "leader_role": None,
            "leader_score": None,
            "held": False,
            "pre_entry": False,
            "pre_entry_at": None,
            "market_rank": None,
            "realtime_rank": None,
        }
        for code in normalized_codes
    }
    with database.connect() as connection:
        held = connection.execute(
            f"""
            SELECT DISTINCT stock_code
            FROM paper_positions
            WHERE stock_code IN ({placeholders})
            """,
            normalized_codes,
        ).fetchall()
        for row in held:
            context[row["stock_code"]]["held"] = True

        run = connection.execute(
            "SELECT id FROM candidate_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if run is not None:
            rows = connection.execute(
                f"""
                SELECT stock_code, rank, total_score
                FROM candidate_scores
                WHERE run_id = ? AND stock_code IN ({placeholders})
                """,
                [run["id"], *normalized_codes],
            ).fetchall()
            for row in rows:
                context[row["stock_code"]]["candidate_rank"] = row["rank"]
                context[row["stock_code"]]["candidate_score"] = row["total_score"]

        leader_date = connection.execute(
            "SELECT MAX(trade_date) AS trade_date FROM leader_stock_scores"
        ).fetchone()
        if leader_date is not None and leader_date["trade_date"] is not None:
            rows = connection.execute(
                f"""
                SELECT stock_code, role, total_score, blocked
                FROM leader_stock_scores
                WHERE trade_date = ? AND stock_code IN ({placeholders})
                ORDER BY scored_at DESC, id DESC
                """,
                [leader_date["trade_date"], *normalized_codes],
            ).fetchall()
            for row in rows:
                if context[row["stock_code"]]["leader_role"] is not None:
                    continue
                context[row["stock_code"]]["leader_role"] = row["role"]
                context[row["stock_code"]]["leader_score"] = row["total_score"]
                context[row["stock_code"]]["leader_blocked"] = bool(row["blocked"])

        rows = connection.execute(
            f"""
            WITH latest AS (
                SELECT stock_code, action, score, decided_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY stock_code
                           ORDER BY decided_at DESC, id DESC
                       ) AS row_number
                FROM strategy_decisions
                WHERE stock_code IN ({placeholders})
            )
            SELECT stock_code, action, score, decided_at
            FROM latest
            WHERE row_number = 1
            """,
            normalized_codes,
        ).fetchall()
        local_current = None
        if current is not None:
            local_current = (
                current.astimezone(SEOUL_TIMEZONE)
                if current.tzinfo
                else current
            ).replace(tzinfo=None)
        for row in rows:
            stock_code = row["stock_code"]
            score = float(row["score"] or 0)
            decided_at = _datetime_or_none(row["decided_at"])
            recent_enough = True
            if local_current is not None and decided_at is not None:
                age = (
                    local_current - _as_naive_datetime(decided_at)
                ).total_seconds()
                recent_enough = 0 <= age <= pre_entry_max_age_minutes * 60
            if row["action"] == "buy" and score >= 80 and recent_enough:
                context[stock_code]["pre_entry"] = True
                context[stock_code]["pre_entry_at"] = row["decided_at"]
        rows = connection.execute(
            f"""
            SELECT i.stock_code, i.stock_name, i.rank
            FROM market_rank_items i
            JOIN market_rank_snapshots s ON s.id = i.snapshot_id
            WHERE i.stock_code IN ({placeholders})
              AND i.rank_type = 'trade_amount'
              AND i.rank IS NOT NULL
            ORDER BY i.collected_at DESC, i.rank, i.rowid DESC
            """,
            normalized_codes,
        ).fetchall()
        seen_rank: set[str] = set()
        for row in rows:
            stock_code = row["stock_code"]
            if is_market_product_name(row["stock_name"]):
                continue
            if stock_code in seen_rank:
                continue
            seen_rank.add(stock_code)
            context[stock_code]["market_rank"] = row["rank"]
        rows = connection.execute(
            f"""
            SELECT stock_code, stock_name, big_data_rank
            FROM realtime_item_rankings
            WHERE stock_code IN ({placeholders})
              AND big_data_rank IS NOT NULL
            ORDER BY observed_at DESC, big_data_rank, rowid DESC
            """,
            normalized_codes,
        ).fetchall()
        seen_realtime_rank: set[str] = set()
        for row in rows:
            stock_code = row["stock_code"]
            if is_market_product_name(row["stock_name"]):
                continue
            if stock_code in seen_realtime_rank:
                continue
            seen_realtime_rank.add(stock_code)
            context[stock_code]["realtime_rank"] = row["big_data_rank"]
    return context


def _current_price_priority(
    database: Database,
    stock_code: str,
) -> ApiCallPriority:
    item = _detail_priority_item(
        normalize_stock_code(stock_code),
        0,
        _api_priority_context(database, [stock_code]),
    )
    priority = item["priority"]
    return priority  # type: ignore[return-value]


def _detail_priority_item(
    stock_code: str,
    input_order: int,
    context: dict[str, dict[str, object]],
) -> dict[str, object]:
    info = context.get(stock_code, {})
    candidate_rank = info.get("candidate_rank")
    candidate_score = float(info.get("candidate_score") or 0)
    leader_role = str(info.get("leader_role") or "")
    rank = int(candidate_rank) if candidate_rank is not None else 9999
    market_rank_values = [
        int(value)
        for value in (info.get("market_rank"), info.get("realtime_rank"))
        if value is not None
    ]
    market_rank = min(market_rank_values) if market_rank_values else 9999
    if info.get("held"):
        return {
            "stock_code": stock_code,
            "priority": ApiCallPriority.CRITICAL,
            "priority_value": 100,
            "rank": rank,
            "input_order": input_order,
            "reason": "held_position",
        }
    if info.get("pre_entry"):
        return {
            "stock_code": stock_code,
            "priority": ApiCallPriority.HIGH,
            "priority_value": 92,
            "rank": rank,
            "input_order": input_order,
            "reason": "pre_entry_candidate",
        }
    if market_rank <= 5:
        return {
            "stock_code": stock_code,
            "priority": ApiCallPriority.HIGH,
            "priority_value": 90,
            "rank": market_rank,
            "input_order": input_order,
            "reason": "market_rank_top",
        }
    if leader_role in {"leader", "second"}:
        return {
            "stock_code": stock_code,
            "priority": ApiCallPriority.HIGH,
            "priority_value": 88,
            "rank": rank,
            "input_order": input_order,
            "reason": f"leader_role:{leader_role}",
        }
    if rank <= 5:
        return {
            "stock_code": stock_code,
            "priority": ApiCallPriority.HIGH,
            "priority_value": 82,
            "rank": rank,
            "input_order": input_order,
            "reason": "top_candidate",
        }
    if candidate_score >= 60 or rank < 9999:
        return {
            "stock_code": stock_code,
            "priority": ApiCallPriority.NORMAL,
            "priority_value": 55,
            "rank": rank,
            "input_order": input_order,
            "reason": "general_candidate",
        }
    return {
        "stock_code": stock_code,
        "priority": ApiCallPriority.LOW,
        "priority_value": 20,
        "rank": rank,
        "input_order": input_order,
        "reason": "low_score_detail_candidate",
    }


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "429" in text
        or "too many" in text
        or "rate limit" in text
        or "요청 제한" in text
    )


def _collect_order_book_with_priority(
    collector: MarketDataCollector,
    stock_code: str,
    priority: ApiCallPriority,
) -> object:
    try:
        return collector.collect_order_book(stock_code, priority=priority)
    except TypeError as exc:
        if "priority" not in str(exc):
            raise
        return collector.collect_order_book(stock_code)


def _collect_trade_strength_with_priority(
    collector: MarketDataCollector,
    stock_code: str,
    *,
    max_pages: int,
    priority: ApiCallPriority,
) -> object:
    try:
        return collector.collect_trade_strength(
            stock_code,
            max_pages=max_pages,
            priority=priority,
        )
    except TypeError as exc:
        if "priority" not in str(exc):
            raise
        return collector.collect_trade_strength(stock_code, max_pages=max_pages)


def _collect_minute_candles_with_priority(
    collector: MarketDataCollector,
    stock_code: str,
    base_date: str,
    interval_minutes: int,
    *,
    max_pages: int,
    priority: ApiCallPriority,
) -> object:
    try:
        return collector.collect_minute_candles(
            stock_code,
            base_date,
            interval_minutes,
            max_pages=max_pages,
            priority=priority,
        )
    except TypeError as exc:
        if "priority" not in str(exc):
            raise
        return collector.collect_minute_candles(
            stock_code,
            base_date,
            interval_minutes,
            max_pages=max_pages,
        )


def _candidate_collection_codes(
    database: Database,
    limit: int,
) -> list[str]:
    if limit < 1:
        raise ValueError("후보 수집 종목 수는 1 이상이어야 합니다.")
    with database.connect() as connection:
        run = connection.execute(
            "SELECT id FROM candidate_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        selected = []
        if run is not None:
            selected = [
                row["stock_code"]
                for row in connection.execute(
                    """
                    SELECT stock_code
                    FROM candidate_scores
                    WHERE run_id = ?
                      AND selected = 1
                      AND (rank <= ? OR total_score >= 60)
                    ORDER BY rank, stock_code
                    LIMIT ?
                    """,
                    (run["id"], min(limit, 10), limit),
                ).fetchall()
            ]
        leader_date = connection.execute(
            "SELECT MAX(trade_date) AS trade_date FROM leader_stock_scores"
        ).fetchone()
        leaders = []
        if leader_date is not None and leader_date["trade_date"] is not None:
            leaders = [
                row["stock_code"]
                for row in connection.execute(
                    """
                    SELECT stock_code
                    FROM leader_stock_scores
                    WHERE trade_date = ?
                      AND role IN ('leader', 'second')
                      AND blocked = 0
                    ORDER BY total_score DESC, stock_code
                    LIMIT ?
                    """,
                    (leader_date["trade_date"], max(5, min(limit, 10))),
                ).fetchall()
            ]
        market_ranked = [
            row["stock_code"]
            for row in connection.execute(
                """
                SELECT i.stock_code, i.stock_name
                FROM market_rank_items i
                JOIN market_rank_snapshots s ON s.id = i.snapshot_id
                WHERE i.rank_type = 'trade_amount'
                  AND i.rank IS NOT NULL
                ORDER BY i.collected_at DESC, i.rank, i.rowid DESC
                LIMIT ?
                """,
                (max(5, min(limit, 10)),),
            ).fetchall()
            if not is_market_product_name(row["stock_name"])
        ]
        realtime_ranked = [
            row["stock_code"]
            for row in connection.execute(
                """
                SELECT stock_code, stock_name
                FROM realtime_item_rankings
                WHERE big_data_rank IS NOT NULL
                ORDER BY observed_at DESC, big_data_rank, rowid DESC
                LIMIT ?
                """,
                (max(5, min(limit, 10)),),
            ).fetchall()
            if not is_market_product_name(row["stock_name"])
        ]
        held = [
            row["stock_code"]
            for row in connection.execute(
                "SELECT DISTINCT stock_code FROM paper_positions"
            ).fetchall()
        ]
    return list(dict.fromkeys([*held, *leaders, *market_ranked, *realtime_ranked, *selected]))


def resolve_stock_codes(
    database: Database,
    stock_codes: Sequence[str],
) -> list[str]:
    if stock_codes:
        return [normalize_stock_code(code) for code in stock_codes]
    codes = WatchlistService(database).active_codes()
    if not codes:
        raise RuntimeError("활성 관심 종목이 없습니다.")
    return codes


def resolve_base_date(value: str | None) -> str:
    if value is None:
        return datetime.now(SEOUL_TIMEZONE).strftime("%Y%m%d")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("기준일자는 YYYYMMDD 형식이어야 합니다.") from exc
    return value


def handle_candles(args: argparse.Namespace, database: Database) -> None:
    if args.pages < 1:
        raise ValueError("페이지 수는 1 이상이어야 합니다.")
    collector = create_market_collector(database)
    stock_codes = resolve_stock_codes(database, args.stock_codes)
    base_date = resolve_base_date(args.base_date)
    failures = 0

    for stock_code in stock_codes:
        try:
            if args.candle_command == "daily":
                result = collector.collect_daily_candles(
                    stock_code,
                    base_date,
                    max_pages=args.pages,
                )
            else:
                result = collector.collect_minute_candles(
                    stock_code,
                    base_date,
                    args.interval,
                    max_pages=args.pages,
                )
            label = (
                "일봉"
                if result.candle_type == "daily"
                else f"{result.interval_minutes}분봉"
            )
            logging.info(
                "%s %s 저장 완료: 수신=%s 저장/갱신=%s 페이지=%s",
                stock_code,
                label,
                result.received_count,
                result.saved_count,
                result.page_count,
            )
        except Exception:
            logging.exception("%s 캔들 수집 실패", stock_code)
            failures += 1
    if failures:
        raise SystemExit(1)


def handle_market(args: argparse.Namespace, database: Database) -> None:
    collector = create_market_collector(database)
    if args.market_command == "mode":
        if args.market_mode_command == "collect":
            base_date = args.base_date or datetime.now(SEOUL_TIMEZONE).strftime(
                "%Y%m%d"
            )
            result = collector.collect_kosdaq_market_mode(
                base_date=base_date,
                max_pages=args.pages,
            )
            trade_amount = (
                f"{result.trade_amount:,}원" if result.trade_amount is not None else "-"
            )
            index_value = (
                f"{result.index_value:.2f}"
                if result.index_value is not None
                else "-"
            )
            ma20 = (
                f"{result.moving_average_20:.2f}"
                if result.moving_average_20 is not None
                else "-"
            )
            print(
                f"시장={result.market}\t기준일={result.market_date}\t"
                f"모드={result.mode}\t신뢰도={result.reliability}\t"
                f"지수={index_value}\t20일선={ma20}\t"
                f"20일선위={result.above_ma20}\t거래대금={trade_amount}\t"
                f"스냅샷={result.snapshot_id}\t사유={result.reason}"
            )
            return
        latest = MarketModeService(database).latest_snapshot()
        if latest is None:
            print("조회 결과가 없습니다.")
            return
        policy = latest.get("policy") or {}
        trade_amount = latest.get("trade_amount")
        print(
            f"시장={latest['market']}\t기준일={latest['market_date']}\t"
            f"모드={latest['mode']}\t신뢰도={latest['reliability']}\t"
            f"지수={latest.get('index_value') or '-'}\t"
            f"20일선={latest.get('moving_average_20') or '-'}\t"
            f"20일선위={bool(latest.get('above_ma20'))}\t"
            f"거래대금={f'{int(trade_amount):,}원' if trade_amount is not None else '-'}\t"
            f"수량배수={policy.get('quantity_multiplier')}\t"
            f"허용등급={policy.get('allowed_entry_grades')}\t"
            f"수집={latest['collected_at']}\t사유={latest['reason']}"
        )
        return

    stock_codes = resolve_stock_codes(database, args.stock_codes)
    failures = 0
    for stock_code in stock_codes:
        try:
            if args.market_command == "depth":
                result = collector.collect_order_book(stock_code)
                logging.info(
                    "%s 호가 저장 완료: 단계=%s DB ID=%s",
                    stock_code,
                    result.level_count,
                    result.database_id,
                )
            else:
                result = collector.collect_trade_strength(
                    stock_code,
                    max_pages=args.pages,
                )
                logging.info(
                    "%s 체결강도 저장 완료: 수신=%s 저장=%s 페이지=%s",
                    stock_code,
                    result.received_count,
                    result.saved_count,
                    result.page_count,
                )
        except Exception:
            logging.exception("%s 시장 상세 데이터 수집 실패", stock_code)
            failures += 1
    if failures:
        raise SystemExit(1)


def _validate_order_date(value: str) -> str:
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("주문일자는 YYYYMMDD 형식이어야 합니다.") from exc
    return value


def _sync_account_data(
    collector: AccountDataCollector,
    database: Database,
    *,
    order_date: str,
    pages: int,
    retention_days: int,
) -> None:
    account_result = collector.collect_snapshot()
    order_result = collector.collect_orders(order_date, max_pages=pages)
    deleted = database.prune_sensitive_account_data(retention_days)
    logging.info(
        "실계좌 데이터 저장 완료: 스냅샷=%s 보유종목=%s 주문체결=%s 페이지=%s",
        account_result.snapshot_id,
        account_result.position_count,
        order_result.saved_count,
        order_result.page_count,
    )
    if any(deleted.values()):
        logging.info("민감 계좌 데이터 정리: %s", deleted)


def handle_account(args: argparse.Namespace, database: Database) -> None:
    retention_days = (
        args.days
        if args.account_command == "prune" and args.days is not None
        else load_account_data_settings().retention_days
    )
    if args.account_command == "prune":
        deleted = database.prune_sensitive_account_data(retention_days)
        print("\t".join(f"{key}={value}" for key, value in deleted.items()))
        return

    if args.pages < 1:
        raise ValueError("페이지 수는 1 이상이어야 합니다.")
    collector = create_account_collector(database)
    if args.account_command == "sync":
        _sync_account_data(
            collector,
            database,
            order_date=_validate_order_date(args.date),
            pages=args.pages,
            retention_days=retention_days,
        )
        return

    if args.interval < 1:
        raise ValueError("수집 간격은 1초 이상이어야 합니다.")
    market_calendar = MarketCalendar(database)
    logging.info(
        "실계좌 주기 저장 시작: 간격=%s초 장중전용=%s",
        args.interval,
        not args.all_hours,
    )
    try:
        while True:
            if args.all_hours or market_calendar.is_market_open():
                try:
                    _sync_account_data(
                        collector,
                        database,
                        order_date="",
                        pages=args.pages,
                        retention_days=retention_days,
                    )
                except Exception:
                    logging.exception("실계좌 데이터 주기 저장 실패")
            else:
                logging.info("정규장 운영 시간이 아니므로 계좌 저장을 건너뜁니다.")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logging.info("실계좌 주기 저장을 종료합니다.")


def handle_watchlist(args: argparse.Namespace, database: Database) -> None:
    service = WatchlistService(database)
    if args.watchlist_command == "add":
        stock_code = service.add(args.stock_code, args.name)
        logging.info("%s 관심 종목 등록 완료", stock_code)
    elif args.watchlist_command == "remove":
        stock_code = normalize_stock_code(args.stock_code)
        if service.remove(stock_code):
            logging.info("%s 관심 종목 비활성화 완료", stock_code)
        else:
            logging.warning("%s은 활성 관심 종목이 아닙니다.", stock_code)
    elif args.watchlist_command == "list":
        rows = database.list_watchlist(include_disabled=args.all)
        if not rows:
            print("등록된 관심 종목이 없습니다.")
            return
        for row in rows:
            status = "활성" if row["enabled"] else "비활성"
            name = row["stock_name"] or "-"
            print(f"{row['stock_code']}\t{name}\t{status}")


def handle_universe(args: argparse.Namespace, database: Database) -> None:
    service = StockUniverseService(database)
    if args.universe_command in {"sync", "sync-collect"} or (
        args.universe_command == "complete" and args.sync_first
    ):
        result = UniverseSyncService(database).sync()
        print(
            f"KIND 동기화\t등록={result.registered_count}\t"
            f"비활성={result.deactivated_count}"
        )
        if args.universe_command == "sync":
            return
    if args.universe_command == "complete":
        _complete_universe_collection(args, database)
        return
    if args.universe_command in {"collect", "sync-collect"}:
        runtime = UniverseCollectionRuntime(load_database_path())
        lock_path = runtime.lock_path
        try:
            with ProcessLock(lock_path):
                result = UniverseDailyCollector(
                    database,
                    create_market_collector(database),
                ).collect(
                    base_date=resolve_base_date(None),
                    target_bars=args.target_bars,
                    max_pages=args.pages,
                    limit=args.limit,
                    delay_seconds=args.delay,
                    stop_requested=runtime.stop_requested,
                )
        finally:
            runtime.clear_stop_request()
        print(
            f"일봉 수집\t시도={result.attempted_count}\t"
            f"완료={result.completed_count}\t실패={result.failed_count}\t"
            f"가용이력완료={result.available_history_completed_count}\t"
            f"대기={result.skipped_count}\t중지={result.stopped}"
        )
        return
    if args.universe_command == "add":
        code = service.upsert(
            args.stock_code,
            stock_name=args.name,
            market=args.market,
            sector=args.sector,
            tradable=not args.not_tradable,
            management=args.management,
            halted=args.halted,
            overheated=args.overheated,
        )
        logging.info("%s 유니버스 등록 완료", code)
        return
    if args.universe_command == "import":
        count = service.import_csv(args.path)
        logging.info("유니버스 CSV %s개 종목 등록 완료", count)
        return
    for row in service.list():
        flags = []
        if not row["tradable"]:
            flags.append("거래불가")
        if row["management"]:
            flags.append("관리")
        if row["halted"]:
            flags.append("정지")
        if row["overheated"]:
            flags.append("과열")
        print(
            f"{row['stock_code']}\t{row['stock_name'] or '-'}\t"
            f"{row['market'] or '-'}\t{row['sector'] or '-'}\t"
            f"일봉={row['daily_bars']}\t관심={bool(row['watched'])}\t"
            f"상태={','.join(flags) if flags else '정상'}"
        )


def _complete_universe_collection(
    args: argparse.Namespace,
    database: Database,
) -> None:
    if args.batch_size < 1:
        raise ValueError("배치 종목 수는 1 이상이어야 합니다.")
    if args.round_delay < 0:
        raise ValueError("배치 사이 대기시간은 0 이상이어야 합니다.")
    if args.max_rounds < 0:
        raise ValueError("최대 반복 횟수는 0 이상이어야 합니다.")
    runtime = UniverseCollectionRuntime(load_database_path())
    collector = UniverseDailyCollector(
        database,
        create_market_collector(database),
    )
    round_number = 0
    with ProcessLock(runtime.lock_path):
        try:
            while True:
                if runtime.stop_requested():
                    print("전 종목 일봉 완주 중지 요청을 확인했습니다.")
                    break
                summary = collector.summary()
                remaining = (
                    int(summary["pending_count"])
                    + int(summary["failed_count"])
                    + int(summary["running_count"])
                )
                print(
                    f"[진행률]\t완료={summary['completed_count']}\t"
                    f"실패={summary['failed_count']}\t"
                    f"남음={remaining}\t"
                    f"가용이력완료={summary['available_history_completed_count']}"
                )
                if remaining == 0:
                    break
                if args.max_rounds and round_number >= args.max_rounds:
                    print("설정한 최대 배치 반복 횟수에 도달했습니다.")
                    break
                round_number += 1
                result = collector.collect(
                    base_date=resolve_base_date(None),
                    target_bars=args.target_bars,
                    max_pages=args.pages,
                    limit=args.batch_size,
                    delay_seconds=args.delay,
                    stop_requested=runtime.stop_requested,
                )
                print(
                    f"[배치 {round_number}]\t시도={result.attempted_count}\t"
                    f"완료={result.completed_count}\t"
                    f"가용이력완료={result.available_history_completed_count}\t"
                    f"실패={result.failed_count}\t대기={result.skipped_count}"
                )
                if result.stopped:
                    break
                if result.attempted_count == 0:
                    break
                if args.round_delay:
                    time.sleep(args.round_delay)
        finally:
            runtime.clear_stop_request()

    summary = collector.summary()
    remaining = (
        int(summary["pending_count"])
        + int(summary["failed_count"])
        + int(summary["running_count"])
    )
    print(
        f"[최종]\t전체={summary['total_count']}\t"
        f"완료={summary['completed_count']}\t"
        f"실패={summary['failed_count']}\t남음={remaining}\t"
        f"제외={summary['skipped_count']}"
    )
    if remaining == 0 and not args.no_scan:
        run = CandidateScreeningEngine(
            database,
            load_candidate_settings(),
        ).scan()
        print(
            f"후보 재계산\t실행={run.run_id}\t전체={run.universe_count}\t"
            f"적격={run.eligible_count}\t선정={run.selected_count}"
        )


def handle_candidates(args: argparse.Namespace, database: Database) -> None:
    engine = CandidateScreeningEngine(database, load_candidate_settings())
    if args.candidate_command == "scan":
        as_of = _parse_backtest_date(args.as_of) if args.as_of else None
        run = engine.scan(as_of=as_of, limit=args.limit)
        print(
            f"실행={run.run_id}\t기준={run.as_of}\t"
            f"전체={run.universe_count}\t적격={run.eligible_count}\t"
            f"선정={run.selected_count}"
        )
        rows = run.results if args.all else tuple(
            row for row in run.results if row.selected
        )
        for row in rows:
            _print_candidate(row)
        return
    if args.candidate_command == "history":
        rows = engine.latest(selected_only=not args.all)
        for row in rows:
            patterns = ",".join(row["patterns"]) or "-"
            print(
                f"{row['rank'] or '-'}\t{row['stock_code']}\t"
                f"점수={row['total_score']:.2f}\t"
                f"기술={row['technical_score']:.2f}\t"
                f"패턴={row['pattern_score']:.2f}\t"
                f"종합={row['composite_score']:.2f}"
                f"({row['composite_status']})\t"
                f"상대={row['relative_strength_score']:.2f}\t"
                f"변화={row['score_change'] if row['score_change'] is not None else '-'}\t"
                f"패턴={patterns}\t"
                f"사유={row['excluded_reason'] or '선정'}"
            )
        return
    if args.candidate_command == "collect-themes":
        collector = create_market_collector(database)
        result = collector.collect_theme_snapshot(
            top_groups=args.top,
            date_type=args.date_type,
            component_date_type=args.component_date_type,
            max_pages=args.max_pages,
        )
        print(
            f"테마={result.group_count}개 저장={result.saved_group_count}개\t"
            f"구성종목={result.component_count}개 저장={result.saved_component_count}개\t"
            f"페이지={result.page_count}"
        )
        return
    if args.candidate_command == "collect-stock-themes":
        collector = create_market_collector(database)
        for stock_code in args.stock_codes:
            result = collector.collect_stock_theme_snapshot(
                stock_code,
                date_type=args.date_type,
                component_date_type=args.component_date_type,
                max_pages=args.max_pages,
                include_components=not args.no_components,
            )
            print(
                f"{result.stock_code}\t테마={result.group_count}개 "
                f"저장={result.saved_group_count}개\t"
                f"구성종목={result.component_count}개 "
                f"저장={result.saved_component_count}개\t"
                f"페이지={result.page_count}"
            )
        return
    if args.candidate_command == "collect-realtime-ranks":
        collector = create_market_collector(database)
        result = collector.collect_realtime_item_ranking(
            query_type=args.query_type,
            max_pages=args.max_pages,
        )
        rows = database.latest_realtime_item_rankings(
            query_type=args.query_type,
            limit=5,
        )
        print(
            f"조회구분={result.query_type}\t수신={result.received_count}\t"
            f"저장={result.saved_count}\t페이지={result.page_count}\t"
            "점수반영=없음"
        )
        for row in rows:
            print(
                f"{row['big_data_rank'] or '-'}\t{row['stock_code']}\t"
                f"{row['stock_name'] or '-'}\t"
                f"변동={row['rank_change'] if row['rank_change'] is not None else '-'}"
                f"{row['rank_change_sign'] or ''}\t"
                f"등락률={row['base_compare_change_rate'] if row['base_compare_change_rate'] is not None else '-'}"
            )
        return
    if args.candidate_command == "collect-market-ranks":
        collector = create_market_collector(database)
        for rank_type in args.types:
            result = collector.collect_market_ranking(
                rank_type,
                market_type=args.market_type,
                max_pages=args.max_pages,
            )
            print(
                f"{result.rank_type}\tAPI={result.api_id}\t"
                f"수신={result.received_count}\t저장={result.saved_count}\t"
                f"페이지={result.page_count}"
            )
        return
    if args.candidate_command == "theme-journal":
        members = [_parse_theme_member(value) for value in args.member]
        journal_id = database.upsert_theme_journal(
            theme_date=args.date,
            theme_name=args.theme,
            strength=args.strength,
            notes=args.notes,
            members=members,
        )
        print(
            f"테마기록={journal_id}\t날짜={args.date}\t"
            f"테마={args.theme}\t강도={args.strength}\t구성={len(members)}"
        )
        return
    if args.candidate_command == "leader-score":
        trade_date = _parse_backtest_date(args.date) if args.date else None
        result = LeaderStockScoringEngine(database).score(
            trade_date=trade_date,
            limit=args.limit,
        )
        print(
            f"기준={result.trade_date}\t평가={result.scored_count}\t"
            f"저장={result.saved_count}"
        )
        for row in database.latest_leader_stock_scores(
            trade_date=result.trade_date,
            limit=20,
        ):
            print(
                f"{row['role']}\t{row['total_score']:.1f}\t"
                f"{row['stock_code']}\t{row['stock_name'] or '-'}\t"
                f"테마={row['theme_name'] or '-'}\t"
                f"거래={row['trade_amount_score']:.1f}\t"
                f"순위={row['rank_score']:.1f}\t"
                f"테마={row['theme_score']:.1f}\t"
                f"지속={row['persistence_score']:.1f}\t"
                f"일봉={row['chart_score']:.1f}\t"
                f"차단={row['blocked_reason'] or '-'}"
            )
        return
    if args.candidate_command == "leader-outcomes":
        trade_date = _parse_backtest_date(args.date) if args.date else None
        result = LeaderOutcomeLearningEngine(database).evaluate(
            trade_date=trade_date,
            limit=args.limit,
        )
        print(
            f"기준={result.trade_date}\t평가={result.evaluated_count}\t"
            f"저장={result.saved_count}\t사후대장={result.actual_leader_count}"
        )
        for row in database.latest_leader_candidate_outcomes(
            trade_date=result.trade_date,
            limit=args.limit,
        ):
            print(
                f"{row['candidate_rank']:02d}\t"
                f"{'대장' if row['actual_leader'] else '미충족'}\t"
                f"{row['stock_code']}\t{row['stock_name'] or '-'}\t"
                f"후보={row['role_at_pick']}\t점수={row['score_at_pick']:.1f}\t"
                f"당일고가={_format_rate(row['same_day_max_up_rate'])}\t"
                f"당일종가={_format_rate(row['same_day_close_rate'])}\t"
                f"익일고가={_format_rate(row['next_day_max_up_rate'])}\t"
                f"익일종가={_format_rate(row['next_day_close_rate'])}\t"
                f"판정={row['actual_leader_reason']}"
            )
        return
    if args.candidate_command == "leader-report":
        trade_date = _parse_backtest_date(args.date) if args.date else None
        report = LeaderOutcomeReportService(database).report(
            trade_date=trade_date,
            days=args.days,
            limit=args.limit,
        )
        summary = report["summary"]
        print(
            "요약\t"
            f"후보={summary['candidate_count']}\t"
            f"사후대장={summary['actual_leader_count']}\t"
            f"적중률={_format_rate(summary['hit_rate'])}\t"
            f"평균당일고가={_format_rate(summary['average_same_day_max_up_rate'])}\t"
            f"평균익일고가={_format_rate(summary['average_next_day_max_up_rate'])}\t"
            f"차단={summary['blocked_count']}\t"
            f"차단적중={summary['blocked_helpful_count']}\t"
            f"놓친급등={summary['blocked_missed_rally_count']}"
        )
        print("[테마별 적중률]")
        for row in report["by_theme"]:
            print(
                f"{row['theme_name']}\t후보={row['candidate_count']}\t"
                f"사후대장={row['actual_leader_count']}\t"
                f"적중률={_format_rate(row['hit_rate'])}\t"
                f"평균점수={row['average_score_at_pick'] or 0:.1f}\t"
                f"당일고가={_format_rate(row['average_same_day_max_up_rate'])}\t"
                f"익일고가={_format_rate(row['average_next_day_max_up_rate'])}\t"
                f"차단={row['blocked_count']}\t놓친급등={row['missed_rally_count']}"
            )
        print("[점수 구성요소별 평균 성과]")
        for row in report["component_performance"]:
            print(
                f"{row['label']}\t후보={row['candidate_count']}\t"
                f"평균구성점수={row['average_component_score'] or 0:.1f}\t"
                f"적중률={_format_rate(row['hit_rate'])}\t"
                f"성과={_format_rate(row['average_performance_score'])}\t"
                f"당일고가={_format_rate(row['average_same_day_max_up_rate'])}\t"
                f"익일고가={_format_rate(row['average_next_day_max_up_rate'])}"
            )
        blocked = report["blocked_review"]
        print(
            "[blocked 복기]\t"
            f"후보={blocked['candidate_count']}\t"
            f"차단적중={blocked['helpful_count']}\t"
            f"놓친급등={blocked['missed_rally_count']}\t"
            f"중립={blocked['neutral_count']}\t"
            f"차단적중률={_format_rate(blocked['helpful_rate'])}\t"
            f"놓친급등률={_format_rate(blocked['missed_rally_rate'])}"
        )
        for row in blocked["by_reason"]:
            print(
                f"{row['reason']}\t후보={row['candidate_count']}\t"
                f"차단적중={row['helpful_count']}\t"
                f"놓친급등={row['missed_rally_count']}\t"
                f"성과={_format_rate(row['average_performance_score'])}"
            )
        return
    if args.candidate_command == "collect-supply":
        collector = create_market_collector(database)
        end_date = args.end_date or datetime.now(SEOUL_TIMEZONE).strftime("%Y%m%d")
        start_date = args.start_date or (
            datetime.strptime(end_date, "%Y%m%d") - timedelta(days=7)
        ).strftime("%Y%m%d")
        for index, stock_code in enumerate(args.stock_codes):
            normalized = normalize_stock_code(stock_code)
            sources = collector.collect_supply_sources(
                normalized,
                start_date=start_date,
                end_date=end_date,
                market_type=args.market_type,
                max_pages=args.max_pages,
                include_foreign_rank=not args.skip_foreign_rank and index == 0,
            )
            if sources.foreign_broker_ranking is not None:
                foreign = sources.foreign_broker_ranking
                print(
                    f"{foreign.flow_type}\tAPI={foreign.api_id}\t"
                    f"수신={foreign.received_count}\t저장={foreign.saved_count}\t"
                    f"페이지={foreign.page_count}"
                )
            broker = sources.broker
            total = sources.investor_total
            intraday = sources.intraday_investor
            program = sources.program_trade
            print(
                f"{normalized}\t거래원={broker.saved_count}/{broker.received_count}\t"
                f"투자자합계={total.saved_count}/{total.received_count}\t"
                f"장중투자자={intraday.saved_count}/{intraday.received_count}\t"
                f"프로그램={program.saved_count}/{program.received_count}"
            )
        return
    if args.candidate_command == "supply-score":
        stock_codes = (
            [normalize_stock_code(code) for code in args.stock_codes]
            if args.stock_codes
            else None
        )
        result = SupplyScoringEngine(database).score(stock_codes)
        print(
            f"수급평가={result.evaluated_at}\t평가={result.scored_count}\t"
            f"저장={result.saved_count}\t주문영향=없음"
        )
        program = result.program_validation
        print(
            "프로그램검증\t"
            f"수집={program['collected_count']}\t"
            f"강한차단={program['hard_block_count']}\t"
            f"경고={program['watch_count']}\t"
            f"기존차단후보={program['legacy_block_candidate_count']}\t"
            f"완화={program['legacy_block_relaxed_count']}"
        )
        for row in database.latest_supply_scores(limit=20):
            print(
                f"{row['total_score']:.1f}\t{row['stock_code']}\t"
                f"{row['stock_name'] or '-'}\t"
                f"거래원={row['broker_score']:.1f}\t"
                f"외국계={row['foreign_broker_score']:.1f}\t"
                f"투자자={row['investor_score']:.1f}\t"
                f"차단={row['blocked_reason'] or '-'}"
            )
        return
    result = PatternPerformanceService(
        database,
        minimum_history=load_candidate_settings().minimum_history_bars,
    ).run(
        start_date=_parse_backtest_date(args.start),
        end_date=_parse_backtest_date(args.end),
        horizon_days=args.horizon,
    )
    print(
        f"실행={result['run_id']}\t종목={result['stock_count']}\t"
        f"기준평균={result['baseline_average_return']:.2%}"
    )
    for row in result["results"]:
        average = row["average_forward_return"]
        contribution = row["contribution"]
        win_rate = row["win_rate"]
        print(
            f"{row['pattern_name']}\t발생={row['occurrence_count']}\t"
            f"승률={win_rate if win_rate is not None else '-'}\t"
            f"평균={f'{average:.2%}' if average is not None else '-'}\t"
            f"기여={f'{contribution:+.2%}' if contribution is not None else '-'}"
        )


def _parse_theme_member(value: str) -> dict[str, str]:
    parts = [part.strip() for part in value.split(":", 2)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("--member는 종목코드:역할[:종목명] 형식이어야 합니다.")
    role = parts[1]
    if role not in {"leader", "second", "follower", "watch", "blocked"}:
        raise ValueError("테마 구성 역할은 leader, second, follower, watch, blocked 중 하나여야 합니다.")
    return {
        "stock_code": normalize_stock_code(parts[0]),
        "role": role,
        "stock_name": parts[2] if len(parts) > 2 else "",
    }


def _format_rate(value: object) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def _print_candidate(row: object) -> None:
    patterns = ",".join(row.patterns) or "-"
    status = (
        "선정"
        if row.selected
        else (f"제외:{row.excluded_reason}" if row.excluded_reason else "적격")
    )
    print(
        f"{row.rank or '-'}\t{row.stock_code}\t점수={row.total_score:.2f}\t"
        f"종합={row.composite_score:.2f}({row.composite_status})\t"
        f"기술={row.technical_score:.2f}\t패턴={row.pattern_score:.2f}\t"
        f"상대={row.relative_strength_score:.2f}\t유동성={row.liquidity_score:.2f}\t"
        f"관심={row.watched}\t패턴={patterns}\t{status}"
    )


def handle_information(args: argparse.Namespace, database: Database) -> None:
    service = InformationService(database, load_information_settings())
    if args.information_command == "sync-companies":
        count = service.sync_dart_companies()
        print(f"OpenDART 기업 {count:,}개 동기화 완료")
        return
    if args.information_command == "recent":
        for row in service.recent(limit=args.limit):
            print(
                f"{row['published_at']}\t{row['source']}\t"
                f"{row['stock_code'] or '-'}\t{row['event_type']}\t"
                f"{'차단' if row['entry_blocked'] else row['sentiment']}\t"
                f"{row['title']}"
            )
        return
    codes = _information_stock_codes(database, args.stock_codes)
    if args.information_command == "collect-dart":
        end = _parse_backtest_date(args.end) if args.end else date.today()
        begin = (
            _parse_backtest_date(args.start)
            if args.start
            else end - timedelta(days=30)
        )
        print(service.collect_dart(codes, begin_date=begin, end_date=end))
        return
    if args.information_command == "collect-naver":
        print(service.collect_naver(codes, display=args.display))
        return
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    for code, score in service.scores(codes, as_of=as_of, save=True).items():
        print(
            f"{code}\t총점={score['total_score']:+.2f}\t"
            f"긍정={score['positive_score']:+.2f}\t"
            f"부정={score['negative_score']:+.2f}\t"
            f"건수={score['item_count']}\t"
            f"차단={score['entry_blocked']}"
        )


def _information_stock_codes(
    database: Database,
    requested: Sequence[str],
) -> list[str]:
    if requested:
        return [normalize_stock_code(code) for code in requested]
    selected = _candidate_collection_codes(database, 20)
    if selected:
        return selected
    with database.connect() as connection:
        return [
            row["stock_code"]
            for row in connection.execute(
                """
                SELECT stock_code FROM stock_universe
                WHERE tradable = 1
                ORDER BY stock_code
                LIMIT 20
                """
            ).fetchall()
        ]


def handle_ai(args: argparse.Namespace, database: Database) -> None:
    service = AiShadowService(database, load_ai_shadow_settings())
    if args.ai_command == "analyze":
        print(service.analyze_latest(limit=args.limit))
        return
    if args.ai_command == "evaluate":
        print(service.evaluate_performance(horizon_days=args.horizon))
        return
    for row in service.recent(limit=args.limit):
        print(
            f"{row['created_at']}\t{row['stock_code']}\t"
            f"{row['provider']}:{row['model']}\t"
            f"알고리즘={row['algorithm_action']}"
            f"({row['algorithm_score']:.1f})\t"
            f"AI={row['ai_action'] or '-'}"
            f"({row['ai_score'] if row['ai_score'] is not None else '-'})\t"
            f"일치={row['agreement'] or '-'}\t상태={row['status']}"
        )


def handle_replay(args: argparse.Namespace, database: Database) -> None:
    target_date = (
        datetime.now(SEOUL_TIMEZONE).date()
        if args.replay_command == "outcomes-today"
        else _parse_calendar_date(args.target_date)
    )
    result = ReplayOutcomeService(database).recompute(target_date)
    print(
        f"기준일={target_date.isoformat()}\t"
        f"상태={result['status']}\t"
        f"대표판단={result['representative_decisions']}건\t"
        f"반복포함={result['represented_occurrences']}회"
    )
    print(
        "[전방성과]\t"
        f"저장={result['saved']}건\t"
        f"미만기보류={result['pending']}건\t"
        f"캔들부족={result['unavailable']}건\t"
        "구간=30/60/120분"
    )


def create_paper_engine(database: Database) -> PaperTradingEngine:
    return PaperTradingEngine(
        database,
        load_paper_trading_settings(),
        load_risk_settings(),
        load_market_data_settings().max_price_age_seconds,
    )


def create_risk_manager(database: Database) -> RiskManager:
    settings = load_risk_settings()
    paper_engine = create_paper_engine(database)
    return RiskManager(
        database,
        paper_engine,
        settings,
        PaperOrderExecutor(paper_engine),
    )


def create_breakout_strategy(database: Database) -> BreakoutStrategy:
    paper_engine = create_paper_engine(database)
    market_mode_settings = MarketModeService(database).settings_from_base(
        load_market_mode_gate_settings()
    )
    return BreakoutStrategy(
        database,
        paper_engine,
        load_strategy_settings(),
        load_paper_trading_settings(),
        load_risk_settings(),
        PaperOrderExecutor(paper_engine),
        market_mode_settings=market_mode_settings,
        candle_freshness=CandleFreshness(database),
    )


def _handle_paper_review(args: argparse.Namespace, database: Database) -> None:
    if args.paper_command == "review-today":
        target_date = datetime.now(SEOUL_TIMEZONE).date()
    else:
        target_date = date.fromisoformat(args.date)
    service = TradingReviewReport(database)
    if args.no_export:
        report = service.build(target_date)
        path = None
    else:
        path = service.export_json(target_date, args.output)
        report = json.loads(path.read_text(encoding="utf-8"))
    _print_trading_review(report, path)


def _print_trading_review(
    report: dict[str, object],
    path: Path | None,
) -> None:
    summary = report["summary"]
    preventable = report["preventable_loss_estimate"]
    missing_supply = report["missing_supply_performance"]
    force_entry = report["force_entry_performance"]
    stop_after = report["buys_after_stop_loss"]
    print(f"복기일={report['report_date']}")
    print(
        "요약 "
        f"BUY={summary['buy_count']} "
        f"SELL={summary['sell_count']} "
        f"실현손익={summary['realized_pnl']:,}원 "
        f"비용={summary['total_cost']:,}원 "
        f"손절이벤트={summary['stop_loss_count']}회 "
        f"손실손절={summary.get('loss_stop_count', 0)}회 "
        f"수익보호={summary.get('profit_protect_stop_count', 0)}회"
    )
    print(
        "데이터 공백/강제진입 "
        f"수급없는진입손익={missing_supply['realized_pnl']:,}원 "
        f"force_entry손익={force_entry['realized_pnl']:,}원"
    )
    print(
        "사후 차단 추정 "
        f"막았을손실={preventable['total_preventable_loss']:,}원 "
        f"손실체결={preventable['losing_sell_count']}건 "
        f"손절후추가진입={stop_after['count']}건"
    )
    entry_rows = report["entry_type_performance"][:5]
    if entry_rows:
        print("진입유형 손익 TOP5(손실 우선)")
        for row in entry_rows:
            print(
                f"- {row['entry_type']}: "
                f"BUY={row['buy_count']} SELL={row['sell_count']} "
                f"손익={row['realized_pnl']:,}원 "
                f"손실SELL={row['losing_sell_count']} "
                f"손실손절={row.get('loss_stop_count', 0)} "
                f"수익보호={row.get('profit_protect_stop_count', 0)}"
            )
    router = report.get("strategy_router_diagnostics") or {}
    route_rows = (router.get("entry_routes") or [])[:5]
    if route_rows:
        print("Router diagnostics TOP5")
        for row in route_rows:
            print(
                f"- {row['entry_type']}: "
                f"impact={row['order_impact']} "
                f"strategy={row['leader_strategy']} "
                f"legacy={row['legacy_candidate']} "
                f"oldBUY={row.get('old_logic_buy_count', 0)} "
                f"newBUY={row.get('new_logic_buy_count', 0)} "
                f"BUY={row['buy_count']} PnL={row['realized_pnl']:,}"
            )
    legacy_rows = (router.get("legacy_candidates") or [])[:5]
    if legacy_rows:
        print("Legacy route candidates TOP5")
        for row in legacy_rows:
            print(
                f"- {row['entry_type']}: "
                f"impact={row['order_impact']} "
                f"reasons={','.join(row.get('legacy_reasons') or [])} "
                f"oldBUY={row.get('old_logic_buy_count', 0)} "
                f"shadow={row.get('legacy_shadow_count', 0)} "
                f"BUY={row['buy_count']} PnL={row['realized_pnl']:,}"
            )
    retirement = report.get("legacy_retirement_review") or {}
    retirement_rows = (retirement.get("routes") or [])[:5]
    retirement_labels = retirement.get("recommendation_labels") or {}
    if retirement_rows:
        print("Legacy retirement candidates TOP5")
        for row in retirement_rows:
            recommendation = row.get("recommendation") or "keep_observe"
            print(
                f"- {row['legacy_route']}: "
                f"recommend={retirement_labels.get(recommendation, recommendation)} "
                f"score={row.get('retirement_score', 0)} "
                f"oldBUY={row.get('old_logic_buy_count', 0)} "
                f"newBUY={row.get('new_logic_buy_count', 0)} "
                f"missed={row.get('missed_rally_count', 0)} "
                f"PnL={row.get('realized_pnl', 0):,}"
            )
    repeat_rows = report["stock_repeat_buys"][:5]
    if repeat_rows:
        print("반복매수 종목 TOP5(손실 우선)")
        for row in repeat_rows:
            print(
                f"- {row['stock_code']}: "
                f"BUY={row['buy_count']} "
                f"수량={row['total_quantity']} "
                f"손익={row['realized_pnl']:,}원"
            )
    if path is not None:
        print(f"JSON={path}")


def handle_paper(args: argparse.Namespace, database: Database) -> None:
    if args.paper_command in {"review-today", "review-date"}:
        _handle_paper_review(args, database)
        return

    engine = create_paper_engine(database)
    if args.paper_command == "init":
        account_id = engine.create_account(args.account, args.cash)
        logging.info("모의계좌 생성 완료: %s (ID=%s)", args.account, account_id)
        return

    if args.paper_command == "account":
        summary = engine.account_summary(args.account)
        print(
            f"계좌={summary['name']} "
            f"현금={summary['cash_balance']:,}원 "
            f"평가금액={summary['market_value']:,}원 "
            f"총자산={summary['total_equity']:,}원 "
            f"실현손익={summary['realized_pnl']:,}원 "
            f"미실현손익={summary['unrealized_pnl']:,}원"
        )
        for position in summary["positions"]:
            current_price = (
                f"{position['current_price']:,}원"
                if position["current_price"] is not None
                else "-"
            )
            unrealized = (
                f"{position['unrealized_pnl']:,}원"
                if position["unrealized_pnl"] is not None
                else "-"
            )
            print(
                f"{position['stock_code']}\t"
                f"{position['quantity']:,}주\t"
                f"평균원가={position['average_price']:,.2f}원\t"
                f"현재가={current_price}\t"
                f"미실현손익={unrealized}"
            )
        return

    if args.paper_command in {"buy", "sell"}:
        result = engine.place_market_order(
            args.account,
            args.stock_code,
            args.paper_command,
            args.quantity,
            reason=args.reason,
            client_order_id=args.client_order_id,
        )
        print(
            f"주문={result.order_id} {result.side.upper()} "
            f"{result.stock_code} {result.quantity:,}주 "
            f"시장가={result.market_price:,}원 "
            f"체결가={result.fill_price:,}원 "
            f"수수료={result.commission:,}원 "
            f"세금={result.tax:,}원 "
            f"실현손익={result.realized_pnl:,}원 "
            f"현금={result.cash_balance:,}원"
        )
        return

    rows = (
        engine.list_orders(args.account, args.limit)
        if args.paper_command == "orders"
        else engine.list_fills(args.account, args.limit)
    )
    if not rows:
        print("조회 결과가 없습니다.")
        return
    for row in rows:
        print("\t".join(f"{key}={value}" for key, value in row.items()))


def handle_risk(args: argparse.Namespace, database: Database) -> None:
    manager = create_risk_manager(database)
    if args.risk_command == "set":
        manager.configure_position(
            args.account,
            args.stock_code,
            stop_loss_rate=args.stop_loss,
            take_profit_rate=args.take_profit,
            trailing_activation_rate=args.trailing_activation,
            trailing_drawdown_rate=args.trailing_drawdown,
        )
        logging.info("%s 리스크 설정 완료", normalize_stock_code(args.stock_code))
        return

    if args.risk_command == "check":
        evaluations = (
            manager.evaluate_account(args.account)
            if args.account
            else manager.evaluate_all_accounts()
        )
        for result in evaluations:
            event = result.event_type or "hold"
            print(
                f"{result.account_name}\t{result.stock_code}\t"
                f"현재가={result.current_price:,}원\t"
                f"최고가={result.highest_price:,}원\t"
                f"판정={event}\t주문={result.order_id or '-'}"
            )
        if not evaluations:
            print("평가할 포지션이 없습니다.")
        return

    if args.risk_command == "close":
        results = manager.liquidate_market_close(
            args.account,
            force=args.force,
        )
        for result in results:
            print(
                f"{result.account_name}\t{result.stock_code}\t"
                f"장마감청산\t주문={result.order_id}"
            )
        if not results:
            print("청산할 포지션이 없거나 장 마감 청산 시간이 아닙니다.")
        return

    rows = (
        manager.status(args.account)
        if args.risk_command == "status"
        else manager.events(args.account, args.limit)
    )
    if not rows:
        print("조회 결과가 없습니다.")
        return
    for row in rows:
        print("\t".join(f"{key}={value}" for key, value in row.items()))


def handle_strategy(args: argparse.Namespace, database: Database) -> None:
    strategy = create_breakout_strategy(database)
    if args.strategy_command == "history":
        rows = strategy.history(args.account, args.limit)
        if not rows:
            print("조회 결과가 없습니다.")
            return
        for row in rows:
            print("\t".join(f"{key}={value}" for key, value in row.items()))
        return

    stock_codes = resolve_stock_codes(database, args.stock_codes)
    results = strategy.run(
        args.account,
        stock_codes,
        execute=args.strategy_command == "run",
    )
    for result in results:
        signal = result["signal"]
        print(
            f"{result['stock_code']}\t신호={result['passed']}\t"
            f"거래량={signal.volume_pass}\t돌파={signal.breakout_pass}\t"
            f"20MA={signal.moving_average_pass}\t추격제한={signal.chase_pass}\t"
            f"처리={result['action']}\t주문={result['order_id'] or '-'}\t"
            f"{result['message']}"
        )


def _parse_backtest_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("백테스트 날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _parse_calendar_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("거래일 날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def handle_calendar(args: argparse.Namespace, database: Database) -> None:
    calendar = MarketCalendar(database)
    if args.calendar_command == "set":
        market_date = _parse_calendar_date(args.market_date)
        calendar.set_day(
            market_date,
            args.status,
            name=args.name,
            source=args.source,
        )
        print(
            f"{market_date.isoformat()}\t상태={args.status}\t"
            f"사유={args.name or '-'}\t출처={args.source}"
        )
        return

    if args.calendar_command == "remove":
        market_date = _parse_calendar_date(args.market_date)
        removed = calendar.remove_day(market_date)
        print(
            f"{market_date.isoformat()}\t"
            f"{'수동 예외 삭제' if removed else '등록된 수동 예외 없음'}"
        )
        return

    if args.calendar_command == "list":
        rows = calendar.list_exceptions(args.year)
        if not rows:
            print(f"{args.year}년 등록 휴장·예외가 없습니다.")
            return
        for row in rows:
            print(
                f"{row.market_date.isoformat()}\t"
                f"상태={row.status}\t"
                f"사유={row.name or '-'}\t"
                f"출처={row.source}"
            )
        return

    market_date = (
        _parse_calendar_date(args.market_date)
        if args.market_date
        else datetime.now(SEOUL_TIMEZONE).date()
    )
    row = calendar.get_day(market_date)
    print(
        f"{row.market_date.isoformat()}\t"
        f"거래일={row.status == 'open'}\t"
        f"상태={row.status}\t"
        f"사유={row.name or '-'}\t"
        f"출처={row.source}"
    )


def handle_backtest(args: argparse.Namespace, database: Database) -> None:
    repository = BacktestRepository(database)
    if args.backtest_command == "history":
        rows = repository.list_runs(args.limit)
        if not rows:
            print("저장된 백테스트가 없습니다.")
            return
        for row in rows:
            print(
                f"ID={row['id']}\t"
                f"전략={row['strategy_name']}\t"
                f"기간={row['start_date']}~{row['end_date']}\t"
                f"종목={','.join(row['stock_codes'])}\t"
                f"수익률={row['total_return_rate']:.2%}\t"
                f"MDD={row['max_drawdown_rate']:.2%}\t"
                f"거래={row['trade_count']}회\t"
                f"실행={row['created_at']}"
            )
        return

    if args.backtest_command == "report":
        report = BacktestReportService(repository).build(args.run_id)
        summary = report["summary"]
        print(
            f"백테스트 ID={args.run_id} "
            f"순손익={summary['net_pnl']:,}원 "
            f"평균수익={summary['average_win']:,}원 "
            f"평균손실={summary['average_loss']:,}원 "
            f"총비용={summary['total_cost']:,}원"
        )
        print(
            f"비용상세=수수료 {summary['total_commission']:,}원, "
            f"세금 {summary['total_tax']:,}원, "
            f"슬리피지 {summary['total_slippage']:,}원"
        )
        _print_backtest_report_group(
            "종목별",
            "stock_code",
            report["by_stock"],
        )
        _print_backtest_report_group(
            "청산일별",
            "date",
            report["by_date"],
        )
        _print_backtest_report_group(
            "청산사유별",
            "exit_reason",
            report["by_exit_reason"],
        )
        return

    if args.backtest_command == "export":
        paths = BacktestReportExporter(repository).export(
            args.run_id,
            args.export_format,
            args.output,
        )
        for path in paths:
            print(path.resolve())
        return

    if args.backtest_command == "compare":
        comparison = BacktestComparisonService(
            BacktestReportService(repository)
        ).compare(args.run_ids)
        for row in comparison["runs"]:
            payoff = (
                f"{row['payoff_ratio']:.2f}"
                if row["payoff_ratio"] is not None
                else "-"
            )
            print(
                f"ID={row['run_id']}\t"
                f"기간={row['start_date']}~{row['end_date']}\t"
                f"종목={','.join(row['stock_codes'])}\t"
                f"수익률={row['total_return_rate']:.2%}\t"
                f"MDD={row['max_drawdown_rate']:.2%}\t"
                f"승률={row['win_rate']:.2%}\t"
                f"손익비={payoff}\t"
                f"순손익={row['net_pnl']:,}원\t"
                f"비용={row['total_cost']:,}원\t"
                f"거래={row['trade_count']}회"
            )
        leader_labels = (
            ("highest_return", "최고 수익률", ".2%"),
            ("lowest_drawdown", "최저 MDD", ".2%"),
            ("highest_win_rate", "최고 승률", ".2%"),
            ("highest_payoff_ratio", "최고 손익비", ".2f"),
            ("lowest_cost", "최저 비용", ","),
        )
        print("[비교 요약]")
        for key, label, value_format in leader_labels:
            leader = comparison["leaders"][key]
            if leader is None:
                print(f"{label}=계산 불가")
                continue
            value = format(leader["value"], value_format)
            suffix = "원" if key == "lowest_cost" else ""
            print(f"{label}=ID {leader['run_id']} ({value}{suffix})")
        for warning in comparison["warnings"]:
            print(f"주의: {warning}")
        return

    if args.backtest_command == "show":
        row = repository.get_run(args.run_id)
        if row is None:
            raise ValueError(
                f"저장된 백테스트를 찾을 수 없습니다: {args.run_id}"
            )
        payoff = (
            f"{row['payoff_ratio']:.2f}"
            if row["payoff_ratio"] is not None
            else "-"
        )
        print(
            f"ID={row['id']} 전략={row['strategy_name']} "
            f"기간={row['start_date']}~{row['end_date']} "
            f"종목={','.join(row['stock_codes'])} "
            f"초기자금={row['initial_cash']:,}원 "
            f"최종자산={row['final_equity']:,}원 "
            f"총수익률={row['total_return_rate']:.2%} "
            f"MDD={row['max_drawdown_rate']:.2%} "
            f"거래={row['trade_count']}회 "
            f"승률={row['win_rate']:.2%} "
            f"손익비={payoff}"
        )
        print(
            f"비용=수수료 {row['commission_rate']:.4%}, "
            f"매도세 {row['sell_tax_rate']:.4%}, "
            f"슬리피지 {row['slippage_rate']:.4%}"
        )
        if args.trades:
            for trade in repository.list_trades(args.run_id):
                print(
                    f"거래#{trade['sequence']}\t"
                    f"{trade['stock_code']}\t"
                    f"{trade['entry_time']} -> {trade['exit_time']}\t"
                    f"{trade['quantity']}주\t"
                    f"{trade['entry_price']:,}원 -> "
                    f"{trade['exit_price']:,}원\t"
                    f"손익={trade['realized_pnl']:,}원\t"
                    f"수익률={trade['return_rate']:.2%}\t"
                    f"청산={trade['exit_reason']}"
                )
        if args.equity is not None:
            for point in repository.list_equity_points(
                args.run_id,
                args.equity,
            ):
                print(
                    f"자산#{point['sequence']}\t"
                    f"{point['point_time']}\t"
                    f"현금={point['cash_balance']:,}원\t"
                    f"평가={point['market_value']:,}원\t"
                    f"총자산={point['total_equity']:,}원"
                )
        return

    if args.backtest_command == "validate":
        stock_codes = resolve_stock_codes(database, args.stock_codes)
        validation = BacktestDataValidator(database).validate(
            stock_codes,
            start_date=_parse_backtest_date(args.start),
            end_date=_parse_backtest_date(args.end),
            interval_minutes=args.interval,
            moving_average_period=(
                load_strategy_settings().moving_average_period
            ),
        )
        _print_data_validation(validation)
        if not validation.is_ready:
            raise ValueError("백테스트 데이터 검사에서 오류가 발견됐습니다.")
        return

    paper_settings = load_paper_trading_settings()
    stock_codes = resolve_stock_codes(database, args.stock_codes)
    strategy_settings = load_strategy_settings()
    risk_settings = load_risk_settings()
    config = BacktestConfig(
        start_date=_parse_backtest_date(args.start),
        end_date=_parse_backtest_date(args.end),
        initial_cash=(
            args.cash if args.cash is not None else paper_settings.initial_cash
        ),
        commission_rate=paper_settings.commission_rate,
        sell_tax_rate=paper_settings.sell_tax_rate,
        slippage_rate=paper_settings.slippage_rate,
        interval_minutes=args.interval,
    )
    validation = BacktestDataValidator(database).validate(
        stock_codes,
        start_date=config.start_date,
        end_date=config.end_date,
        interval_minutes=config.interval_minutes,
        moving_average_period=strategy_settings.moving_average_period,
    )
    _print_data_validation_issues(validation)
    if not validation.is_ready:
        raise ValueError("백테스트 데이터 검사에서 오류가 발견됐습니다.")
    result = BreakoutBacktester(
        database,
        strategy_settings,
        risk_settings,
    ).run(stock_codes, config)
    run_id = repository.save(
        stock_codes,
        config,
        strategy_settings,
        risk_settings,
        result,
    )
    payoff = (
        f"{result.payoff_ratio:.2f}"
        if result.payoff_ratio is not None
        else "-"
    )
    print(
        f"기간={result.start_date}~{result.end_date} "
        f"초기자금={result.initial_cash:,}원 "
        f"최종자산={result.final_equity:,}원 "
        f"총수익률={result.total_return_rate:.2%} "
        f"MDD={result.max_drawdown_rate:.2%} "
        f"거래={result.trade_count}회 "
        f"승률={result.win_rate:.2%} "
        f"손익비={payoff} "
        f"저장ID={run_id}"
    )
    if args.trades:
        for trade in result.trades:
            print(
                f"{trade.stock_code}\t"
                f"{trade.entry_time} -> {trade.exit_time}\t"
                f"{trade.quantity}주\t"
                f"{trade.entry_price:,}원 -> {trade.exit_price:,}원\t"
                f"손익={trade.realized_pnl:,}원\t"
                f"수익률={trade.return_rate:.2%}\t"
                f"청산={trade.exit_reason}"
            )


def handle_system(args: argparse.Namespace, database: Database) -> None:
    service = OperationsService(database)
    if args.system_command == "approval":
        approval_service = PaperOperationApprovalService(database)
        result = approval_service.evaluate(
            required_days=args.days,
            interval_seconds=args.interval,
            minimum_round_trips=args.round_trips,
        )
        print(
            f"판정={result.decision}\t"
            f"기준일={result.as_of_date.isoformat()}\t"
            f"검증거래일={','.join(day.isoformat() for day in result.reviewed_dates)}"
        )
        for check in result.checks:
            print(
                f"[{check.status.upper()}]\t"
                f"{check.code}\t{check.message}"
            )
        print("[수동 확인]")
        for index, item in enumerate(result.manual_checklist, start=1):
            print(f"{index}. {item}")
        if args.json or args.output is not None:
            path = approval_service.export_json(result, args.output)
            print(f"JSON={path.resolve()}")
        if args.fail_on_reject and result.decision != "ready_for_manual_approval":
            raise SystemExit(1)
        return

    if args.system_command == "alerts":
        settings = load_operations_alert_settings()
        if args.delay_seconds is not None or args.consecutive is not None:
            settings = type(settings)(
                collection_delay_seconds=(
                    args.delay_seconds
                    if args.delay_seconds is not None
                    else settings.collection_delay_seconds
                ),
                consecutive_failures=(
                    args.consecutive
                    if args.consecutive is not None
                    else settings.consecutive_failures
                ),
            )
        alert_service = OperationsAlertService(
            database,
            settings,
            service,
        )
        alert_service.evaluate(interval_seconds=args.interval)
        rows = alert_service.list_alerts(include_resolved=args.all)
        if not rows:
            print("활성 운영 경보가 없습니다.")
            return
        for row in rows:
            print(
                f"{row['alert_key']}\t"
                f"상태={row['status']}\t"
                f"심각도={row['severity']}\t"
                f"발생={row['occurrence_count']}회\t"
                f"최근={row['last_triggered_at']}\t"
                f"{row['message']}"
            )
        return

    if args.system_command == "daily-report":
        target_date = (
            _parse_calendar_date(args.date)
            if args.date
            else datetime.now(SEOUL_TIMEZONE).date()
        )
        report_service = DailyOperationsReport(database)
        report = report_service.build(
            target_date,
            expected_interval_seconds=args.interval,
        )
        validation = report["validation"]
        collection = report["collection"]["summary"]
        event_severity = report["events"]["severity"]
        strategy = report["strategy"]["summary"]
        forward = report["forward_outcomes"]
        risk = report["risk"]["summary"]
        accounts = report["trading"]["accounts"]
        order_count = sum(row["order_count"] for row in accounts)
        filled_count = sum(row["filled_orders"] for row in accounts)
        rejected_count = sum(row["rejected_orders"] for row in accounts)
        realized_pnl = sum(row["realized_pnl"] for row in accounts)
        total_cost = sum(row["total_cost"] for row in accounts)
        print(
            f"리포트일={report['report_date']}\t"
            f"검증상태={validation['status']}\t"
            f"거래일={validation['trading_day']}"
        )
        print(
            "[수집]\t"
            f"실행={collection['run_count']}회\t"
            f"성공={collection['success_runs']}회\t"
            f"부분실패={collection['partial_runs']}회\t"
            f"실패={collection['failed_runs']}회\t"
            f"종목성공={collection['success_count']}건\t"
            f"종목실패={collection['failure_count']}건"
        )
        print(
            "[이벤트]\t"
            f"정보={event_severity['info']}건\t"
            f"경고={event_severity['warning']}건\t"
            f"오류={event_severity['error']}건"
        )
        print(
            "[거래]\t"
            f"주문={order_count}건\t"
            f"체결={filled_count}건\t"
            f"거절={rejected_count}건\t"
            f"실현손익={realized_pnl:,}원\t"
            f"비용={total_cost:,}원"
        )
        print(
            "[자동화]\t"
            f"전략신호={strategy['signal_count']}건\t"
            f"전략실행={strategy['executed_count']}건\t"
            f"전략실패={strategy['failed_count']}건\t"
            f"리스크이벤트={risk['event_count']}건\t"
            f"리스크실패={risk['failed_count']}건"
        )
        print(
            "[전방성과]\t"
            f"상태={forward['status']}\t"
            f"대표판단={forward['representative_decisions']}건\t"
            f"반복포함={forward['represented_occurrences']}회\t"
            f"저장={forward['saved']}건\t"
            f"미만기보류={forward['pending']}건\t"
            f"캔들부족={forward['unavailable']}건"
        )
        for row in report["prices"]:
            print(
                "[가격]\t"
                f"{row['stock_code']}\t"
                f"수집={row['snapshot_count']}건\t"
                f"시작={row['first_collected_at'] or '-'}\t"
                f"종료={row['last_collected_at'] or '-'}"
            )
        for event in report["events"]["notable"]:
            print(
                f"[{event['severity'].upper()}]\t"
                f"{event['occurred_at']}\t"
                f"{event['module']}/{event['event_type']}\t"
                f"{event['message']}"
            )
        if args.json or args.output is not None:
            path = report_service.export_json(
                target_date,
                args.output,
                expected_interval_seconds=args.interval,
            )
            print(f"JSON={path.resolve()}")
        return

    if args.system_command == "daily-check":
        target_date = (
            _parse_calendar_date(args.date)
            if args.date
            else datetime.now(SEOUL_TIMEZONE).date()
        )
        result = DailyOperationsChecker(database).check(
            target_date,
            expected_interval_seconds=args.interval,
        )
        print(
            f"검증일={result.target_date.isoformat()}\t"
            f"상태={result.status}\t"
            f"거래일={result.trading_day}\t"
            f"종일판정={result.full_day_expected}"
        )
        for check in result.checks:
            print(
                f"[{check.status.upper()}]\t"
                f"{check.code}\t{check.message}"
            )
        summary = result.summary
        print(
            "[요약]\t"
            f"수집={summary.get('run_count', 0)}회\t"
            f"주문={summary['orders']}건\t"
            f"체결={summary['fills']}건\t"
            f"거절={summary['rejected_orders']}건\t"
            f"전략신호={summary['strategy_signals']}건\t"
            f"리스크이벤트={summary['risk_events']}건\t"
            f"실현손익={summary['realized_pnl']:,}원"
        )
        if args.fail_on_error and result.status == "failed":
            raise SystemExit(1)
        return

    if args.system_command == "prices":
        max_age_seconds = (
            args.max_age_seconds
            if args.max_age_seconds is not None
            else load_market_data_settings().max_price_age_seconds
        )
        if max_age_seconds < 1:
            raise ValueError("가격 신선도 기준은 1초 이상이어야 합니다.")
        stock_codes = WatchlistService(database).active_codes()
        if not stock_codes:
            print("활성 관심 종목이 없습니다.")
            return
        now = datetime.now()
        stale_count = 0
        missing_count = 0
        for stock_code in stock_codes:
            price = database.get_latest_price(stock_code)
            if price is None:
                missing_count += 1
                print(f"{stock_code}\t상태=없음")
                continue
            price_time = datetime.fromisoformat(str(price["price_time"]))
            if price_time.tzinfo is not None:
                price_time = price_time.replace(tzinfo=None)
            age_seconds = max(0, int((now - price_time).total_seconds()))
            stale = age_seconds > max_age_seconds
            stale_count += int(stale)
            print(
                f"{stock_code}\t"
                f"상태={'오래됨' if stale else '정상'}\t"
                f"가격={int(price['price']):,}원\t"
                f"시각={price['price_time']}\t"
                f"경과={age_seconds}초\t"
                f"출처={price['price_source']}"
            )
        print(
            f"기준={max_age_seconds}초\t"
            f"오래됨={stale_count}개\t"
            f"없음={missing_count}개"
        )
        return

    if args.system_command == "recover":
        lock_path = Path(load_database_path()).with_suffix(".collector.lock")
        lock = ProcessLock(lock_path)
        status = lock.inspect()
        if status.active:
            print(f"수집기가 실행 중입니다. PID={status.pid} 잠금={lock_path}")
            return
        lock_recovered = lock.recover_stale()
        run_ids = service.recover_running_runs(args.stale_minutes)
        print(
            f"잠금복구={lock_recovered}\t"
            f"실행복구={len(run_ids)}건\t"
            f"실행ID={','.join(str(run_id) for run_id in run_ids) or '-'}"
        )
        return

    if args.system_command == "events":
        rows = service.list_events(
            limit=args.limit,
            module=args.module,
            severity=args.severity,
        )
        if not rows:
            print("운영 이벤트가 없습니다.")
            return
        for row in rows:
            print(
                f"ID={row['id']}\t"
                f"실행={row['run_id'] or '-'}\t"
                f"{row['occurred_at']}\t"
                f"{row['severity'].upper()}\t"
                f"{row['module']}/{row['event_type']}\t"
                f"종목={row['stock_code'] or '-'}\t"
                f"{row['message']}"
            )
        return

    if args.system_command == "health":
        health = service.health(args.stale_minutes)
        print(
            f"전체상태={'정상' if health['healthy'] else '확인필요'} "
            f"신선도기준={health['stale_minutes']}분 "
            f"활성경보={len(health['active_alerts'])}건"
        )
        if not health["checks"]:
            print("기록된 운영 모듈 상태가 없습니다.")
            return
        for row in health["checks"]:
            print(
                f"{row['module']}\t"
                f"상태={row['status']}\t"
                f"마지막성공={row['last_success_at'] or '-'}\t"
                f"마지막실패={row['last_failure_at'] or '-'}\t"
                f"오래됨={row['stale']}\t"
                f"{row['last_message'] or '-'}"
            )
        for row in health["active_alerts"]:
            print(
                f"경보={row['alert_key']}\t"
                f"심각도={row['severity']}\t"
                f"발생={row['occurrence_count']}회\t"
                f"{row['message']}"
            )
        return

    statuses = service.module_status()
    print("[모듈 상태]")
    if not statuses:
        print("기록된 운영 모듈 상태가 없습니다.")
    for row in statuses:
        print(
            f"{row['module']}\t"
            f"상태={row['status']}\t"
            f"마지막성공={row['last_success_at'] or '-'}\t"
            f"마지막실패={row['last_failure_at'] or '-'}\t"
            f"{row['last_message'] or '-'}"
        )
    print("[최근 실행]")
    runs = service.list_runs(args.limit)
    if not runs:
        print("운영 실행 이력이 없습니다.")
        return
    for row in runs:
        print(
            f"ID={row['id']}\t"
            f"유형={row['run_type']}\t"
            f"상태={row['status']}\t"
            f"시작={row['started_at']}\t"
            f"종료={row['completed_at'] or '-'}\t"
            f"성공={row['success_count']}\t"
            f"실패={row['failure_count']}"
        )


def _print_data_validation(result: DataValidationResult) -> None:
    for summary in result.summaries:
        print(
            f"{summary.stock_code}\t"
            f"기간={summary.first_day or '-'}~{summary.last_day or '-'}\t"
            f"거래일={summary.trading_days}일\t"
            f"분봉={summary.minute_bars}개\t"
            f"일별={summary.minimum_day_bars}~"
            f"{summary.maximum_day_bars}개\t"
            f"일봉={summary.daily_bars}개"
        )
    _print_data_validation_issues(result)
    print(
        f"검사결과={'통과' if result.is_ready else '오류'} "
        f"오류={result.error_count}건 경고={result.warning_count}건"
    )


def _print_data_validation_issues(result: DataValidationResult) -> None:
    for issue in result.issues:
        day = f" {issue.candle_day}" if issue.candle_day else ""
        print(
            f"{issue.severity.upper()}: "
            f"{issue.stock_code}{day} [{issue.code}] {issue.message}"
        )


def _print_backtest_report_group(
    title: str,
    key_name: str,
    rows: list[dict],
) -> None:
    print(f"[{title}]")
    if not rows:
        print("거래 없음")
        return
    for row in rows:
        print(
            f"{row[key_name]}\t"
            f"거래={row['trade_count']}회\t"
            f"승률={row['win_rate']:.2%}\t"
            f"순손익={row['net_pnl']:,}원\t"
            f"평균수익률={row['average_return_rate']:.2%}\t"
            f"비용={row['total_cost']:,}원"
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args()

    if args.command == "dashboard":
        from dashboard.server import run_dashboard

        run_dashboard(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return

    database = create_database()

    if args.command == "watchlist":
        handle_watchlist(args, database)
        return

    if args.command == "universe":
        handle_universe(args, database)
        return

    if args.command == "candidates":
        handle_candidates(args, database)
        return

    if args.command == "information":
        handle_information(args, database)
        return

    if args.command == "ai":
        handle_ai(args, database)
        return

    if args.command == "replay":
        handle_replay(args, database)
        return

    if args.command == "collect":
        stock_codes = (
            [normalize_stock_code(code) for code in args.stock_codes]
            if args.stock_codes
            else None
        )
        tracker = CollectorOperationsTracker(
            OperationsService(database),
            "collect_once",
            metadata={"requested_codes": stock_codes or []},
        )
        periodic = create_periodic_collector(
            database,
            stock_codes,
            operations_tracker=tracker,
        )
        _, failures = periodic.collect_once()
        if failures:
            raise SystemExit(1)
        return

    if args.command == "run":
        if args.trade_pages < 1:
            raise ValueError("체결강도 페이지 수는 1 이상이어야 합니다.")
        if args.candidate_limit < 1:
            raise ValueError("후보 종목 수는 1 이상이어야 합니다.")
        if args.unattended:
            candidate_run = CandidateScreeningEngine(
                database,
                load_candidate_settings(),
            ).scan(limit=args.candidate_limit)
            if candidate_run.selected_count < 1:
                raise RuntimeError(
                    "무인 운전을 시작할 평가 가능한 후보 종목이 없습니다."
                )
        operations_service = OperationsService(database)
        notifier = TelegramNotifier.from_env()
        alert_service = OperationsAlertService(
            database,
            load_operations_alert_settings(),
            operations_service,
            notifier,
        )
        tracker = CollectorOperationsTracker(
            operations_service,
            "periodic_collect",
            metadata={
                "interval_seconds": args.interval,
                "market_hours_only": not args.all_hours,
                "auto_strategy": args.auto_strategy,
                "account_name": args.account,
                "with_depth": args.with_depth,
                "with_trades": args.with_trades,
                "unattended": args.unattended,
                "preopen_scan": args.preopen_scan or args.unattended,
                "candidate_limit": args.candidate_limit,
            },
            cycle_completed_callback=lambda _: alert_service.evaluate(
                interval_seconds=args.interval
            ),
        )
        periodic = create_periodic_collector(
            database,
            auto_strategy=args.auto_strategy,
            account_name=args.account,
            with_depth=args.with_depth,
            with_trades=args.with_trades,
            trade_pages=args.trade_pages,
            operations_tracker=tracker,
            candidate_mode=args.unattended,
            candidate_limit=args.candidate_limit,
            preopen_scan=args.preopen_scan or args.unattended,
        )
        lock_path = Path(load_database_path()).with_suffix(".collector.lock")
        runtime_control = CollectorRuntimeControl(load_database_path())
        with ProcessLock(lock_path) as process_lock:
            if process_lock.recovered_stale:
                operations_service.record_event(
                    "system",
                    "stale_lock_recovered",
                    "warning",
                    f"오래된 수집기 잠금 파일을 복구했습니다: {lock_path}",
                )
            operations_service.recover_running_runs(stale_minutes=10)
            if args.unattended:
                send_unattended_start_notification(
                    notifier,
                    account=args.account,
                    interval=args.interval,
                    candidate_limit=args.candidate_limit,
                )
            try:
                periodic.run(
                    args.interval,
                    market_hours_only=not args.all_hours,
                    stop_requested=runtime_control.stop_requested,
                    auto_stop_requested=(
                        lambda: (
                            datetime.now(SEOUL_TIMEZONE)
                            .time()
                            .replace(tzinfo=None)
                            >= datetime.strptime("15:35", "%H:%M").time()
                            and MarketCalendar(database).is_trading_day(
                                datetime.now(SEOUL_TIMEZONE).date()
                            )
                        )
                        if args.unattended
                        else None
                    ),
                )
            except KeyboardInterrupt:
                logging.info("주기 수집을 종료합니다.")
            finally:
                runtime_control.clear_stop_request()
        if args.unattended:
            report_path = DailyOperationsReport(database).export_json(
                datetime.now(SEOUL_TIMEZONE).date(),
                expected_interval_seconds=args.interval,
            )
            logging.info("무인 운전 일일 보고서 저장: %s", report_path)
            send_daily_report_notification(notifier, report_path)
        return

    if args.command == "candles":
        handle_candles(args, database)
        return

    if args.command == "market":
        handle_market(args, database)
        return

    if args.command == "account":
        handle_account(args, database)
        return

    if args.command == "paper":
        handle_paper(args, database)
        return

    if args.command == "risk":
        handle_risk(args, database)
        return

    if args.command == "strategy":
        handle_strategy(args, database)
        return

    if args.command == "backtest":
        handle_backtest(args, database)
        return

    if args.command == "calendar":
        handle_calendar(args, database)
        return

    if args.command == "system":
        handle_system(args, database)


if __name__ == "__main__":
    try:
        main()
    except (PaperTradingError, ValueError) as exc:
        logging.error("%s", exc)
        raise SystemExit(1)
