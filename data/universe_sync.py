from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Iterable

import requests

from data.watchlist import normalize_stock_code
from db.database import Database


KIND_CORPORATION_LIST_URL = (
    "https://kind.krx.co.kr/corpgeneral/"
    "corpList.do?method=download&searchType=13"
)


@dataclass(frozen=True)
class ListedStock:
    stock_code: str
    stock_name: str
    market: str
    sector: str | None
    listing_date: str | None


@dataclass(frozen=True)
class UniverseSyncResult:
    received_count: int
    registered_count: int
    excluded_count: int
    deactivated_count: int


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None:
            assert self._row is not None
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


class KindUniverseClient:
    def __init__(self, *, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("KIND 요청 제한 시간은 0초보다 커야 합니다.")
        self.timeout = timeout

    def fetch(self) -> list[ListedStock]:
        response = requests.get(
            KIND_CORPORATION_LIST_URL,
            timeout=self.timeout,
            headers={"User-Agent": "auto-invest-research/1.0"},
        )
        response.raise_for_status()
        text = response.content.decode("euc-kr")
        return self.parse(text)

    @staticmethod
    def parse(text: str) -> list[ListedStock]:
        parser = _TableParser()
        parser.feed(text)
        if not parser.rows:
            raise ValueError("KIND 상장법인 목록에 표가 없습니다.")
        headers = parser.rows[0]
        required = {"회사명", "시장구분", "종목코드", "업종", "상장일"}
        if not required.issubset(headers):
            raise ValueError("KIND 상장법인 목록 형식이 변경되었습니다.")
        index = {name: headers.index(name) for name in required}
        stocks = []
        for row in parser.rows[1:]:
            if len(row) < len(headers):
                continue
            name = row[index["회사명"]].strip()
            raw_code = row[index["종목코드"]].strip()
            raw_market = row[index["시장구분"]].strip()
            if raw_market not in {"유가", "유가증권", "코스닥"}:
                continue
            if len(raw_code) != 6 or not raw_code.isalnum():
                continue
            if _excluded_name(name):
                continue
            stocks.append(
                ListedStock(
                    stock_code=normalize_stock_code(raw_code),
                    stock_name=name,
                    market=(
                        "KOSPI"
                        if raw_market in {"유가", "유가증권"}
                        else "KOSDAQ"
                    ),
                    sector=row[index["업종"]].strip() or None,
                    listing_date=row[index["상장일"]].strip() or None,
                )
            )
        return stocks


class UniverseSyncService:
    def __init__(
        self,
        database: Database,
        client: KindUniverseClient | None = None,
    ) -> None:
        self.database = database
        self.client = client or KindUniverseClient()

    def sync(self) -> UniverseSyncResult:
        stocks = self.client.fetch()
        timestamp = datetime.now().isoformat(timespec="seconds")
        codes = {stock.stock_code for stock in stocks}
        with self.database.connect() as connection:
            existing_kind = {
                row["stock_code"]
                for row in connection.execute(
                    "SELECT stock_code FROM stock_universe WHERE source = 'kind'"
                ).fetchall()
            }
            connection.executemany(
                """
                INSERT INTO stock_universe(
                    stock_code, stock_name, market, sector, tradable,
                    management, halted, overheated, source, updated_at,
                    listing_date, security_type, last_synced_at
                )
                VALUES (?, ?, ?, ?, 1, 0, 0, 0, 'kind', ?, ?, 'common_stock', ?)
                ON CONFLICT(stock_code) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    market = excluded.market,
                    sector = excluded.sector,
                    tradable = 1,
                    source = 'kind',
                    updated_at = excluded.updated_at,
                    listing_date = excluded.listing_date,
                    security_type = excluded.security_type,
                    last_synced_at = excluded.last_synced_at
                """,
                [
                    (
                        stock.stock_code,
                        stock.stock_name,
                        stock.market,
                        stock.sector,
                        timestamp,
                        stock.listing_date,
                        timestamp,
                    )
                    for stock in stocks
                ],
            )
            missing = existing_kind - codes
            if missing:
                placeholders = ", ".join("?" for _ in missing)
                connection.execute(
                    f"""
                    UPDATE stock_universe
                    SET tradable = 0, updated_at = ?, last_synced_at = ?
                    WHERE stock_code IN ({placeholders})
                    """,
                    (timestamp, timestamp, *sorted(missing)),
                )
            connection.executemany(
                """
                INSERT INTO universe_collection_status(
                    stock_code, status, target_bars, daily_bar_count, updated_at
                )
                VALUES (?, 'pending', 200, 0, ?)
                ON CONFLICT(stock_code) DO NOTHING
                """,
                [(stock.stock_code, timestamp) for stock in stocks],
            )
            connection.execute(
                """
                UPDATE universe_collection_status
                SET daily_bar_count = (
                        SELECT COUNT(*)
                        FROM candles c
                        WHERE c.stock_code = universe_collection_status.stock_code
                          AND c.candle_type = 'daily'
                    ),
                    latest_candle_time = (
                        SELECT MAX(candle_time)
                        FROM candles c
                        WHERE c.stock_code = universe_collection_status.stock_code
                          AND c.candle_type = 'daily'
                    ),
                    status = CASE
                        WHEN (
                            SELECT COUNT(*)
                            FROM candles c
                            WHERE c.stock_code = universe_collection_status.stock_code
                              AND c.candle_type = 'daily'
                        ) >= target_bars
                        THEN 'completed'
                        WHEN status = 'completed' THEN 'pending'
                        ELSE status
                    END,
                    updated_at = ?
                """,
                (timestamp,),
            )
        return UniverseSyncResult(
            received_count=len(stocks),
            registered_count=len(stocks),
            excluded_count=0,
            deactivated_count=len(existing_kind - codes),
        )


def _excluded_name(name: str) -> bool:
    normalized = name.replace(" ", "").upper()
    excluded_tokens: Iterable[str] = (
        "스팩",
        "SPAC",
        "리츠",
        "REIT",
        "기업인수목적",
    )
    return any(token in normalized for token in excluded_tokens)
