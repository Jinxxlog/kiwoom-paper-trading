from __future__ import annotations

import re
from typing import Any

from screening.market_product import is_market_product_name


COMMON_STOCK = "common_stock"
PREFERRED_OR_SPECIAL = "preferred_or_special"
MARKET_PRODUCT = "market_product"

STOCK_CATEGORY_LABELS = {
    COMMON_STOCK: "일반주",
    PREFERRED_OR_SPECIAL: "우선주/특수코드",
    MARKET_PRODUCT: "ETF/ETN/상품형",
}


def classify_stock_category(stock_code: Any, stock_name: Any = None) -> str:
    if is_market_product_name(stock_name):
        return MARKET_PRODUCT
    if _looks_preferred_name(stock_name):
        return PREFERRED_OR_SPECIAL

    normalized = str(stock_code or "").strip().upper()
    if normalized.startswith("A"):
        normalized = normalized[1:]
    if re.fullmatch(r"\d{6}", normalized):
        return COMMON_STOCK
    return PREFERRED_OR_SPECIAL


def stock_category_payload(stock_code: Any, stock_name: Any = None) -> dict[str, str]:
    key = classify_stock_category(stock_code, stock_name)
    return {
        "key": key,
        "label": STOCK_CATEGORY_LABELS[key],
    }


def stock_category_label(key: str) -> str:
    return STOCK_CATEGORY_LABELS.get(key, key)


def _looks_preferred_name(stock_name: Any) -> bool:
    if stock_name is None:
        return False
    compact = re.sub(r"\s+", "", str(stock_name).upper())
    if not compact:
        return False
    if "우선주" in compact:
        return True
    return bool(re.search(r"(?:\d*)우(?:B|C)?(?:\(|$)", compact))
