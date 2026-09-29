import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from data.api_budget import (
    ApiBudgetBlockedError,
    ApiBudgetManager,
    ApiBudgetPolicy,
    ApiCallPriority,
)
from db.database import Database
from kiwoom_rest_client import KiwoomRestClient


class KiwoomRestClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.client = KiwoomRestClient(
            "app-key",
            "secret-key",
            use_mock=True,
            token_cache=Path(self.temp_dir.name) / "token.json",
            timeout=1,
            max_retries=2,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_invalid_retry_settings_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            KiwoomRestClient("key", "secret", timeout=0)
        with self.assertRaises(ValueError):
            KiwoomRestClient("key", "secret", max_retries=-1)

    def test_valid_bootstrap_token_skips_new_token_request(self) -> None:
        client = KiwoomRestClient(
            "app-key",
            "secret-key",
            initial_token="bootstrap-token",
            initial_expires_dt="20991231235959",
        )
        client.request_access_token = Mock()

        client.ensure_access_token()

        client.request_access_token.assert_not_called()

    @patch("kiwoom_rest_client.time.sleep")
    def test_timeout_is_retried_with_bounded_backoff(self, sleep: Mock) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        self.client.session.post = Mock(
            side_effect=[
                requests.Timeout("first"),
                requests.ConnectionError("second"),
                response,
            ]
        )

        result = self.client._post(
            "https://example.test",
            headers={},
            payload={},
            retryable=True,
        )

        self.assertIs(result, response)
        self.assertEqual(self.client.session.post.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.5, 1.0],
        )

    @patch("kiwoom_rest_client.time.sleep")
    def test_retry_limit_propagates_last_timeout(self, sleep: Mock) -> None:
        self.client.session.post = Mock(
            side_effect=requests.Timeout("unavailable")
        )

        with self.assertRaises(requests.Timeout):
            self.client._post(
                "https://example.test",
                headers={},
                payload={},
                retryable=True,
            )

        self.assertEqual(self.client.session.post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("kiwoom_rest_client.time.sleep")
    def test_non_retryable_request_is_attempted_once(self, sleep: Mock) -> None:
        self.client.session.post = Mock(
            side_effect=requests.ConnectionError("unavailable")
        )

        with self.assertRaises(requests.ConnectionError):
            self.client._post(
                "https://example.test",
                headers={},
                payload={},
                retryable=False,
            )

        self.assertEqual(self.client.session.post.call_count, 1)
        sleep.assert_not_called()

    def test_budget_manager_records_successful_request(self) -> None:
        database = Database(Path(self.temp_dir.name) / "budget.db")
        database.initialize()
        client = KiwoomRestClient(
            "app-key",
            "secret-key",
            use_mock=True,
            timeout=1,
            initial_token="token",
            initial_expires_dt="20991231235959",
            api_budget_manager=ApiBudgetManager(database),
        )
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {"return_code": 0, "output": []}
        response.raise_for_status.return_value = None
        client.session.post = Mock(return_value=response)

        result = client.request_api(
            "ka10006",
            "/api/dostk/mrkcond",
            {"stk_cd": "A005930"},
        )

        self.assertEqual(result["output"], [])
        events = database.latest_api_budget_events()
        self.assertEqual(events[0]["api_group"], "current_price")
        self.assertEqual(events[0]["stock_code"], "A005930")
        self.assertEqual(events[0]["status_code"], 200)

    def test_budget_manager_blocks_request_before_http_call(self) -> None:
        database = Database(Path(self.temp_dir.name) / "blocked.db")
        database.initialize()
        manager = ApiBudgetManager(
            database,
            policies={
                "current_price": ApiBudgetPolicy(
                    "current_price",
                    limit_count=0,
                ),
                "default": ApiBudgetPolicy("default", limit_count=10),
            },
        )
        client = KiwoomRestClient(
            "app-key",
            "secret-key",
            use_mock=True,
            timeout=1,
            initial_token="token",
            initial_expires_dt="20991231235959",
            api_budget_manager=manager,
        )
        client.session.post = Mock()

        with self.assertRaises(ApiBudgetBlockedError):
            client.request_api(
                "ka10006",
                "/api/dostk/mrkcond",
                {"stk_cd": "A005930"},
            )

        client.session.post.assert_not_called()
        events = database.latest_api_budget_events()
        self.assertEqual(events[0]["reason"], "skipped_by_budget")
        self.assertTrue(events[0]["blocked"])

    def test_budget_manager_records_429_and_sets_backoff(self) -> None:
        database = Database(Path(self.temp_dir.name) / "backoff.db")
        database.initialize()
        client = KiwoomRestClient(
            "app-key",
            "secret-key",
            use_mock=True,
            timeout=1,
            initial_token="token",
            initial_expires_dt="20991231235959",
            api_budget_manager=ApiBudgetManager(database),
        )
        response = Mock()
        response.status_code = 429
        error = requests.HTTPError("too many requests", response=response)
        response.raise_for_status.side_effect = error
        client.session.post = Mock(return_value=response)

        with self.assertRaises(requests.HTTPError):
            client.request_api(
                "ka10004",
                "/api/dostk/mrkcond",
                {"stk_cd": "A005930"},
            )

        events = database.latest_api_budget_events()
        self.assertEqual(events[0]["status_code"], 429)
        self.assertEqual(events[0]["reason"], "http_429_backoff")
        self.assertEqual(events[0]["backoff_seconds"], 30)
        window = database.get_api_budget_window("order_book")
        self.assertIsNotNone(window)
        self.assertIsNotNone(window["backoff_until"])

    def test_theme_api_wrappers_use_theme_endpoint(self) -> None:
        page = Mock()
        self.client.request_api_page = Mock(return_value=page)

        self.assertIs(self.client.get_theme_groups(date_type="10"), page)
        self.client.request_api_page.assert_called_with(
            "ka90001",
            "/api/dostk/thme",
            {
                "qry_tp": "0",
                "stk_cd": "",
                "date_tp": "10",
                "thema_nm": "",
                "flu_pl_amt_tp": "1",
                "stex_tp": "1",
            },
            cont_yn=None,
            next_key=None,
        )

        self.client.get_theme_components("123", date_type="2")
        self.client.request_api_page.assert_called_with(
            "ka90002",
            "/api/dostk/thme",
            {
                "date_tp": "2",
                "thema_grp_cd": "123",
                "stex_tp": "1",
            },
            cont_yn=None,
            next_key=None,
        )

    def test_theme_group_stock_search_uses_qry_type_two(self) -> None:
        page = Mock()
        self.client.request_api_page = Mock(return_value=page)

        self.assertIs(
            self.client.get_theme_groups(query_type="2", stock_code="005930"),
            page,
        )

        self.client.request_api_page.assert_called_with(
            "ka90001",
            "/api/dostk/thme",
            {
                "qry_tp": "2",
                "stk_cd": "005930",
                "date_tp": "10",
                "thema_nm": "",
                "flu_pl_amt_tp": "1",
                "stex_tp": "1",
            },
            cont_yn=None,
            next_key=None,
        )

    def test_realtime_item_ranking_uses_stock_info_endpoint(self) -> None:
        page = Mock()
        self.client.request_api_page = Mock(return_value=page)

        self.assertIs(self.client.get_realtime_item_ranking(query_type="5"), page)

        self.client.request_api_page.assert_called_with(
            "ka00198",
            "/api/dostk/stkinfo",
            {"qry_tp": "5"},
            cont_yn=None,
            next_key=None,
            priority=ApiCallPriority.HIGH,
        )

    def test_program_trade_wrapper_uses_market_condition_endpoint(self) -> None:
        page = Mock()
        self.client.request_api_page = Mock(return_value=page)

        self.assertIs(
            self.client.get_stock_daily_program_trade(
                stk_cd="005930",
                date="20260624",
            ),
            page,
        )
        self.client.request_api_page.assert_called_with(
            "ka90013",
            "/api/dostk/mrkcond",
            {
                "amt_qty_tp": "",
                "stk_cd": "005930",
                "date": "20260624",
            },
            cont_yn=None,
            next_key=None,
            priority=ApiCallPriority.NORMAL,
        )

    def test_sector_daily_chart_wrapper_uses_chart_endpoint(self) -> None:
        page = Mock()
        self.client.request_api_page = Mock(return_value=page)

        self.assertIs(
            self.client.get_sector_daily_chart(
                "101",
                "20260626",
                priority=ApiCallPriority.HIGH,
            ),
            page,
        )
        self.client.request_api_page.assert_called_with(
            "ka20006",
            "/api/dostk/chart",
            {
                "inds_cd": "101",
                "base_dt": "20260626",
            },
            cont_yn=None,
            next_key=None,
            priority=ApiCallPriority.HIGH,
        )

    def test_market_rank_wrappers_use_rank_info_endpoint(self) -> None:
        page = Mock()
        self.client.request_api_page = Mock(return_value=page)

        self.assertIs(self.client.get_expected_contract_change_ranking(), page)
        self.client.request_api_page.assert_called_with(
            "ka10029",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": "000",
                "sort_tp": "1",
                "trde_qty_cnd": "0",
                "stk_cnd": "0",
                "crd_cnd": "0",
                "pric_cnd": "0",
                "stex_tp": "3",
            },
            cont_yn=None,
            next_key=None,
            priority=ApiCallPriority.HIGH,
        )

        self.client.get_trade_amount_ranking(market_type="101")
        self.client.request_api_page.assert_called_with(
            "ka10032",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": "101",
                "mang_stk_incls": "0",
                "stex_tp": "3",
            },
            cont_yn=None,
            next_key=None,
            priority=ApiCallPriority.HIGH,
        )

        self.client.get_change_rate_ranking(market_type="101")
        self.client.request_api_page.assert_called_with(
            "ka10027",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": "101",
                "sort_tp": "1",
                "trde_qty_cnd": "0000",
                "stk_cnd": "0",
                "crd_cnd": "0",
                "updown_incls": "1",
                "pric_cnd": "0",
                "trde_prica_cnd": "0",
                "stex_tp": "3",
            },
            cont_yn=None,
            next_key=None,
            priority=ApiCallPriority.HIGH,
        )


if __name__ == "__main__":
    unittest.main()
