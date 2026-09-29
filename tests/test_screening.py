import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from config.settings import CandidateSettings
from db.database import Database
from screening.engine import CandidateScreeningEngine, StockUniverseService
from screening.features import (
    PATTERN_BOX_BREAKOUT,
    PATTERN_ASCENDING_TRIANGLE,
    PATTERN_BULL_FLAG,
    PATTERN_BULL_PENNANT,
    PATTERN_BULL_ALIGNMENT,
    PATTERN_DOUBLE_BOTTOM,
    PATTERN_DOUBLE_TOP,
    PATTERN_HIGHER_HIGHS_LOWS,
    PATTERN_HEAD_SHOULDERS,
    PATTERN_INVERSE_HEAD_SHOULDERS,
    PATTERN_CUP_HANDLE,
    PATTERN_VOLATILITY_CONTRACTION_BREAKOUT,
    DailyBar,
    calculate_features,
)
from screening.patterns import (
    ASCENDING_TRIANGLE_BREAKOUT_PENDING,
    ASCENDING_TRIANGLE_CONFIRMED,
    CONTINUATION_BREAKOUT_PENDING,
    CONTINUATION_CONFIRMED,
    CUP_HANDLE_BREAKOUT_PENDING,
    CUP_HANDLE_CONFIRMED,
    build_price_zones,
    detect_ascending_triangle,
    detect_bull_flag,
    detect_bull_pennant,
    detect_cup_handle,
    detect_double_bottom,
    detect_head_shoulders,
    detect_inverse_head_shoulders,
    find_pivots,
    fit_trendline,
)
from screening.performance import PatternPerformanceService


