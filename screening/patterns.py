from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from screening.features import DailyBar


CUP_HANDLE_FORMING = "forming"
CUP_HANDLE_READY = "ready"
CUP_HANDLE_BREAKOUT_PENDING = "breakout_pending"
CUP_HANDLE_CONFIRMED = "confirmed"
CUP_HANDLE_FAILED = "failed"
ASCENDING_TRIANGLE_FORMING = "forming"
ASCENDING_TRIANGLE_READY = "ready"
ASCENDING_TRIANGLE_BREAKOUT_PENDING = "breakout_pending"
ASCENDING_TRIANGLE_CONFIRMED = "confirmed"
ASCENDING_TRIANGLE_FAILED = "failed"
CONTINUATION_FORMING = "forming"
CONTINUATION_READY = "ready"
CONTINUATION_BREAKOUT_PENDING = "breakout_pending"
CONTINUATION_CONFIRMED = "confirmed"
CONTINUATION_FAILED = "failed"


@dataclass(frozen=True)
class Pivot:
    index: int
    kind: str
    price: float


@dataclass(frozen=True)
class PriceZone:
    kind: str
    level: float
    lower: float
    upper: float
    touch_count: int
    first_index: int
    last_index: int
    pivot_indexes: tuple[int, ...]


@dataclass(frozen=True)
class TrendLine:
    kind: str
    slope: float
    intercept: float
    fit_score: float
    start_index: int
    end_index: int
    touch_count: int

    def value_at(self, index: int) -> float:
        return self.intercept + self.slope * index


@dataclass(frozen=True)
class Flagpole:
    start_index: int
    end_index: int
    start_price: float
    end_price: float
    height: float
    gain_rate: float
    positive_bar_ratio: float
    volume_ratio: float


@dataclass(frozen=True)
class CupHandleDetection:
    state: str
    score: float
    left_rim_index: int
    bottom_index: int
    right_rim_index: int
    left_rim_price: float
    bottom_price: float
    right_rim_price: float
    handle_low: float
    pivot_price: float
    stop_price: float
    target_price: float
    cup_depth: float
    rim_difference: float
    handle_depth: float
    handle_volume_ratio: float
    breakout_volume_ratio: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AscendingTriangleDetection:
    state: str
    score: float
    start_index: int
    resistance_price: float
    resistance_lower: float
    resistance_upper: float
    resistance_touches: int
    support_slope: float
    support_fit_score: float
    current_support_price: float
    height: float
    stop_price: float
    target_price: float
    volume_contraction_ratio: float
    breakout_volume_ratio: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ContinuationDetection:
    pattern_name: str
    state: str
    score: float
    pole_start_index: int
    pole_end_index: int
    consolidation_start_index: int
    pole_gain_rate: float
    pole_height: float
    pole_volume_ratio: float
    upper_slope: float
    lower_slope: float
    upper_fit_score: float
    lower_fit_score: float
    current_upper_price: float
    current_lower_price: float
    retracement_rate: float
    volume_contraction_ratio: float
    breakout_volume_ratio: float
    stop_price: float
    target_price: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BoxBreakoutDetection:
    state: str
    score: float
    start_index: int
    support_price: float
    resistance_price: float
    support_touches: int
    resistance_touches: int
    width_rate: float
    containment_ratio: float
    breakout_volume_ratio: float
    stop_price: float
    target_price: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DoubleExtremeDetection:
    pattern_name: str
    state: str
    score: float
    first_index: int
    second_index: int
    first_price: float
    second_price: float
    neckline_price: float
    similarity_rate: float
    height_rate: float
    breakout_volume_ratio: float
    stop_price: float
    target_price: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HeadShouldersDetection:
    pattern_name: str
    state: str
    score: float
    left_shoulder_index: int
    head_index: int
    right_shoulder_index: int
    left_shoulder_price: float
    head_price: float
    right_shoulder_price: float
    neckline_price: float
    neckline_slope: float
    shoulder_similarity_rate: float
    head_prominence_rate: float
    breakout_volume_ratio: float
    stop_price: float
    target_price: float
    reasons: tuple[str, ...]


def find_pivots(
    bars: Sequence[DailyBar],
    *,
    radius: int = 2,
) -> tuple[Pivot, ...]:
    if radius < 1:
        raise ValueError("피벗 반경은 1 이상이어야 합니다.")
    if len(bars) < radius * 2 + 1:
        return ()
    pivots: list[Pivot] = []
    for index in range(radius, len(bars) - radius):
        current = bars[index]
        neighbors = bars[index - radius : index + radius + 1]
        other = [bar for offset, bar in enumerate(neighbors) if offset != radius]
        if (
            all(current.high >= bar.high for bar in other)
            and any(current.high > bar.high for bar in other)
        ):
            pivots.append(Pivot(index=index, kind="high", price=float(current.high)))
        if (
            all(current.low <= bar.low for bar in other)
            and any(current.low < bar.low for bar in other)
        ):
            pivots.append(Pivot(index=index, kind="low", price=float(current.low)))
    return tuple(pivots)


def build_price_zones(
    pivots: Sequence[Pivot],
    *,
    kind: str,
    reference_price: float,
    atr: float | None = None,
    tolerance_rate: float = 0.015,
    minimum_touches: int = 2,
) -> tuple[PriceZone, ...]:
    if kind not in {"high", "low"}:
        raise ValueError("가격 구간 종류는 high 또는 low여야 합니다.")
    if reference_price <= 0:
        return ()
    tolerance = max(
        reference_price * tolerance_rate,
        (atr or 0.0) * 0.5,
    )
    selected = sorted(
        (pivot for pivot in pivots if pivot.kind == kind),
        key=lambda pivot: (pivot.price, pivot.index),
    )
    clusters: list[list[Pivot]] = []
    for pivot in selected:
        best: list[Pivot] | None = None
        best_distance = float("inf")
        for cluster in clusters:
            center = _average([item.price for item in cluster])
            distance = abs(pivot.price - center)
            if distance <= tolerance and distance < best_distance:
                best = cluster
                best_distance = distance
        if best is None:
            clusters.append([pivot])
        else:
            best.append(pivot)

    zones = []
    for cluster in clusters:
        if len(cluster) < minimum_touches:
            continue
        ordered = sorted(cluster, key=lambda pivot: pivot.index)
        level = _average([pivot.price for pivot in ordered])
        zones.append(
            PriceZone(
                kind=kind,
                level=round(level, 4),
                lower=round(min(pivot.price for pivot in ordered), 4),
                upper=round(max(pivot.price for pivot in ordered), 4),
                touch_count=len(ordered),
                first_index=ordered[0].index,
                last_index=ordered[-1].index,
                pivot_indexes=tuple(pivot.index for pivot in ordered),
            )
        )
    return tuple(
        sorted(
            zones,
            key=lambda zone: (-zone.touch_count, -zone.last_index),
        )
    )


