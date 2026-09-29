from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev
from typing import Any, Sequence


PATTERN_BOX_BREAKOUT = "box_breakout"
PATTERN_BULL_ALIGNMENT = "bull_alignment"
PATTERN_BEAR_ALIGNMENT = "bear_alignment"
PATTERN_DOUBLE_BOTTOM = "double_bottom"
PATTERN_DOUBLE_TOP = "double_top"
PATTERN_HIGHER_HIGHS_LOWS = "higher_highs_lows"
PATTERN_VOLATILITY_CONTRACTION_BREAKOUT = "volatility_contraction_breakout"
PATTERN_CUP_HANDLE = "cup_handle"
PATTERN_ASCENDING_TRIANGLE = "ascending_triangle"
PATTERN_BULL_FLAG = "bull_flag"
PATTERN_BULL_PENNANT = "bull_pennant"
PATTERN_HEAD_SHOULDERS = "head_shoulders"
PATTERN_INVERSE_HEAD_SHOULDERS = "inverse_head_shoulders"

ALL_PATTERNS = (
    PATTERN_BOX_BREAKOUT,
    PATTERN_BULL_ALIGNMENT,
    PATTERN_BEAR_ALIGNMENT,
    PATTERN_DOUBLE_BOTTOM,
    PATTERN_DOUBLE_TOP,
    PATTERN_HIGHER_HIGHS_LOWS,
    PATTERN_VOLATILITY_CONTRACTION_BREAKOUT,
    PATTERN_CUP_HANDLE,
    PATTERN_ASCENDING_TRIANGLE,
    PATTERN_BULL_FLAG,
    PATTERN_BULL_PENNANT,
    PATTERN_HEAD_SHOULDERS,
    PATTERN_INVERSE_HEAD_SHOULDERS,
)


@dataclass(frozen=True)
class DailyBar:
    candle_time: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    trade_amount: int | None = None


@dataclass(frozen=True)
class FeatureResult:
    values: dict[str, Any]
    patterns: tuple[str, ...]
    technical_score: float
    pattern_score: float
    reasons: tuple[str, ...]


