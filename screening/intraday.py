from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import mean
from typing import Any, Sequence

from db.database import Database


@dataclass(frozen=True)
class IntradayBar:
    candle_time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class VwapPoint:
    candle_time: str
    vwap: float
    close: float
    low: float
    high: float
    volume: float


@dataclass(frozen=True)
class IntradayPatternResult:
    stock_code: str
    session_date: str
    interval_minutes: int
    source_data_time: str
    current_price: float
    vwap: float
    vwap_distance_rate: float
    reclaim_state: str
    reclaim_score: float
    bounce_state: str
    bounce_score: float
    liquidity_sweep_state: str
    liquidity_sweep_side: str
    liquidity_sweep_score: float
    liquidity_level: float | None
    fvg_state: str
    fvg_side: str
    fvg_score: float
    fvg_lower: float | None
    fvg_upper: float | None
    fvg_fill_rate: float | None
    fvg_formed_at: str | None
    order_block_state: str
    order_block_side: str
    order_block_score: float
    order_block_lower: float | None
    order_block_upper: float | None
    order_block_formed_at: str | None
    details: dict[str, Any]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        return value


def calculate_session_vwap(
    bars: Sequence[IntradayBar],
) -> list[VwapPoint]:
    if not bars:
        return []
    session_date = bars[0].candle_time[:10]
    cumulative_value = 0.0
    cumulative_volume = 0.0
    points: list[VwapPoint] = []
    for bar in sorted(bars, key=lambda item: item.candle_time):
        if bar.candle_time[:10] != session_date or bar.volume <= 0:
            continue
        typical_price = (bar.high + bar.low + bar.close) / 3
        cumulative_value += typical_price * bar.volume
        cumulative_volume += bar.volume
        points.append(
            VwapPoint(
                candle_time=bar.candle_time,
                vwap=cumulative_value / cumulative_volume,
                close=bar.close,
                low=bar.low,
                high=bar.high,
                volume=bar.volume,
            )
        )
    return points


def evaluate_vwap_patterns(
    stock_code: str,
    bars: Sequence[IntradayBar],
    *,
    interval_minutes: int = 5,
) -> IntradayPatternResult | None:
    points = calculate_session_vwap(bars)
    if len(points) < 6:
        return None
    latest = points[-1]
    previous = points[:-1]
    previous_five = previous[-5:]
    average_volume = mean(point.volume for point in previous_five)
    volume_ratio = (
        latest.volume / average_volume if average_volume > 0 else 0.0
    )
    distance_rate = latest.close / latest.vwap - 1
    previous_distances = [
        point.close / point.vwap - 1 for point in previous[-8:]
    ]
    reclaim_state, reclaim_score, reclaim_reason = _evaluate_reclaim(
        latest,
        previous_distances,
        volume_ratio,
    )
    bounce_state, bounce_score, bounce_reason = _evaluate_bounce(
        latest,
        previous[-4:],
        volume_ratio,
    )
    (
        sweep_state,
        sweep_side,
        sweep_score,
        liquidity_level,
        sweep_reason,
        sweep_details,
    ) = _evaluate_liquidity_sweep(
        bars,
        volume_ratio,
    )
    (
        fvg_state,
        fvg_side,
        fvg_score,
        fvg_lower,
        fvg_upper,
        fvg_fill_rate,
        fvg_formed_at,
        fvg_reason,
        fvg_details,
    ) = _evaluate_fvg(bars)
    (
        order_block_state,
        order_block_side,
        order_block_score,
        order_block_lower,
        order_block_upper,
        order_block_formed_at,
        order_block_reason,
        order_block_details,
    ) = _evaluate_order_block(bars)
    details = {
        "bar_count": len(points),
        "volume_ratio": round(volume_ratio, 4),
        "previous_below_count": sum(
            distance < -0.001 for distance in previous_distances
        ),
        "previous_above_count": sum(
            point.close >= point.vwap for point in previous[-4:]
        ),
        "touch_distance_rate": round(
            abs(latest.low / latest.vwap - 1),
            6,
        ),
        **sweep_details,
        **fvg_details,
        **order_block_details,
    }
    return IntradayPatternResult(
        stock_code=stock_code,
        session_date=latest.candle_time[:10],
        interval_minutes=interval_minutes,
        source_data_time=latest.candle_time,
        current_price=latest.close,
        vwap=latest.vwap,
        vwap_distance_rate=distance_rate,
        reclaim_state=reclaim_state,
        reclaim_score=reclaim_score,
        bounce_state=bounce_state,
        bounce_score=bounce_score,
        liquidity_sweep_state=sweep_state,
        liquidity_sweep_side=sweep_side,
        liquidity_sweep_score=sweep_score,
        liquidity_level=liquidity_level,
        fvg_state=fvg_state,
        fvg_side=fvg_side,
        fvg_score=fvg_score,
        fvg_lower=fvg_lower,
        fvg_upper=fvg_upper,
        fvg_fill_rate=fvg_fill_rate,
        fvg_formed_at=fvg_formed_at,
        order_block_state=order_block_state,
        order_block_side=order_block_side,
        order_block_score=order_block_score,
        order_block_lower=order_block_lower,
        order_block_upper=order_block_upper,
        order_block_formed_at=order_block_formed_at,
        details=details,
        reasons=(
            reclaim_reason,
            bounce_reason,
            sweep_reason,
            fvg_reason,
            order_block_reason,
        ),
    )