def fit_trendline(
    pivots: Sequence[Pivot],
    *,
    kind: str,
) -> TrendLine | None:
    selected = sorted(
        (pivot for pivot in pivots if pivot.kind == kind),
        key=lambda pivot: pivot.index,
    )
    if len(selected) < 2:
        return None
    xs = [float(pivot.index) for pivot in selected]
    ys = [pivot.price for pivot in selected]
    x_mean = _average(xs)
    y_mean = _average(ys)
    denominator = sum((value - x_mean) ** 2 for value in xs)
    if denominator <= 0:
        return None
    slope = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(xs, ys)
    ) / denominator
    intercept = y_mean - slope * x_mean
    total = sum((value - y_mean) ** 2 for value in ys)
    residual = sum(
        (y - (intercept + slope * x)) ** 2
        for x, y in zip(xs, ys)
    )
    fit_score = 1.0 if total <= 0 else max(0.0, 1 - residual / total)
    return TrendLine(
        kind=kind,
        slope=round(slope, 8),
        intercept=round(intercept, 8),
        fit_score=round(fit_score, 6),
        start_index=selected[0].index,
        end_index=selected[-1].index,
        touch_count=len(selected),
    )


def find_bullish_flagpole(
    bars: Sequence[DailyBar],
    *,
    consolidation_start: int,
    atr: float | None = None,
) -> Flagpole | None:
    candidates = []
    for length in range(5, 16):
        start = consolidation_start - length
        end = consolidation_start - 1
        if start < 1 or end <= start:
            continue
        start_price = float(bars[start].close)
        end_price = float(bars[end].close)
        if start_price <= 0:
            continue
        gain = end_price / start_price - 1
        height = end_price - start_price
        if gain < 0.15:
            continue
        if atr and atr > 0 and height < atr * 3:
            continue
        segment = bars[start : end + 1]
        maximum_close = max(float(bar.close) for bar in segment)
        if end_price < maximum_close * 0.99:
            continue
        positive_ratio = sum(
            current.close > previous.close
            for previous, current in zip(segment, segment[1:])
        ) / max(len(segment) - 1, 1)
        if positive_ratio < 0.6:
            continue
        preceding = bars[max(0, start - length) : start]
        preceding_volume = _average(
            [float(max(bar.volume, 0)) for bar in preceding]
        )
        pole_volume = _average(
            [float(max(bar.volume, 0)) for bar in segment]
        )
        volume_ratio = (
            pole_volume / preceding_volume if preceding_volume > 0 else 1.0
        )
        candidates.append(
            Flagpole(
                start_index=start,
                end_index=end,
                start_price=start_price,
                end_price=end_price,
                height=height,
                gain_rate=gain,
                positive_bar_ratio=positive_ratio,
                volume_ratio=volume_ratio,
            )
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda pole: (
            pole.gain_rate,
            pole.positive_bar_ratio,
            pole.volume_ratio,
        ),
    )