class TechnicalFeaturesTest(unittest.TestCase):
    def test_indicators_and_breakout_patterns_are_calculated(self) -> None:
        bars = self._bars("A005930", count=70, breakout=True)

        result = calculate_features(bars)

        self.assertIsNotNone(result.values["ma5"])
        self.assertIsNotNone(result.values["ema12"])
        self.assertIsNotNone(result.values["rsi14"])
        self.assertIsNotNone(result.values["atr14"])
        self.assertIsNotNone(result.values["bollinger_upper"])
        self.assertIn(PATTERN_BULL_ALIGNMENT, result.patterns)
        self.assertGreater(result.technical_score, 0)
        self.assertGreater(result.pattern_score, 0)

    def test_trade_amount_unit_is_normalized_against_price_and_volume(self) -> None:
        bars = [
            DailyBar(
                candle_time=bar.candle_time,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                trade_amount=(bar.close * bar.volume) // 1_000_000,
            )
            for bar in self._bars("A005930", count=60)
        ]

        result = calculate_features(bars)

        self.assertGreater(result.values["average_trade_amount20"], 1_000_000_000)

    def test_daily_setup_features_capture_breakout_resistance_and_prior_day(self) -> None:
        bars = self._bars("A005930", count=70)
        bars = self._replace_bar(
            bars,
            -2,
            high=10_500,
            low=10_100,
            close=10_200,
            volume=300_000,
        )
        bars = self._replace_bar(
            bars,
            -1,
            open=10_650,
            high=11_700,
            low=10_500,
            close=11_550,
            volume=700_000,
        )

        result = calculate_features(bars)

        self.assertTrue(result.values["recent5_high_breakout"])
        self.assertEqual(result.values["previous_high"], 10500)
        self.assertEqual(result.values["previous_low"], 10100)
        self.assertGreater(result.values["previous_trade_amount"], 0)
        self.assertGreater(result.values["relative_trade_amount20"], 1)
        self.assertTrue(result.values["healthy_gap"])
        self.assertIn("최근 5일 고점 돌파", result.reasons)

    def test_daily_setup_marks_near_resistance_without_breakout(self) -> None:
        bars = self._bars("A005930", count=70)
        bars = self._replace_bar(bars, -15, high=10_900, close=10_300)
        bars = self._replace_bar(bars, -9, high=10_880, close=10_250)
        bars = self._replace_bar(
            bars,
            -1,
            open=10_400,
            high=10_560,
            low=10_300,
            close=10_540,
        )

        result = calculate_features(bars)

        self.assertTrue(result.values["near_resistance"])
        self.assertFalse(result.values["resistance_breakout"])
        self.assertIn("주요 저항선 근처", result.reasons)

    def test_double_bottom_and_double_top_are_detected(self) -> None:
        bottom = self._pivot_pattern_bars(
            [
                (0, 11_000),
                (10, 10_000),
                (20, 8_000),
                (28, 10_000),
                (38, 8_100),
                (45, 9_800),
                (46, 10_300),
            ]
        )
        top = self._pivot_pattern_bars(
            [
                (0, 8_000),
                (10, 9_000),
                (20, 11_000),
                (28, 9_000),
                (38, 10_900),
                (45, 9_200),
                (46, 8_700),
            ]
        )

        bottom_result = calculate_features(bottom)
        top_result = calculate_features(top)

        self.assertIn(PATTERN_DOUBLE_BOTTOM, bottom_result.patterns)
        self.assertIn(PATTERN_DOUBLE_TOP, top_result.patterns)
        self.assertGreater(bottom_result.values["double_bottom_target_price"], 10_000)
        self.assertLess(top_result.values["double_top_target_price"], 9_000)

    def test_box_breakout_requires_repeated_support_and_resistance(self) -> None:
        box = self._pivot_pattern_bars(
            [
                (0, 9_000),
                (10, 9_000),
                (15, 10_000),
                (20, 9_000),
                (25, 10_000),
                (30, 9_000),
                (35, 10_000),
                (40, 9_000),
                (45, 10_000),
                (50, 10_300),
            ]
        )
        simple_rise = self._bars("A005930", count=70, breakout=True)

        box_result = calculate_features(box)
        simple_result = calculate_features(simple_rise)

        self.assertIn(PATTERN_BOX_BREAKOUT, box_result.patterns)
        self.assertGreaterEqual(box_result.values["box_breakout_support_touches"], 2)
        self.assertGreaterEqual(
            box_result.values["box_breakout_resistance_touches"],
            2,
        )
        self.assertNotIn(PATTERN_BOX_BREAKOUT, simple_result.patterns)

    def test_head_shoulders_and_inverse_confirm_on_neckline_break(self) -> None:
        head_shoulders = self._pivot_pattern_bars(
            [
                (0, 8_000),
                (12, 10_000),
                (20, 11_000),
                (26, 9_500),
                (34, 12_500),
                (42, 9_600),
                (50, 11_100),
                (58, 9_400),
                (59, 9_100),
            ]
        )
        inverse = self._pivot_pattern_bars(
            [
                (0, 12_000),
                (12, 10_000),
                (20, 9_000),
                (26, 10_500),
                (34, 7_500),
                (42, 10_400),
                (50, 8_900),
                (58, 10_600),
                (59, 11_000),
            ]
        )

        head_result = calculate_features(head_shoulders)
        inverse_result = calculate_features(inverse)

        self.assertIn(PATTERN_HEAD_SHOULDERS, head_result.patterns)
        self.assertIn(PATTERN_INVERSE_HEAD_SHOULDERS, inverse_result.patterns)
        self.assertLess(
            head_result.values["head_shoulders_target_price"],
            head_result.values["head_shoulders_neckline_price"],
        )
        self.assertGreater(
            inverse_result.values["inverse_head_shoulders_target_price"],
            inverse_result.values["inverse_head_shoulders_neckline_price"],
        )
        self.assertIsNotNone(detect_head_shoulders(head_shoulders, atr=100))
        self.assertIsNotNone(
            detect_inverse_head_shoulders(inverse, atr=100)
        )

    def test_double_pattern_requires_two_real_pivots(self) -> None:
        bars = self._bars("A005930", count=60)
        bars = self._replace_bar(bars, 25, low=9_000)

        self.assertIsNone(detect_double_bottom(bars, atr=100))

    def test_higher_structure_and_volatility_contraction_are_detected(self) -> None:
        higher = self._bars("A005930", count=60)
        contraction = self._bars("A000660", count=60)
        for index in range(34, 55):
            contraction = self._replace_bar(
                contraction,
                index,
                high=10_500,
                low=9_500,
                close=10_000,
            )
        for index in range(55, 60):
            close = 10_100 if index < 59 else 10_600
            contraction = self._replace_bar(
                contraction,
                index,
                high=close + 20,
                low=close - 20,
                close=close,
            )

        higher_result = calculate_features(higher)
        contraction_result = calculate_features(contraction)

        self.assertIn(PATTERN_HIGHER_HIGHS_LOWS, higher_result.patterns)
        self.assertIn(
            PATTERN_VOLATILITY_CONTRACTION_BREAKOUT,
            contraction_result.patterns,
        )

    def test_cup_handle_requires_rounded_base_and_confirmed_volume(self) -> None:
        confirmed = self._cup_handle_bars(breakout_volume=500_000)
        low_volume = self._cup_handle_bars(breakout_volume=210_000)

        confirmed_result = calculate_features(confirmed)
        low_volume_result = calculate_features(low_volume)

        self.assertEqual(
            confirmed_result.values["cup_handle_state"],
            CUP_HANDLE_CONFIRMED,
        )
        self.assertIn(PATTERN_CUP_HANDLE, confirmed_result.patterns)
        self.assertGreaterEqual(confirmed_result.values["cup_handle_score"], 75)
        self.assertGreater(
            confirmed_result.values["cup_handle_target_price"],
            confirmed_result.values["cup_handle_pivot_price"],
        )
        self.assertLess(
            confirmed_result.values["cup_handle_stop_price"],
            confirmed_result.values["cup_handle_pivot_price"],
        )
        self.assertEqual(
            low_volume_result.values["cup_handle_state"],
            CUP_HANDLE_BREAKOUT_PENDING,
        )
        self.assertNotIn(PATTERN_CUP_HANDLE, low_volume_result.patterns)

    def test_v_shaped_rebound_is_not_a_cup_handle(self) -> None:
        bars = self._cup_handle_bars(breakout_volume=500_000)
        cup_start = 20
        cup_end = 60
        for index in range(cup_start, cup_end + 1):
            distance = abs(index - 40)
            close = 8_200 + distance * 90
            bars = self._replace_bar(
                bars,
                index,
                open=close - 30,
                high=close + 50,
                low=close - 50,
                close=close,
            )

        self.assertIsNone(detect_cup_handle(bars))

    def test_price_zones_and_trendline_are_reusable(self) -> None:
        bars = self._ascending_triangle_bars(breakout_volume=600_000)
        pivots = find_pivots(bars)
        resistance = build_price_zones(
            pivots,
            kind="high",
            reference_price=10_000,
            atr=300,
        )
        lows = [
            pivot
            for pivot in pivots
            if pivot.kind == "low" and pivot.index >= 20
        ]
        support = fit_trendline(lows, kind="low")

        self.assertGreaterEqual(resistance[0].touch_count, 4)
        self.assertAlmostEqual(resistance[0].level, 10_050)
        self.assertIsNotNone(support)
        assert support is not None
        self.assertGreater(support.slope, 0)
        self.assertGreaterEqual(support.fit_score, 0.95)

    def test_ascending_triangle_requires_rising_support_and_breakout_volume(
        self,
    ) -> None:
        confirmed = self._ascending_triangle_bars(breakout_volume=600_000)
        low_volume = self._ascending_triangle_bars(breakout_volume=180_000)
        flat_lows = self._ascending_triangle_bars(
            breakout_volume=600_000,
            rising_lows=False,
        )

        confirmed_result = calculate_features(confirmed)
        low_volume_result = calculate_features(low_volume)

        self.assertEqual(
            confirmed_result.values["ascending_triangle_state"],
            ASCENDING_TRIANGLE_CONFIRMED,
        )
        self.assertIn(PATTERN_ASCENDING_TRIANGLE, confirmed_result.patterns)
        self.assertGreaterEqual(
            confirmed_result.values["ascending_triangle_resistance_touches"],
            3,
        )
        self.assertGreater(
            confirmed_result.values["ascending_triangle_target_price"],
            confirmed_result.values["ascending_triangle_resistance_price"],
        )
        self.assertEqual(
            low_volume_result.values["ascending_triangle_state"],
            ASCENDING_TRIANGLE_BREAKOUT_PENDING,
        )
        self.assertNotIn(PATTERN_ASCENDING_TRIANGLE, low_volume_result.patterns)
        self.assertIsNone(detect_ascending_triangle(flat_lows, atr=300))

    def test_bull_flag_requires_parallel_pullback_and_breakout_volume(
        self,
    ) -> None:
        confirmed = self._continuation_bars(
            pattern="flag",
            breakout_volume=600_000,
        )
        low_volume = self._continuation_bars(
            pattern="flag",
            breakout_volume=150_000,
        )

        confirmed_result = calculate_features(confirmed)
        low_volume_result = calculate_features(low_volume)

        self.assertEqual(
            confirmed_result.values["bull_flag_state"],
            CONTINUATION_CONFIRMED,
        )
        self.assertIn(PATTERN_BULL_FLAG, confirmed_result.patterns)
        self.assertIsNone(detect_bull_pennant(confirmed, atr=250))
        self.assertEqual(
            low_volume_result.values["bull_flag_state"],
            CONTINUATION_BREAKOUT_PENDING,
        )
        self.assertNotIn(PATTERN_BULL_FLAG, low_volume_result.patterns)

    def test_bull_pennant_requires_convergence_and_real_flagpole(self) -> None:
        confirmed = self._continuation_bars(
            pattern="pennant",
            breakout_volume=600_000,
        )
        weak_pole = self._continuation_bars(
            pattern="pennant",
            breakout_volume=600_000,
            pole_gain=0.10,
        )

        result = calculate_features(confirmed)

        self.assertEqual(
            result.values["bull_pennant_state"],
            CONTINUATION_CONFIRMED,
        )
        self.assertIn(PATTERN_BULL_PENNANT, result.patterns)
        self.assertIsNone(detect_bull_flag(confirmed, atr=250))
        self.assertIsNone(detect_bull_pennant(weak_pole, atr=250))

    @staticmethod
    def _bars(
        stock_code: str,
        *,
        count: int,
        breakout: bool = False,
    ) -> list[DailyBar]:
        del stock_code
        start = date(2026, 1, 1)
        bars = []
        for index in range(count):
            close = 10_000 + index * 20
            volume = 200_000
            if breakout and index == count - 1:
                close += 1_000
                volume = 500_000
            bars.append(
                DailyBar(
                    candle_time=datetime.combine(
                        start + timedelta(days=index),
                        datetime.min.time(),
                    ).isoformat(),
                    open=close - 20,
                    high=close + 50,
                    low=close - 50,
                    close=close,
                    volume=volume,
                )
            )
        return bars

    @staticmethod
    def _replace_bar(
        bars: list[DailyBar],
        index: int,
        **changes: int,
    ) -> list[DailyBar]:
        current = bars[index]
        values = {
            "candle_time": current.candle_time,
            "open": current.open,
            "high": current.high,
            "low": current.low,
            "close": current.close,
            "volume": current.volume,
            "trade_amount": current.trade_amount,
        }
        values.update(changes)
        copied = list(bars)
        copied[index] = DailyBar(**values)
        return copied

    @staticmethod
    def _cup_handle_bars(*, breakout_volume: int) -> list[DailyBar]:
        closes = [8_000 + index * 100 for index in range(20)]
        for index in range(41):
            position = index / 40
            depth = 1_800 * (1 - (2 * position - 1) ** 2)
            closes.append(round(10_000 - depth))
        closes.extend([9_850, 9_750, 9_700, 9_720, 9_760, 9_800, 9_840, 9_880])
        closes.append(10_200)

        start = date(2025, 1, 1)
        bars = []
        for index, close in enumerate(closes):
            volume = 200_000
            if 61 <= index < len(closes) - 1:
                volume = 130_000
            if index == len(closes) - 1:
                volume = breakout_volume
            bars.append(
                DailyBar(
                    candle_time=datetime.combine(
                        start + timedelta(days=index),
                        datetime.min.time(),
                    ).isoformat(),
                    open=close - 30,
                    high=close + 50,
                    low=close - 50,
                    close=close,
                    volume=volume,
                )
            )
        return bars

    @staticmethod
    def _ascending_triangle_bars(
        *,
        breakout_volume: int,
        rising_lows: bool = True,
    ) -> list[DailyBar]:
        points = [
            (0, 7_000),
            (15, 9_000),
            (20, 10_000),
            (24, 8_500),
            (30, 10_000),
            (34, 8_800 if rising_lows else 8_500),
            (40, 10_000),
            (44, 9_100 if rising_lows else 8_500),
            (50, 10_000),
            (54, 9_400 if rising_lows else 8_500),
            (58, 9_900),
            (59, 10_300),
        ]
        closes = []
        for (start, start_price), (end, end_price) in zip(points, points[1:]):
            for index in range(start, end):
                fraction = (index - start) / (end - start)
                closes.append(
                    round(start_price + (end_price - start_price) * fraction)
                )
        closes.append(points[-1][1])

        start = date(2025, 1, 1)
        bars = []
        for index, close in enumerate(closes):
            volume = 250_000 if index < 35 else 140_000
            if index == len(closes) - 1:
                volume = breakout_volume
            bars.append(
                DailyBar(
                    candle_time=datetime.combine(
                        start + timedelta(days=index),
                        datetime.min.time(),
                    ).isoformat(),
                    open=close - 30,
                    high=close + 50,
                    low=close - 50,
                    close=close,
                    volume=volume,
                )
            )
        return bars

    @staticmethod
    def _continuation_bars(
        *,
        pattern: str,
        breakout_volume: int,
        pole_gain: float = 0.27,
    ) -> list[DailyBar]:
        start = date(2025, 1, 1)
        bars = []
        for index in range(20):
            close = 8_000 + (index % 3) * 10
            bars.append(
                DailyBar(
                    candle_time=str(start + timedelta(days=index)),
                    open=close - 20,
                    high=close + 50,
                    low=close - 50,
                    close=close,
                    volume=120_000,
                )
            )
        pole_start = 8_200
        pole_end = round(pole_start * (1 + pole_gain))
        for index in range(10):
            close = round(
                pole_start + (pole_end - pole_start) * index / 9
            )
            bars.append(
                DailyBar(
                    candle_time=str(start + timedelta(days=20 + index)),
                    open=close - 40,
                    high=close + 80,
                    low=close - 80,
                    close=close,
                    volume=300_000,
                )
            )
        scale = pole_end / 10_400
        for index in range(10):
            high = round((10_500 - 40 * index) * scale)
            low = round(
                (
                    10_000 - 40 * index
                    if pattern == "flag"
                    else 9_500 + 20 * index
                )
                * scale
            )
            close = round((high + low) / 2)
            bars.append(
                DailyBar(
                    candle_time=str(start + timedelta(days=30 + index)),
                    open=close,
                    high=high,
                    low=low,
                    close=close,
                    volume=120_000,
                )
            )
        breakout_close = round(10_300 * scale)
        bars.append(
            DailyBar(
                candle_time=str(start + timedelta(days=40)),
                open=round(10_150 * scale),
                high=round(10_400 * scale),
                low=round(10_100 * scale),
                close=breakout_close,
                volume=breakout_volume,
            )
        )
        return bars

    @staticmethod
    def _pivot_pattern_bars(
        points: list[tuple[int, int]],
        *,
        breakout_volume: int = 600_000,
    ) -> list[DailyBar]:
        closes = []
        for (start, start_price), (end, end_price) in zip(points, points[1:]):
            for index in range(start, end):
                fraction = (index - start) / (end - start)
                closes.append(
                    round(start_price + (end_price - start_price) * fraction)
                )
        closes.append(points[-1][1])
        start_date = date(2025, 1, 1)
        return [
            DailyBar(
                candle_time=str(start_date + timedelta(days=index)),
                open=close,
                high=close + 50,
                low=close - 50,
                close=close,
                volume=(
                    breakout_volume if index == len(closes) - 1 else 200_000
                ),
            )
            for index, close in enumerate(closes)
        ]


class CandidateScreeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.settings = CandidateSettings(
            minimum_history_bars=60,
            minimum_price=100,
            minimum_average_volume=1,
            minimum_average_trade_amount=1,
            maximum_daily_volatility=1,
            maximum_order_book_spread_rate=0.02,
            selected_count=10,
        )
        self.engine = CandidateScreeningEngine(self.database, self.settings)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_non_watchlist_stock_is_selected_and_reproducible_in_db(self) -> None:
        self._save_bars("A000660", count=70, breakout=True)

        run = self.engine.scan(as_of=date(2026, 3, 11))

        candidate = next(row for row in run.results if row.stock_code == "A000660")
        self.assertTrue(candidate.selected)
        self.assertFalse(candidate.watched)
        with self.database.connect() as connection:
            saved = connection.execute(
                """
                SELECT s.*, r.as_of
                FROM candidate_scores s
                JOIN candidate_runs r ON r.id = s.run_id
                WHERE s.stock_code = 'A000660'
                """
            ).fetchone()
        self.assertEqual(saved["run_id"], run.run_id)
        self.assertEqual(saved["selected"], 1)
        self.assertAlmostEqual(saved["total_score"], candidate.total_score)
        self.assertIn("관심 종목 외 자동 검색", saved["reasons_json"])
        self.assertIsNotNone(saved["composite_decision_id"])
        self.assertIn(
            saved["composite_status"],
            {"buy_candidate", "observe", "excluded"},
        )
        with self.database.connect() as connection:
            decision = connection.execute(
                """
                SELECT d.*, v.strategy_name
                FROM strategy_decisions d
                JOIN strategy_versions v ON v.id = d.strategy_version_id
                WHERE d.id = ?
                """,
                (saved["composite_decision_id"],),
            ).fetchone()
            component_count = connection.execute(
                "SELECT COUNT(*) FROM score_components WHERE decision_id = ?",
                (saved["composite_decision_id"],),
            ).fetchone()[0]
        self.assertEqual(decision["strategy_name"], "composite")
        self.assertEqual(decision["outcome"], "observe")
        self.assertEqual(component_count, 6)

    def test_market_product_universe_stock_is_excluded_from_selection(self) -> None:
        universe = StockUniverseService(self.database)
        universe.upsert("A122630", stock_name="KODEX 레버리지")
        self._save_bars("A122630", count=70, breakout=True)
        self._save_bars("A000660", count=70, breakout=True)

        run = self.engine.scan(as_of=date(2026, 3, 11))
        by_code = {row.stock_code: row for row in run.results}

        self.assertEqual(
            by_code["A122630"].excluded_reason,
            "market_product_excluded",
        )
        self.assertFalse(by_code["A122630"].selected)
        self.assertTrue(by_code["A000660"].selected)

    def test_as_of_date_prevents_future_candle_reference(self) -> None:
        self._save_bars("A005930", count=70)
        as_of = date(2026, 3, 11)
        first = self.engine.scan(as_of=as_of)
        first_row = first.results[0]
        self._save_single_bar("A005930", date(2026, 3, 12), close=99_000)

        second = self.engine.scan(as_of=as_of)
        second_row = second.results[0]

        self.assertEqual(first_row.features["close"], second_row.features["close"])
        self.assertEqual(first_row.total_score, second_row.total_score)

    def test_blocking_disclosure_excludes_candidate_and_saves_score(self) -> None:
        self._save_bars("A005930", count=70)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO information_items(
                    source, source_item_id, stock_code, title,
                    normalized_title, logical_key, published_at, available_at,
                    first_collected_at, last_collected_at, event_type,
                    sentiment, base_score, importance, confidence,
                    entry_blocked, raw_json
                )
                VALUES (
                    'dart', 'test-block', 'A005930', '유상증자결정',
                    '유상증자결정', 'A005930:유상증자결정',
                    '2026-03-10T23:59:59+09:00',
                    '2026-03-10T23:59:59+09:00',
                    '2026-03-11T00:00:00+09:00',
                    '2026-03-11T00:00:00+09:00',
                    'rights_offering', 'negative', -60, 0.9, 0.95, 1, '{}'
                )
                """
            )

        run = self.engine.scan(as_of=date(2026, 3, 11))
        candidate = next(row for row in run.results if row.stock_code == "A005930")

        self.assertEqual(candidate.excluded_reason, "뉴스·공시 진입 차단")
        self.assertTrue(candidate.information_blocked)
        self.assertLess(candidate.information_score, 0)
        with self.database.connect() as connection:
            saved = connection.execute(
                "SELECT information_score, information_blocked "
                "FROM candidate_scores WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
        self.assertEqual(saved["information_blocked"], 1)
        self.assertLess(saved["information_score"], 0)

    def test_positive_news_score_is_not_used_as_candidate_bonus(self) -> None:
        self._save_bars("A005930", count=70)
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO information_items(
                    source, source_item_id, stock_code, title,
                    normalized_title, logical_key, published_at, available_at,
                    first_collected_at, last_collected_at, event_type,
                    sentiment, base_score, importance, confidence,
                    entry_blocked, raw_json
                )
                VALUES (
                    'naver', 'test-positive', 'A005930', '공급계약 체결',
                    '공급계약 체결', 'A005930:공급계약 체결',
                    '2026-03-10T23:59:59+09:00',
                    '2026-03-10T23:59:59+09:00',
                    '2026-03-11T00:00:00+09:00',
                    '2026-03-11T00:00:00+09:00',
                    'supply_contract', 'positive', 40, 0.9, 0.95, 0, '{}'
                )
                """
            )

        run = self.engine.scan(as_of=date(2026, 3, 11))
        candidate = next(row for row in run.results if row.stock_code == "A005930")

        self.assertEqual(candidate.information_score, 0)
        self.assertIn("information_positive_score_ignored", candidate.features)
        with self.database.connect() as connection:
            saved = connection.execute(
                "SELECT information_score, information_blocked, features_json "
                "FROM candidate_scores WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
        self.assertEqual(saved["information_score"], 0)
        self.assertEqual(saved["information_blocked"], 0)
        self.assertIn("information_positive_score_ignored", saved["features_json"])

    def test_management_and_order_book_gap_are_excluded(self) -> None:
        universe = StockUniverseService(self.database)
        universe.upsert("A005930", management=True)
        universe.upsert("A000660")
        self._save_bars("A005930", count=70)
        self._save_bars("A000660", count=70)
        self._save_order_book_gap("A000660")

        run = self.engine.scan(as_of=date(2026, 3, 11))
        by_code = {row.stock_code: row for row in run.results}

        self.assertEqual(by_code["A005930"].excluded_reason, "관리 종목")
        self.assertEqual(by_code["A000660"].excluded_reason, "호가 공백")

    def test_overheated_daily_gap_is_excluded(self) -> None:
        self._save_bars("A005930", count=70)
        with self.database.connect() as connection:
            last = connection.execute(
                """
                SELECT candle_time
                FROM candles
                WHERE stock_code = 'A005930'
                ORDER BY candle_time DESC
                LIMIT 1
                """
            ).fetchone()["candle_time"]
            connection.execute(
                """
                UPDATE candles
                SET open_price = 14000,
                    high_price = 14500,
                    low_price = 13800,
                    close_price = 14400
                WHERE stock_code = 'A005930'
                  AND candle_time = ?
                """,
                (last,),
            )

        run = self.engine.scan(as_of=date(2026, 3, 11))
        candidate = next(row for row in run.results if row.stock_code == "A005930")

        self.assertEqual(candidate.excluded_reason, "과열 갭 상승")

    def test_trading_status_flags_are_excluded(self) -> None:
        universe = StockUniverseService(self.database)
        cases = {
            "A000001": ({"tradable": False}, "거래 불가 종목"),
            "A000002": ({"halted": True}, "거래 정지 종목"),
            "A000003": ({"overheated": True}, "단기 과열 종목"),
        }
        for code, (flags, _) in cases.items():
            universe.upsert(code, **flags)
            self._save_bars(code, count=70)

        run = self.engine.scan(as_of=date(2026, 3, 11))
        by_code = {row.stock_code: row for row in run.results}

        for code, (_, expected) in cases.items():
            self.assertEqual(by_code[code].excluded_reason, expected)

    def test_score_change_is_saved_between_runs(self) -> None:
        self._save_bars("A005930", count=70)
        self.engine.scan(as_of=date(2026, 3, 11))
        self._save_single_bar("A005930", date(2026, 3, 12), close=12_500)
        self.engine.scan(as_of=date(2026, 3, 12))

        latest = self.engine.latest()[0]

        self.assertIsNotNone(latest["score_change"])

    def test_high_trade_amount_adds_day_trading_liquidity_reason(self) -> None:
        self._save_bars("A123456", count=70, volume=6_000_000)

        run = self.engine.scan(as_of=date(2026, 3, 11))
        candidate = next(row for row in run.results if row.stock_code == "A123456")

        self.assertEqual(candidate.liquidity_score, 15)
        self.assertIn("단타 유동성 기준 500억원 이상", candidate.reasons)

    def test_theme_context_adds_selection_score_and_reason(self) -> None:
        self._save_bars("A005930", count=70, volume=100_000)
        collected_at = datetime(2026, 3, 11, 9, 10)
        self.database.save_theme_groups(
            {"test": True},
            [
                {
                    "theme_code": "101",
                    "theme_name": "AI",
                    "stock_count": 20,
                    "change_rate": 5.0,
                    "rising_stock_count": 16,
                    "falling_stock_count": 2,
                    "day_profit_rate": 4.0,
                    "main_stock": "삼성전자",
                }
            ],
            collected_at=collected_at,
        )
        self.database.save_theme_components(
            "101",
            {"test": True},
            [
                {
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "current_price": 10_690,
                    "change_rate": 6.0,
                    "accumulated_volume": 2_000_000,
                }
            ],
            collected_at=collected_at,
        )

        run = self.engine.scan(as_of=datetime(2026, 3, 11, 10, 0))
        candidate = next(row for row in run.results if row.stock_code == "A005930")

        self.assertEqual(candidate.features["theme_score"], 12.0)
        self.assertEqual(candidate.features["theme"]["theme_name"], "AI")
        self.assertIn("테마 1위 AI +12.00점", candidate.reasons)
        self.assertIn("leader_theme_score", candidate.features)
        self.assertGreaterEqual(candidate.features["leader_theme_score"], 0)

    def test_trade_amount_rank_is_main_candidate_signal(self) -> None:
        self._save_bars("A005930", count=70, breakout=True, volume=300_000)
        self._save_bars("A000660", count=70, breakout=True, volume=300_000)
        self.database.save_market_rank_snapshot(
            rank_type="trade_amount",
            api_id="ka10032",
            market_type="000",
            query_params={"mrkt_tp": "000"},
            payload={"test": True},
            observed_at="2026-03-11T09:30:00",
            collected_at=datetime(2026, 3, 11, 9, 30),
            items=[
                {
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "rank": 1,
                    "trade_amount": 90_000_000_000,
                }
            ],
        )

        run = self.engine.scan(as_of=datetime(2026, 3, 11, 10, 0), limit=1)
        by_code = {row.stock_code: row for row in run.results}

        self.assertTrue(by_code["A005930"].selected)
        self.assertEqual(by_code["A005930"].rank, 1)
        self.assertEqual(by_code["A005930"].features["market_rank_score"], 35.0)
        self.assertEqual(
            by_code["A005930"].features["market_rank"]["trade_amount"]["rank"],
            1,
        )
        self.assertFalse(by_code["A000660"].selected)

    def test_leader_stock_score_gets_selection_priority(self) -> None:
        self._save_bars("A005930", count=70, breakout=True, volume=300_000)
        self._save_bars("A000660", count=70, breakout=True, volume=300_000)
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": "2026-03-11",
                    "stock_code": "A000660",
                    "stock_name": "leader",
                    "theme_code": "101",
                    "theme_name": "AI",
                    "role": "leader",
                    "total_score": 92.0,
                    "rank_score": 24.0,
                    "trade_amount_score": 35.0,
                    "theme_score": 18.0,
                    "persistence_score": 10.0,
                    "chart_score": 5.0,
                    "risk_score": 0.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["test leader"],
                    "components": {"test": True},
                }
            ],
            scored_at=datetime(2026, 3, 11, 9, 20),
        )

        run = self.engine.scan(as_of=datetime(2026, 3, 11, 10, 0), limit=1)
        by_code = {row.stock_code: row for row in run.results}

        self.assertTrue(by_code["A000660"].selected)
        self.assertEqual(by_code["A000660"].rank, 1)
        self.assertFalse(by_code["A005930"].selected)
        self.assertEqual(by_code["A000660"].features["leader_score_bonus"], 35.0)
        self.assertEqual(
            by_code["A000660"].features["leader_stock_score"]["role"],
            "leader",
        )

    def test_non_leader_chart_candidate_can_still_be_selected(self) -> None:
        self._save_bars("A005930", count=70, breakout=True, volume=300_000)
        self._save_bars("A000660", count=70, breakout=True, volume=300_000)
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": "2026-03-11",
                    "stock_code": "A000660",
                    "stock_name": "leader",
                    "theme_code": "101",
                    "theme_name": "AI",
                    "role": "leader",
                    "total_score": 92.0,
                    "rank_score": 24.0,
                    "trade_amount_score": 35.0,
                    "theme_score": 18.0,
                    "persistence_score": 10.0,
                    "chart_score": 5.0,
                    "risk_score": 0.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["test leader"],
                    "components": {"test": True},
                }
            ],
            scored_at=datetime(2026, 3, 11, 9, 20),
        )

        run = self.engine.scan(as_of=datetime(2026, 3, 11, 10, 0), limit=2)
        by_code = {row.stock_code: row for row in run.results}

        self.assertTrue(by_code["A000660"].selected)
        self.assertTrue(by_code["A005930"].selected)
        self.assertNotIn("leader_stock_score", by_code["A005930"].features)

    def test_pattern_performance_is_saved(self) -> None:
        self._save_bars("A005930", count=90, breakout=True)

        result = PatternPerformanceService(
            self.database,
            minimum_history=60,
        ).run(
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 20),
            horizon_days=5,
        )

        self.assertEqual(len(result["results"]), 13)
        with self.database.connect() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM pattern_performance_results
                WHERE run_id = ?
                """,
                (result["run_id"],),
            ).fetchone()[0]
        self.assertEqual(count, 13)

    def _save_bars(
        self,
        stock_code: str,
        *,
        count: int,
        breakout: bool = False,
        volume: int = 200_000,
    ) -> None:
        start = date(2026, 1, 1)
        candles = []
        for index in range(count):
            close = 10_000 + index * 10
            bar_volume = volume
            if breakout and index == count - 1:
                close += 1_000
                bar_volume = max(volume, 500_000)
            candles.append(
                {
                    "candle_time": datetime.combine(
                        start + timedelta(days=index),
                        datetime.min.time(),
                    ).isoformat(),
                    "open_price": close - 10,
                    "high_price": close + 30,
                    "low_price": close - 30,
                    "close_price": close,
                    "volume": bar_volume,
                    "trade_amount": close * bar_volume,
                }
            )
        self.database.save_candles(
            "test",
            stock_code,
            {"test": True},
            candles,
            candle_type="daily",
        )

    def _save_single_bar(
        self,
        stock_code: str,
        candle_date: date,
        *,
        close: int,
    ) -> None:
        self.database.save_candles(
            "test",
            stock_code,
            {"future": True},
            [
                {
                    "candle_time": datetime.combine(
                        candle_date,
                        datetime.min.time(),
                    ).isoformat(),
                    "open_price": close,
                    "high_price": close + 100,
                    "low_price": close - 100,
                    "close_price": close,
                    "volume": 500_000,
                    "trade_amount": close * 500_000,
                }
            ],
            candle_type="daily",
        )

    def _save_order_book_gap(self, stock_code: str) -> None:
        self.database.save_order_book(
            stock_code,
            {"test": True},
            [],
            quote_time="20260311150000",
            best_ask_price=None,
            best_ask_quantity=None,
            best_bid_price=None,
            best_bid_quantity=None,
            total_ask_quantity=0,
            total_bid_quantity=0,
            collected_at=datetime(2026, 3, 11, 15, 0),
        )


if __name__ == "__main__":
    unittest.main()
