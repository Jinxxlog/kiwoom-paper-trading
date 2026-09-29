import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from db.database import Database
from screening.intraday import (
    IntradayBar,
    IntradayVwapService,
    calculate_session_vwap,
    evaluate_vwap_patterns,
)


def bar(
    time: str,
    close: float,
    *,
    low: float | None = None,
    high: float | None = None,
    volume: float = 100,
) -> IntradayBar:
    return IntradayBar(
        candle_time=time,
        open=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        close=close,
        volume=volume,
    )


def bullish_fvg_bars() -> list[IntradayBar]:
    return [
        IntradayBar("2026-06-15T09:00:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:05:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:10:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:15:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:20:00", 100, 106, 99.5, 105.5, 160),
        IntradayBar("2026-06-15T09:25:00", 105, 107, 103, 106, 120),
    ]


def bullish_order_block_bars() -> list[IntradayBar]:
    return [
        IntradayBar("2026-06-15T09:00:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:05:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:10:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:15:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:20:00", 100, 101, 99, 100, 100),
        IntradayBar("2026-06-15T09:25:00", 100.5, 101, 99, 99.5, 100),
        IntradayBar("2026-06-15T09:30:00", 99.5, 102, 99.5, 101.5, 150),
        IntradayBar("2026-06-15T09:35:00", 101.5, 104, 101, 103, 160),
    ]