def detect_bull_flag(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> ContinuationDetection | None:
    return _detect_bullish_continuation(bars, pattern_name="bull_flag", atr=atr)


def detect_bull_pennant(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> ContinuationDetection | None:
    return _detect_bullish_continuation(
        bars,
        pattern_name="bull_pennant",
        atr=atr,
    )


def _detect_bullish_continuation(
    bars: Sequence[DailyBar],
    *,
    pattern_name: str,
    atr: float | None,
) -> ContinuationDetection | None:
    if len(bars) < 25:
        return None
    window = list(bars[-70:])
    offset = len(bars) - len(window)
    current_index = len(window) - 1
    candidates = []
    for duration in range(5, 21):
        consolidation_start = current_index - duration
        if consolidation_start < 16:
            continue
        pole = find_bullish_flagpole(
            window,
            consolidation_start=consolidation_start,
            atr=atr,
        )
        if pole is None:
            continue
        consolidation = window[consolidation_start:current_index]
        high_line = fit_trendline(
            [
                Pivot(
                    index=consolidation_start + index,
                    kind="high",
                    price=float(bar.high),
                )
                for index, bar in enumerate(consolidation)
            ],
            kind="high",
        )
        low_line = fit_trendline(
            [
                Pivot(
                    index=consolidation_start + index,
                    kind="low",
                    price=float(bar.low),
                )
                for index, bar in enumerate(consolidation)
            ],
            kind="low",
        )
        if high_line is None or low_line is None:
            continue
        candidate = _evaluate_bullish_continuation(
            window,
            pole,
            consolidation_start,
            high_line,
            low_line,
            pattern_name=pattern_name,
            atr=atr,
            offset=offset,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.state == CONTINUATION_CONFIRMED,
            item.score,
            item.consolidation_start_index,
        ),
    )


def _evaluate_bullish_continuation(
    bars: Sequence[DailyBar],
    pole: Flagpole,
    consolidation_start: int,
    high_line: TrendLine,
    low_line: TrendLine,
    *,
    pattern_name: str,
    atr: float | None,
    offset: int,
) -> ContinuationDetection | None:
    current_index = len(bars) - 1
    consolidation = bars[consolidation_start:current_index]
    highest = max(float(bar.high) for bar in consolidation)
    lowest = min(float(bar.low) for bar in consolidation)
    retracement = (pole.end_price - lowest) / pole.height
    if retracement < 0 or retracement > 0.5:
        return None
    if lowest < pole.start_price + pole.height * 0.5:
        return None
    price = max(pole.end_price, 1.0)
    high_slope_rate = high_line.slope / price
    low_slope_rate = low_line.slope / price
    start_width = (
        high_line.value_at(consolidation_start)
        - low_line.value_at(consolidation_start)
    )
    end_width = (
        high_line.value_at(current_index - 1)
        - low_line.value_at(current_index - 1)
    )
    if start_width <= 0 or end_width <= 0:
        return None

    if pattern_name == "bull_flag":
        parallel_gap = abs(high_slope_rate - low_slope_rate)
        if not (
            -0.02 <= high_slope_rate <= 0.002
            and -0.02 <= low_slope_rate <= 0.002
            and parallel_gap <= 0.004
            and high_line.fit_score >= 0.45
            and low_line.fit_score >= 0.45
            and end_width <= start_width * 1.25
        ):
            return None
        shape_quality = max(0.0, 1 - parallel_gap / 0.004)
    else:
        width_ratio = end_width / start_width
        if not (
            high_slope_rate < -0.0005
            and low_slope_rate > 0.0005
            and width_ratio <= 0.75
            and high_line.fit_score >= 0.45
            and low_line.fit_score >= 0.45
        ):
            return None
        shape_quality = max(0.0, min(1.0, 1 - width_ratio))

    consolidation_volume = _average(
        [float(max(bar.volume, 0)) for bar in consolidation]
    )
    pole_volume = _average(
        [
            float(max(bar.volume, 0))
            for bar in bars[pole.start_index : pole.end_index + 1]
        ]
    )
    volume_contraction = (
        consolidation_volume / pole_volume if pole_volume > 0 else 0.0
    )
    prior_volumes = [
        float(max(bar.volume, 0))
        for bar in bars[max(0, current_index - 20) : current_index]
    ]
    average_volume = _average(prior_volumes)
    breakout_volume = (
        float(max(bars[-1].volume, 0)) / average_volume
        if average_volume > 0
        else 0.0
    )
    current_upper = high_line.value_at(current_index)
    current_lower = low_line.value_at(current_index)
    if current_upper <= current_lower:
        return None
    buffer = max(current_upper * 0.005, (atr or 0.0) * 0.3)
    close = float(bars[-1].close)
    if close < current_lower - buffer:
        state = CONTINUATION_FAILED
    elif close >= current_upper + buffer:
        state = (
            CONTINUATION_CONFIRMED
            if breakout_volume >= 1.4
            else CONTINUATION_BREAKOUT_PENDING
        )
    elif float(bars[-1].high) > current_upper:
        state = CONTINUATION_BREAKOUT_PENDING
    elif close >= current_upper * 0.97:
        state = CONTINUATION_READY
    else:
        state = CONTINUATION_FORMING

    score = min(20.0, pole.gain_rate * 80)
    score += pole.positive_bar_ratio * 10
    score += min(10.0, max(0.0, (pole.volume_ratio - 1) * 10))
    score += shape_quality * 20
    score += (high_line.fit_score + low_line.fit_score) * 10
    score += max(0.0, 10 * (1 - retracement / 0.5))
    if volume_contraction <= 0.8:
        score += 10
    score += min(15.0, max(0.0, (breakout_volume - 1) * 15))
    if state == CONTINUATION_CONFIRMED:
        score += 5

    label = "상승 플래그" if pattern_name == "bull_flag" else "상승 페넌트"
    reasons = [
        f"{label} 선행 상승 {pole.gain_rate:.1%}",
        f"조정폭은 깃대의 {retracement:.1%}",
        (
            f"채널 기울기 {high_slope_rate:.3%}/{low_slope_rate:.3%}"
            if pattern_name == "bull_flag"
            else f"수렴 추세선 {high_slope_rate:.3%}/{low_slope_rate:.3%}"
        ),
    ]
    if volume_contraction <= 0.8:
        reasons.append(f"조정 거래량 감소 {volume_contraction:.2f}배")
    if state == CONTINUATION_CONFIRMED:
        reasons.append(f"상단 종가 돌파·거래량 {breakout_volume:.2f}배")
    elif state == CONTINUATION_BREAKOUT_PENDING:
        reasons.append(f"돌파 확인 대기·거래량 {breakout_volume:.2f}배")
    elif state == CONTINUATION_READY:
        reasons.append("상단 추세선 3% 이내에서 돌파 대기")
    elif state == CONTINUATION_FAILED:
        reasons.append("하단 추세선 이탈로 패턴 무효")

    stop_price = max(0.0, current_lower - buffer)
    return ContinuationDetection(
        pattern_name=pattern_name,
        state=state,
        score=round(min(100.0, score), 2),
        pole_start_index=pole.start_index + offset,
        pole_end_index=pole.end_index + offset,
        consolidation_start_index=consolidation_start + offset,
        pole_gain_rate=round(pole.gain_rate, 6),
        pole_height=round(pole.height, 4),
        pole_volume_ratio=round(pole.volume_ratio, 6),
        upper_slope=round(high_line.slope, 8),
        lower_slope=round(low_line.slope, 8),
        upper_fit_score=round(high_line.fit_score, 6),
        lower_fit_score=round(low_line.fit_score, 6),
        current_upper_price=round(current_upper, 4),
        current_lower_price=round(current_lower, 4),
        retracement_rate=round(retracement, 6),
        volume_contraction_ratio=round(volume_contraction, 6),
        breakout_volume_ratio=round(breakout_volume, 6),
        stop_price=round(stop_price, 4),
        target_price=round(current_upper + pole.height, 4),
        reasons=tuple(reasons),
    )


def detect_box_breakout(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> BoxBreakoutDetection | None:
    if len(bars) < 25:
        return None
    window = list(bars[-80:])
    offset = len(bars) - len(window)
    pivots = find_pivots(window)
    reference = float(window[-1].close)
    resistance_zones = build_price_zones(
        pivots,
        kind="high",
        reference_price=reference,
        atr=atr,
        tolerance_rate=0.012,
    )
    support_zones = build_price_zones(
        pivots,
        kind="low",
        reference_price=reference,
        atr=atr,
        tolerance_rate=0.012,
    )
    candidates = []
    for resistance in resistance_zones:
        for support in support_zones:
            if support.level >= resistance.level:
                continue
            start = min(resistance.first_index, support.first_index)
            end = max(resistance.last_index, support.last_index)
            duration = end - start
            if not 15 <= duration <= 65:
                continue
            if len(window) - 1 - end > 12:
                continue
            width = resistance.level - support.level
            width_rate = width / resistance.level
            if not 0.04 <= width_rate <= 0.30:
                continue
            formation = window[start:-1]
            tolerance = max(reference * 0.01, (atr or 0.0) * 0.5)
            contained = sum(
                bar.high <= resistance.upper + tolerance
                and bar.low >= support.lower - tolerance
                for bar in formation
            )
            containment = contained / len(formation) if formation else 0.0
            if containment < 0.75:
                continue
            current = window[-1]
            prior_volumes = [
                float(max(bar.volume, 0))
                for bar in window[max(0, len(window) - 21) : -1]
            ]
            average_volume = _average(prior_volumes)
            breakout_volume = (
                float(max(current.volume, 0)) / average_volume
                if average_volume > 0
                else 0.0
            )
            buffer = max(resistance.level * 0.005, (atr or 0.0) * 0.3)
            close = float(current.close)
            if close < support.lower - buffer:
                state = CONTINUATION_FAILED
            elif close >= resistance.upper + buffer:
                state = (
                    CONTINUATION_CONFIRMED
                    if breakout_volume >= 1.4
                    else CONTINUATION_BREAKOUT_PENDING
                )
            elif float(current.high) > resistance.upper:
                state = CONTINUATION_BREAKOUT_PENDING
            elif close >= resistance.level * 0.97:
                state = CONTINUATION_READY
            else:
                state = CONTINUATION_FORMING
            spread = (
                (resistance.upper - resistance.lower) / resistance.level
                + (support.upper - support.lower) / support.level
            )
            score = min(20.0, (resistance.touch_count + support.touch_count) * 5)
            score += containment * 20
            score += max(0.0, 15 * (1 - spread / 0.05))
            score += max(0.0, 10 * (1 - abs(width_rate - 0.12) / 0.18))
            score += min(20.0, max(0.0, (breakout_volume - 1) * 15))
            if state == CONTINUATION_CONFIRMED:
                score += 5
            reasons = [
                f"박스 상단 {resistance.touch_count}회·하단 {support.touch_count}회 접촉",
                f"박스 내부 체류 {containment:.0%}",
                f"박스 폭 {width_rate:.1%}",
            ]
            if state == CONTINUATION_CONFIRMED:
                reasons.append(f"박스 상단 종가 돌파·거래량 {breakout_volume:.2f}배")
            elif state == CONTINUATION_BREAKOUT_PENDING:
                reasons.append(f"박스 돌파 확인 대기·거래량 {breakout_volume:.2f}배")
            elif state == CONTINUATION_READY:
                reasons.append("박스 상단 3% 이내에서 돌파 대기")
            elif state == CONTINUATION_FAILED:
                reasons.append("박스 하단 이탈로 패턴 무효")
            candidates.append(
                BoxBreakoutDetection(
                    state=state,
                    score=round(min(100.0, score), 2),
                    start_index=start + offset,
                    support_price=round(support.level, 4),
                    resistance_price=round(resistance.level, 4),
                    support_touches=support.touch_count,
                    resistance_touches=resistance.touch_count,
                    width_rate=round(width_rate, 6),
                    containment_ratio=round(containment, 6),
                    breakout_volume_ratio=round(breakout_volume, 6),
                    stop_price=round(max(0.0, support.level - buffer), 4),
                    target_price=round(resistance.level + width, 4),
                    reasons=tuple(reasons),
                )
            )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.state == CONTINUATION_CONFIRMED,
            item.score,
            item.start_index,
        ),
    )