def _evaluate_reclaim(
    latest: VwapPoint,
    previous_distances: Sequence[float],
    volume_ratio: float,
) -> tuple[str, float, str]:
    was_below = sum(
        distance < -0.001 for distance in previous_distances
    ) >= 2
    closes_above = latest.close >= latest.vwap * 1.001
    crosses_vwap = latest.low <= latest.vwap
    if latest.close < latest.vwap * 0.995:
        return "failed", 0.0, "VWAP 아래로 0.5% 이상 이탈"
    if was_below and closes_above and crosses_vwap:
        score = min(100.0, 70.0 + max(0.0, volume_ratio - 1) * 30.0)
        if volume_ratio >= 1.2:
            return "confirmed", score, "VWAP 재돌파와 거래량 증가 확인"
        return "ready", score, "VWAP 재돌파, 거래량 확인 대기"
    if was_below and latest.close >= latest.vwap:
        return "ready", 55.0, "VWAP 위 안착 여부 확인 대기"
    return "forming", 20.0 if was_below else 0.0, "리클레임 구조 형성 중"


def _evaluate_bounce(
    latest: VwapPoint,
    previous: Sequence[VwapPoint],
    volume_ratio: float,
) -> tuple[str, float, str]:
    above_count = sum(point.close >= point.vwap for point in previous)
    touch_distance = abs(latest.low / latest.vwap - 1)
    touched = latest.low <= latest.vwap * 1.003 and touch_distance <= 0.005
    recovered = latest.close >= latest.vwap * 1.001
    bullish_close = latest.close > (latest.high + latest.low) / 2
    if latest.close < latest.vwap * 0.995:
        return "failed", 0.0, "VWAP 지지 실패"
    if above_count >= 3 and touched and recovered and bullish_close:
        score = min(100.0, 72.0 + max(0.0, volume_ratio - 0.8) * 25.0)
        if volume_ratio >= 1.0:
            return "confirmed", score, "VWAP 지지 후 반등 확인"
        return "ready", score, "VWAP 반등, 거래량 확인 대기"
    if above_count >= 3 and touched and latest.close >= latest.vwap:
        return "ready", 55.0, "VWAP 지지선 반응 확인 대기"
    return "forming", 20.0 if above_count >= 3 else 0.0, "바운스 구조 형성 중"


