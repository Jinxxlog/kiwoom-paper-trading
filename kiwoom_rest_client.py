import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from data.api_budget import (
    ApiBudgetBlockedError,
    ApiBudgetDecision,
    ApiBudgetManager,
    ApiCallPriority,
    stock_code_from_body,
)


class KiwoomApiError(RuntimeError):
    pass


def _status_code_from_http_error(error: requests.HTTPError) -> Optional[int]:
    response = getattr(error, "response", None)
    if response is None:
        return None
    return getattr(response, "status_code", None)


@dataclass(frozen=True)
class ApiPage:
    data: Dict[str, Any]
    continuation: bool
    next_key: Optional[str]


class KiwoomRestClient:
    def __init__(
        self,
        app_key: str,
        secret_key: str,
        use_mock: bool = False,
        token_cache: Optional[Path] = None,
        timeout: float = 10.0,
        max_retries: int = 2,
        initial_token: Optional[str] = None,
        initial_expires_dt: Optional[str] = None,
        api_budget_manager: Optional[ApiBudgetManager] = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("API 제한 시간은 0초보다 커야 합니다.")
        if max_retries < 0:
            raise ValueError("API 재시도 횟수는 0 이상이어야 합니다.")
        self.host = "https://mockapi.kiwoom.com" if use_mock else "https://api.kiwoom.com"
        self.session = requests.Session()
        self.app_key = app_key
        self.secret_key = secret_key
        self.token_cache = token_cache
        self.timeout = timeout
        self.max_retries = max_retries
        self.token = initial_token
        self.expires_dt = initial_expires_dt
        self.api_budget_manager = api_budget_manager
        self._load_token_cache()

    def _load_token_cache(self) -> None:
        if self.token_cache is None or not self.token_cache.exists():
            return
        try:
            data = json.loads(self.token_cache.read_text(encoding="utf-8"))
            self.token = data.get("token")
            self.expires_dt = data.get("expires_dt")
        except json.JSONDecodeError:
            return

    def _save_token_cache(self, token_data: Dict[str, Any]) -> None:
        self.token = token_data.get("token")
        self.expires_dt = token_data.get("expires_dt")
        if self.token_cache is None:
            return
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache.write_text(json.dumps(token_data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _token_is_valid(self) -> bool:
        if not self.token or not self.expires_dt:
            return False
        try:
            expires_at = datetime.strptime(self.expires_dt, "%Y%m%d%H%M%S")
        except (TypeError, ValueError):
            return False
        return datetime.now() + timedelta(minutes=5) < expires_at

    def ensure_access_token(self) -> None:
        if not self._token_is_valid():
            self.request_access_token()

    @staticmethod
    def _validate_response(data: Dict[str, Any], api_id: str) -> Dict[str, Any]:
        return_code = data.get("return_code")
        if return_code is not None and return_code != 0:
            message = data.get("return_msg", "알 수 없는 오류")
            raise KiwoomApiError(f"{api_id} 요청 실패: {message} ({return_code})")
        return data

    def _post(
        self,
        url: str,
        *,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        retryable: bool,
    ) -> requests.Response:
        attempts = self.max_retries + 1 if retryable else 1
        for attempt in range(attempts):
            try:
                response = self.session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response
            except (requests.Timeout, requests.ConnectionError):
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError("API 요청 재시도 처리 중 예기치 않은 오류가 발생했습니다.")

    def request_access_token(self) -> Dict[str, Any]:
        endpoint = "/oauth2/token"
        url = self.host + endpoint
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.secret_key,
        }
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        decision = self._before_budget_call(
            api_id="au10001",
            endpoint=endpoint,
            stock_code=None,
            priority=ApiCallPriority.CRITICAL,
        )
        try:
            response = self._post(url, headers=headers, payload=payload, retryable=True)
        except requests.HTTPError as exc:
            status_code = _status_code_from_http_error(exc)
            self._after_budget_call(
                decision,
                status_code=status_code,
                reason="http_429_backoff" if status_code == 429 else "http_error",
            )
            raise
        self._after_budget_call(decision, status_code=response.status_code)
        token_data = self._validate_response(response.json(), "au10001")
        self._save_token_cache(token_data)
        return token_data

    def _get_headers(
        self,
        api_id: Optional[str] = None,
        *,
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
    ) -> Dict[str, str]:
        self.ensure_access_token()
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        if api_id:
            headers["api-id"] = api_id
        if cont_yn:
            headers["cont-yn"] = cont_yn
        if next_key:
            headers["next-key"] = next_key
        headers["authorization"] = f"Bearer {self.token}"
        return headers

    def request_api(
        self,
        api_id: str,
        url_path: str,
        body: Optional[Dict[str, Any]] = None,
        retryable: bool = True,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> Dict[str, Any]:
        return self.request_api_page(
            api_id,
            url_path,
            body,
            retryable=retryable,
            priority=priority,
        ).data

    def request_api_page(
        self,
        api_id: str,
        url_path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        retryable: bool = True,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        url = self.host + url_path
        decision = self._before_budget_call(
            api_id=api_id,
            endpoint=url_path,
            stock_code=stock_code_from_body(body),
            priority=priority,
        )
        headers = self._get_headers(
            api_id,
            cont_yn=cont_yn,
            next_key=next_key,
        )
        try:
            response = self._post(
                url,
                headers=headers,
                payload=body or {},
                retryable=retryable,
            )
        except requests.HTTPError as exc:
            status_code = _status_code_from_http_error(exc)
            self._after_budget_call(
                decision,
                status_code=status_code,
                reason="http_429_backoff" if status_code == 429 else "http_error",
            )
            raise
        self._after_budget_call(decision, status_code=response.status_code)
        data = self._validate_response(response.json(), api_id)
        response_cont_yn = response.headers.get("cont-yn", "").upper()
        response_next_key = response.headers.get("next-key")
        return ApiPage(
            data=data,
            continuation=response_cont_yn == "Y" and bool(response_next_key),
            next_key=response_next_key,
        )

    def _before_budget_call(
        self,
        *,
        api_id: str,
        endpoint: str,
        stock_code: Optional[str],
        priority: ApiCallPriority | str | int,
    ) -> Optional[ApiBudgetDecision]:
        if self.api_budget_manager is None:
            return None
        decision = self.api_budget_manager.before_call(
            api_id=api_id,
            endpoint=endpoint,
            stock_code=stock_code,
            priority=priority,
        )
        if decision.allowed:
            return decision
        raise ApiBudgetBlockedError(
            f"{api_id} 호출이 API 예산 정책으로 차단되었습니다: "
            f"{decision.reason}"
        )

    def _after_budget_call(
        self,
        decision: Optional[ApiBudgetDecision],
        *,
        status_code: Optional[int],
        reason: Optional[str] = None,
    ) -> None:
        if self.api_budget_manager is None or decision is None:
            return
        self.api_budget_manager.after_call(
            decision,
            status_code=status_code,
            reason=reason,
        )

    def get_account_number(self) -> Dict[str, Any]:
        return self.request_api(
            "ka00001",
            "/api/dostk/acnt",
            {},
            priority=ApiCallPriority.CRITICAL,
        )

    def get_hoga(
        self,
        stk_cd: str,
        *,
        priority: ApiCallPriority | str | int = ApiCallPriority.LOW,
    ) -> Dict[str, Any]:
        return self.request_api(
            "ka10004",
            "/api/dostk/mrkcond",
            {"stk_cd": stk_cd},
            priority=priority,
        )

    def get_trade_info(
        self,
        stk_cd: str,
        *,
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.LOW,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10003",
            "/api/dostk/stkinfo",
            {"stk_cd": stk_cd},
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_realtime_item_ranking(
        self,
        *,
        query_type: str = "1",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.HIGH,
    ) -> ApiPage:
        return self.request_api_page(
            "ka00198",
            "/api/dostk/stkinfo",
            {"qry_tp": str(query_type)},
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_change_rate_ranking(
        self,
        *,
        market_type: str = "000",
        sort_type: str = "1",
        trade_volume_condition: str = "0000",
        stock_condition: str = "0",
        credit_condition: str = "0",
        include_upper_lower: str = "1",
        price_condition: str = "0",
        trade_amount_condition: str = "0",
        exchange_type: str = "3",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.HIGH,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10027",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": market_type,
                "sort_tp": sort_type,
                "trde_qty_cnd": trade_volume_condition,
                "stk_cnd": stock_condition,
                "crd_cnd": credit_condition,
                "updown_incls": include_upper_lower,
                "pric_cnd": price_condition,
                "trde_prica_cnd": trade_amount_condition,
                "stex_tp": exchange_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_expected_contract_change_ranking(
        self,
        *,
        market_type: str = "000",
        sort_type: str = "1",
        trade_volume_condition: str = "0",
        stock_condition: str = "0",
        credit_condition: str = "0",
        price_condition: str = "0",
        exchange_type: str = "3",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.HIGH,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10029",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": market_type,
                "sort_tp": sort_type,
                "trde_qty_cnd": trade_volume_condition,
                "stk_cnd": stock_condition,
                "crd_cnd": credit_condition,
                "pric_cnd": price_condition,
                "stex_tp": exchange_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_trade_amount_ranking(
        self,
        *,
        market_type: str = "000",
        include_management: str = "0",
        exchange_type: str = "3",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.HIGH,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10032",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": market_type,
                "mang_stk_incls": include_management,
                "stex_tp": exchange_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_foreign_broker_ranking(
        self,
        *,
        market_type: str = "000",
        period: str = "0",
        trade_type: str = "1",
        sort_type: str = "1",
        exchange_type: str = "3",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10037",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": market_type,
                "dt": period,
                "trde_tp": trade_type,
                "sort_tp": sort_type,
                "stex_tp": exchange_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_today_main_brokers(
        self,
        stk_cd: str,
        *,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> Dict[str, Any]:
        return self.request_api(
            "ka10040",
            "/api/dostk/rkinfo",
            {"stk_cd": stk_cd},
            priority=priority,
        )

    def get_stock_investor_trend(
        self,
        *,
        stk_cd: str,
        date: str,
        amount_quantity_type: str = "1",
        trade_type: str = "0",
        unit_type: str = "1000",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10060",
            "/api/dostk/chart",
            {
                "dt": date,
                "stk_cd": stk_cd,
                "amt_qty_tp": amount_quantity_type,
                "trde_tp": trade_type,
                "unit_tp": unit_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_stock_investor_total(
        self,
        *,
        stk_cd: str,
        start_date: str,
        end_date: str,
        amount_quantity_type: str = "1",
        trade_type: str = "0",
        unit_type: str = "1000",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10061",
            "/api/dostk/stkinfo",
            {
                "stk_cd": stk_cd,
                "strt_dt": start_date,
                "end_dt": end_date,
                "amt_qty_tp": amount_quantity_type,
                "trde_tp": trade_type,
                "unit_tp": unit_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_intraday_investor_trend(
        self,
        *,
        stk_cd: str,
        market_type: str = "000",
        amount_quantity_type: str = "1",
        trade_type: str = "0",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10064",
            "/api/dostk/chart",
            {
                "mrkt_tp": market_type,
                "amt_qty_tp": amount_quantity_type,
                "trde_tp": trade_type,
                "stk_cd": stk_cd,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_stock_daily_program_trade(
        self,
        *,
        stk_cd: str,
        date: str = "",
        amount_quantity_type: str = "",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka90013",
            "/api/dostk/mrkcond",
            {
                "amt_qty_tp": amount_quantity_type,
                "stk_cd": stk_cd,
                "date": date,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_intraday_investor_top(
        self,
        *,
        trade_type: str = "1",
        market_type: str = "000",
        organization_type: str = "49000",
        amount_quantity_type: str = "1",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10065",
            "/api/dostk/rkinfo",
            {
                "trde_tp": trade_type,
                "mrkt_tp": market_type,
                "orgn_tp": organization_type,
                "amt_qty_tp": amount_quantity_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_theme_groups(
        self,
        *,
        query_type: str = "0",
        stock_code: str = "",
        date_type: str = "10",
        theme_name: str = "",
        fluctuation_amount_type: str = "1",
        exchange_type: str = "1",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
    ) -> ApiPage:
        return self.request_api_page(
            "ka90001",
            "/api/dostk/thme",
            {
                "qry_tp": query_type,
                "stk_cd": stock_code,
                "date_tp": date_type,
                "thema_nm": theme_name,
                "flu_pl_amt_tp": fluctuation_amount_type,
                "stex_tp": exchange_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )

    def get_theme_components(
        self,
        theme_code: str,
        *,
        date_type: str = "2",
        exchange_type: str = "1",
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
    ) -> ApiPage:
        return self.request_api_page(
            "ka90002",
            "/api/dostk/thme",
            {
                "date_tp": date_type,
                "thema_grp_cd": str(theme_code),
                "stex_tp": exchange_type,
            },
            cont_yn=cont_yn,
            next_key=next_key,
        )

    def get_daily_prices(
        self,
        stk_cd: str,
        *,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> Dict[str, Any]:
        return self.request_api(
            "ka10005",
            "/api/dostk/mrkcond",
            {"stk_cd": stk_cd},
            priority=priority,
        )

    def get_intraday_prices(
        self,
        stk_cd: str,
        *,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> Dict[str, Any]:
        return self.request_api(
            "ka10006",
            "/api/dostk/mrkcond",
            {"stk_cd": stk_cd},
            priority=priority,
        )

    def get_minute_chart(
        self,
        stk_cd: str,
        tic_scope: int,
        base_dt: str,
        *,
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10080",
            "/api/dostk/chart",
            {
                "stk_cd": stk_cd,
                "tic_scope": str(tic_scope),
                "upd_stkpc_tp": "1",
                "base_dt": base_dt,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_daily_chart(
        self,
        stk_cd: str,
        base_dt: str,
        *,
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka10081",
            "/api/dostk/chart",
            {
                "stk_cd": stk_cd,
                "base_dt": base_dt,
                "upd_stkpc_tp": "1",
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_sector_daily_chart(
        self,
        sector_code: str,
        base_dt: str,
        *,
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> ApiPage:
        return self.request_api_page(
            "ka20006",
            "/api/dostk/chart",
            {
                "inds_cd": sector_code,
                "base_dt": base_dt,
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )

    def get_deposit_details(
        self,
        qry_tp: str = "0",
        dmst_stex_tp: str = "KRX",
    ) -> Dict[str, Any]:
        return self.request_api(
            "kt00001",
            "/api/dostk/acnt",
            {"qry_tp": qry_tp, "dmst_stex_tp": dmst_stex_tp},
            priority=ApiCallPriority.CRITICAL,
        )

    def get_balance_details(self, dmst_stex_tp: str = "KRX") -> Dict[str, Any]:
        return self.request_api(
            "kt00005",
            "/api/dostk/acnt",
            {"dmst_stex_tp": dmst_stex_tp},
            priority=ApiCallPriority.CRITICAL,
        )

    def get_account_evaluation(
        self,
        qry_tp: str = "1",
        dmst_stex_tp: str = "KRX",
    ) -> Dict[str, Any]:
        return self.request_api(
            "kt00018",
            "/api/dostk/acnt",
            {"qry_tp": qry_tp, "dmst_stex_tp": dmst_stex_tp},
            priority=ApiCallPriority.CRITICAL,
        )

    def get_order_execution_details(
        self,
        order_date: str = "",
        *,
        cont_yn: Optional[str] = None,
        next_key: Optional[str] = None,
        priority: ApiCallPriority | str | int = ApiCallPriority.CRITICAL,
    ) -> ApiPage:
        return self.request_api_page(
            "kt00007",
            "/api/dostk/acnt",
            {
                "ord_dt": order_date,
                "qry_tp": "1",
                "stk_bond_tp": "0",
                "sell_tp": "0",
                "stk_cd": "",
                "fr_ord_no": "",
                "dmst_stex_tp": "%",
            },
            cont_yn=cont_yn,
            next_key=next_key,
            priority=priority,
        )