def detect_double_bottom(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> DoubleExtremeDetection | None:
    return _detect_double_extreme(bars, bottom=True, atr=atr)


def detect_double_top(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> DoubleExtremeDetection | None:
    return _detect_double_extreme(bars, bottom=False, atr=atr)


def _detect_double_extreme(
    bars: Sequence[DailyBar],
    *,
    bottom: bool,
    atr: float | None,
) -> DoubleExtremeDetection | None:
    if len(bars) < 30:
        return None
    window = list(bars[-80:])
    offset = len(bars) - len(window)
    kind = "low" if bottom else "high"
    pivots = [pivot for pivot in find_pivots(window) if pivot.kind == kind]
    candidates = []
    for first_index, first in enumerate(pivots):
        for second in pivots[first_index + 1 :]:
            separation = second.index - first.index
            if not 8 <= separation <= 50:
                continue
            if len(window) - 1 - second.index > 15:
                continue
            prior_start = max(0, first.index - 20)
            if first.index - prior_start < 8:
                continue
            prior_close = float(window[prior_start].close)
            if prior_close <= 0:
                continue
            prior_move = first.price / prior_close - 1
            if bottom and prior_move > -0.05:
                continue
            if not bottom and prior_move < 0.05:
                continue
            average_price = (first.price + second.price) / 2
            tolerance = max(0.03, (atr or 0.0) * 1.5 / max(average_price, 1))
            tolerance = min(tolerance, 0.06)
            similarity = abs(first.price - second.price) / max(average_price, 1)
            if similarity > tolerance:
                continue
            between = window[first.index + 1 : second.index]
            if not between:
                continue
            neckline = (
                max(float(bar.high) for bar in between)
                if bottom
                else min(float(bar.low) for bar in between)
            )
            height = (
                neckline - average_price
                if bottom
                else average_price - neckline
            )
            height_rate = height / max(average_price, 1)
            if not 0.05 <= height_rate <= 0.35:
                continue
            current = window[-1]
            prior_volumes = [
                float(max(bar.volume, 0))
                for bar in window[max(0, len(window) - 21) : -1]
            ]
            average_volume = _average(prior_volumes)
            breakout_volume = (
                float(max(current.volume, 0)) / average_volume
                if average_volume > 0
                else 0.0
            )
            buffer = max(neckline * 0.005, (atr or 0.0) * 0.3)
            close = float(current.close)
            invalidation = (
                max(first.price, second.price) + buffer
                if not bottom
                else min(first.price, second.price) - buffer
            )
            if bottom:
                if close < invalidation:
                    state = CONTINUATION_FAILED
                elif close >= neckline + buffer:
                    state = (
                        CONTINUATION_CONFIRMED
                        if breakout_volume >= 1.3
                        else CONTINUATION_BREAKOUT_PENDING
                    )
                elif float(current.high) > neckline:
                    state = CONTINUATION_BREAKOUT_PENDING
                elif close >= neckline * 0.97:
                    state = CONTINUATION_READY
                else:
                    state = CONTINUATION_FORMING
                stop = min(first.price, second.price) - buffer
                target = neckline + height
            else:
                if close > invalidation:
                    state = CONTINUATION_FAILED
                elif close <= neckline - buffer:
                    state = (
                        CONTINUATION_CONFIRMED
                        if breakout_volume >= 1.2
                        else CONTINUATION_BREAKOUT_PENDING
                    )
                elif float(current.low) < neckline:
                    state = CONTINUATION_BREAKOUT_PENDING
                elif close <= neckline * 1.03:
                    state = CONTINUATION_READY
                else:
                    state = CONTINUATION_FORMING
                stop = max(first.price, second.price) + buffer
                target = max(0.0, neckline - height)
            score = max(0.0, 25 * (1 - similarity / tolerance))
            score += min(20.0, height_rate * 150)
            score += max(0.0, 15 * (1 - abs(separation - 20) / 30))
            score += min(20.0, max(0.0, (breakout_volume - 1) * 20))
            if state == CONTINUATION_CONFIRMED:
                score += 10
            label = "이중바닥" if bottom else "이중천장"
            reasons = [
                f"{label} 피벗 간격 {separation}봉",
                f"두 극점 차이 {similarity:.1%}",
                f"목선 높이 {height_rate:.1%}",
                f"선행 추세 {prior_move:+.1%}",
            ]
            if state == CONTINUATION_CONFIRMED:
                reasons.append(f"목선 종가 돌파·거래량 {breakout_volume:.2f}배")
            elif state == CONTINUATION_BREAKOUT_PENDING:
                reasons.append(f"목선 돌파 확인 대기·거래량 {breakout_volume:.2f}배")
            elif state == CONTINUATION_READY:
                reasons.append("목선 3% 이내에서 돌파 대기")
            elif state == CONTINUATION_FAILED:
                reasons.append("두 극점 기준 이탈로 패턴 무효")
            candidates.append(
                DoubleExtremeDetection(
                    pattern_name="double_bottom" if bottom else "double_top",
                    state=state,
                    score=round(min(100.0, score), 2),
                    first_index=first.index + offset,
                    second_index=second.index + offset,
                    first_price=round(first.price, 4),
                    second_price=round(second.price, 4),
                    neckline_price=round(neckline, 4),
                    similarity_rate=round(similarity, 6),
                    height_rate=round(height_rate, 6),
                    breakout_volume_ratio=round(breakout_volume, 6),
                    stop_price=round(max(0.0, stop), 4),
                    target_price=round(max(0.0, target), 4),
                    reasons=tuple(reasons),
                )
            )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.state == CONTINUATION_CONFIRMED,
            item.score,
            item.second_index,
        ),
    )