def _evaluate_liquidity_sweep(
    bars: Sequence[IntradayBar],
    volume_ratio: float,
) -> tuple[str, str, float, float | None, str, dict[str, Any]]:
    if len(bars) < 9:
        return (
            "forming",
            "none",
            0.0,
            None,
            "유동성 레벨 계산용 분봉 부족",
            {},
        )
    latest = bars[-1]
    lookback = list(bars[-21:-1])
    prior_low = min(bar.low for bar in lookback)
    prior_high = max(bar.high for bar in lookback)
    average_range = mean(max(bar.high - bar.low, 0) for bar in lookback)
    low_buffer = max(prior_low * 0.0005, average_range * 0.1)
    high_buffer = max(prior_high * 0.0005, average_range * 0.1)
    bar_range = max(latest.high - latest.low, 0)
    if bar_range <= 0:
        return (
            "forming",
            "none",
            0.0,
            None,
            "가격 범위가 없어 스윕 판정 불가",
            {
                "prior_liquidity_low": prior_low,
                "prior_liquidity_high": prior_high,
            },
        )

    lower_wick_rate = (
        min(latest.open, latest.close) - latest.low
    ) / bar_range
    upper_wick_rate = (
        latest.high - max(latest.open, latest.close)
    ) / bar_range
    bullish_pierced = latest.low <= prior_low - low_buffer
    bearish_pierced = latest.high >= prior_high + high_buffer
    bullish_reclaimed = latest.close > prior_low
    bearish_reclaimed = latest.close < prior_high
    bullish_penetration = max(0.0, prior_low / latest.low - 1)
    bearish_penetration = max(0.0, latest.high / prior_high - 1)
    details = {
        "prior_liquidity_low": round(prior_low, 4),
        "prior_liquidity_high": round(prior_high, 4),
        "lower_wick_rate": round(lower_wick_rate, 4),
        "upper_wick_rate": round(upper_wick_rate, 4),
        "liquidity_volume_ratio": round(volume_ratio, 4),
    }

    candidates: list[
        tuple[str, str, float, float, str]
    ] = []
    if bullish_pierced:
        if not bullish_reclaimed:
            candidates.append(
                (
                    "failed",
                    "bullish",
                    0.0,
                    prior_low,
                    "이전 저점 이탈 후 종가 복귀 실패",
                )
            )
        elif bullish_penetration <= 0.015 and lower_wick_rate >= 0.3:
            score = min(
                100.0,
                65.0
                + lower_wick_rate * 20.0
                + max(0.0, volume_ratio - 1) * 25.0,
            )
            state = "confirmed" if volume_ratio >= 1.2 else "ready"
            reason = (
                "저점 유동성 스윕 후 종가 복귀와 거래량 증가 확인"
                if state == "confirmed"
                else "저점 유동성 스윕 후 복귀, 거래량 확인 대기"
            )
            candidates.append(
                (state, "bullish", score, prior_low, reason)
            )
    if bearish_pierced:
        if not bearish_reclaimed:
            candidates.append(
                (
                    "failed",
                    "bearish",
                    0.0,
                    prior_high,
                    "이전 고점 돌파 후 종가 복귀 실패",
                )
            )
        elif bearish_penetration <= 0.015 and upper_wick_rate >= 0.3:
            score = min(
                100.0,
                65.0
                + upper_wick_rate * 20.0
                + max(0.0, volume_ratio - 1) * 25.0,
            )
            state = "confirmed" if volume_ratio >= 1.2 else "ready"
            reason = (
                "고점 유동성 스윕 후 종가 복귀와 거래량 증가 확인"
                if state == "confirmed"
                else "고점 유동성 스윕 후 복귀, 거래량 확인 대기"
            )
            candidates.append(
                (state, "bearish", score, prior_high, reason)
            )

    if not candidates:
        return (
            "forming",
            "none",
            0.0,
            None,
            "유동성 스윕 조건 미충족",
            details,
        )
    priority = {"confirmed": 2, "ready": 1, "failed": 0}
    state, side, score, level, reason = max(
        candidates,
        key=lambda item: (priority[item[0]], item[2]),
    )
    return state, side, score, level, reason, details


