from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config.settings import InformationSettings
from data.market_calendar import SEOUL_TIMEZONE
from data.watchlist import normalize_stock_code
from db.database import Database
from information.clients import DartClient, NaverNewsClient
from information.scoring import (
    classify,
    decayed_score,
    entry_block_active,
    normalized_title,
)


class InformationService:
    def __init__(
        self,
        database: Database,
        settings: InformationSettings,
        *,
        dart_client: DartClient | None = None,
        naver_client: NaverNewsClient | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.dart = dart_client or DartClient(
            settings.dart_api_key,
            timeout=settings.request_timeout,
        )
        self.naver = naver_client or NaverNewsClient(
            settings.naver_client_id,
            settings.naver_client_secret,
            timeout=settings.request_timeout,
        )

    def sync_dart_companies(self) -> int:
        timestamp = _now().isoformat(timespec="seconds")
        companies = self.dart.companies()
        with self.database.connect() as connection:
            for company in companies:
                stock_code = company["stock_code"]
                normalized_code = (
                    normalize_stock_code(str(stock_code)) if stock_code else None
                )
                connection.execute(
                    """
                    INSERT INTO dart_companies(
                        corp_code, corp_name, corp_eng_name, stock_code,
                        modify_date, synced_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(corp_code) DO UPDATE SET
                        corp_name = excluded.corp_name,
                        corp_eng_name = excluded.corp_eng_name,
                        stock_code = excluded.stock_code,
                        modify_date = excluded.modify_date,
                        synced_at = excluded.synced_at
                    """,
                    (
                        company["corp_code"],
                        company["corp_name"],
                        company["corp_eng_name"],
                        normalized_code,
                        company["modify_date"],
                        timestamp,
                    ),
                )
                if normalized_code:
                    connection.execute(
                        """
                        UPDATE stock_universe
                        SET stock_name = COALESCE(stock_name, ?)
                        WHERE stock_code = ?
                        """,
                        (company["corp_name"], normalized_code),
                    )
        return len(companies)

    def collect_dart(
        self,
        stock_codes: Iterable[str],
        *,
        begin_date: date,
        end_date: date,
    ) -> dict[str, int]:
        collected = duplicates = 0
        mappings = self._company_mappings(stock_codes)
        for mapping in mappings:
            items = self.dart.disclosures(
                corp_code=mapping["corp_code"],
                begin_date=begin_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
            for item in items:
                saved = self._save_dart_item(mapping, item)
                collected += int(saved)
                duplicates += int(not saved)
        return {"saved": collected, "duplicates": duplicates, "stocks": len(mappings)}

    def collect_naver(
        self,
        stock_codes: Iterable[str],
        *,
        display: int = 20,
    ) -> dict[str, int]:
        collected = duplicates = ignored = 0
        for stock in self._stock_names(stock_codes):
            items = self.naver.search(stock["stock_name"], display=display)
            for item in items:
                result = self._save_naver_item(stock, item)
                collected += int(result == "saved")
                duplicates += int(result == "duplicate")
                ignored += int(result == "ignored")
        return {"saved": collected, "duplicates": duplicates, "ignored": ignored}

    def scores(
        self,
        stock_codes: Iterable[str] | None = None,
        *,
        as_of: datetime | None = None,
        save: bool = False,
    ) -> dict[str, dict[str, Any]]:
        point = _aware(as_of or _now())
        requested = (
            [normalize_stock_code(code) for code in stock_codes]
            if stock_codes is not None
            else None
        )
        query = """
            SELECT *
            FROM information_items
            WHERE available_at <= ?
        """
        parameters: list[Any] = [point.isoformat(timespec="seconds")]
        if requested:
            placeholders = ",".join("?" for _ in requested)
            query += f" AND stock_code IN ({placeholders})"
            parameters.extend(requested)
        query += " ORDER BY published_at, id"
        with self.database.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, parameters)]
        latest_by_logical: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            latest_by_logical[(row["source"], row["logical_key"])] = row
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in latest_by_logical.values():
            if row["stock_code"]:
                grouped.setdefault(row["stock_code"], []).append(row)
        result: dict[str, dict[str, Any]] = {}
        for code in requested or grouped:
            details = []
            positive = negative = 0.0
            blocked = False
            for row in grouped.get(code, []):
                published = _aware(datetime.fromisoformat(row["published_at"]))
                half_life = (
                    self.settings.disclosure_half_life_hours
                    if row["source"] == "dart"
                    else self.settings.news_half_life_hours
                )
                score = decayed_score(
                    float(row["base_score"]),
                    float(row["importance"]),
                    float(row["confidence"]),
                    published,
                    point,
                    half_life_hours=half_life,
                )
                positive += max(score, 0)
                negative += min(score, 0)
                active_block = bool(row["entry_blocked"]) and entry_block_active(
                    row["event_type"],
                    published,
                    point,
                )
                blocked = blocked or active_block
                details.append(
                    {
                        "id": row["id"],
                        "source": row["source"],
                        "title": row["title"],
                        "event_type": row["event_type"],
                        "confidence": row["confidence"],
                        "published_at": row["published_at"],
                        "score": round(score, 4),
                        "entry_blocked": active_block,
                    }
                )
            positive_for_entry = min(
                self.settings.candidate_score_limit * 0.20,
                positive * 0.25,
            )
            total = max(
                -self.settings.candidate_score_limit,
                min(
                    self.settings.candidate_score_limit * 0.20,
                    positive_for_entry + negative,
                ),
            )
            summary = {
                "stock_code": code,
                "as_of": point.isoformat(timespec="seconds"),
                "total_score": round(total, 4),
                "positive_score": round(positive, 4),
                "negative_score": round(negative, 4),
                "entry_blocked": blocked,
                "item_count": len(details),
                "details": sorted(
                    details,
                    key=lambda item: item["published_at"],
                    reverse=True,
                ),
            }
            result[code] = summary
            if save and details:
                self._save_score_snapshot(summary)
        return result

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*, u.stock_name
                FROM information_items i
                LEFT JOIN stock_universe u ON u.stock_code = i.stock_code
                ORDER BY i.published_at DESC, i.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        point = _now()
        result = []
        for row in rows:
            item = dict(row)
            available = _aware(datetime.fromisoformat(item["available_at"])) <= point
            half_life = (
                self.settings.disclosure_half_life_hours
                if item["source"] == "dart"
                else self.settings.news_half_life_hours
            )
            item["current_score"] = (
                round(
                    decayed_score(
                        float(item["base_score"]),
                        float(item["importance"]),
                        float(item["confidence"]),
                        _aware(datetime.fromisoformat(item["published_at"])),
                        point,
                        half_life_hours=half_life,
                    ),
                    4,
                )
                if available
                else 0.0
            )
            item["entry_blocked"] = (
                available
                and bool(item["entry_blocked"])
                and entry_block_active(
                    item["event_type"],
                    _aware(datetime.fromisoformat(item["published_at"])),
                    point,
                )
            )
            result.append(item)
        return result

    def _save_dart_item(
        self,
        mapping: dict[str, Any],
        item: dict[str, Any],
    ) -> bool:
        receipt_date = datetime.strptime(str(item["rcept_dt"]), "%Y%m%d").date()
        # DART 목록은 접수 시각을 주지 않으므로 당일 장중 미래참조를 막기 위해
        # 그 날 23:59:59부터 사용 가능한 것으로 보수적으로 저장한다.
        published = datetime.combine(
            receipt_date,
            time(23, 59, 59),
            tzinfo=SEOUL_TIMEZONE,
        )
        title = str(item.get("report_nm") or "").strip()
        normalized = normalized_title(title)
        classification = classify(title, "", source="dart")
        logical_key = f"{mapping['stock_code']}:{item['rcept_no']}"
        if "정정" in title:
            prior_key = self._latest_dart_logical_key(
                mapping["stock_code"],
                normalized,
                published,
            )
            if prior_key:
                logical_key = prior_key
        return self._save_item(
            source="dart",
            source_item_id=str(item["rcept_no"]),
            stock_code=mapping["stock_code"],
            corp_code=mapping["corp_code"],
            title=title,
            summary=str(item.get("rm") or ""),
            url=(
                "https://dart.fss.or.kr/dsaf001/main.do?"
                f"rcpNo={item['rcept_no']}"
            ),
            original_url=None,
            normalized=normalized,
            logical_key=logical_key,
            published_at=published,
            available_at=published,
            classification=classification,
            raw=item,
        )

    def _latest_dart_logical_key(
        self,
        stock_code: str,
        normalized: str,
        published_at: datetime,
    ) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT logical_key
                FROM information_items
                WHERE source = 'dart'
                  AND stock_code = ?
                  AND normalized_title = ?
                  AND published_at < ?
                  AND published_at >= ?
                ORDER BY published_at DESC, id DESC
                LIMIT 1
                """,
                (
                    stock_code,
                    normalized,
                    published_at.isoformat(timespec="seconds"),
                    (published_at - timedelta(days=30)).isoformat(
                        timespec="seconds"
                    ),
                ),
            ).fetchone()
        return row["logical_key"] if row is not None else None

    def _save_naver_item(
        self,
        stock: dict[str, str],
        item: dict[str, Any],
    ) -> str:
        title = _clean_html(str(item.get("title") or ""))
        summary = _clean_html(str(item.get("description") or ""))
        if not _is_relevant_to_stock(title, summary, stock["stock_name"]):
            return "ignored"
        published = parsedate_to_datetime(str(item["pubDate"]))
        published = _aware(published)
        collected = _now()
        normalized = normalized_title(title)
        url = str(item.get("link") or "")
        original = str(item.get("originallink") or "")
        canonical = _canonical_url(original or url)
        logical_key = hashlib.sha256(
            f"{stock['stock_code']}:{canonical or normalized}".encode("utf-8")
        ).hexdigest()
        classification = classify(title, summary, source="naver")
        saved = self._save_item(
            source="naver",
            source_item_id=hashlib.sha256(
                f"{canonical}|{normalized}|{published.isoformat()}".encode("utf-8")
            ).hexdigest(),
            stock_code=stock["stock_code"],
            corp_code=None,
            title=title,
            summary=summary,
            url=url,
            original_url=original or None,
            normalized=normalized,
            logical_key=logical_key,
            published_at=published,
            available_at=max(published, collected),
            classification=classification,
            raw=item,
            canonical_url=canonical,
        )
        return "saved" if saved else "duplicate"

    def _save_item(
        self,
        *,
        source: str,
        source_item_id: str,
        stock_code: str,
        corp_code: str | None,
        title: str,
        summary: str,
        url: str | None,
        original_url: str | None,
        normalized: str,
        logical_key: str,
        published_at: datetime,
        available_at: datetime,
        classification: Any,
        raw: dict[str, Any],
        canonical_url: str | None = None,
    ) -> bool:
        timestamp = _now().isoformat(timespec="seconds")
        with self.database.connect() as connection:
            duplicate = connection.execute(
                """
                SELECT id, normalized_title
                FROM information_items
                WHERE source = ?
                  AND (
                    source_item_id = ?
                    OR (stock_code = ? AND ? IS NOT NULL AND canonical_url = ?)
                  )
                ORDER BY id DESC LIMIT 1
                """,
                (
                    source,
                    source_item_id,
                    stock_code,
                    canonical_url,
                    canonical_url,
                ),
            ).fetchone()
            if duplicate is None and source == "naver":
                candidates = connection.execute(
                    """
                    SELECT id, normalized_title
                    FROM information_items
                    WHERE source = 'naver' AND stock_code = ?
                      AND published_at >= ?
                    ORDER BY published_at DESC LIMIT 100
                    """,
                    (
                        stock_code,
                        (_aware(published_at) - timedelta(days=7)).isoformat(
                            timespec="seconds"
                        ),
                    ),
                ).fetchall()
                duplicate = next(
                    (
                        row
                        for row in candidates
                        if SequenceMatcher(
                            None,
                            normalized,
                            row["normalized_title"],
                        ).ratio()
                        >= 0.88
                    ),
                    None,
                )
            if duplicate is not None:
                connection.execute(
                    """
                    UPDATE information_items
                    SET last_collected_at = ?, duplicate_count = duplicate_count + 1
                    WHERE id = ?
                    """,
                    (timestamp, duplicate["id"]),
                )
                return False
            connection.execute(
                """
                INSERT INTO information_items(
                    source, source_item_id, stock_code, corp_code, title,
                    summary, url, original_url, normalized_title,
                    canonical_url, logical_key, published_at, available_at,
                    first_collected_at, last_collected_at, event_type,
                    sentiment, base_score, importance, confidence,
                    entry_blocked, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    source_item_id,
                    stock_code,
                    corp_code,
                    title,
                    summary,
                    url,
                    original_url,
                    normalized,
                    canonical_url,
                    logical_key,
                    _aware(published_at).isoformat(timespec="seconds"),
                    _aware(available_at).isoformat(timespec="seconds"),
                    timestamp,
                    timestamp,
                    classification.event_type,
                    classification.sentiment,
                    classification.base_score,
                    classification.importance,
                    classification.confidence,
                    int(classification.entry_blocked),
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
        return True

    def _company_mappings(
        self,
        stock_codes: Iterable[str],
    ) -> list[dict[str, Any]]:
        codes = [normalize_stock_code(code) for code in stock_codes]
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT corp_code, corp_name, stock_code
                FROM dart_companies
                WHERE stock_code IN ({placeholders})
                """,
                codes,
            ).fetchall()
        return [dict(row) for row in rows]

    def _stock_names(
        self,
        stock_codes: Iterable[str],
    ) -> list[dict[str, str]]:
        codes = [normalize_stock_code(code) for code in stock_codes]
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT stock_code, stock_name
                FROM stock_universe
                WHERE stock_code IN ({placeholders}) AND stock_name IS NOT NULL
                """,
                codes,
            ).fetchall()
        return [dict(row) for row in rows]

    def _save_score_snapshot(self, summary: dict[str, Any]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO information_score_snapshots(
                    stock_code, as_of, total_score, positive_score,
                    negative_score, entry_blocked, item_count,
                    details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stock_code, as_of) DO UPDATE SET
                    total_score = excluded.total_score,
                    positive_score = excluded.positive_score,
                    negative_score = excluded.negative_score,
                    entry_blocked = excluded.entry_blocked,
                    item_count = excluded.item_count,
                    details_json = excluded.details_json,
                    created_at = excluded.created_at
                """,
                (
                    summary["stock_code"],
                    summary["as_of"],
                    summary["total_score"],
                    summary["positive_score"],
                    summary["negative_score"],
                    int(summary["entry_blocked"]),
                    summary["item_count"],
                    json.dumps(summary["details"], ensure_ascii=False),
                    _now().isoformat(timespec="seconds"),
                ),
            )


def _now() -> datetime:
    return datetime.now(SEOUL_TIMEZONE)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SEOUL_TIMEZONE)
    return value.astimezone(SEOUL_TIMEZONE)


def _canonical_url(value: str) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        ]
    )
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            query,
            "",
        )
    )


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _is_relevant_to_stock(title: str, summary: str, stock_name: str) -> bool:
    clean_title = _strip_reference_noise(title)
    clean_summary = _strip_reference_noise(summary)
    title_text = normalized_title(clean_title)
    text = normalized_title(f"{clean_title} {clean_summary}")
    aliases = _stock_name_aliases(stock_name)
    compact = re.sub(r"[^0-9a-z가-힣]", "", normalized_title(stock_name))
    if compact == "naver":
        return _is_relevant_naver_news(
            title_text,
            text,
            aliases,
        )
    if _requires_title_alias(compact):
        return any(_contains_title_alias(title_text, alias) for alias in aliases)
    title_has_alias = any(_contains_title_alias(title_text, alias) for alias in aliases)
    if not title_has_alias:
        return False
    return any(_contains_alias(text, alias) for alias in aliases)


def _strip_reference_noise(value: str) -> str:
    text = value or ""
    text = re.sub(
        r"\b[\w.+-]+\s*@\s*[\w.-]+\s*(?:\.\s*[A-Za-z]{2,})+\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(?:blog|cafe|m|n|news|site)\s*\.\s*naver\s*\.\s*com\S*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bnaver\s*\.\s*(?:com|me)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _contains_alias(text: str, alias: str) -> bool:
    if not alias:
        return False
    if re.fullmatch(r"[0-9a-z]+", alias):
        return re.search(rf"\b{re.escape(alias)}\b", text) is not None
    return alias in text


def _is_relevant_naver_news(
    title_text: str,
    text: str,
    aliases: set[str],
) -> bool:
    if not any(_contains_alias(text, alias) for alias in aliases):
        return False
    title_has_alias = any(_contains_title_alias(title_text, alias) for alias in aliases)
    if not title_has_alias:
        return False
    content_only_phrases = (
        "naver webtoon",
        "popular naver webtoon",
        "based on the popular naver webtoon",
        "네이버 웹툰",
        "네이버 블로그",
        "네이버 카페",
    )
    corporate_terms = (
        "035420",
        "주가",
        "증권",
        "코스피",
        "시가총액",
        "실적",
        "매출",
        "영업이익",
        "공시",
        "자사주",
        "배당",
        "투자",
        "인수",
        "계약",
        "수주",
        "출시",
        "도입",
        "인증",
        "광고",
        "검색",
        "커머스",
        "쇼핑",
        "클라우드",
        "페이",
        "플레이스",
        "지도",
        "치지직",
        "라인야후",
        "하이퍼클로바",
        "파트너",
        "사업",
        "대표",
    )
    has_corporate_context = any(term in text for term in corporate_terms)
    if any(phrase in text for phrase in content_only_phrases):
        return has_corporate_context
    if has_corporate_context:
        return True
    return title_text.startswith("네이버 ") or title_text.startswith("naver ")


def _requires_title_alias(compact: str) -> bool:
    return compact in {
        "kakao",
        "카카오",
        "sk",
        "lg",
        "kt",
        "한화",
        "현대",
        "두산",
    }


def _contains_title_alias(text: str, alias: str) -> bool:
    if not alias:
        return False
    if re.fullmatch(r"[0-9a-z]+", alias):
        return re.search(rf"\b{re.escape(alias)}\b", text) is not None
    return re.search(
        rf"(^|[^0-9a-z가-힣]){re.escape(alias)}($|[^0-9a-z가-힣])",
        text,
    ) is not None


def _stock_name_aliases(stock_name: str) -> set[str]:
    normalized = normalized_title(stock_name)
    aliases = {normalized}
    compact = re.sub(r"[^0-9a-z가-힣]", "", normalized)
    if compact:
        aliases.add(compact)
    manual = {
        "naver": {"naver", "네이버"},
        "kakao": {"kakao", "카카오"},
        "카카오": {"kakao", "카카오"},
        "lg": {"lg", "엘지"},
        "sk": {"sk"},
        "kt": {"kt"},
    }
    aliases.update(manual.get(compact, set()))
    return aliases