def detect_head_shoulders(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> HeadShouldersDetection | None:
    return _detect_head_shoulders(bars, inverse=False, atr=atr)


def detect_inverse_head_shoulders(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> HeadShouldersDetection | None:
    return _detect_head_shoulders(bars, inverse=True, atr=atr)


def _detect_head_shoulders(
    bars: Sequence[DailyBar],
    *,
    inverse: bool,
    atr: float | None,
) -> HeadShouldersDetection | None:
    if len(bars) < 40:
        return None
    window = list(bars[-100:])
    offset = len(bars) - len(window)
    kind = "low" if inverse else "high"
    extrema = [pivot for pivot in find_pivots(window) if pivot.kind == kind]
    candidates = []
    for head_position in range(1, len(extrema) - 1):
        head = extrema[head_position]
        for left in extrema[:head_position]:
            for right in extrema[head_position + 1 :]:
                if not 6 <= head.index - left.index <= 35:
                    continue
                if not 6 <= right.index - head.index <= 35:
                    continue
                if len(window) - 1 - right.index > 15:
                    continue
                prior_start = max(0, left.index - 20)
                if left.index - prior_start < 8:
                    continue
                prior_close = float(window[prior_start].close)
                if prior_close <= 0:
                    continue
                prior_move = left.price / prior_close - 1
                if inverse and prior_move > -0.08:
                    continue
                if not inverse and prior_move < 0.08:
                    continue
                shoulders_average = (left.price + right.price) / 2
                similarity = abs(left.price - right.price) / max(
                    shoulders_average,
                    1,
                )
                if similarity > 0.06:
                    continue
                prominence = (
                    (shoulders_average - head.price) / shoulders_average
                    if inverse
                    else (head.price - shoulders_average) / shoulders_average
                )
                minimum_prominence = max(
                    0.05,
                    (atr or 0.0) * 1.5 / max(shoulders_average, 1),
                )
                if not minimum_prominence <= prominence <= 0.40:
                    continue
                first_segment = window[left.index + 1 : head.index]
                second_segment = window[head.index + 1 : right.index]
                if not first_segment or not second_segment:
                    continue
                if inverse:
                    neck_one = Pivot(
                        index=left.index + 1 + max(
                            range(len(first_segment)),
                            key=lambda i: first_segment[i].high,
                        ),
                        kind="high",
                        price=max(float(bar.high) for bar in first_segment),
                    )
                    neck_two = Pivot(
                        index=head.index + 1 + max(
                            range(len(second_segment)),
                            key=lambda i: second_segment[i].high,
                        ),
                        kind="high",
                        price=max(float(bar.high) for bar in second_segment),
                    )
                    neck_kind = "high"
                else:
                    neck_one = Pivot(
                        index=left.index + 1 + min(
                            range(len(first_segment)),
                            key=lambda i: first_segment[i].low,
                        ),
                        kind="low",
                        price=min(float(bar.low) for bar in first_segment),
                    )
                    neck_two = Pivot(
                        index=head.index + 1 + min(
                            range(len(second_segment)),
                            key=lambda i: second_segment[i].low,
                        ),
                        kind="low",
                        price=min(float(bar.low) for bar in second_segment),
                    )
                    neck_kind = "low"
                neckline = fit_trendline([neck_one, neck_two], kind=neck_kind)
                if neckline is None:
                    continue
                neckline_price = neckline.value_at(len(window) - 1)
                if neckline_price <= 0:
                    continue
                depth = (
                    neckline.value_at(head.index) - head.price
                    if inverse
                    else head.price - neckline.value_at(head.index)
                )
                if depth <= 0:
                    continue
                current = window[-1]
                prior_volumes = [
                    float(max(bar.volume, 0))
                    for bar in window[max(0, len(window) - 21) : -1]
                ]
                average_volume = _average(prior_volumes)
                breakout_volume = (
                    float(max(current.volume, 0)) / average_volume
                    if average_volume > 0
                    else 0.0
                )
                buffer = max(neckline_price * 0.005, (atr or 0.0) * 0.3)
                close = float(current.close)
                if inverse:
                    if close < right.price - buffer:
                        state = CONTINUATION_FAILED
                    elif close >= neckline_price + buffer:
                        state = (
                            CONTINUATION_CONFIRMED
                            if breakout_volume >= 1.3
                            else CONTINUATION_BREAKOUT_PENDING
                        )
                    elif float(current.high) > neckline_price:
                        state = CONTINUATION_BREAKOUT_PENDING
                    elif close >= neckline_price * 0.97:
                        state = CONTINUATION_READY
                    else:
                        state = CONTINUATION_FORMING
                    stop = right.price - buffer
                    target = neckline_price + depth
                else:
                    if close > right.price + buffer:
                        state = CONTINUATION_FAILED
                    elif close <= neckline_price - buffer:
                        state = (
                            CONTINUATION_CONFIRMED
                            if breakout_volume >= 1.2
                            else CONTINUATION_BREAKOUT_PENDING
                        )
                    elif float(current.low) < neckline_price:
                        state = CONTINUATION_BREAKOUT_PENDING
                    elif close <= neckline_price * 1.03:
                        state = CONTINUATION_READY
                    else:
                        state = CONTINUATION_FORMING
                    stop = right.price + buffer
                    target = max(0.0, neckline_price - depth)
                left_duration = head.index - left.index
                right_duration = right.index - head.index
                symmetry = min(left_duration, right_duration) / max(
                    left_duration,
                    right_duration,
                )
                if symmetry < 0.4:
                    continue
                score = max(0.0, 20 * (1 - similarity / 0.06))
                score += min(20.0, prominence * 200)
                score += symmetry * 15
                score += max(0.0, 10 * (1 - abs(neckline.slope) / max(neckline_price * 0.02, 1)))
                score += min(20.0, max(0.0, (breakout_volume - 1) * 20))
                if state == CONTINUATION_CONFIRMED:
                    score += 10
                label = "역헤드 앤 숄더" if inverse else "헤드 앤 숄더"
                reasons = [
                    f"{label} 어깨 차이 {similarity:.1%}",
                    f"머리 돌출 {prominence:.1%}",
                    f"좌우 기간 대칭 {symmetry:.0%}",
                    f"목선 기울기 {neckline.slope / neckline_price:.3%}",
                    f"선행 추세 {prior_move:+.1%}",
                ]
                if state == CONTINUATION_CONFIRMED:
                    reasons.append(f"목선 종가 돌파·거래량 {breakout_volume:.2f}배")
                elif state == CONTINUATION_BREAKOUT_PENDING:
                    reasons.append(f"목선 돌파 확인 대기·거래량 {breakout_volume:.2f}배")
                elif state == CONTINUATION_READY:
                    reasons.append("목선 3% 이내에서 돌파 대기")
                elif state == CONTINUATION_FAILED:
                    reasons.append("오른쪽 어깨 기준 이탈로 패턴 무효")
                candidates.append(
                    HeadShouldersDetection(
                        pattern_name=(
                            "inverse_head_shoulders"
                            if inverse
                            else "head_shoulders"
                        ),
                        state=state,
                        score=round(min(100.0, score), 2),
                        left_shoulder_index=left.index + offset,
                        head_index=head.index + offset,
                        right_shoulder_index=right.index + offset,
                        left_shoulder_price=round(left.price, 4),
                        head_price=round(head.price, 4),
                        right_shoulder_price=round(right.price, 4),
                        neckline_price=round(neckline_price, 4),
                        neckline_slope=round(neckline.slope, 8),
                        shoulder_similarity_rate=round(similarity, 6),
                        head_prominence_rate=round(prominence, 6),
                        breakout_volume_ratio=round(breakout_volume, 6),
                        stop_price=round(max(0.0, stop), 4),
                        target_price=round(max(0.0, target), 4),
                        reasons=tuple(reasons),
                    )
                )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.state == CONTINUATION_CONFIRMED,
            item.score,
            item.right_shoulder_index,
        ),
    )


