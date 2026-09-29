import unittest

from screening.stock_category import (
    COMMON_STOCK,
    MARKET_PRODUCT,
    PREFERRED_OR_SPECIAL,
    classify_stock_category,
    stock_category_payload,
)


class StockCategoryTest(unittest.TestCase):
    def test_common_stock_accepts_numeric_kiwoom_codes(self) -> None:
        self.assertEqual(classify_stock_category("A005930"), COMMON_STOCK)
        self.assertEqual(classify_stock_category("005930"), COMMON_STOCK)

    def test_special_code_is_not_grouped_with_common_stock(self) -> None:
        payload = stock_category_payload("A0039P0")

        self.assertEqual(payload["key"], PREFERRED_OR_SPECIAL)
        self.assertEqual(payload["label"], "우선주/특수코드")

    def test_preferred_name_and_market_product_name_are_split(self) -> None:
        self.assertEqual(
            classify_stock_category("A005935", "삼성전자우"),
            PREFERRED_OR_SPECIAL,
        )
        self.assertEqual(
            classify_stock_category("A122630", "KODEX 레버리지"),
            MARKET_PRODUCT,
        )


if __name__ == "__main__":
    unittest.main()