class IntradayVwapTest(unittest.TestCase):
    def test_session_vwap_does_not_mix_another_date(self) -> None:
        points = calculate_session_vwap(
            [
                bar("2026-06-15T09:00:00", 100),
                bar("2026-06-15T09:05:00", 110),
                bar("2026-06-16T09:00:00", 500),
            ]
        )

        self.assertEqual(len(points), 2)
        self.assertAlmostEqual(points[-1].vwap, 105)

    def test_reclaim_is_confirmed_with_volume_expansion(self) -> None:
        bars = [
            bar(f"2026-06-15T09:{index * 5:02d}:00", close, volume=100)
            for index, close in enumerate([100, 99, 98, 98, 99, 99, 99])
        ]
        bars.append(
            bar(
                "2026-06-15T09:35:00",
                101,
                low=98,
                high=102,
                volume=150,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.reclaim_state, "confirmed")
        self.assertGreaterEqual(result.reclaim_score, 70)

    def test_reclaim_waits_when_volume_is_weak(self) -> None:
        bars = [
            bar(f"2026-06-15T09:{index * 5:02d}:00", close, volume=100)
            for index, close in enumerate([100, 99, 98, 98, 99, 99, 99])
        ]
        bars.append(
            bar(
                "2026-06-15T09:35:00",
                101,
                low=98,
                high=102,
                volume=80,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.reclaim_state, "ready")

    def test_bounce_is_confirmed_after_vwap_support(self) -> None:
        bars = [
            bar(f"2026-06-15T09:{index * 5:02d}:00", close, volume=100)
            for index, close in enumerate([100, 102, 103, 104, 104, 105, 105])
        ]
        bars.append(
            bar(
                "2026-06-15T09:35:00",
                104.5,
                low=103,
                high=105,
                volume=120,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.bounce_state, "confirmed")

    def test_bullish_liquidity_sweep_requires_close_reclaim(self) -> None:
        bars = [
            bar(
                f"2026-06-15T09:{index * 5:02d}:00",
                101 + index * 0.1,
                low=100,
                high=103,
                volume=100,
            )
            for index in range(9)
        ]
        bars.append(
            IntradayBar(
                candle_time="2026-06-15T09:45:00",
                open=100.5,
                high=102,
                low=99,
                close=101.5,
                volume=150,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.liquidity_sweep_state, "confirmed")
        self.assertEqual(result.liquidity_sweep_side, "bullish")
        self.assertEqual(result.liquidity_level, 100)

    def test_bearish_liquidity_sweep_is_warning(self) -> None:
        bars = [
            bar(
                f"2026-06-15T09:{index * 5:02d}:00",
                101 - index * 0.1,
                low=98,
                high=102,
                volume=100,
            )
            for index in range(9)
        ]
        bars.append(
            IntradayBar(
                candle_time="2026-06-15T09:45:00",
                open=101.5,
                high=103,
                low=99,
                close=100,
                volume=160,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.liquidity_sweep_state, "confirmed")
        self.assertEqual(result.liquidity_sweep_side, "bearish")

    def test_liquidity_break_without_reclaim_is_failed(self) -> None:
        bars = [
            bar(
                f"2026-06-15T09:{index * 5:02d}:00",
                101,
                low=100,
                high=103,
                volume=100,
            )
            for index in range(9)
        ]
        bars.append(
            IntradayBar(
                candle_time="2026-06-15T09:45:00",
                open=100,
                high=101,
                low=98.5,
                close=99,
                volume=160,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.liquidity_sweep_state, "failed")
        self.assertEqual(result.liquidity_sweep_side, "bullish")

    def test_bullish_fvg_is_forming_before_mitigation(self) -> None:
        result = evaluate_vwap_patterns("A005930", bullish_fvg_bars())

        self.assertIsNotNone(result)
        self.assertEqual(result.fvg_state, "forming")
        self.assertEqual(result.fvg_side, "bullish")
        self.assertEqual(result.fvg_lower, 101)
        self.assertEqual(result.fvg_upper, 103)
        self.assertEqual(result.fvg_fill_rate, 0)

    def test_bullish_fvg_confirms_after_partial_fill_and_reaction(self) -> None:
        bars = bullish_fvg_bars()
        bars.append(
            IntradayBar(
                "2026-06-15T09:30:00",
                102.5,
                104.5,
                102,
                104,
                130,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.fvg_state, "confirmed")
        self.assertEqual(result.fvg_side, "bullish")
        self.assertAlmostEqual(result.fvg_fill_rate, 0.5)

    def test_bullish_fvg_fails_after_full_fill(self) -> None:
        bars = bullish_fvg_bars()
        bars.append(
            IntradayBar(
                "2026-06-15T09:30:00",
                102,
                103,
                100,
                100.5,
                140,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.fvg_state, "failed")
        self.assertEqual(result.fvg_fill_rate, 1)

    def test_bearish_fvg_is_detected(self) -> None:
        bars = [
            IntradayBar("2026-06-15T09:00:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:05:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:10:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:15:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:20:00", 100, 100.5, 94, 94.5, 160),
            IntradayBar("2026-06-15T09:25:00", 95, 97, 93, 94, 120),
        ]

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.fvg_state, "forming")
        self.assertEqual(result.fvg_side, "bearish")
        self.assertEqual(result.fvg_lower, 97)
        self.assertEqual(result.fvg_upper, 99)

    def test_fvg_rejects_non_consecutive_bars(self) -> None:
        bars = bullish_fvg_bars()
        last = bars[-1]
        bars[-1] = IntradayBar(
            "2026-06-15T09:30:00",
            last.open,
            last.high,
            last.low,
            last.close,
            last.volume,
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.fvg_state, "forming")
        self.assertEqual(result.fvg_side, "none")

    def test_bullish_order_block_is_forming_before_retest(self) -> None:
        result = evaluate_vwap_patterns(
            "A005930",
            bullish_order_block_bars(),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.order_block_state, "forming")
        self.assertEqual(result.order_block_side, "bullish")
        self.assertEqual(result.order_block_lower, 99)
        self.assertEqual(result.order_block_upper, 100.5)

    def test_bullish_order_block_confirms_after_retest(self) -> None:
        bars = bullish_order_block_bars()
        bars.append(
            IntradayBar(
                "2026-06-15T09:40:00",
                100,
                102,
                99.8,
                101.5,
                130,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.order_block_state, "confirmed")
        self.assertEqual(result.order_block_side, "bullish")

    def test_bullish_order_block_fails_on_close_below_zone(self) -> None:
        bars = bullish_order_block_bars()
        bars.append(
            IntradayBar(
                "2026-06-15T09:40:00",
                100,
                100.5,
                98,
                98.5,
                140,
            )
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.order_block_state, "failed")
        self.assertEqual(result.order_block_score, 0)

    def test_bearish_order_block_is_detected(self) -> None:
        bars = [
            IntradayBar("2026-06-15T09:00:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:05:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:10:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:15:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:20:00", 100, 101, 99, 100, 100),
            IntradayBar("2026-06-15T09:25:00", 99.5, 101, 99, 100.5, 100),
            IntradayBar("2026-06-15T09:30:00", 100.5, 100.5, 98, 98.5, 150),
            IntradayBar("2026-06-15T09:35:00", 98.5, 99, 96, 97, 160),
        ]

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.order_block_state, "forming")
        self.assertEqual(result.order_block_side, "bearish")
        self.assertEqual(result.order_block_lower, 99.5)
        self.assertEqual(result.order_block_upper, 101)

    def test_order_block_rejects_non_consecutive_displacement(self) -> None:
        bars = bullish_order_block_bars()
        last = bars[-1]
        bars[-1] = IntradayBar(
            "2026-06-15T09:40:00",
            last.open,
            last.high,
            last.low,
            last.close,
            last.volume,
        )

        result = evaluate_vwap_patterns("A005930", bars)

        self.assertIsNotNone(result)
        self.assertEqual(result.order_block_state, "forming")
        self.assertEqual(result.order_block_side, "none")


class IntradayVwapServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _save(self, candle_time: str, close: int, volume: int = 100) -> None:
        self.database.save_candles(
            "test",
            "A005930",
            {"ok": True},
            [
                {
                    "candle_time": candle_time,
                    "open_price": close,
                    "high_price": close + 1,
                    "low_price": close - 1,
                    "close_price": close,
                    "volume": volume,
                    "accumulated_volume": volume,
                    "trade_amount": close * volume,
                }
            ],
            candle_type="minute",
            interval_minutes=5,
        )

    def test_as_of_excludes_future_bars_and_result_is_saved(self) -> None:
        for index, close in enumerate([100, 99, 98, 98, 99, 99, 101]):
            self._save(
                f"2026-06-15T09:{index * 5:02d}:00",
                close,
                150 if index == 6 else 100,
            )
        self._save("2026-06-15T10:00:00", 200, 1000)
        service = IntradayVwapService(self.database)

        result = service.evaluate(
            "A005930",
            as_of=datetime(2026, 6, 15, 9, 30),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.source_data_time, "2026-06-15T09:30:00")
        self.assertLess(result.current_price, 200)
        latest = service.latest()
        self.assertEqual(
            latest["A005930"]["source_data_time"],
            result.source_data_time,
        )
        self.assertIn("liquidity_sweep_state", latest["A005930"])
        self.assertIn("fvg_state", latest["A005930"])
        self.assertIn("order_block_state", latest["A005930"])


if __name__ == "__main__":
    unittest.main()