def detect_ascending_triangle(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> AscendingTriangleDetection | None:
    if len(bars) < 30:
        return None
    window = list(bars[-80:])
    offset = len(bars) - len(window)
    pivots = find_pivots(window)
    zones = build_price_zones(
        pivots,
        kind="high",
        reference_price=float(window[-1].close),
        atr=atr,
    )
    candidates = []
    for resistance in zones:
        duration = resistance.last_index - resistance.first_index
        if not 10 <= duration <= 60:
            continue
        if len(window) - 1 - resistance.last_index > 15:
            continue
        lows = [
            pivot
            for pivot in pivots
            if pivot.kind == "low"
            and resistance.first_index <= pivot.index <= len(window) - 2
            and pivot.price < resistance.lower
        ]
        if len(lows) < 2:
            continue
        support = fit_trendline(lows, kind="low")
        if support is None or support.slope <= 0 or support.fit_score < 0.55:
            continue
        low_rise = lows[-1].price / lows[0].price - 1
        if low_rise < 0.02:
            continue
        current_support = support.value_at(len(window) - 1)
        if current_support >= resistance.lower:
            continue
        candidate = _evaluate_ascending_triangle(
            window,
            resistance,
            support,
            lows,
            atr=atr,
            offset=offset,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.state == ASCENDING_TRIANGLE_CONFIRMED,
            item.score,
            item.start_index,
        ),
    )