def _evaluate_fvg(
    bars: Sequence[IntradayBar],
) -> tuple[
    str,
    str,
    float,
    float | None,
    float | None,
    float | None,
    str | None,
    str,
    dict[str, Any],
]:
    if len(bars) < 6:
        return (
            "forming",
            "none",
            0.0,
            None,
            None,
            None,
            None,
            "FVG 계산용 분봉 부족",
            {},
        )
    start = max(0, len(bars) - 22)
    candidates: list[dict[str, Any]] = []
    for index in range(start, len(bars) - 2):
        first, middle, third = bars[index:index + 3]
        first_time = datetime.fromisoformat(first.candle_time)
        middle_time = datetime.fromisoformat(middle.candle_time)
        third_time = datetime.fromisoformat(third.candle_time)
        if (
            (middle_time - first_time).total_seconds() != 300
            or (third_time - middle_time).total_seconds() != 300
        ):
            continue
        reference = bars[max(0, index - 5):index + 1]
        average_range = mean(
            max(bar.high - bar.low, 0) for bar in reference
        )
        average_volume = mean(max(bar.volume, 0) for bar in reference)
        middle_range = max(middle.high - middle.low, 0)
        middle_body = abs(middle.close - middle.open)
        if average_range <= 0 or middle_range <= 0:
            continue
        body_rate = middle_body / middle_range
        range_ratio = middle_range / average_range
        volume_ratio = (
            middle.volume / average_volume if average_volume > 0 else 0.0
        )
        side = "none"
        lower = upper = 0.0
        direction_matches = False
        if third.low > first.high:
            side = "bullish"
            lower, upper = first.high, third.low
            direction_matches = middle.close > middle.open
        elif third.high < first.low:
            side = "bearish"
            lower, upper = third.high, first.low
            direction_matches = middle.close < middle.open
        if side == "none" or not direction_matches:
            continue
        gap_size = upper - lower
        gap_rate = gap_size / ((upper + lower) / 2)
        minimum_gap = max(((upper + lower) / 2) * 0.001, average_range * 0.2)
        if (
            gap_size < minimum_gap
            or body_rate < 0.55
            or range_ratio < 1.2
        ):
            continue
        quality_score = min(
            100.0,
            50.0
            + min(gap_rate / 0.01, 1.0) * 20.0
            + min(max(body_rate - 0.55, 0) / 0.45, 1.0) * 15.0
            + min(max(volume_ratio - 1, 0), 1.0) * 15.0,
        )
        candidates.append(
            {
                "index": index,
                "side": side,
                "lower": lower,
                "upper": upper,
                "gap_rate": gap_rate,
                "body_rate": body_rate,
                "range_ratio": range_ratio,
                "volume_ratio": volume_ratio,
                "quality_score": quality_score,
                "formed_at": third.candle_time,
            }
        )
    if not candidates:
        return (
            "forming",
            "none",
            0.0,
            None,
            None,
            None,
            None,
            "유효한 3봉 FVG 미감지",
            {},
        )

    candidate = candidates[-1]
    following = bars[candidate["index"] + 3:]
    lower = float(candidate["lower"])
    upper = float(candidate["upper"])
    gap_size = upper - lower
    side = str(candidate["side"])
    fill_rate = 0.0
    touched = False
    failed = False
    if side == "bullish" and following:
        lowest = min(bar.low for bar in following)
        touched = lowest < upper
        fill_rate = min(1.0, max(0.0, (upper - lowest) / gap_size))
        failed = any(bar.close <= lower for bar in following)
    elif side == "bearish" and following:
        highest = max(bar.high for bar in following)
        touched = highest > lower
        fill_rate = min(1.0, max(0.0, (highest - lower) / gap_size))
        failed = any(bar.close >= upper for bar in following)

    latest = bars[-1]
    reacted = (
        side == "bullish"
        and touched
        and latest.close >= upper
        and latest.close > latest.open
    ) or (
        side == "bearish"
        and touched
        and latest.close <= lower
        and latest.close < latest.open
    )
    if failed or fill_rate >= 0.95:
        state = "failed"
        score = 0.0
        reason = f"{side} FVG 완전 충전 또는 반대편 종가 이탈"
    elif reacted and 0.1 <= fill_rate <= 0.9:
        state = "confirmed"
        score = min(100.0, float(candidate["quality_score"]) + 10.0)
        reason = f"{side} FVG 재진입 후 방향성 반응 확인"
    elif touched:
        state = "ready"
        score = float(candidate["quality_score"])
        reason = f"{side} FVG {fill_rate * 100:.1f}% 충전, 반응 확인 대기"
    else:
        state = "forming"
        score = float(candidate["quality_score"])
        reason = f"{side} FVG 미충전 상태"
    details = {
        "fvg_gap_rate": round(float(candidate["gap_rate"]), 6),
        "fvg_middle_body_rate": round(float(candidate["body_rate"]), 4),
        "fvg_middle_range_ratio": round(
            float(candidate["range_ratio"]),
            4,
        ),
        "fvg_middle_volume_ratio": round(
            float(candidate["volume_ratio"]),
            4,
        ),
    }
    return (
        state,
        side,
        score,
        lower,
        upper,
        fill_rate,
        str(candidate["formed_at"]),
        reason,
        details,
    )


