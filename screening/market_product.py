from __future__ import annotations

from typing import Any


MARKET_PRODUCT_BRANDS = (
    "KODEX",
    "TIGER",
    "ACE",
    "SOL",
    "KBSTAR",
    "HANARO",
    "KOSEF",
    "ARIRANG",
    "TIMEFOLIO",
    "RISE",
    "PLUS",
    "TREX",
    "FOCUS",
    "히어로즈",
    "마이티",
)

MARKET_PRODUCT_KEYWORDS = (
    "ETF",
    "ETN",
    "레버리지",
    "인버스",
    "선물",
    "단일종목",
    "채권",
    "국고채",
    "머니마켓",
    "합성",
)


def is_market_product_name(stock_name: Any) -> bool:
    if stock_name is None:
        return False
    text = str(stock_name).strip()
    if not text:
        return False
    upper = text.upper()
    tokens = upper.split()
    first_token = tokens[0] if tokens else upper
    compact = upper.replace(" ", "")

    if first_token in MARKET_PRODUCT_BRANDS:
        return True
    if any(compact.startswith(brand) for brand in MARKET_PRODUCT_BRANDS):
        return True
    return any(keyword.upper() in upper for keyword in MARKET_PRODUCT_KEYWORDS)


def is_market_product_item(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    return is_market_product_name(item.get("stock_name"))
