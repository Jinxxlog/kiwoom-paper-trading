import re

from db.database import Database


STOCK_CODE_PATTERN = re.compile(r"^A[A-Z0-9]{6}$")


def normalize_stock_code(stock_code: str) -> str:
    normalized = stock_code.strip().upper()
    if normalized.isalnum() and len(normalized) == 6:
        normalized = f"A{normalized}"
    if not STOCK_CODE_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"잘못된 종목코드입니다: {stock_code!r}. "
            "A005930, 005930, A0017J0 형식을 사용하세요."
        )
    return normalized


class WatchlistService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, stock_code: str, stock_name: str | None = None) -> str:
        normalized = normalize_stock_code(stock_code)
        normalized_name = stock_name.strip() if stock_name and stock_name.strip() else None
        self.database.add_watchlist_stock(normalized, normalized_name)
        return normalized

    def remove(self, stock_code: str) -> bool:
        return self.database.disable_watchlist_stock(normalize_stock_code(stock_code))

    def active_codes(self) -> list[str]:
        return [row["stock_code"] for row in self.database.list_watchlist()]
