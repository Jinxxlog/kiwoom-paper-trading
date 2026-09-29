from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

from config.settings import (
    CandidateSettings,
    load_composite_score_settings,
    load_information_settings,
)
from data.watchlist import normalize_stock_code
from db.database import Database
from screening.market_product import is_market_product_item, is_market_product_name
from information.service import InformationService
from screening.features import DailyBar, FeatureResult, calculate_features, json_safe_features
from strategy.composite import CompositeScoringEngine
from strategy.registry import register_composite_strategy


DAY_TRADING_TRADE_AMOUNT = 50_000_000_000
LEADER_ROLE_BONUS = {
    "leader": 35.0,
    "second": 24.0,
    "follower": 12.0,
    "watch": 4.0,
}
LEADER_ROLE_PRIORITY = {
    "leader": 4,
    "second": 3,
    "follower": 2,
    "watch": 1,
}
MARKET_RANK_TYPES = (
    "trade_amount",
    "realtime_inquiry",
    "expected_change",
    "change_rate",
)


@dataclass(frozen=True)
class CandidateResult:
    stock_code: str
    stock_name: str | None
    rank: int | None
    selected: bool
    excluded_reason: str | None
    total_score: float
    technical_score: float
    pattern_score: float
    relative_strength_score: float
    liquidity_score: float
    information_score: float
    information_blocked: bool
    information_item_count: int
    composite_score: float
    composite_status: str
    composite_confidence: float
    composite_decision_id: int | None
    relative_strength: float | None
    watched: bool
    features: dict[str, Any]
    patterns: tuple[str, ...]
    reasons: tuple[str, ...]
    score_change: float | None


@dataclass(frozen=True)
class CandidateRun:
    run_id: int
    as_of: str
    universe_count: int
    eligible_count: int
    selected_count: int
    results: tuple[CandidateResult, ...]