def _evaluate_order_block(
    bars: Sequence[IntradayBar],
) -> tuple[
    str,
    str,
    float,
    float | None,
    float | None,
    str | None,
    str,
    dict[str, Any],
]:
    if len(bars) < 8:
        return (
            "forming",
            "none",
            0.0,
            None,
            None,
            None,
            "Order Block 계산용 분봉 부족",
            {},
        )
    start = max(3, len(bars) - 32)
    candidates: list[dict[str, Any]] = []
    for index in range(start, len(bars) - 2):
        origin, first_move, second_move = bars[index:index + 3]
        times = [
            datetime.fromisoformat(bar.candle_time)
            for bar in (origin, first_move, second_move)
        ]
        if (
            (times[1] - times[0]).total_seconds() != 300
            or (times[2] - times[1]).total_seconds() != 300
        ):
            continue
        prior = bars[max(0, index - 6):index]
        if len(prior) < 3:
            continue
        average_range = mean(
            max(bar.high - bar.low, 0) for bar in prior
        )
        average_volume = mean(max(bar.volume, 0) for bar in prior)
        origin_range = max(origin.high - origin.low, 0)
        origin_body = abs(origin.close - origin.open)
        if average_range <= 0 or origin_range <= 0:
            continue
        if origin_body / origin_range < 0.2:
            continue

        prior_high = max(bar.high for bar in prior)
        prior_low = min(bar.low for bar in prior)
        displacement_volume = mean(
            max(bar.volume, 0) for bar in (first_move, second_move)
        )
        volume_ratio = (
            displacement_volume / average_volume
            if average_volume > 0
            else 0.0
        )
        side = "none"
        lower = upper = structure_level = 0.0
        displacement = 0.0
        if (
            origin.close < origin.open
            and second_move.close > prior_high
            and second_move.close > first_move.close
            and first_move.close > origin.close
        ):
            side = "bullish"
            lower, upper = origin.low, origin.open
            structure_level = prior_high
            displacement = second_move.close - origin.close
        elif (
            origin.close > origin.open
            and second_move.close < prior_low
            and second_move.close < first_move.close
            and first_move.close < origin.close
        ):
            side = "bearish"
            lower, upper = origin.open, origin.high
            structure_level = prior_low
            displacement = origin.close - second_move.close
        if side == "none":
            continue
        displacement_ratio = displacement / average_range
        zone_width = upper - lower
        if (
            displacement_ratio < 1.5
            or zone_width <= 0
            or zone_width > average_range * 1.5
        ):
            continue
        score = min(
            100.0,
            55.0
            + min(max(displacement_ratio - 1.5, 0) / 2, 1.0) * 20.0
            + min(max(volume_ratio - 1, 0), 1.0) * 15.0
            + min(
                abs(second_move.close - structure_level)
                / average_range,
                1.0,
            )
            * 10.0,
        )
        candidates.append(
            {
                "index": index,
                "side": side,
                "lower": lower,
                "upper": upper,
                "formed_at": second_move.candle_time,
                "structure_level": structure_level,
                "displacement_ratio": displacement_ratio,
                "volume_ratio": volume_ratio,
                "quality_score": score,
            }
        )
    if not candidates:
        return (
            "forming",
            "none",
            0.0,
            None,
            None,
            None,
            "구조 돌파를 만든 Order Block 미감지",
            {},
        )

    candidate = candidates[-1]
    side = str(candidate["side"])
    lower = float(candidate["lower"])
    upper = float(candidate["upper"])
    following = bars[candidate["index"] + 3:]
    touched = any(
        bar.low <= upper and bar.high >= lower
        for bar in following
    )
    failed = (
        any(bar.close < lower for bar in following)
        if side == "bullish"
        else any(bar.close > upper for bar in following)
    )
    latest = bars[-1]
    reacted = (
        side == "bullish"
        and touched
        and latest.close > upper
        and latest.close > latest.open
    ) or (
        side == "bearish"
        and touched
        and latest.close < lower
        and latest.close < latest.open
    )
    if failed:
        state = "failed"
        score = 0.0
        reason = f"{side} Order Block 반대편 종가 이탈"
    elif reacted:
        state = "confirmed"
        score = min(100.0, float(candidate["quality_score"]) + 10.0)
        reason = f"{side} Order Block 재진입 후 방향성 반응 확인"
    elif touched:
        state = "ready"
        score = float(candidate["quality_score"])
        reason = f"{side} Order Block 재진입, 반응 확인 대기"
    else:
        state = "forming"
        score = float(candidate["quality_score"])
        reason = f"{side} Order Block 미재진입 상태"
    details = {
        "order_block_structure_level": round(
            float(candidate["structure_level"]),
            4,
        ),
        "order_block_displacement_ratio": round(
            float(candidate["displacement_ratio"]),
            4,
        ),
        "order_block_volume_ratio": round(
            float(candidate["volume_ratio"]),
            4,
        ),
    }
    return (
        state,
        side,
        score,
        lower,
        upper,
        str(candidate["formed_at"]),
        reason,
        details,
    )