def calculate_features(bars: Sequence[DailyBar]) -> FeatureResult:
    if not bars:
        raise ValueError("기술 특징 계산에는 일봉이 필요합니다.")
    closes = [float(bar.close) for bar in bars]
    highs = [float(bar.high) for bar in bars]
    lows = [float(bar.low) for bar in bars]
    volumes = [float(max(bar.volume, 0)) for bar in bars]
    amounts = [
        _normalized_trade_amount(bar)
        for bar in bars
    ]
    returns = [
        closes[index] / closes[index - 1] - 1
        for index in range(1, len(closes))
        if closes[index - 1] > 0
    ]
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    rsi14 = _rsi(closes, 14)
    atr14 = _atr(bars, 14)
    bb_middle = ma20
    bb_std = pstdev(closes[-20:]) if len(closes) >= 20 else None
    bb_upper = bb_middle + 2 * bb_std if bb_middle is not None else None
    bb_lower = bb_middle - 2 * bb_std if bb_middle is not None else None
    average_volume20 = _average(volumes[-20:])
    average_amount20 = _average(amounts[-20:])
    volume_ratio = (
        volumes[-1] / average_volume20 if average_volume20 > 0 else None
    )
    volatility20 = (
        pstdev(returns[-20:]) if len(returns) >= 20 else None
    )
    return20 = (
        closes[-1] / closes[-21] - 1 if len(closes) >= 21 else None
    )
    return60 = (
        closes[-1] / closes[-61] - 1 if len(closes) >= 61 else None
    )
    previous_bar = bars[-2] if len(bars) >= 2 else None
    previous_high = previous_bar.high if previous_bar is not None else None
    previous_low = previous_bar.low if previous_bar is not None else None
    previous_close = previous_bar.close if previous_bar is not None else None
    previous_trade_amount = (
        _normalized_trade_amount(previous_bar)
        if previous_bar is not None
        else None
    )
    recent5_high = max(highs[-6:-1]) if len(highs) >= 6 else None
    recent5_low = min(lows[-6:-1]) if len(lows) >= 6 else None
    recent5_high_breakout = (
        closes[-1] > recent5_high if recent5_high is not None else False
    )
    recent5_high_near = (
        closes[-1] >= recent5_high * 0.98 if recent5_high is not None else False
    )
    prior60_high = max(highs[-61:-1]) if len(highs) >= 61 else None
    new_60d_high = closes[-1] > prior60_high if prior60_high is not None else False
    gap_rate = (
        bars[-1].open / previous_close - 1
        if previous_close not in (None, 0)
        else None
    )
    day_gain_rate = (
        closes[-1] / previous_close - 1
        if previous_close not in (None, 0)
        else None
    )
    previous_trade_amount_ratio = (
        amounts[-1] / previous_trade_amount
        if previous_trade_amount not in (None, 0)
        else None
    )
    relative_trade_amount20 = (
        amounts[-1] / average_amount20 if average_amount20 > 0 else None
    )
    previous_return = (
        closes[-1] / closes[-2] - 1 if len(closes) >= 2 else None
    )
    previous_return2 = (
        closes[-2] / closes[-3] - 1 if len(closes) >= 3 else None
    )
    bullish_body_rate = (
        (closes[-1] - bars[-1].open) / bars[-1].open
        if bars[-1].open not in (None, 0)
        else None
    )
    big_bullish_candle = (
        closes[-1] > bars[-1].open
        and previous_high is not None
        and closes[-1] > previous_high
        and bullish_body_rate is not None
        and bullish_body_rate >= 0.03
        and day_gain_rate is not None
        and day_gain_rate >= 0.03
    )
    resistance = _nearest_resistance(bars)
    resistance_price = resistance["price"]
    resistance_touch_count = resistance["touch_count"]
    resistance_distance_rate = (
        resistance_price / closes[-1] - 1
        if resistance_price is not None and closes[-1] > 0
        else None
    )
    near_resistance = (
        resistance_distance_rate is not None
        and -0.01 <= resistance_distance_rate <= 0.025
    )
    resistance_breakout = (
        resistance_price is not None and closes[-1] > resistance_price * 1.003
    )
    healthy_gap = gap_rate is not None and 0.03 <= gap_rate <= 0.06
    gap_overheated = gap_rate is not None and gap_rate >= 0.10
    day_gain_overheated = day_gain_rate is not None and day_gain_rate >= 0.18

    patterns: list[str] = []
    reasons: list[str] = []
    pattern_score = 0.0
    breakout_family_score = 0.0
    bullish_reversal_score = 0.0
    bearish_reversal_score = 0.0
    if None not in (ma5, ma20, ma60):
        if ma5 > ma20 > ma60:
            patterns.append(PATTERN_BULL_ALIGNMENT)
            pattern_score += 10
            reasons.append("5·20·60일 이동평균 정배열")
        elif ma5 < ma20 < ma60:
            patterns.append(PATTERN_BEAR_ALIGNMENT)
            pattern_score -= 16
            reasons.append("5·20·60일 이동평균 역배열")
    if len(bars) >= 20:
        prior = bars[-20:-10]
        recent = bars[-10:]
        if (
            max(bar.high for bar in recent) > max(bar.high for bar in prior)
            and min(bar.low for bar in recent) > min(bar.low for bar in prior)
        ):
            patterns.append(PATTERN_HIGHER_HIGHS_LOWS)
            pattern_score += 10
            reasons.append("고점과 저점이 함께 상승")
    if _volatility_contraction_breakout(bars):
        patterns.append(PATTERN_VOLATILITY_CONTRACTION_BREAKOUT)
        pattern_score += 15
        reasons.append("변동성 수축 뒤 20일 고점 돌파")

    from screening.patterns import (
        ASCENDING_TRIANGLE_CONFIRMED,
        CONTINUATION_CONFIRMED,
        CUP_HANDLE_CONFIRMED,
        detect_ascending_triangle,
        detect_box_breakout,
        detect_bull_flag,
        detect_bull_pennant,
        detect_cup_handle,
        detect_double_bottom,
        detect_double_top,
        detect_head_shoulders,
        detect_inverse_head_shoulders,
    )

    box_breakout = detect_box_breakout(bars, atr=atr14)
    if box_breakout is not None:
        reasons.extend(box_breakout.reasons)
        if box_breakout.state == CONTINUATION_CONFIRMED:
            patterns.append(PATTERN_BOX_BREAKOUT)
            box_score = min(20.0, 10.0 + box_breakout.score * 0.1)
            pattern_score += max(0.0, box_score - breakout_family_score)
            breakout_family_score = max(breakout_family_score, box_score)

    cup_handle = detect_cup_handle(bars, atr=atr14)
    if cup_handle is not None:
        reasons.extend(cup_handle.reasons)
        if cup_handle.state == CUP_HANDLE_CONFIRMED:
            patterns.append(PATTERN_CUP_HANDLE)
            cup_handle_score = min(20.0, 10.0 + cup_handle.score * 0.1)
            pattern_score += max(
                0.0,
                cup_handle_score - breakout_family_score,
            )
            breakout_family_score = max(
                breakout_family_score,
                cup_handle_score,
            )

    ascending_triangle = detect_ascending_triangle(bars, atr=atr14)
    if ascending_triangle is not None:
        reasons.extend(ascending_triangle.reasons)
        if ascending_triangle.state == ASCENDING_TRIANGLE_CONFIRMED:
            patterns.append(PATTERN_ASCENDING_TRIANGLE)
            triangle_score = min(
                20.0,
                10.0 + ascending_triangle.score * 0.1,
            )
            pattern_score += max(
                0.0,
                triangle_score - breakout_family_score,
            )
            breakout_family_score = max(
                breakout_family_score,
                triangle_score,
            )

    bull_flag = detect_bull_flag(bars, atr=atr14)
    if bull_flag is not None:
        reasons.extend(bull_flag.reasons)
        if bull_flag.state == CONTINUATION_CONFIRMED:
            patterns.append(PATTERN_BULL_FLAG)
            flag_score = min(20.0, 10.0 + bull_flag.score * 0.1)
            pattern_score += max(
                0.0,
                flag_score - breakout_family_score,
            )
            breakout_family_score = max(breakout_family_score, flag_score)

    bull_pennant = detect_bull_pennant(bars, atr=atr14)
    if bull_pennant is not None:
        reasons.extend(bull_pennant.reasons)
        if bull_pennant.state == CONTINUATION_CONFIRMED:
            patterns.append(PATTERN_BULL_PENNANT)
            pennant_score = min(20.0, 10.0 + bull_pennant.score * 0.1)
            pattern_score += max(
                0.0,
                pennant_score - breakout_family_score,
            )
            breakout_family_score = max(breakout_family_score, pennant_score)

    double_bottom = detect_double_bottom(bars, atr=atr14)
    if double_bottom is not None:
        reasons.extend(double_bottom.reasons)
        if double_bottom.state == CONTINUATION_CONFIRMED:
            patterns.append(PATTERN_DOUBLE_BOTTOM)
            score = min(18.0, 8.0 + double_bottom.score * 0.1)
            pattern_score += max(0.0, score - bullish_reversal_score)
            bullish_reversal_score = max(bullish_reversal_score, score)

    double_top = detect_double_top(bars, atr=atr14)
    if double_top is not None:
        reasons.extend(double_top.reasons)
        if double_top.state == CONTINUATION_CONFIRMED:
            patterns.append(PATTERN_DOUBLE_TOP)
            score = min(18.0, 8.0 + double_top.score * 0.1)
            pattern_score -= max(0.0, score - bearish_reversal_score)
            bearish_reversal_score = max(bearish_reversal_score, score)

    inverse_head_shoulders = detect_inverse_head_shoulders(bars, atr=atr14)
    if inverse_head_shoulders is not None:
        reasons.extend(inverse_head_shoulders.reasons)
        if inverse_head_shoulders.state == CONTINUATION_CONFIRMED:
            patterns.append(PATTERN_INVERSE_HEAD_SHOULDERS)
            score = min(22.0, 10.0 + inverse_head_shoulders.score * 0.12)
            pattern_score += max(0.0, score - bullish_reversal_score)
            bullish_reversal_score = max(bullish_reversal_score, score)

    head_shoulders = detect_head_shoulders(bars, atr=atr14)
    if head_shoulders is not None:
        reasons.extend(head_shoulders.reasons)
        if head_shoulders.state == CONTINUATION_CONFIRMED:
            patterns.append(PATTERN_HEAD_SHOULDERS)
            score = min(22.0, 10.0 + head_shoulders.score * 0.12)
            pattern_score -= max(0.0, score - bearish_reversal_score)
            bearish_reversal_score = max(bearish_reversal_score, score)

    technical_score = 0.0
    if ma20 is not None:
        technical_score += 8 if closes[-1] > ma20 else -8
    if ema12 is not None and ema26 is not None:
        technical_score += 7 if ema12 > ema26 else -7
    if rsi14 is not None:
        if 50 <= rsi14 <= 70:
            technical_score += 10
        elif rsi14 >= 80:
            technical_score -= 8
        elif rsi14 < 30:
            technical_score -= 4
    if return20 is not None:
        technical_score += max(-10, min(10, return20 * 50))
    if bb_upper is not None and closes[-1] > bb_upper:
        technical_score += 4
    if recent5_high_breakout:
        technical_score += 8
        reasons.append("최근 5일 고점 돌파")
    elif recent5_high_near:
        technical_score += 3
        reasons.append("최근 5일 고점 근접")
    if new_60d_high:
        technical_score += 5
        reasons.append("60일 신고가 돌파")
    if resistance_breakout:
        technical_score += 4
        reasons.append("주요 저항선 돌파")
    elif near_resistance:
        technical_score -= 2
        reasons.append("주요 저항선 근처")
    if healthy_gap:
        technical_score += 2
        reasons.append("건강한 갭 상승")
    if gap_overheated:
        technical_score -= 8
        reasons.append("과열 갭 상승")
    if day_gain_overheated:
        technical_score -= 8
        reasons.append("당일 상승률 과열")
    if relative_trade_amount20 is not None and relative_trade_amount20 >= 2:
        technical_score += 4
        reasons.append(f"당일 거래대금 20일 평균 대비 {relative_trade_amount20:.2f}배")

    return FeatureResult(
        values={
            "close": int(closes[-1]),
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "ema12": ema12,
            "ema26": ema26,
            "rsi14": rsi14,
            "atr14": atr14,
            "bollinger_middle": bb_middle,
            "bollinger_upper": bb_upper,
            "bollinger_lower": bb_lower,
            "average_volume20": average_volume20,
            "average_trade_amount20": average_amount20,
            "volume_ratio": volume_ratio,
            "volatility20": volatility20,
            "return20": return20,
            "return60": return60,
            "previous_high": previous_high,
            "previous_low": previous_low,
            "previous_close": previous_close,
            "previous_trade_amount": previous_trade_amount,
            "recent5_high": recent5_high,
            "recent5_low": recent5_low,
            "recent5_high_breakout": recent5_high_breakout,
            "recent5_high_near": recent5_high_near,
            "prior60_high": prior60_high,
            "new_60d_high": new_60d_high,
            "gap_rate": gap_rate,
            "day_gain_rate": day_gain_rate,
            "previous_trade_amount_ratio": previous_trade_amount_ratio,
            "relative_trade_amount20": relative_trade_amount20,
            "previous_return": previous_return,
            "previous_return2": previous_return2,
            "bullish_body_rate": bullish_body_rate,
            "big_bullish_candle": big_bullish_candle,
            "resistance_price": resistance_price,
            "resistance_touch_count": resistance_touch_count,
            "resistance_distance_rate": resistance_distance_rate,
            "near_resistance": near_resistance,
            "resistance_breakout": resistance_breakout,
            "healthy_gap": healthy_gap,
            "gap_overheated": gap_overheated,
            "day_gain_overheated": day_gain_overheated,
            **_detection_feature_values(
                "box_breakout",
                box_breakout,
                (
                    "state",
                    "score",
                    "start_index",
                    "support_price",
                    "resistance_price",
                    "support_touches",
                    "resistance_touches",
                    "width_rate",
                    "containment_ratio",
                    "breakout_volume_ratio",
                    "stop_price",
                    "target_price",
                ),
            ),
            "cup_handle_state": (
                cup_handle.state if cup_handle is not None else None
            ),
            "cup_handle_score": (
                cup_handle.score if cup_handle is not None else None
            ),
            "cup_handle_pivot_price": (
                cup_handle.pivot_price if cup_handle is not None else None
            ),
            "cup_handle_left_rim_price": (
                cup_handle.left_rim_price if cup_handle is not None else None
            ),
            "cup_handle_bottom_price": (
                cup_handle.bottom_price if cup_handle is not None else None
            ),
            "cup_handle_right_rim_price": (
                cup_handle.right_rim_price if cup_handle is not None else None
            ),
            "cup_handle_handle_low": (
                cup_handle.handle_low if cup_handle is not None else None
            ),
            "cup_handle_stop_price": (
                cup_handle.stop_price if cup_handle is not None else None
            ),
            "cup_handle_target_price": (
                cup_handle.target_price if cup_handle is not None else None
            ),
            "cup_handle_cup_depth": (
                cup_handle.cup_depth if cup_handle is not None else None
            ),
            "cup_handle_handle_depth": (
                cup_handle.handle_depth if cup_handle is not None else None
            ),
            "cup_handle_rim_difference": (
                cup_handle.rim_difference if cup_handle is not None else None
            ),
            "cup_handle_handle_volume_ratio": (
                cup_handle.handle_volume_ratio
                if cup_handle is not None
                else None
            ),
            "cup_handle_breakout_volume_ratio": (
                cup_handle.breakout_volume_ratio
                if cup_handle is not None
                else None
            ),
            "ascending_triangle_state": (
                ascending_triangle.state
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_score": (
                ascending_triangle.score
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_resistance_price": (
                ascending_triangle.resistance_price
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_resistance_touches": (
                ascending_triangle.resistance_touches
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_support_slope": (
                ascending_triangle.support_slope
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_support_fit_score": (
                ascending_triangle.support_fit_score
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_current_support_price": (
                ascending_triangle.current_support_price
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_stop_price": (
                ascending_triangle.stop_price
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_target_price": (
                ascending_triangle.target_price
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_volume_contraction_ratio": (
                ascending_triangle.volume_contraction_ratio
                if ascending_triangle is not None
                else None
            ),
            "ascending_triangle_breakout_volume_ratio": (
                ascending_triangle.breakout_volume_ratio
                if ascending_triangle is not None
                else None
            ),
            **_continuation_feature_values("bull_flag", bull_flag),
            **_continuation_feature_values("bull_pennant", bull_pennant),
            **_detection_feature_values(
                "double_bottom",
                double_bottom,
                (
                    "state",
                    "score",
                    "first_index",
                    "second_index",
                    "first_price",
                    "second_price",
                    "neckline_price",
                    "similarity_rate",
                    "height_rate",
                    "breakout_volume_ratio",
                    "stop_price",
                    "target_price",
                ),
            ),
            **_detection_feature_values(
                "double_top",
                double_top,
                (
                    "state",
                    "score",
                    "first_index",
                    "second_index",
                    "first_price",
                    "second_price",
                    "neckline_price",
                    "similarity_rate",
                    "height_rate",
                    "breakout_volume_ratio",
                    "stop_price",
                    "target_price",
                ),
            ),
            **_head_shoulders_feature_values(
                "inverse_head_shoulders",
                inverse_head_shoulders,
            ),
            **_head_shoulders_feature_values(
                "head_shoulders",
                head_shoulders,
            ),
        },
        patterns=tuple(patterns),
        technical_score=round(technical_score, 4),
        pattern_score=round(pattern_score, 4),
        reasons=tuple(reasons),
    )