class StockUniverseService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(
        self,
        stock_code: str,
        *,
        stock_name: str | None = None,
        market: str | None = None,
        sector: str | None = None,
        tradable: bool = True,
        management: bool = False,
        halted: bool = False,
        overheated: bool = False,
        source: str = "manual",
    ) -> str:
        code = normalize_stock_code(stock_code)
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO stock_universe(
                    stock_code, stock_name, market, sector, tradable,
                    management, halted, overheated, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stock_code) DO UPDATE SET
                    stock_name = COALESCE(excluded.stock_name, stock_universe.stock_name),
                    market = COALESCE(excluded.market, stock_universe.market),
                    sector = COALESCE(excluded.sector, stock_universe.sector),
                    tradable = excluded.tradable,
                    management = excluded.management,
                    halted = excluded.halted,
                    overheated = excluded.overheated,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    stock_name,
                    market,
                    sector,
                    int(tradable),
                    int(management),
                    int(halted),
                    int(overheated),
                    source,
                    timestamp,
                ),
            )
        return code

    def import_csv(self, path: Path) -> int:
        count = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                self.upsert(
                    row.get("stock_code", ""),
                    stock_name=row.get("stock_name") or None,
                    market=row.get("market") or None,
                    sector=row.get("sector") or None,
                    tradable=_csv_bool(row.get("tradable"), True),
                    management=_csv_bool(row.get("management"), False),
                    halted=_csv_bool(row.get("halted"), False),
                    overheated=_csv_bool(row.get("overheated"), False),
                    source="csv",
                )
                count += 1
        return count

    def list(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT u.*, COALESCE(w.enabled, 0) AS watched,
                       (
                           SELECT COUNT(*) FROM candles c
                           WHERE c.stock_code = u.stock_code
                             AND c.candle_type = 'daily'
                       ) AS daily_bars
                FROM stock_universe u
                LEFT JOIN watchlist w ON w.stock_code = u.stock_code
                ORDER BY u.stock_code
                """
            ).fetchall()
        return [dict(row) for row in rows]


class CandidateScreeningEngine:
    def __init__(self, database: Database, settings: CandidateSettings) -> None:
        self.database = database
        self.settings = settings
        self._validate_settings()

    def _validate_settings(self) -> None:
        if self.settings.minimum_history_bars < 21:
            raise ValueError("후보 검색 최소 일봉 수는 21 이상이어야 합니다.")
        if self.settings.minimum_price < 0:
            raise ValueError("후보 검색 최소 주가는 0 이상이어야 합니다.")
        if self.settings.minimum_average_volume < 0:
            raise ValueError("최소 평균 거래량은 0 이상이어야 합니다.")
        if self.settings.minimum_average_trade_amount < 0:
            raise ValueError("최소 평균 거래대금은 0 이상이어야 합니다.")
        if self.settings.maximum_daily_volatility <= 0:
            raise ValueError("최대 일간 변동성은 0보다 커야 합니다.")
        if self.settings.maximum_order_book_spread_rate <= 0:
            raise ValueError("최대 호가 스프레드는 0보다 커야 합니다.")
        if self.settings.selected_count < 1:
            raise ValueError("기본 후보 개수는 1 이상이어야 합니다.")
        if self.settings.theme_score_limit < 0:
            raise ValueError("테마 점수 한도는 0 이상이어야 합니다.")
        if self.settings.theme_max_age_minutes < 0:
            raise ValueError("테마 데이터 신선도 기준은 0 이상이어야 합니다.")
        if self.settings.theme_collection_top_groups < 1:
            raise ValueError("테마 자동 수집 그룹 수는 1 이상이어야 합니다.")
        if self.settings.theme_collection_interval_minutes < 0:
            raise ValueError("테마 자동 수집 간격은 0 이상이어야 합니다.")
        if self.settings.realtime_rank_query_type not in {"1", "2", "3", "4", "5"}:
            raise ValueError("실시간 조회종목 순위 구분은 1~5여야 합니다.")
        if self.settings.realtime_rank_collection_interval_minutes < 0:
            raise ValueError("실시간 조회종목 순위 수집 간격은 0 이상이어야 합니다.")
        if self.settings.realtime_rank_max_pages < 1:
            raise ValueError("실시간 조회종목 순위 수집 페이지 수는 1 이상이어야 합니다.")
        if self.settings.maximum_open_gap_rate < 0:
            raise ValueError("최대 갭 상승률은 0 이상이어야 합니다.")
        if self.settings.maximum_day_gain_rate < 0:
            raise ValueError("최대 당일 상승률은 0 이상이어야 합니다.")

    def scan(
        self,
        *,
        as_of: date | datetime | None = None,
        limit: int | None = None,
    ) -> CandidateRun:
        if limit is not None and limit < 1:
            raise ValueError("후보 개수는 1 이상이어야 합니다.")
        as_of_time = _as_of_datetime(as_of)
        universe = self._load_universe()
        information_scores = InformationService(
            self.database,
            load_information_settings(required=False),
        ).scores(
            (row["stock_code"] for row in universe),
            as_of=as_of_time,
            save=True,
        )
        provisional = []
        for row in universe:
            bars = self._load_bars(row["stock_code"], as_of_time)
            feature = calculate_features(bars) if bars else None
            excluded = self._excluded_reason(row, bars, feature, as_of_time)
            information = information_scores.get(row["stock_code"], {})
            if excluded is None and information.get("entry_blocked"):
                excluded = "뉴스·공시 진입 차단"
            provisional.append((row, bars, feature, excluded))

        eligible_features = [
            feature
            for _, _, feature, excluded in provisional
            if feature is not None and excluded is None
        ]
        market_return = _average_present(
            feature.values.get("return20") for feature in eligible_features
        )
        sector_returns: dict[str, float] = {}
        sectors = {
            str(row["sector"])
            for row, _, feature, excluded in provisional
            if row["sector"] and feature is not None and excluded is None
        }
        for sector in sectors:
            sector_returns[sector] = _average_present(
                feature.values.get("return20")
                for row, _, feature, excluded in provisional
                if row["sector"] == sector
                and feature is not None
                and excluded is None
        )
        theme_contexts = self._load_theme_contexts(as_of_time)
        leader_contexts = self._load_leader_contexts(as_of_time)
        market_rank_contexts = self._load_market_rank_contexts(as_of_time)

        results = []
        for row, _, feature, excluded in provisional:
            result = self._score(
                row,
                feature,
                excluded,
                market_return,
                sector_returns.get(str(row["sector"])) if row["sector"] else None,
                as_of_time,
                information_scores.get(row["stock_code"]),
                theme_contexts.get(row["stock_code"], ()),
                leader_contexts.get(row["stock_code"]),
                market_rank_contexts.get(row["stock_code"], {}),
            )
            results.append(result)
        composite_settings = load_composite_score_settings()
        composite_engine = CompositeScoringEngine(composite_settings)
        composite_results = []
        evaluations = []
        for result in results:
            evaluation = composite_engine.evaluate(
                stock_code=result.stock_code,
                decided_at=as_of_time,
                technical_score=result.technical_score,
                pattern_score=result.pattern_score,
                relative_strength_score=result.relative_strength_score,
                liquidity_score=result.liquidity_score,
                information_score=result.information_score,
                market_return=market_return,
                excluded_reason=result.excluded_reason,
                information_item_count=result.information_item_count,
                has_features=bool(result.features),
            )
            evaluations.append(evaluation)
            composite_results.append(
                CandidateResult(
                    **{
                        **asdict(result),
                        "composite_score": float(evaluation.decision.score),
                        "composite_status": evaluation.status,
                        "composite_confidence": float(
                            evaluation.decision.confidence
                        ),
                    }
                )
            )
        results = composite_results
        eligible = sorted(
            (result for result in results if result.excluded_reason is None),
            key=lambda item: (
                -item.total_score,
                -int(item.features.get("leader_priority") or 0),
                item.stock_code,
            ),
        )
        selected_count = min(limit or self.settings.selected_count, len(eligible))
        rank_by_code = {
            result.stock_code: index
            for index, result in enumerate(eligible, start=1)
        }
        selected_codes = {
            result.stock_code for result in eligible[:selected_count]
        }
        ranked = [
            CandidateResult(
                **{
                    **asdict(result),
                    "rank": rank_by_code.get(result.stock_code),
                    "selected": result.stock_code in selected_codes,
                }
            )
            for result in results
        ]
        ranked.sort(
            key=lambda item: (
                item.excluded_reason is not None,
                item.rank if item.rank is not None else 10**9,
                item.stock_code,
            )
        )
        version_id, config_id = register_composite_strategy(
            self.database,
            composite_settings,
        )
        decision_ids = self.database.save_shadow_strategy_decisions(
            [evaluation.decision for evaluation in evaluations],
            strategy_version_id=version_id,
            strategy_config_id=config_id,
            source_data_time=as_of_time.isoformat(timespec="seconds"),
            message="shadow mode: 주문에 사용하지 않음",
            state_keys={
                evaluation.decision.stock_code: (
                    f"composite:{evaluation.status}:"
                    f"{evaluation.blocked_reason or ''}"
                )
                for evaluation in evaluations
            },
        )
        ranked = [
            CandidateResult(
                **{
                    **asdict(result),
                    "composite_decision_id": decision_ids.get(
                        result.stock_code
                    ),
                }
            )
            for result in ranked
        ]
        run_id = self._save_run(as_of_time, universe, ranked)
        return CandidateRun(
            run_id=run_id,
            as_of=as_of_time.isoformat(timespec="seconds"),
            universe_count=len(universe),
            eligible_count=len(eligible),
            selected_count=selected_count,
            results=tuple(ranked),
        )

    def latest(self, *, selected_only: bool = False) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            run = connection.execute(
                "SELECT id FROM candidate_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if run is None:
                return []
            query = """
                SELECT s.*, u.stock_name, u.market, u.sector
                FROM candidate_scores s
                LEFT JOIN stock_universe u ON u.stock_code = s.stock_code
                WHERE s.run_id = ?
            """
            parameters: list[Any] = [run["id"]]
            if selected_only:
                query += " AND s.selected = 1"
            query += " ORDER BY s.selected DESC, s.rank, s.stock_code"
            rows = connection.execute(query, parameters).fetchall()
        return [_decode_candidate_row(row) for row in rows]

    def _load_universe(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO stock_universe(stock_code, source, updated_at)
                SELECT DISTINCT c.stock_code, 'candle', datetime('now', 'localtime')
                FROM candles c
                WHERE NOT EXISTS (
                    SELECT 1 FROM stock_universe u
                    WHERE u.stock_code = c.stock_code
                )
                """
            )
            rows = connection.execute(
                """
                SELECT u.*, COALESCE(w.enabled, 0) AS watched
                FROM stock_universe u
                LEFT JOIN watchlist w ON w.stock_code = u.stock_code
                ORDER BY u.stock_code
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _load_bars(self, stock_code: str, as_of: datetime) -> list[DailyBar]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT candle_time, open_price, high_price, low_price,
                       close_price, volume, trade_amount
                FROM candles
                WHERE stock_code = ?
                  AND candle_type = 'daily'
                  AND interval_minutes = 0
                  AND candle_time <= ?
                  AND open_price IS NOT NULL
                  AND high_price IS NOT NULL
                  AND low_price IS NOT NULL
                  AND close_price IS NOT NULL
                ORDER BY candle_time, id
                """,
                (stock_code, as_of.isoformat(timespec="seconds")),
            ).fetchall()
        return [
            DailyBar(
                candle_time=row["candle_time"],
                open=int(row["open_price"]),
                high=int(row["high_price"]),
                low=int(row["low_price"]),
                close=int(row["close_price"]),
                volume=int(row["volume"] or 0),
                trade_amount=(
                    int(row["trade_amount"])
                    if row["trade_amount"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def _excluded_reason(
        self,
        row: dict[str, Any],
        bars: list[DailyBar],
        feature: FeatureResult | None,
        as_of: datetime,
    ) -> str | None:
        if not row["tradable"]:
            return "거래 불가 종목"
        if row["management"]:
            return "관리 종목"
        if row["halted"]:
            return "거래 정지 종목"
        if row["overheated"]:
            return "단기 과열 종목"
        if len(bars) < self.settings.minimum_history_bars:
            return (
                f"일봉 부족 {len(bars)}/{self.settings.minimum_history_bars}"
            )
        assert feature is not None
        values = feature.values
        if int(values["close"] or 0) < self.settings.minimum_price:
            return "최소 주가 미달"
        if float(values["average_volume20"] or 0) < self.settings.minimum_average_volume:
            return "평균 거래량 미달"
        if (
            float(values["average_trade_amount20"] or 0)
            < self.settings.minimum_average_trade_amount
        ):
            return "평균 거래대금 미달"
        if (
            values["volatility20"] is not None
            and float(values["volatility20"])
            > float(self.settings.maximum_daily_volatility)
        ):
            return "과도한 변동성"
        if (
            values.get("gap_rate") is not None
            and float(values["gap_rate"])
            > float(self.settings.maximum_open_gap_rate)
        ):
            return "과열 갭 상승"
        if (
            values.get("day_gain_rate") is not None
            and float(values["day_gain_rate"])
            > float(self.settings.maximum_day_gain_rate)
        ):
            return "당일 상승률 과열"
        gap = self._order_book_gap(row["stock_code"], as_of)
        if gap:
            return gap
        return None

    def _order_book_gap(self, stock_code: str, as_of: datetime) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT best_ask_price, best_ask_quantity,
                       best_bid_price, best_bid_quantity
                FROM order_book_snapshots
                WHERE stock_code = ? AND collected_at <= ?
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (stock_code, as_of.isoformat(timespec="seconds")),
            ).fetchone()
        if row is None:
            return None
        ask = row["best_ask_price"]
        bid = row["best_bid_price"]
        if not ask or not bid or not row["best_ask_quantity"] or not row["best_bid_quantity"]:
            return "호가 공백"
        midpoint = (ask + bid) / 2
        if midpoint <= 0 or (ask - bid) / midpoint > float(
            self.settings.maximum_order_book_spread_rate
        ):
            return "호가 스프레드 과다"
        return None

    def _score(
        self,
        row: dict[str, Any],
        feature: FeatureResult | None,
        excluded: str | None,
        market_return: float,
        sector_return: float | None,
        as_of: datetime,
        information: dict[str, Any] | None,
        theme_contexts: tuple[dict[str, Any], ...],
        leader_context: dict[str, Any] | None,
        market_rank_contexts: dict[str, dict[str, Any]],
    ) -> CandidateResult:
        if is_market_product_name(row.get("stock_name")):
            excluded = excluded or "market_product_excluded"
        raw_information_score = float((information or {}).get("total_score") or 0)
        information_score = min(0.0, raw_information_score)
        information_blocked = bool((information or {}).get("entry_blocked"))
        information_item_count = int((information or {}).get("item_count") or 0)
        if feature is None:
            values: dict[str, Any] = {}
            patterns: tuple[str, ...] = ()
            reasons = (excluded or "기술 데이터 없음",)
            technical = pattern = relative_score = liquidity = total = 0.0
            relative = None
        else:
            values = json_safe_features(feature.values)
            if raw_information_score > 0:
                values["information_positive_score_ignored"] = round(
                    raw_information_score,
                    4,
                )
            patterns = feature.patterns
            reasons_list = list(feature.reasons)
            current_return = feature.values.get("return20")
            relative = (
                float(current_return) - market_return
                if current_return is not None
                else None
            )
            sector_relative = (
                float(current_return) - sector_return
                if current_return is not None and sector_return is not None
                else 0.0
            )
            relative_score = max(
                -20.0,
                min(
                    20.0,
                    (relative or 0.0) * 60 + sector_relative * 40,
                ),
            )
            amount = float(feature.values.get("average_trade_amount20") or 0)
            volume = float(feature.values.get("average_volume20") or 0)
            volume_ratio = feature.values.get("volume_ratio")
            theme_score, theme_context = self._theme_score(feature, theme_contexts)
            if theme_context:
                values["theme"] = theme_context
                values["theme_score"] = round(theme_score, 4)
            leader_theme_score, leader_theme_candidate = self._leader_theme_score(
                feature, theme_context
            )
            values["leader_theme_score"] = round(leader_theme_score, 4)
            values["leader_theme_candidate"] = leader_theme_candidate
            leader_bonus, leader_priority, leader_context = self._leader_score_bonus(
                leader_context
            )
            if leader_context:
                values["leader_stock_score"] = leader_context
                values["leader_score_bonus"] = round(leader_bonus, 4)
                values["leader_priority"] = leader_priority
            market_rank_score, market_rank_summary = self._market_rank_score(
                market_rank_contexts
            )
            if market_rank_summary:
                values["market_rank"] = market_rank_summary
                values["market_rank_score"] = round(market_rank_score, 4)
            liquidity = min(
                15.0,
                self._liquidity_base_score(amount, volume)
                + self._day_trading_liquidity_bonus(amount),
            )
            technical = feature.technical_score
            pattern = feature.pattern_score
            total = (
                technical
                + pattern
                + relative_score
                + liquidity
                + information_score
                + theme_score
                + leader_bonus
                + market_rank_score
            )
            if (information or {}).get("item_count"):
                reasons_list.append(
                    "뉴스·공시 "
                    f"{int(information['item_count'])}건 "
                    f"{information_score:+.2f}점"
                )
            if information_blocked:
                reasons_list.append("악재 공시로 신규 진입 차단")
            if relative is not None:
                reasons_list.append(f"시장 대비 20일 상대강도 {relative:+.2%}")
            if amount >= DAY_TRADING_TRADE_AMOUNT:
                reasons_list.append("단타 유동성 기준 500억원 이상")
            elif amount >= DAY_TRADING_TRADE_AMOUNT * 0.4:
                reasons_list.append("단타 유동성 기준 근접")
            if volume_ratio is not None and float(volume_ratio) >= 2:
                reasons_list.append(
                    f"최근 거래량 20일 평균 대비 {float(volume_ratio):.2f}배"
                )
            if (
                feature.values.get("previous_high") is not None
                and feature.values.get("previous_low") is not None
            ):
                reasons_list.append(
                    "전일 고가/저가 "
                    f"{int(feature.values['previous_high']):,}/"
                    f"{int(feature.values['previous_low']):,}"
                )
            if feature.values.get("previous_trade_amount"):
                reasons_list.append(
                    "전일 거래대금 "
                    f"{int(feature.values['previous_trade_amount']) // 100_000_000:,}억원"
                )
            if theme_context:
                reasons_list.append(
                    "테마 "
                    f"{theme_context['theme_rank']}위 "
                    f"{theme_context['theme_name']} "
                    f"{theme_score:+.2f}점"
                )
                if theme_context.get("relative_trade_amount"):
                    reasons_list.append(
                        "테마 내 당일 거래대금 "
                        f"20일 평균 대비 {theme_context['relative_trade_amount']:.2f}배"
                    )
            if leader_theme_score:
                reason_label = (
                    "대장주 후보"
                    if leader_theme_candidate
                    else "대장주 잠재력"
                )
                reasons_list.append(
                    f"{reason_label} {leader_theme_score:+.2f}점"
                )
            if leader_context:
                role = str(leader_context.get("role") or "watch")
                if leader_context.get("blocked"):
                    blocked_reason = leader_context.get("blocked_reason") or role
                    reasons_list.append(f"leader score blocked: {blocked_reason}")
                elif leader_bonus:
                    reasons_list.append(
                        f"leader score {role} {leader_bonus:+.2f}pt"
                    )
            if market_rank_summary:
                trade_rank = (
                    market_rank_summary.get("trade_amount") or {}
                ).get("rank")
                if trade_rank is not None:
                    reasons_list.append(
                        f"0187 거래대금 {int(trade_rank)}위 "
                        f"{market_rank_score:+.2f}점"
                    )
                elif market_rank_score:
                    reasons_list.append(
                        f"시장 순위 반복 {market_rank_score:+.2f}점"
                    )
            if row["watched"]:
                reasons_list.append("관심 종목")
            else:
                reasons_list.append("관심 종목 외 자동 검색")
            reasons_list.append(
                "저장된 호가 확인"
                if self._has_order_book(row["stock_code"], as_of)
                else "호가 미확인: 실시간 후보 단계에서 확인 필요"
            )
            if excluded:
                reasons_list.append(f"제외: {excluded}")
            reasons = tuple(reasons_list)
        return CandidateResult(
            stock_code=row["stock_code"],
            stock_name=row["stock_name"],
            rank=None,
            selected=False,
            excluded_reason=excluded,
            total_score=round(total, 4),
            technical_score=round(technical, 4),
            pattern_score=round(pattern, 4),
            relative_strength_score=round(relative_score, 4),
            liquidity_score=round(liquidity, 4),
            information_score=round(information_score, 4),
            information_blocked=information_blocked,
            information_item_count=information_item_count,
            composite_score=0.0,
            composite_status="pending",
            composite_confidence=0.0,
            composite_decision_id=None,
            relative_strength=round(relative, 8) if relative is not None else None,
            watched=bool(row["watched"]),
            features=values,
            patterns=patterns,
            reasons=reasons,
            score_change=None,
        )

    def _load_leader_contexts(self, as_of: datetime) -> dict[str, dict[str, Any]]:
        selected_date = as_of.date().isoformat()
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                {Database.leader_stock_scores_latest_cte()}
                SELECT MAX(trade_date) AS trade_date
                FROM latest_leader_stock_scores
                WHERE trade_date <= ?
                """,
                (selected_date,),
            ).fetchone()
            trade_date = row["trade_date"] if row else None
            if trade_date is None:
                return {}
            rows = connection.execute(
                f"""
                {Database.leader_stock_scores_latest_cte()}
                SELECT *
                FROM latest_leader_stock_scores
                WHERE trade_date = ?
                ORDER BY blocked, total_score DESC, trade_amount_score DESC,
                         rank_score DESC, stock_code
                """,
                (trade_date,),
            ).fetchall()
        contexts: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            try:
                item["reasons"] = json.loads(item.get("reasons_json") or "[]")
            except json.JSONDecodeError:
                item["reasons"] = []
            try:
                item["components"] = json.loads(item.get("components_json") or "{}")
            except json.JSONDecodeError:
                item["components"] = {}
            item["blocked"] = bool(item.get("blocked"))
            item.pop("reasons_json", None)
            item.pop("components_json", None)
            contexts.setdefault(str(item["stock_code"]), item)
        return contexts

    @staticmethod
    def _leader_score_bonus(
        context: dict[str, Any] | None,
    ) -> tuple[float, int, dict[str, Any] | None]:
        if not context:
            return 0.0, 0, None
        role = str(context.get("role") or "watch")
        blocked = bool(context.get("blocked"))
        priority = 0 if blocked else LEADER_ROLE_PRIORITY.get(role, 0)
        if blocked:
            bonus = 0.0
        else:
            bonus = LEADER_ROLE_BONUS.get(role, 0.0)
            total_score = float(context.get("total_score") or 0)
            if role == "leader" and total_score >= 100:
                bonus = min(40.0, bonus + 5.0)
        compact = {
            "trade_date": context.get("trade_date"),
            "scored_at": context.get("scored_at"),
            "stock_code": context.get("stock_code"),
            "stock_name": context.get("stock_name"),
            "theme_code": context.get("theme_code"),
            "theme_name": context.get("theme_name"),
            "role": role,
            "total_score": float(context.get("total_score") or 0),
            "rank_score": float(context.get("rank_score") or 0),
            "trade_amount_score": float(context.get("trade_amount_score") or 0),
            "theme_score": float(context.get("theme_score") or 0),
            "persistence_score": float(context.get("persistence_score") or 0),
            "chart_score": float(context.get("chart_score") or 0),
            "risk_score": float(context.get("risk_score") or 0),
            "blocked": blocked,
            "blocked_reason": context.get("blocked_reason"),
            "reasons": context.get("reasons") or [],
        }
        return bonus, priority, compact

    def _load_market_rank_contexts(
        self,
        as_of: datetime,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        placeholders = ",".join("?" for _ in MARKET_RANK_TYPES)
        with self.database.connect() as connection:
            snapshots = connection.execute(
                f"""
                SELECT rank_type, MAX(id) AS snapshot_id
                FROM market_rank_snapshots
                WHERE rank_type IN ({placeholders})
                  AND observed_at <= ?
                GROUP BY rank_type
                """,
                (*MARKET_RANK_TYPES, as_of.isoformat(timespec="seconds")),
            ).fetchall()
            snapshot_ids = [int(row["snapshot_id"]) for row in snapshots]
            if not snapshot_ids:
                return {}
            item_placeholders = ",".join("?" for _ in snapshot_ids)
            rows = connection.execute(
                f"""
                SELECT s.observed_at, s.market_type, i.*
                FROM market_rank_items i
                JOIN market_rank_snapshots s ON s.id = i.snapshot_id
                WHERE i.snapshot_id IN ({item_placeholders})
                """,
                snapshot_ids,
            ).fetchall()
        contexts: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["metadata"] = {}
            if is_market_product_item(item):
                continue
            stock_code = str(item.get("stock_code") or "")
            rank_type = str(item.get("rank_type") or "")
            if stock_code and rank_type:
                contexts.setdefault(stock_code, {})[rank_type] = item
        return contexts

    @staticmethod
    def _market_rank_score(
        contexts: dict[str, dict[str, Any]],
    ) -> tuple[float, dict[str, dict[str, Any]]]:
        if not contexts:
            return 0.0, {}
        score = 0.0
        repeated = 0
        summary: dict[str, dict[str, Any]] = {}
        for rank_type in MARKET_RANK_TYPES:
            context = contexts.get(rank_type)
            if not context:
                continue
            rank = context.get("rank")
            if rank is None:
                continue
            rank_int = int(rank)
            repeated += 1
            trade_amount = context.get("trade_amount")
            if rank_type == "trade_amount":
                if rank_int <= 10:
                    score += 35.0
                elif rank_int <= 30:
                    score += 25.0
                elif rank_int <= 60:
                    score += 15.0
                elif rank_int <= 100:
                    score += 8.0
                if trade_amount is not None and int(trade_amount) >= DAY_TRADING_TRADE_AMOUNT:
                    score = max(score, 28.0)
            elif rank_type == "expected_change":
                if rank_int <= 10:
                    score += 6.0
                elif rank_int <= 30:
                    score += 4.0
            elif rank_type == "realtime_inquiry":
                if rank_int <= 10:
                    score += 5.0
                elif rank_int <= 30:
                    score += 3.0
            elif rank_type == "change_rate":
                if rank_int <= 10:
                    score += 4.0
                elif rank_int <= 30:
                    score += 2.0
            summary[rank_type] = {
                "rank": rank_int,
                "stock_name": context.get("stock_name"),
                "current_price": context.get("current_price"),
                "change_rate": context.get("change_rate"),
                "trade_amount": trade_amount,
                "observed_at": context.get("observed_at"),
            }
        if repeated >= 3:
            score += 6.0
        elif repeated >= 2:
            score += 3.0
        return min(45.0, score), summary

    def _load_theme_contexts(
        self,
        as_of: datetime,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        if self.settings.theme_score_limit == 0:
            return {}
        min_collected_at = (
            as_of - timedelta(minutes=self.settings.theme_max_age_minutes)
        ).isoformat(timespec="seconds")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT tc.stock_code, tc.stock_name, tc.current_price,
                       tc.change_rate AS component_change_rate,
                       tc.accumulated_volume, tc.collected_at,
                       tg.theme_code, tg.theme_name, tg.stock_count,
                       tg.change_rate AS theme_change_rate,
                       tg.rising_stock_count, tg.falling_stock_count,
                       tg.day_profit_rate,
                       ROW_NUMBER() OVER (
                           ORDER BY COALESCE(tg.change_rate, 0) DESC,
                                    COALESCE(tg.day_profit_rate, 0) DESC,
                                    tg.theme_code
                       ) AS theme_rank
                FROM theme_components tc
                JOIN theme_groups tg ON tg.theme_code = tc.theme_code
                WHERE tc.collected_at <= ?
                  AND tc.collected_at >= ?
                ORDER BY tc.stock_code, theme_rank
                """,
                (as_of.isoformat(timespec="seconds"), min_collected_at),
            ).fetchall()
        contexts: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            current_price = item.get("current_price")
            accumulated_volume = item.get("accumulated_volume")
            item["trade_amount"] = (
                int(current_price) * int(accumulated_volume)
                if current_price is not None and accumulated_volume is not None
                else None
            )
            contexts.setdefault(str(item["stock_code"]), []).append(item)
        return {key: tuple(value) for key, value in contexts.items()}

    def _theme_score(
        self,
        feature: FeatureResult,
        contexts: tuple[dict[str, Any], ...],
    ) -> tuple[float, dict[str, Any] | None]:
        best_score = 0.0
        best_context = None
        average_amount = float(feature.values.get("average_trade_amount20") or 0)
        for context in contexts:
            score = 0.0
            rank = int(context["theme_rank"] or 10**9)
            if rank <= 5:
                score += 4.0
            elif rank <= 10:
                score += 3.0
            elif rank <= 20:
                score += 2.0
            theme_change = float(context["theme_change_rate"] or 0)
            if theme_change >= 5:
                score += 3.0
            elif theme_change >= 3:
                score += 2.0
            elif theme_change > 0:
                score += 1.0
            component_change = float(context["component_change_rate"] or 0)
            if component_change >= 7:
                score += 3.0
            elif component_change >= 3:
                score += 2.0
            elif component_change > 0:
                score += 1.0
            trade_amount = float(context["trade_amount"] or 0)
            if trade_amount >= DAY_TRADING_TRADE_AMOUNT:
                score += 3.0
            elif trade_amount >= DAY_TRADING_TRADE_AMOUNT * 0.4:
                score += 2.0
            elif trade_amount >= DAY_TRADING_TRADE_AMOUNT * 0.2:
                score += 1.0
            relative_amount = (
                trade_amount / average_amount
                if trade_amount and average_amount
                else None
            )
            if relative_amount is not None:
                if relative_amount >= 3:
                    score += 3.0
                elif relative_amount >= 2:
                    score += 2.0
                elif relative_amount >= 1.2:
                    score += 1.0
            stock_count = int(context["stock_count"] or 0)
            rising_count = int(context["rising_stock_count"] or 0)
            rising_ratio = rising_count / stock_count if stock_count else None
            if rising_ratio is not None:
                if rising_ratio >= 0.7:
                    score += 2.0
                elif rising_ratio >= 0.5:
                    score += 1.0
            score = min(float(self.settings.theme_score_limit), score)
            if score > best_score:
                best_context = {
                    "theme_code": context["theme_code"],
                    "theme_name": context["theme_name"],
                    "theme_rank": rank,
                    "theme_change_rate": theme_change,
                    "component_change_rate": component_change,
                    "trade_amount": int(trade_amount) if trade_amount else None,
                    "relative_trade_amount": round(relative_amount, 4)
                    if relative_amount is not None
                    else None,
                    "rising_ratio": round(rising_ratio, 4)
                    if rising_ratio is not None
                    else None,
                    "collected_at": context["collected_at"],
                }
                best_score = score
        return best_score, best_context

    def _leader_theme_score(
        self,
        feature: FeatureResult,
        context: dict[str, Any] | None,
    ) -> tuple[float, bool]:
        if context is None:
            return 0.0, False
        score = 0.0
        rank = int(context.get("theme_rank") or 10**9)
        if rank <= 5:
            score += 3.0
        elif rank <= 10:
            score += 2.0
        elif rank <= 20:
            score += 1.0
        previous_return = float(feature.values.get("previous_return") or 0)
        previous_return2 = float(feature.values.get("previous_return2") or 0)
        if previous_return > 0:
            score += 1.0
        if previous_return2 > 0:
            score += 1.0
        if previous_return >= 0.02 and previous_return2 >= 0.02:
            score += 1.0
        relative_trade_amount = float(feature.values.get("relative_trade_amount20") or 0)
        if relative_trade_amount >= 3.0:
            score += 2.0
        elif relative_trade_amount >= 2.0:
            score += 1.0
        if feature.values.get("big_bullish_candle"):
            score += 2.0
        leader_candidate = score >= 4.0
        return min(8.0, score), leader_candidate

    def _liquidity_base_score(self, amount: float, volume: float) -> float:
        return min(
            8.0,
            4.0 * amount / max(self.settings.minimum_average_trade_amount, 1)
            + 4.0 * volume / max(self.settings.minimum_average_volume, 1),
        )

    @staticmethod
    def _day_trading_liquidity_bonus(amount: float) -> float:
        if amount >= DAY_TRADING_TRADE_AMOUNT:
            return 7.0
        if amount >= DAY_TRADING_TRADE_AMOUNT * 0.4:
            return 4.0
        if amount >= DAY_TRADING_TRADE_AMOUNT * 0.2:
            return 2.0
        return 0.0

    def _has_order_book(self, stock_code: str, as_of: datetime) -> bool:
        with self.database.connect() as connection:
            return (
                connection.execute(
                    """
                    SELECT 1
                    FROM order_book_snapshots
                    WHERE stock_code = ? AND collected_at <= ?
                    LIMIT 1
                    """,
                    (stock_code, as_of.isoformat(timespec="seconds")),
                ).fetchone()
                is not None
            )

    def _save_run(
        self,
        as_of: datetime,
        universe: list[dict[str, Any]],
        results: list[CandidateResult],
    ) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        eligible_count = sum(result.excluded_reason is None for result in results)
        selected_count = sum(result.selected for result in results)
        with self.database.connect() as connection:
            run_id = int(
                connection.execute(
                    """
                    INSERT INTO candidate_runs(
                        as_of, universe_count, eligible_count, selected_count,
                        config_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        as_of.isoformat(timespec="seconds"),
                        len(universe),
                        eligible_count,
                        selected_count,
                        json.dumps(asdict(self.settings), default=str, ensure_ascii=False),
                        timestamp,
                    ),
                ).lastrowid
            )
            for result in results:
                prior = connection.execute(
                    """
                    SELECT total_score
                    FROM candidate_scores
                    WHERE stock_code = ?
                    ORDER BY evaluated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (result.stock_code,),
                ).fetchone()
                change = (
                    result.total_score - float(prior["total_score"])
                    if prior is not None
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO candidate_scores(
                        run_id, stock_code, rank, selected, excluded_reason,
                        total_score, technical_score, pattern_score,
                        relative_strength_score, liquidity_score,
                        information_score, information_blocked, close_price,
                        information_item_count, composite_score,
                        composite_status, composite_confidence,
                        composite_decision_id,
                        rsi, atr, relative_strength, watched, patterns_json,
                        features_json, reasons_json, score_change, evaluated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        result.stock_code,
                        result.rank,
                        int(result.selected),
                        result.excluded_reason,
                        result.total_score,
                        result.technical_score,
                        result.pattern_score,
                        result.relative_strength_score,
                        result.liquidity_score,
                        result.information_score,
                        int(result.information_blocked),
                        result.features.get("close"),
                        result.information_item_count,
                        result.composite_score,
                        result.composite_status,
                        result.composite_confidence,
                        result.composite_decision_id,
                        result.features.get("rsi14"),
                        result.features.get("atr14"),
                        result.relative_strength,
                        int(result.watched),
                        json.dumps(result.patterns, ensure_ascii=False),
                        json.dumps(result.features, ensure_ascii=False),
                        json.dumps(result.reasons, ensure_ascii=False),
                        change,
                        timestamp,
                    ),
                )
        return run_id


def _as_of_datetime(value: date | datetime | None) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.max.replace(microsecond=0))


def _average_present(values: Iterable[Any]) -> float:
    present = [float(value) for value in values if value is not None]
    return sum(present) / len(present) if present else 0.0


def _csv_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _decode_candidate_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["patterns"] = json.loads(item.pop("patterns_json"))
    item["features"] = json.loads(item.pop("features_json"))
    item["reasons"] = json.loads(item.pop("reasons_json"))
    return item