class IntradayVwapService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def evaluate(
        self,
        stock_code: str,
        *,
        interval_minutes: int = 5,
        as_of: datetime | None = None,
        save: bool = True,
    ) -> IntradayPatternResult | None:
        end_time = as_of.isoformat(timespec="seconds") if as_of else None
        rows = self.database.list_candles(
            [stock_code],
            candle_type="minute",
            interval_minutes=interval_minutes,
            end_time=end_time,
        )
        if not rows:
            return None
        session_date = rows[-1]["candle_time"][:10]
        bars = [
            IntradayBar(
                candle_time=row["candle_time"],
                open=float(row["open_price"]),
                high=float(row["high_price"]),
                low=float(row["low_price"]),
                close=float(row["close_price"]),
                volume=float(row["volume"] or 0),
            )
            for row in rows
            if row["candle_time"][:10] == session_date
            and "09:00:00" <= row["candle_time"][11:19] <= "15:30:00"
        ]
        result = evaluate_vwap_patterns(
            stock_code,
            bars,
            interval_minutes=interval_minutes,
        )
        if result is not None and save:
            self._save(result)
        return result

    def latest(
        self,
        *,
        interval_minutes: int = 5,
    ) -> dict[str, dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.*
                FROM intraday_pattern_evaluations e
                WHERE e.interval_minutes = ?
                  AND e.id = (
                      SELECT e2.id
                      FROM intraday_pattern_evaluations e2
                      WHERE e2.stock_code = e.stock_code
                        AND e2.interval_minutes = e.interval_minutes
                      ORDER BY e2.source_data_time DESC, e2.id DESC
                      LIMIT 1
                  )
                ORDER BY e.stock_code
                """,
                (interval_minutes,),
            ).fetchall()
        values: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            item["reasons"] = json.loads(item.pop("reasons_json"))
            values[item["stock_code"]] = item
        return values

    def _save(self, result: IntradayPatternResult) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO intraday_pattern_evaluations(
                    stock_code, session_date, interval_minutes,
                    source_data_time, evaluated_at, current_price, vwap,
                    vwap_distance_rate, reclaim_state, reclaim_score,
                    bounce_state, bounce_score, details_json, reasons_json
                    , liquidity_sweep_state, liquidity_sweep_side,
                    liquidity_sweep_score, liquidity_level
                    , fvg_state, fvg_side, fvg_score, fvg_lower,
                    fvg_upper, fvg_fill_rate, fvg_formed_at
                    , order_block_state, order_block_side,
                    order_block_score, order_block_lower,
                    order_block_upper, order_block_formed_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(stock_code, interval_minutes, source_data_time)
                DO UPDATE SET
                    evaluated_at = excluded.evaluated_at,
                    current_price = excluded.current_price,
                    vwap = excluded.vwap,
                    vwap_distance_rate = excluded.vwap_distance_rate,
                    reclaim_state = excluded.reclaim_state,
                    reclaim_score = excluded.reclaim_score,
                    bounce_state = excluded.bounce_state,
                    bounce_score = excluded.bounce_score,
                    liquidity_sweep_state = excluded.liquidity_sweep_state,
                    liquidity_sweep_side = excluded.liquidity_sweep_side,
                    liquidity_sweep_score = excluded.liquidity_sweep_score,
                    liquidity_level = excluded.liquidity_level,
                    fvg_state = excluded.fvg_state,
                    fvg_side = excluded.fvg_side,
                    fvg_score = excluded.fvg_score,
                    fvg_lower = excluded.fvg_lower,
                    fvg_upper = excluded.fvg_upper,
                    fvg_fill_rate = excluded.fvg_fill_rate,
                    fvg_formed_at = excluded.fvg_formed_at,
                    order_block_state = excluded.order_block_state,
                    order_block_side = excluded.order_block_side,
                    order_block_score = excluded.order_block_score,
                    order_block_lower = excluded.order_block_lower,
                    order_block_upper = excluded.order_block_upper,
                    order_block_formed_at = excluded.order_block_formed_at,
                    details_json = excluded.details_json,
                    reasons_json = excluded.reasons_json
                """,
                (
                    result.stock_code,
                    result.session_date,
                    result.interval_minutes,
                    result.source_data_time,
                    timestamp,
                    result.current_price,
                    result.vwap,
                    result.vwap_distance_rate,
                    result.reclaim_state,
                    result.reclaim_score,
                    result.bounce_state,
                    result.bounce_score,
                    json.dumps(result.details, ensure_ascii=False),
                    json.dumps(result.reasons, ensure_ascii=False),
                    result.liquidity_sweep_state,
                    result.liquidity_sweep_side,
                    result.liquidity_sweep_score,
                    result.liquidity_level,
                    result.fvg_state,
                    result.fvg_side,
                    result.fvg_score,
                    result.fvg_lower,
                    result.fvg_upper,
                    result.fvg_fill_rate,
                    result.fvg_formed_at,
                    result.order_block_state,
                    result.order_block_side,
                    result.order_block_score,
                    result.order_block_lower,
                    result.order_block_upper,
                    result.order_block_formed_at,
                ),
            )
