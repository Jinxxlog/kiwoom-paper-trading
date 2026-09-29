from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from typing import Iterable


@dataclass(frozen=True)
class Classification:
    event_type: str
    sentiment: str
    base_score: float
    importance: float
    confidence: float
    entry_blocked: bool


RULES = (
    (("횡령", "배임"), "embezzlement", "negative", -100, 1.0, True),
    (("거래정지", "매매거래 정지"), "trading_halt", "negative", -100, 1.0, True),
    (("상장폐지", "상장 폐지"), "delisting", "negative", -100, 1.0, True),
    (("감사의견 거절", "의견거절"), "audit_rejection", "negative", -100, 1.0, True),
    (("부도", "회생절차", "파산"), "insolvency", "negative", -95, 1.0, True),
    (("유상증자",), "rights_offering", "negative", -60, 0.9, True),
    (("전환사채", "신주인수권부사채"), "convertible_bond", "negative", -45, 0.8, False),
    (("최대주주 변경",), "control_change", "negative", -30, 0.7, False),
    (("소송", "피소"), "lawsuit", "negative", -25, 0.7, False),
    (("공급계약", "수주", "계약 체결"), "supply_contract", "positive", 35, 0.75, False),
    (("흑자전환", "영업이익 증가"), "earnings_improvement", "positive", 30, 0.75, False),
    (("자기주식 취득", "자사주 취득", "주식 소각"), "shareholder_return", "positive", 30, 0.8, False),
    (("무상증자",), "bonus_issue", "positive", 15, 0.6, False),
    (("배당",), "dividend", "positive", 12, 0.55, False),
)


def classify(title: str, summary: str, *, source: str) -> Classification:
    text = normalize_text(f"{title} {summary}")
    source_confidence = 0.95 if source == "dart" else 0.65
    for keywords, event_type, sentiment, score, importance, blocked in RULES:
        if any(normalize_text(keyword) in text for keyword in keywords):
            return Classification(
                event_type,
                sentiment,
                float(score),
                float(importance),
                source_confidence,
                blocked,
            )
    return Classification("other", "neutral", 0.0, 0.3, source_confidence, False)


def decayed_score(
    base_score: float,
    importance: float,
    confidence: float,
    published_at: datetime,
    as_of: datetime,
    *,
    half_life_hours: float,
) -> float:
    age_hours = max(0.0, (as_of - published_at).total_seconds() / 3600)
    decay = math.pow(0.5, age_hours / max(half_life_hours, 1.0))
    return base_score * importance * confidence * decay


def entry_block_active(
    event_type: str,
    published_at: datetime,
    as_of: datetime,
) -> bool:
    days = {
        "embezzlement": 30,
        "trading_halt": 30,
        "delisting": 30,
        "audit_rejection": 30,
        "insolvency": 30,
        "rights_offering": 14,
    }.get(event_type, 0)
    return days > 0 and as_of - published_at <= timedelta(days=days)


def normalize_text(value: str) -> str:
    cleaned = unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"[^0-9A-Za-z가-힣]+", " ", cleaned).strip().lower()


def normalized_title(value: str) -> str:
    text = normalize_text(value)
    correction_words: Iterable[str] = (
        "정정",
        "첨부정정",
        "기재정정",
        "정정신고",
    )
    for word in correction_words:
        text = re.sub(rf"\b{word}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()
