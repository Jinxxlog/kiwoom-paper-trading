from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from ai_shadow.providers import (
    ANALYSIS_SCHEMA,
    SYSTEM_INSTRUCTIONS,
    AiProvider,
    AnthropicProvider,
    OpenAiProvider,
)
from config.settings import AiShadowSettings
from data.market_calendar import SEOUL_TIMEZONE
from db.database import Database


class AiShadowService:
    def __init__(
        self,
        database: Database,
        settings: AiShadowSettings,
        *,
        provider: AiProvider | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.provider = provider or self._create_provider()

    def configured(self) -> bool:
        return bool(
            self.settings.openai_api_key
            if self.settings.provider == "openai"
            else self.settings.anthropic_api_key
        )

    def analyze_latest(self, *, limit: int | None = None) -> dict[str, int]:
        if not self.configured():
            raise RuntimeError(
                "OPENAI_API_KEY를 설정하세요."
                if self.settings.provider == "openai"
                else "ANTHROPIC_API_KEY를 설정하세요."
            )
        candidates = self._latest_candidates(
            limit or self.settings.candidate_limit
        )
        success = failed = 0
        for candidate in candidates:
            try:
                self._analyze_candidate(candidate)
                success += 1
            except Exception as exc:
                self._save_failure(candidate, exc)
                failed += 1
        return {
            "requested": len(candidates),
            "success": success,
            "failed": failed,
        }

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, u.stock_name
                FROM ai_shadow_analyses a
                LEFT JOIN stock_universe u ON u.stock_code = a.stock_code
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["risks"] = json.loads(item.pop("risks_json"))
            item["catalysts"] = json.loads(item.pop("catalysts_json"))
            result.append(item)
        return result

    def evaluate_performance(self, *, horizon_days: int = 5) -> dict[str, int]:
        if horizon_days < 1:
            raise ValueError("AI 성과 평가 기간은 1일 이상이어야 합니다.")
        evaluated = pending = 0
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, stock_code, source_data_time, ai_action
                FROM ai_shadow_analyses
                WHERE status = 'success'
                  AND (
                    evaluated_horizon_days IS NULL
                    OR evaluated_horizon_days != ?
                  )
                ORDER BY id
                """,
                (horizon_days,),
            ).fetchall()
            for row in rows:
                prices = connection.execute(
                    """
                    SELECT candle_time, close_price
                    FROM candles
                    WHERE stock_code = ?
                      AND candle_type = 'daily'
                      AND interval_minutes = 0
                      AND candle_time >= ?
                      AND close_price IS NOT NULL
                    ORDER BY candle_time, id
                    LIMIT ?
                    """,
                    (
                        row["stock_code"],
                        row["source_data_time"],
                        horizon_days + 1,
                    ),
                ).fetchall()
                if len(prices) < horizon_days + 1:
                    pending += 1
                    continue
                start = float(prices[0]["close_price"])
                end = float(prices[-1]["close_price"])
                forward_return = end / start - 1
                status = _performance_status(row["ai_action"], forward_return)
                connection.execute(
                    """
                    UPDATE ai_shadow_analyses
                    SET evaluated_horizon_days = ?, forward_return = ?,
                        performance_status = ?, evaluated_at = ?
                    WHERE id = ?
                    """,
                    (
                        horizon_days,
                        forward_return,
                        status,
                        _now().isoformat(timespec="seconds"),
                        row["id"],
                    ),
                )
                evaluated += 1
        return {"evaluated": evaluated, "pending": pending}

    def _analyze_candidate(self, candidate: dict[str, Any]) -> None:
        payload = self._payload(candidate)
        request_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        prompt_hash = hashlib.sha256(
            json.dumps(
                {
                    "version": self.settings.prompt_version,
                    "instructions": SYSTEM_INSTRUCTIONS,
                    "schema": ANALYSIS_SCHEMA,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        started = time.perf_counter()
        result = self.provider.analyze(payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        agreement = _agreement(
            candidate["composite_status"],
            float(candidate["composite_score"]),
            result.analysis.action,
            result.analysis.score,
        )
        cost = self._estimated_cost(
            result.input_tokens,
            result.output_tokens,
        )
        timestamp = _now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_shadow_analyses(
                    candidate_run_id, composite_decision_id, stock_code,
                    source_data_time, provider, model, prompt_version,
                    prompt_hash, request_hash, status, algorithm_action,
                    algorithm_score, ai_action, ai_score, ai_confidence,
                    agreement, summary, risks_json, catalysts_json,
                    input_tokens, output_tokens, estimated_cost, latency_ms,
                    response_id, raw_response_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'success', ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    provider, model, prompt_version,
                    stock_code, source_data_time
                ) DO UPDATE SET
                    status = 'success',
                    algorithm_action = excluded.algorithm_action,
                    algorithm_score = excluded.algorithm_score,
                    ai_action = excluded.ai_action,
                    ai_score = excluded.ai_score,
                    ai_confidence = excluded.ai_confidence,
                    agreement = excluded.agreement,
                    summary = excluded.summary,
                    risks_json = excluded.risks_json,
                    catalysts_json = excluded.catalysts_json,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    estimated_cost = excluded.estimated_cost,
                    latency_ms = excluded.latency_ms,
                    response_id = excluded.response_id,
                    raw_response_json = excluded.raw_response_json,
                    error_type = NULL,
                    error_message = NULL,
                    created_at = excluded.created_at
                """,
                (
                    candidate["run_id"],
                    candidate["composite_decision_id"],
                    candidate["stock_code"],
                    candidate["source_data_time"],
                    self.settings.provider,
                    self.settings.model,
                    self.settings.prompt_version,
                    prompt_hash,
                    request_hash,
                    candidate["algorithm_action"],
                    candidate["composite_score"],
                    result.analysis.action,
                    result.analysis.score,
                    result.analysis.confidence,
                    agreement,
                    result.analysis.summary,
                    json.dumps(result.analysis.risks, ensure_ascii=False),
                    json.dumps(result.analysis.catalysts, ensure_ascii=False),
                    result.input_tokens,
                    result.output_tokens,
                    float(cost) if cost is not None else None,
                    latency_ms,
                    result.response_id,
                    json.dumps(result.raw_response, ensure_ascii=False),
                    timestamp,
                ),
            )

    def _save_failure(
        self,
        candidate: dict[str, Any],
        exc: Exception,
    ) -> None:
        timestamp = _now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_shadow_analyses(
                    candidate_run_id, composite_decision_id, stock_code,
                    source_data_time, provider, model, prompt_version,
                    prompt_hash, request_hash, status, algorithm_action,
                    algorithm_score, error_type, error_message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 'failed', ?, ?, ?, ?, ?)
                ON CONFLICT(
                    provider, model, prompt_version,
                    stock_code, source_data_time
                ) DO UPDATE SET
                    status = 'failed',
                    error_type = excluded.error_type,
                    error_message = excluded.error_message,
                    created_at = excluded.created_at
                """,
                (
                    candidate["run_id"],
                    candidate["composite_decision_id"],
                    candidate["stock_code"],
                    candidate["source_data_time"],
                    self.settings.provider,
                    self.settings.model,
                    self.settings.prompt_version,
                    candidate["algorithm_action"],
                    candidate["composite_score"],
                    type(exc).__name__,
                    str(exc)[:1000],
                    timestamp,
                ),
            )

    def _latest_candidates(self, limit: int) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            run = connection.execute(
                "SELECT id, as_of FROM candidate_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if run is None:
                return []
            rows = connection.execute(
                """
                SELECT s.*, u.stock_name, u.market, u.sector
                FROM candidate_scores s
                LEFT JOIN stock_universe u ON u.stock_code = s.stock_code
                WHERE s.run_id = ?
                  AND s.composite_status IN ('buy_candidate', 'observe')
                ORDER BY
                    CASE s.composite_status
                        WHEN 'buy_candidate' THEN 0 ELSE 1
                    END,
                    s.composite_score DESC,
                    s.stock_code
                LIMIT ?
                """,
                (run["id"], limit),
            ).fetchall()
        return [
            {
                **dict(row),
                "source_data_time": run["as_of"],
                "algorithm_action": (
                    "buy_candidate"
                    if row["composite_status"] == "buy_candidate"
                    else "observe"
                ),
            }
            for row in rows
        ]

    def _payload(self, candidate: dict[str, Any]) -> dict[str, Any]:
        features = json.loads(candidate["features_json"])
        patterns = json.loads(candidate["patterns_json"])
        reasons = json.loads(candidate["reasons_json"])
        with self.database.connect() as connection:
            information = connection.execute(
                """
                SELECT source, title, event_type, sentiment, base_score,
                       confidence, entry_blocked, published_at
                FROM information_items
                WHERE stock_code = ? AND available_at <= ?
                ORDER BY published_at DESC, id DESC
                LIMIT 10
                """,
                (
                    candidate["stock_code"],
                    candidate["source_data_time"],
                ),
            ).fetchall()
        feature_keys = (
            "close",
            "rsi14",
            "atr14",
            "return20",
            "volatility20",
            "average_volume20",
            "average_trade_amount20",
        )
        return {
            "schema_version": "1.0",
            "stock": {
                "code": candidate["stock_code"],
                "name": candidate["stock_name"],
                "market": candidate["market"],
                "sector": candidate["sector"],
            },
            "as_of": candidate["source_data_time"],
            "algorithm": {
                "action": candidate["algorithm_action"],
                "score": candidate["composite_score"],
                "confidence": candidate["composite_confidence"],
                "technical_score": candidate["technical_score"],
                "pattern_score": candidate["pattern_score"],
                "relative_strength_score": candidate["relative_strength_score"],
                "liquidity_score": candidate["liquidity_score"],
                "information_score": candidate["information_score"],
            },
            "features": {
                key: features.get(key)
                for key in feature_keys
            },
            "patterns": patterns,
            "algorithm_reasons": reasons[-12:],
            "information": [dict(row) for row in information],
            "constraints": {
                "shadow_only": True,
                "no_order_authority": True,
                "risk_rules_cannot_be_relaxed": True,
            },
        }

    def _create_provider(self) -> AiProvider:
        if self.settings.provider == "openai":
            return OpenAiProvider(
                self.settings.openai_api_key,
                self.settings.model,
                timeout=self.settings.request_timeout,
                max_output_tokens=self.settings.max_output_tokens,
            )
        return AnthropicProvider(
            self.settings.anthropic_api_key,
            self.settings.model,
            timeout=self.settings.request_timeout,
            max_output_tokens=self.settings.max_output_tokens,
        )

    def _estimated_cost(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> Decimal | None:
        if (
            input_tokens is None
            or output_tokens is None
            or self.settings.input_cost_per_million is None
            or self.settings.output_cost_per_million is None
        ):
            return None
        return (
            Decimal(input_tokens)
            * self.settings.input_cost_per_million
            / Decimal(1_000_000)
            + Decimal(output_tokens)
            * self.settings.output_cost_per_million
            / Decimal(1_000_000)
        )


def _agreement(
    algorithm_action: str,
    algorithm_score: float,
    ai_action: str,
    ai_score: float,
) -> str:
    if algorithm_action == ai_action and abs(algorithm_score - ai_score) <= 15:
        return "agree"
    if algorithm_action == ai_action or abs(algorithm_score - ai_score) <= 15:
        return "partial"
    return "disagree"


def _performance_status(action: str, forward_return: float) -> str:
    if abs(forward_return) < 0.01:
        return "neutral"
    if action == "buy_candidate":
        return "correct" if forward_return > 0 else "incorrect"
    if action == "avoid":
        return "correct" if forward_return < 0 else "incorrect"
    return "neutral"


def _now() -> datetime:
    return datetime.now(SEOUL_TIMEZONE)
