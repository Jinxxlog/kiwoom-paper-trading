import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from ai_shadow.models import AiAnalysis, ProviderResult
from ai_shadow.providers import OpenAiProvider
from ai_shadow.service import AiShadowService
from config.settings import AiShadowSettings
from db.database import Database


class FakeProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def analyze(self, payload):
        if self.fail:
            raise TimeoutError("provider timeout")
        return ProviderResult(
            analysis=AiAnalysis(
                action="buy_candidate",
                score=78,
                confidence=0.82,
                summary="상승 구조는 유효하지만 뉴스 근거는 제한적이다.",
                risks=("변동성 확대",),
                catalysts=("상승 추세 지속",),
            ),
            response_id="resp_test",
            input_tokens=500,
            output_tokens=100,
            raw_response={"id": "resp_test"},
        )


class AiShadowServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.settings = AiShadowSettings(
            enabled=True,
            provider="openai",
            model="gpt-test",
            prompt_version="1.0.0",
            candidate_limit=3,
            interval_minutes=60,
            request_timeout=10,
            max_output_tokens=700,
            openai_api_key="test-key",
            anthropic_api_key="",
            input_cost_per_million=Decimal("2"),
            output_cost_per_million=Decimal("8"),
        )
        self._save_candidate()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_structured_analysis_is_saved_with_usage_and_agreement(self) -> None:
        service = AiShadowService(
            self.database,
            self.settings,
            provider=FakeProvider(),
        )

        result = service.analyze_latest()
        saved = service.recent()[0]

        self.assertEqual(result, {"requested": 1, "success": 1, "failed": 0})
        self.assertEqual(saved["status"], "success")
        self.assertEqual(saved["agreement"], "agree")
        self.assertEqual(saved["input_tokens"], 500)
        self.assertAlmostEqual(saved["estimated_cost"], 0.0018)
        self.assertEqual(saved["risks"], ["변동성 확대"])

    def test_provider_failure_is_recorded_and_does_not_escape(self) -> None:
        service = AiShadowService(
            self.database,
            self.settings,
            provider=FakeProvider(fail=True),
        )

        result = service.analyze_latest()
        saved = service.recent()[0]

        self.assertEqual(result, {"requested": 1, "success": 0, "failed": 1})
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error_type"], "TimeoutError")

    def test_malformed_structured_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "스키마"):
            AiAnalysis.from_dict(
                {
                    "action": "observe",
                    "score": 50,
                    "confidence": 0.5,
                    "summary": "요약",
                    "risks": [],
                }
            )

    @patch("ai_shadow.providers.requests.post")
    def test_openai_provider_uses_strict_json_schema(self, post: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "id": "resp_1",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"action":"observe","score":60,'
                                '"confidence":0.7,"summary":"관찰",'
                                '"risks":[],"catalysts":[]}'
                            ),
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        post.return_value = response
        provider = OpenAiProvider(
            "key",
            "gpt-test",
            timeout=10,
            max_output_tokens=700,
        )

        result = provider.analyze({"stock": {"code": "A005930"}})

        body = post.call_args.kwargs["json"]
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["store"])
        self.assertEqual(result.analysis.action, "observe")
        self.assertEqual(result.input_tokens, 10)

    def test_forward_performance_is_evaluated_when_bars_exist(self) -> None:
        service = AiShadowService(
            self.database,
            self.settings,
            provider=FakeProvider(),
        )
        service.analyze_latest()
        start = datetime(2026, 6, 15, 19, 17, 30)
        candles = []
        for index in range(6):
            close = 10_000 + index * 100
            candles.append(
                {
                    "candle_time": (
                        start + timedelta(days=index)
                    ).isoformat(timespec="seconds"),
                    "open_price": close,
                    "high_price": close,
                    "low_price": close,
                    "close_price": close,
                    "volume": 100,
                    "trade_amount": close * 100,
                }
            )
        self.database.save_candles(
            "test",
            "A005930",
            {"test": True},
            candles,
            candle_type="daily",
        )

        result = service.evaluate_performance(horizon_days=5)
        saved = service.recent()[0]

        self.assertEqual(result, {"evaluated": 1, "pending": 0})
        self.assertEqual(saved["performance_status"], "correct")
        self.assertGreater(saved["forward_return"], 0)

    def _save_candidate(self) -> None:
        timestamp = "2026-06-15T19:17:30"
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO stock_universe(
                    stock_code, stock_name, market, sector,
                    source, updated_at
                )
                VALUES (
                    'A005930', '삼성전자', 'KOSPI', '반도체',
                    'test', ?
                )
                """,
                (timestamp,),
            )
            run_id = connection.execute(
                """
                INSERT INTO candidate_runs(
                    as_of, universe_count, eligible_count, selected_count,
                    config_json, created_at
                )
                VALUES (?, 1, 1, 1, '{}', ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO candidate_scores(
                    run_id, stock_code, rank, selected, total_score,
                    technical_score, pattern_score,
                    relative_strength_score, liquidity_score,
                    information_score, information_blocked,
                    information_item_count, composite_score,
                    composite_status, composite_confidence, watched,
                    patterns_json, features_json, reasons_json, evaluated_at
                )
                VALUES (
                    ?, 'A005930', 1, 1, 100, 35, 30, 20, 15,
                    0, 0, 0, 78, 'buy_candidate', 0.9, 0,
                    '["bull_alignment"]',
                    '{"close":70000,"rsi14":62,"return20":0.12}',
                    '["상승 추세"]', ?
                )
                """,
                (run_id, timestamp),
            )


if __name__ == "__main__":
    unittest.main()
