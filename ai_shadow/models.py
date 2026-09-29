from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AiAnalysis:
    action: str
    score: float
    confidence: float
    summary: str
    risks: tuple[str, ...]
    catalysts: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AiAnalysis":
        required = {"action", "score", "confidence", "summary", "risks", "catalysts"}
        if set(value) != required:
            raise ValueError("AI 응답 필드가 스키마와 일치하지 않습니다.")
        action = str(value["action"])
        if action not in {"buy_candidate", "observe", "avoid"}:
            raise ValueError(f"지원하지 않는 AI 판단입니다: {action}")
        score = float(value["score"])
        confidence = float(value["confidence"])
        if not 0 <= score <= 100 or not 0 <= confidence <= 1:
            raise ValueError("AI 점수 또는 신뢰도 범위가 올바르지 않습니다.")
        summary = str(value["summary"]).strip()
        risks = _strings(value["risks"], "risks")
        catalysts = _strings(value["catalysts"], "catalysts")
        if not summary or len(summary) > 500:
            raise ValueError("AI 요약 길이가 올바르지 않습니다.")
        return cls(action, score, confidence, summary, risks, catalysts)


@dataclass(frozen=True)
class ProviderResult:
    analysis: AiAnalysis
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw_response: dict[str, Any]


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 5:
        raise ValueError(f"AI {name}는 최대 5개 문자열 배열이어야 합니다.")
    items = tuple(str(item).strip() for item in value)
    if any(not item or len(item) > 200 for item in items):
        raise ValueError(f"AI {name} 항목 길이가 올바르지 않습니다.")
    return items