def _continuation_feature_values(
    prefix: str,
    detection: Any,
) -> dict[str, Any]:
    fields = (
        "state",
        "score",
        "pole_start_index",
        "pole_end_index",
        "consolidation_start_index",
        "pole_gain_rate",
        "pole_height",
        "pole_volume_ratio",
        "upper_slope",
        "lower_slope",
        "upper_fit_score",
        "lower_fit_score",
        "current_upper_price",
        "current_lower_price",
        "retracement_rate",
        "volume_contraction_ratio",
        "breakout_volume_ratio",
        "stop_price",
        "target_price",
    )
    return {
        f"{prefix}_{field}": (
            getattr(detection, field) if detection is not None else None
        )
        for field in fields
    }


def _head_shoulders_feature_values(
    prefix: str,
    detection: Any,
) -> dict[str, Any]:
    return _detection_feature_values(
        prefix,
        detection,
        (
            "state",
            "score",
            "left_shoulder_index",
            "head_index",
            "right_shoulder_index",
            "left_shoulder_price",
            "head_price",
            "right_shoulder_price",
            "neckline_price",
            "neckline_slope",
            "shoulder_similarity_rate",
            "head_prominence_rate",
            "breakout_volume_ratio",
            "stop_price",
            "target_price",
        ),
    )