def _evaluate_ascending_triangle(
    bars: Sequence[DailyBar],
    resistance: PriceZone,
    support: TrendLine,
    lows: Sequence[Pivot],
    *,
    atr: float | None,
    offset: int,
) -> AscendingTriangleDetection | None:
    current_index = len(bars) - 1
    current = bars[-1]
    current_support = support.value_at(current_index)
    initial_support = support.value_at(resistance.first_index)
    height = resistance.level - initial_support
    if height <= resistance.level * 0.04:
        return None
    if height > resistance.level * 0.35:
        return None

    formation = bars[resistance.first_index : -1]
    if len(formation) < 15:
        return None
    early_count = max(5, len(formation) // 3)
    early_volume = _average(
        [float(max(bar.volume, 0)) for bar in formation[:early_count]]
    )
    late_volume = _average(
        [float(max(bar.volume, 0)) for bar in formation[-early_count:]]
    )
    volume_contraction = (
        late_volume / early_volume if early_volume > 0 else 0.0
    )
    prior_volumes = [
        float(max(bar.volume, 0))
        for bar in bars[max(0, current_index - 20) : current_index]
    ]
    average_volume = _average(prior_volumes)
    breakout_volume = (
        float(max(current.volume, 0)) / average_volume
        if average_volume > 0
        else 0.0
    )
    breakout_buffer = max(
        resistance.level * 0.005,
        (atr or 0.0) * 0.3,
    )
    close = float(current.close)
    if close < current_support - max(resistance.level * 0.005, (atr or 0) * 0.3):
        state = ASCENDING_TRIANGLE_FAILED
    elif close >= resistance.upper + breakout_buffer:
        state = (
            ASCENDING_TRIANGLE_CONFIRMED
            if breakout_volume >= 1.4
            else ASCENDING_TRIANGLE_BREAKOUT_PENDING
        )
    elif float(current.high) > resistance.upper:
        state = ASCENDING_TRIANGLE_BREAKOUT_PENDING
    elif close >= resistance.level * 0.97:
        state = ASCENDING_TRIANGLE_READY
    else:
        state = ASCENDING_TRIANGLE_FORMING

    low_rise = lows[-1].price / lows[0].price - 1
    resistance_spread = (
        (resistance.upper - resistance.lower) / resistance.level
        if resistance.level > 0
        else 1.0
    )
    score = min(20.0, resistance.touch_count * 7.0)
    score += max(0.0, 15.0 * (1 - resistance_spread / 0.03))
    score += min(20.0, low_rise * 200)
    score += support.fit_score * 15
    score += 10 if volume_contraction <= 0.85 else 0
    score += min(15.0, max(0.0, (breakout_volume - 1.0) * 15))
    if state == ASCENDING_TRIANGLE_CONFIRMED:
        score += 5

    reasons = [
        f"수평 저항 {resistance.touch_count}회 접촉",
        f"상승 지지선 적합도 {support.fit_score:.2f}",
        f"저점 상승률 {low_rise:.1%}",
    ]
    if volume_contraction <= 0.85:
        reasons.append(f"형성 구간 거래량 수축 {volume_contraction:.2f}배")
    if state == ASCENDING_TRIANGLE_CONFIRMED:
        reasons.append(f"저항 종가 돌파·거래량 {breakout_volume:.2f}배")
    elif state == ASCENDING_TRIANGLE_BREAKOUT_PENDING:
        reasons.append(f"돌파 확인 대기·거래량 {breakout_volume:.2f}배")
    elif state == ASCENDING_TRIANGLE_READY:
        reasons.append("수평 저항 3% 이내에서 돌파 대기")
    elif state == ASCENDING_TRIANGLE_FAILED:
        reasons.append("상승 지지선 이탈로 패턴 무효")

    stop_buffer = max(resistance.level * 0.003, (atr or 0.0) * 0.3)
    stop_price = max(0.0, max(current_support, lows[-1].price) - stop_buffer)
    return AscendingTriangleDetection(
        state=state,
        score=round(min(100.0, score), 2),
        start_index=resistance.first_index + offset,
        resistance_price=round(resistance.level, 4),
        resistance_lower=round(resistance.lower, 4),
        resistance_upper=round(resistance.upper, 4),
        resistance_touches=resistance.touch_count,
        support_slope=round(support.slope, 8),
        support_fit_score=round(support.fit_score, 6),
        current_support_price=round(current_support, 4),
        height=round(height, 4),
        stop_price=round(stop_price, 4),
        target_price=round(resistance.level + height, 4),
        volume_contraction_ratio=round(volume_contraction, 6),
        breakout_volume_ratio=round(breakout_volume, 6),
        reasons=tuple(reasons),
    )


def detect_cup_handle(
    bars: Sequence[DailyBar],
    *,
    atr: float | None = None,
) -> CupHandleDetection | None:
    if len(bars) < 45:
        return None
    window = list(bars[-130:])
    offset = len(bars) - len(window)
    pivots = find_pivots(window)
    high_pivots = [pivot for pivot in pivots if pivot.kind == "high"]
    low_pivots = [pivot for pivot in pivots if pivot.kind == "low"]
    last_index = len(window) - 1
    candidates: list[CupHandleDetection] = []

    for right in high_pivots:
        handle_length = last_index - right.index - 1
        if not 5 <= handle_length <= 20:
            continue
        for left in high_pivots:
            cup_length = right.index - left.index
            if not 30 <= cup_length <= 110:
                continue
            bottoms = [
                pivot
                for pivot in low_pivots
                if left.index + 8 <= pivot.index <= right.index - 8
            ]
            if not bottoms:
                continue
            bottom = min(bottoms, key=lambda pivot: pivot.price)
            candidate = _evaluate_cup_handle(
                window,
                left,
                bottom,
                right,
                atr=atr,
                offset=offset,
            )
            if candidate is not None:
                candidates.append(candidate)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.state == CUP_HANDLE_CONFIRMED,
            item.score,
            item.right_rim_index,
        ),
    )