def _detection_feature_values(
    prefix: str,
    detection: Any,
    fields: Sequence[str],
) -> dict[str, Any]:
    return {
        f"{prefix}_{field}": (
            getattr(detection, field) if detection is not None else None
        )
        for field in fields
    }


def _sma(values: Sequence[float], period: int) -> float | None:
    return _average(values[-period:]) if len(values) >= period else None


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _normalized_trade_amount(bar: DailyBar) -> float:
    derived = float(bar.close * max(bar.volume, 0))
    reported = float(bar.trade_amount or 0)
    if reported <= 0 or derived <= 0:
        return derived
    # Kiwoom chart responses can report trade amount in won, thousand won,
    # or million won. Choose the scale closest to price * volume.
    scaled = [reported, reported * 1_000, reported * 1_000_000]
    return min(scaled, key=lambda value: abs(value - derived))


def _nearest_resistance(bars: Sequence[DailyBar]) -> dict[str, Any]:
    if len(bars) < 10:
        return {"price": None, "touch_count": 0}
    current_close = float(bars[-1].close)
    atr_value = _atr(bars, min(14, len(bars) - 1))
    tolerance = max(current_close * 0.012, float(atr_value or 0) * 0.35, 1.0)
    highs = sorted(float(bar.high) for bar in bars[-61:-1])
    levels: list[dict[str, Any]] = []
    for high in highs:
        matched = None
        for level in levels:
            if abs(high - level["price"]) <= tolerance:
                matched = level
                break
        if matched is None:
            levels.append({"price": high, "touch_count": 1})
        else:
            count = matched["touch_count"] + 1
            matched["price"] = (matched["price"] * matched["touch_count"] + high) / count
            matched["touch_count"] = count
    candidates = [
        level
        for level in levels
        if level["touch_count"] >= 2 and level["price"] >= current_close * 0.97
    ]
    if not candidates:
        return {"price": None, "touch_count": 0}
    best = min(
        candidates,
        key=lambda level: (
            abs(level["price"] - current_close),
            -level["touch_count"],
        ),
    )
    return {
        "price": round(best["price"], 4),
        "touch_count": int(best["touch_count"]),
    }


def _ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = _average(values[:period])
    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def _rsi(values: Sequence[float], period: int) -> float | None:
    if len(values) < period + 1:
        return None
    changes = [
        values[index] - values[index - 1]
        for index in range(len(values) - period, len(values))
    ]
    gains = _average([max(change, 0) for change in changes])
    losses = _average([max(-change, 0) for change in changes])
    if losses == 0:
        return 100.0
    relative = gains / losses
    return 100 - 100 / (1 + relative)


def _atr(bars: Sequence[DailyBar], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    ranges = []
    for previous, current in zip(bars[-period - 1 : -1], bars[-period:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return _average([float(value) for value in ranges])


def _volatility_contraction_breakout(bars: Sequence[DailyBar]) -> bool:
    if len(bars) < 26:
        return False
    ranges = [
        (bar.high - bar.low) / bar.close
        for bar in bars[-26:]
        if bar.close > 0
    ]
    if len(ranges) < 26:
        return False
    contracted = _average(ranges[-5:]) < _average(ranges[-20:-5]) * 0.7
    prior_high = max(bar.high for bar in bars[-21:-1])
    return contracted and bars[-1].close > prior_high


def json_safe_features(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: round(value, 8) if isinstance(value, float) else value
        for key, value in values.items()
    }