def _evaluate_cup_handle(
    bars: Sequence[DailyBar],
    left: Pivot,
    bottom: Pivot,
    right: Pivot,
    *,
    atr: float | None,
    offset: int,
) -> CupHandleDetection | None:
    rim_average = (left.price + right.price) / 2
    if rim_average <= 0:
        return None
    cup_depth = (rim_average - bottom.price) / rim_average
    rim_difference = abs(left.price - right.price) / rim_average
    atr_tolerance = (1.5 * atr / rim_average) if atr and atr > 0 else 0.0
    rim_tolerance = min(0.08, max(0.05, atr_tolerance))
    if not 0.12 <= cup_depth <= 0.35 or rim_difference > rim_tolerance:
        return None

    left_duration = bottom.index - left.index
    right_duration = right.index - bottom.index
    duration_ratio = min(left_duration, right_duration) / max(
        left_duration,
        right_duration,
    )
    if duration_ratio < 0.35:
        return None

    cup_range = rim_average - bottom.price
    near_bottom = [
        bar
        for bar in bars[left.index + 1 : right.index]
        if bar.low <= bottom.price + cup_range * 0.12
    ]
    if len(near_bottom) < 6:
        return None

    handle = list(bars[right.index + 1 : -1])
    if len(handle) < 5:
        return None
    handle_low = float(min(bar.low for bar in handle))
    handle_depth = (right.price - handle_low) / right.price
    if handle_depth < 0 or handle_depth > min(0.12, cup_depth / 3):
        return None
    if handle_low < bottom.price + cup_range * 0.5:
        return None

    current = bars[-1]
    handle_high = max(float(bar.high) for bar in handle)
    pivot_price = max(right.price, handle_high)
    prior_volumes = [
        float(max(bar.volume, 0))
        for bar in bars[max(0, len(bars) - 21) : -1]
    ]
    average_volume = _average(prior_volumes)
    breakout_volume_ratio = (
        float(max(current.volume, 0)) / average_volume
        if average_volume > 0
        else 0.0
    )
    cup_volumes = [
        float(max(bar.volume, 0))
        for bar in bars[left.index : right.index + 1]
    ]
    handle_volume_ratio = (
        _average([float(max(bar.volume, 0)) for bar in handle])
        / _average(cup_volumes)
        if _average(cup_volumes) > 0
        else 0.0
    )

    breakout_buffer = max(
        pivot_price * 0.005,
        (atr or 0.0) * 0.3,
    )
    close = float(current.close)
    if close < handle_low:
        state = CUP_HANDLE_FAILED
    elif close >= pivot_price + breakout_buffer:
        state = (
            CUP_HANDLE_CONFIRMED
            if breakout_volume_ratio >= 1.4
            else CUP_HANDLE_BREAKOUT_PENDING
        )
    elif float(current.high) > pivot_price:
        state = CUP_HANDLE_BREAKOUT_PENDING
    elif close >= pivot_price * 0.97:
        state = CUP_HANDLE_READY
    else:
        state = CUP_HANDLE_FORMING

    score = 0.0
    reasons: list[str] = []
    depth_quality = max(0.0, 1 - abs(cup_depth - 0.22) / 0.13)
    score += depth_quality * 15
    score += max(0.0, 1 - rim_difference / rim_tolerance) * 15
    score += duration_ratio * 10
    score += min(1.0, len(near_bottom) / 8) * 10

    prior_start = max(0, left.index - 20)
    prior_start_close = float(bars[prior_start].close)
    prior_return = (
        float(bars[left.index].close) / prior_start_close - 1
        if prior_start_close > 0 and left.index > prior_start
        else 0.0
    )
    score += max(0.0, min(15.0, prior_return * 75))
    if prior_return > 0:
        reasons.append(f"컵 형성 전 상승률 {prior_return:.1%}")

    handle_limit = min(0.12, cup_depth / 3)
    score += max(0.0, 1 - handle_depth / max(handle_limit, 0.001)) * 12
    if handle_volume_ratio <= 0.85:
        score += 8
        reasons.append(f"핸들 거래량 감소 {handle_volume_ratio:.2f}배")
    elif handle_volume_ratio > 1.1:
        reasons.append(f"핸들 거래량 증가 주의 {handle_volume_ratio:.2f}배")
    if breakout_volume_ratio >= 1.4:
        score += min(15.0, 8 + (breakout_volume_ratio - 1.4) * 7)
        reasons.append(f"돌파 거래량 {breakout_volume_ratio:.2f}배")
    elif state == CUP_HANDLE_BREAKOUT_PENDING:
        reasons.append(f"돌파 거래량 부족 {breakout_volume_ratio:.2f}배")
    if state == CUP_HANDLE_CONFIRMED:
        score += 5
        reasons.append("피벗을 종가와 거래량으로 돌파")
    elif state == CUP_HANDLE_READY:
        reasons.append("피벗 3% 이내에서 돌파 대기")
    elif state == CUP_HANDLE_FAILED:
        reasons.append("핸들 저점 이탈로 패턴 무효")

    stop_buffer = max(pivot_price * 0.003, (atr or 0.0) * 0.3)
    stop_price = max(0.0, handle_low - stop_buffer)
    target_price = pivot_price + cup_range
    return CupHandleDetection(
        state=state,
        score=round(min(100.0, score), 2),
        left_rim_index=left.index + offset,
        bottom_index=bottom.index + offset,
        right_rim_index=right.index + offset,
        left_rim_price=round(left.price, 4),
        bottom_price=round(bottom.price, 4),
        right_rim_price=round(right.price, 4),
        handle_low=round(handle_low, 4),
        pivot_price=round(pivot_price, 4),
        stop_price=round(stop_price, 4),
        target_price=round(target_price, 4),
        cup_depth=round(cup_depth, 6),
        rim_difference=round(rim_difference, 6),
        handle_depth=round(handle_depth, 6),
        handle_volume_ratio=round(handle_volume_ratio, 6),
        breakout_volume_ratio=round(breakout_volume_ratio, 6),
        reasons=tuple(reasons),
    )


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
