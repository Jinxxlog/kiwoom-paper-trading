from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from data.watchlist import normalize_stock_code
from db.database import Database
from data.api_budget import ApiCallPriority
from kiwoom_rest_client import KiwoomRestClient
from operations.market_mode import classify_market_mode


SUPPLY_NORMALIZATION_VERSION = "1"
def _to_api_stock_code(stock_code: str) -> str:
    normalized = normalize_stock_code(stock_code)
    return normalized[1:] if normalized.startswith("A") else normalized


def _to_integer(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    normalized = _normalize_number_text(value)
    try:
        return abs(int(normalized))
    except ValueError:
        return None


def _to_signed_integer(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    normalized = _normalize_number_text(value)
    try:
        return int(normalized)
    except ValueError:
        return None


def _normalize_number_text(value: Any) -> str:
    normalized = str(value).strip().replace(",", "")
    while normalized.startswith("--"):
        normalized = "-" + normalized[2:]
    while normalized.startswith("++"):
        normalized = "+" + normalized[2:]
    return normalized


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    normalized = str(value).strip().replace(",", "")
    try:
        return float(normalized)
    except ValueError:
        return None


def _to_abs_float(value: Any) -> Optional[float]:
    parsed = _to_float(value)
    return abs(parsed) if parsed is not None else None


def _to_trade_amount_won(value: Any) -> Optional[int]:
    amount = _to_integer(value)
    if amount is None:
        return None
    if amount < 1_000_000_000:
        return amount * 1_000_000
    return amount


def _first_value(payload: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _call_with_priority(
    method: Any,
    *args: Any,
    priority: ApiCallPriority | str | int,
    **kwargs: Any,
) -> Any:
    try:
        return method(*args, **kwargs, priority=priority)
    except TypeError as exc:
        if "priority" not in str(exc):
            raise
        return method(*args, **kwargs)


def _supply_metadata(source: str, **extra: Any) -> Dict[str, Any]:
    return {
        "normalized_source": source,
        "normalization_version": SUPPLY_NORMALIZATION_VERSION,
        "signed_quantity": "positive=buy_or_net_buy, negative=sell_or_net_sell",
        **{key: value for key, value in extra.items() if value is not None},
    }


@dataclass(frozen=True)
class CollectedPrice:
    stock_code: str
    current_price: Optional[int]
    open_price: Optional[int]
    high_price: Optional[int]
    low_price: Optional[int]
    volume: Optional[int]
    database_id: int


@dataclass(frozen=True)
class CollectedCandles:
    stock_code: str
    candle_type: str
    interval_minutes: int
    received_count: int
    saved_count: int
    page_count: int
    has_more: bool = False


@dataclass(frozen=True)
class CollectedOrderBook:
    stock_code: str
    level_count: int
    database_id: int


@dataclass(frozen=True)
class CollectedTradeStrength:
    stock_code: str
    received_count: int
    saved_count: int
    page_count: int


@dataclass(frozen=True)
class CollectedThemes:
    group_count: int
    saved_group_count: int
    component_count: int
    saved_component_count: int
    page_count: int


@dataclass(frozen=True)
class CollectedStockThemes:
    stock_code: str
    group_count: int
    saved_group_count: int
    component_count: int
    saved_component_count: int
    page_count: int


@dataclass(frozen=True)
class CollectedRealtimeRankings:
    query_type: str
    received_count: int
    saved_count: int
    page_count: int


@dataclass(frozen=True)
class CollectedMarketRankings:
    rank_type: str
    api_id: str
    received_count: int
    saved_count: int
    page_count: int


@dataclass(frozen=True)
class CollectedMarketMode:
    market: str
    market_date: str
    index_value: float | None
    moving_average_20: float | None
    above_ma20: bool
    trade_amount: int | None
    mode: str
    reliability: str
    reason: str
    snapshot_id: int
    received_count: int


@dataclass(frozen=True)
class CollectedBrokerFlow:
    stock_code: str | None
    flow_type: str
    api_id: str
    received_count: int
    saved_count: int
    page_count: int = 1


@dataclass(frozen=True)
class CollectedInvestorFlow:
    stock_code: str | None
    flow_type: str
    api_id: str
    received_count: int
    saved_count: int
    page_count: int


@dataclass(frozen=True)
class CollectedProgramTrade:
    stock_code: str
    api_id: str
    received_count: int
    saved_count: int
    page_count: int


@dataclass(frozen=True)
class CollectedSupplySources:
    stock_code: str
    broker: CollectedBrokerFlow
    investor_total: CollectedInvestorFlow
    intraday_investor: CollectedInvestorFlow
    foreign_broker_ranking: CollectedBrokerFlow | None
    program_trade: CollectedProgramTrade


MARKET_RANK_CONFIGS: dict[str, dict[str, str]] = {
    "realtime_inquiry": {
        "api_id": "ka00198",
        "list_key": "item_inq_rank",
    },
    "expected_change": {
        "api_id": "ka10029",
        "list_key": "exp_cntr_flu_rt_upper",
    },
    "trade_amount": {
        "api_id": "ka10032",
        "list_key": "trde_prica_upper",
    },
    "change_rate": {
        "api_id": "ka10027",
        "list_key": "pred_pre_flu_rt_upper",
    },
}

KOSDAQ_SECTOR_CODE = "101"
KOSDAQ_MARKET = "KOSDAQ"
SECTOR_DAILY_LIST_KEYS = (
    "inds_dt_pole_chart_qry",
    "inds_daily_chart_qry",
    "sect_daily_chart_qry",
    "upjong_daily_chart_qry",
    "stk_dt_pole_chart_qry",
)


def _parse_candle_time(value: Any, format_string: str) -> str:
    if value is None:
        raise ValueError("캔들 시각 필드가 없습니다.")
    parsed = datetime.strptime(str(value), format_string)
    return parsed.isoformat(timespec="seconds")


def _parse_yyyymmdd(value: Any) -> str:
    if value is None:
        raise ValueError("날짜 필드가 없습니다.")
    return datetime.strptime(str(value)[:8], "%Y%m%d").date().isoformat()


def _sector_daily_rows(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    for key in SECTOR_DAILY_LIST_KEYS:
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _normalize_sector_daily_row(row: Dict[str, Any]) -> Dict[str, Any]:
    market_date = _parse_yyyymmdd(
        _first_value(row, ("dt", "date", "trde_dt", "base_dt", "일자"))
    )
    index_value = _to_abs_float(
        _first_value(
            row,
            (
                "cur_prc",
                "close_pric",
                "close_price",
                "inds_prc",
                "now_pric",
                "종가",
                "현재가",
            ),
        )
    )
    trade_amount = _to_trade_amount_won(
        _first_value(
            row,
            (
                "trde_prica",
                "acc_trde_prica",
                "trade_amount",
                "acc_trade_amount",
                "거래대금",
            ),
        )
    )
    return {
        "market_date": market_date,
        "index_value": index_value,
        "trade_amount": trade_amount,
        "raw": row,
    }


def _normalize_theme_group(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "theme_code": row.get("thema_grp_cd") or row.get("theme_code"),
        "theme_name": row.get("thema_nm") or row.get("theme_name") or "",
        "stock_count": _to_integer(row.get("stk_num")),
        "change_rate": _to_float(row.get("flu_rt")),
        "rising_stock_count": _to_integer(row.get("rising_stk_num")),
        "falling_stock_count": _to_integer(row.get("fall_stk_num")),
        "day_profit_rate": _to_float(row.get("dt_prft_rt")),
        "main_stock": row.get("main_stk"),
    }


def _normalize_theme_component(row: Dict[str, Any]) -> Dict[str, Any]:
    stock_code = row.get("stk_cd") or row.get("stock_code")
    return {
        "stock_code": _normalize_api_stock_code(stock_code),
        "stock_name": row.get("stk_nm") or row.get("stock_name"),
        "current_price": _to_integer(row.get("cur_prc")),
        "change_rate": _to_float(row.get("flu_rt")),
        "accumulated_volume": _to_integer(row.get("acc_trde_qty")),
    }


def _normalize_realtime_ranking(
    row: Dict[str, Any],
    query_type: str,
) -> Dict[str, Any]:
    stock_code = row.get("stk_cd") or row.get("stock_code")
    return {
        "query_type": str(query_type),
        "stock_code": _normalize_api_stock_code(stock_code),
        "stock_name": row.get("stk_nm") or row.get("stock_name"),
        "big_data_rank": _to_integer(row.get("bigd_rank")),
        "rank_change": _to_signed_integer(row.get("rank_chg")),
        "rank_change_sign": row.get("rank_chg_sign"),
        "past_current_price": _to_integer(row.get("past_curr_prc")),
        "base_compare_sign": row.get("base_comp_sign"),
        "base_compare_change_rate": _to_float(row.get("base_comp_chgr")),
        "previous_base_sign": row.get("prev_base_sign"),
        "previous_base_change_rate": _to_float(row.get("prev_base_chgr")),
        "ranking_date": row.get("dt"),
        "ranking_time": row.get("tm"),
    }


def _normalize_market_rank_item(
    row: Dict[str, Any],
    rank_type: str,
    index: int,
) -> Dict[str, Any]:
    stock_code = row.get("stk_cd") or row.get("stock_code")
    rank = _to_integer(
        row.get("now_rank")
        if rank_type == "trade_amount"
        else row.get("bigd_rank")
    )
    if rank is None:
        rank = index + 1
    trade_amount = _to_integer(row.get("trde_prica"))
    if trade_amount is not None:
        trade_amount *= 1_000_000
    current_price = _to_integer(
        _first_value(
            row,
            (
                "cur_prc",
                "past_curr_prc",
                "exp_cntr_pric",
                "current_price",
            ),
        )
    )
    return {
        "stock_code": _normalize_api_stock_code(stock_code),
        "stock_name": row.get("stk_nm") or row.get("stock_name"),
        "rank": rank,
        "previous_rank": _to_integer(row.get("pred_rank")),
        "rank_change": _to_signed_integer(row.get("rank_chg")),
        "current_price": current_price,
        "change_price": _to_signed_integer(row.get("pred_pre")),
        "change_rate": _to_float(
            _first_value(row, ("flu_rt", "base_comp_chgr"))
        ),
        "trade_volume": _to_integer(
            _first_value(row, ("now_trde_qty", "acc_trde_qty"))
        ),
        "previous_trade_volume": _to_integer(row.get("pred_trde_qty")),
        "trade_amount": trade_amount,
        "expected_price": _to_integer(row.get("exp_cntr_pric")),
        "expected_volume": _to_integer(row.get("exp_cntr_qty")),
        "sell_quantity": _to_integer(row.get("sel_req")),
        "buy_quantity": _to_integer(row.get("buy_req")),
        "trade_strength": _to_float(row.get("cntr_str")),
        "metadata": {
            "rank_type": rank_type,
            "base_price": _to_integer(row.get("base_pric")),
            "sell_quote": _to_integer(row.get("sel_bid")),
            "buy_quote": _to_integer(row.get("buy_bid")),
            "compare_sign": _first_value(
                row,
                ("pred_pre_sig", "base_comp_sign"),
            ),
            "ranking_date": row.get("dt"),
            "ranking_time": row.get("tm"),
            "raw": row,
        },
    }


def _normalize_api_stock_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if "_" in text:
        text = text.split("_", 1)[0]
    return normalize_stock_code(text)


def _normalize_main_broker_items(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for rank in range(1, 6):
        sell_name = payload.get(f"sel_trde_ori_{rank}")
        sell_code = payload.get(f"sel_trde_ori_cd_{rank}")
        if sell_name or sell_code:
            items.append(
                {
                    "side": "sell",
                    "rank": rank,
                    "broker_code": sell_code,
                    "broker_name": sell_name,
                    "quantity": _to_signed_integer(
                        payload.get(f"sel_trde_ori_qty_{rank}")
                    ),
                    "quantity_change": _to_signed_integer(
                        payload.get(f"sel_trde_ori_irds_{rank}")
                    ),
                    "metadata": _supply_metadata(
                        "ka10040.today_main_brokers",
                        unit="shares",
                        broker_side="sell",
                    ),
                }
            )
        buy_name = payload.get(f"buy_trde_ori_{rank}")
        buy_code = payload.get(f"buy_trde_ori_cd_{rank}")
        if buy_name or buy_code:
            items.append(
                {
                    "side": "buy",
                    "rank": rank,
                    "broker_code": buy_code,
                    "broker_name": buy_name,
                    "quantity": _to_signed_integer(
                        payload.get(f"buy_trde_ori_qty_{rank}")
                    ),
                    "quantity_change": _to_signed_integer(
                        payload.get(f"buy_trde_ori_irds_{rank}")
                    ),
                    "metadata": _supply_metadata(
                        "ka10040.today_main_brokers",
                        unit="shares",
                        broker_side="buy",
                    ),
                }
            )
    for row in payload.get("tdy_main_trde_ori") or []:
        if not isinstance(row, dict):
            continue
        if row.get("sel_upper_scesn_ori"):
            items.append(
                {
                    "side": "sell_escape",
                    "broker_name": row.get("sel_upper_scesn_ori"),
                    "quantity": _to_signed_integer(row.get("sell_qty")),
                    "metadata": _supply_metadata(
                        "ka10040.today_main_brokers",
                        unit="shares",
                        broker_side="sell_escape",
                        time=row.get("sel_scesn_tm"),
                    ),
                }
            )
        if row.get("buy_upper_scesn_ori"):
            items.append(
                {
                    "side": "buy_escape",
                    "broker_name": row.get("buy_upper_scesn_ori"),
                    "quantity": _to_signed_integer(row.get("buy_qty")),
                    "metadata": _supply_metadata(
                        "ka10040.today_main_brokers",
                        unit="shares",
                        broker_side="buy_escape",
                        time=row.get("buy_scesn_tm"),
                    ),
                }
            )
    return items


def _normalize_foreign_broker_rank_item(
    row: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:
    stock_code = row.get("stk_cd") or row.get("stock_code")
    return {
        "stock_code": _normalize_api_stock_code(stock_code),
        "stock_name": row.get("stk_nm") or row.get("stock_name"),
        "side": "foreign_net",
        "rank": _to_integer(row.get("rank")) or index + 1,
        "quantity": _to_signed_integer(row.get("netprps_trde_qty")),
        "amount": _to_signed_integer(row.get("netprps_prica")),
        "metadata": _supply_metadata(
            "ka10037.foreign_broker_ranking",
            unit="api_default",
            current_price=_to_integer(row.get("cur_prc")),
            change_price=_to_signed_integer(row.get("pred_pre")),
            change_rate=_to_float(row.get("flu_rt")),
            sell_quantity=_to_signed_integer(row.get("sel_trde_qty")),
            buy_quantity=_to_signed_integer(row.get("buy_trde_qty")),
            trade_volume=_to_integer(row.get("trde_qty")),
            trade_amount=_to_integer(row.get("trde_prica")),
        ),
    }


def _normalize_investor_flow_item(
    row: Dict[str, Any],
    *,
    stock_code: str | None,
    source: str,
    amount_quantity_type: str,
    unit_type: str | None = None,
) -> Dict[str, Any]:
    row_stock_code = row.get("stk_cd") or row.get("stock_code") or stock_code
    return {
        "stock_code": _normalize_api_stock_code(row_stock_code),
        "stock_name": row.get("stk_nm") or row.get("stock_name"),
        "observed_key": row.get("dt") or row.get("tm"),
        "current_price": _to_integer(row.get("cur_prc")),
        "change_price": _to_signed_integer(row.get("pred_pre")),
        "change_rate": _to_float(row.get("flu_rt")),
        "trade_volume": _to_integer(row.get("acc_trde_qty")),
        "trade_amount": _to_integer(row.get("acc_trde_prica")),
        "individual": _to_signed_integer(row.get("ind_invsr")),
        "foreigner": _to_signed_integer(row.get("frgnr_invsr")),
        "institution": _to_signed_integer(row.get("orgn")),
        "finance": _to_signed_integer(row.get("fnnc_invt")),
        "insurance": _to_signed_integer(row.get("insrnc")),
        "trust": _to_signed_integer(row.get("invtrt")),
        "other_finance": _to_signed_integer(row.get("etc_fnnc")),
        "bank": _to_signed_integer(row.get("bank")),
        "pension": _to_signed_integer(row.get("penfnd_etc")),
        "private_fund": _to_signed_integer(row.get("samo_fund")),
        "nation": _to_signed_integer(row.get("natn")),
        "other_corp": _to_signed_integer(row.get("etc_corp")),
        "natfor": _to_signed_integer(row.get("natfor")),
        "sell_value": _to_signed_integer(row.get("sel_qty")),
        "buy_value": _to_signed_integer(row.get("buy_qty")),
        "net_value": _to_signed_integer(row.get("netslmt")),
        "metadata": _supply_metadata(
            source,
            unit="api_default",
            amount_quantity_type=amount_quantity_type,
            unit_type=unit_type,
        ),
    }


def _normalize_program_trade_item(
    row: Dict[str, Any],
    *,
    stock_code: str,
) -> Dict[str, Any]:
    return {
        "stock_code": stock_code,
        "observed_date": row.get("dt"),
        "current_price": _to_integer(row.get("cur_prc")),
        "change_price": _to_signed_integer(row.get("pred_pre")),
        "change_rate": _to_float(row.get("flu_rt")),
        "trade_volume": _to_integer(row.get("trde_qty")),
        "sell_amount": _to_integer(row.get("prm_sell_amt")),
        "buy_amount": _to_integer(row.get("prm_buy_amt")),
        "net_buy_amount": _to_signed_integer(row.get("prm_netprps_amt")),
        "net_buy_amount_change": _to_signed_integer(
            row.get("prm_netprps_amt_irds")
        ),
        "sell_quantity": _to_integer(row.get("prm_sell_qty")),
        "buy_quantity": _to_integer(row.get("prm_buy_qty")),
        "net_buy_quantity": _to_signed_integer(row.get("prm_netprps_qty")),
        "net_buy_quantity_change": _to_signed_integer(
            row.get("prm_netprps_qty_irds")
        ),
        "base_price_time": row.get("base_pric_tm"),
        "exchange_type": row.get("stex_tp"),
        "metadata": _supply_metadata(
            "ka90013.stock_daily_program_trade",
            unit="amount=KRW_million,quantity=shares",
        ),
    }


class MarketDataCollector:
    def __init__(self, client: KiwoomRestClient, database: Database) -> None:
        self.client = client
        self.database = database

    def collect_current_price(
        self,
        stock_code: str,
        *,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> CollectedPrice:
        payload = _call_with_priority(
            self.client.get_intraday_prices,
            stock_code,
            priority=priority,
        )
        current_price = _to_integer(
            _first_value(
                payload,
                ("cur_prc", "close_pric", "stk_prpr", "current_price", "현재가"),
            )
        )
        open_price = _to_integer(
            _first_value(payload, ("open_pric", "open_price", "시가"))
        )
        high_price = _to_integer(
            _first_value(payload, ("high_pric", "high_price", "고가"))
        )
        low_price = _to_integer(
            _first_value(payload, ("low_pric", "low_price", "저가"))
        )
        volume = _to_integer(
            _first_value(payload, ("trde_qty", "acml_vol", "volume", "거래량"))
        )

        database_id = self.database.save_price_snapshot(
            "ka10006",
            stock_code,
            payload,
            current_price=current_price,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            volume=volume,
        )
        return CollectedPrice(
            stock_code=stock_code,
            current_price=current_price,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            volume=volume,
            database_id=database_id,
        )

    def collect_order_book(
        self,
        stock_code: str,
        *,
        priority: ApiCallPriority | str | int = ApiCallPriority.LOW,
    ) -> CollectedOrderBook:
        payload = _call_with_priority(
            self.client.get_hoga,
            stock_code,
            priority=priority,
        )
        levels = []
        for level in range(1, 11):
            ask_price_key = (
                "sel_fpr_bid" if level == 1 else f"sel_{level}th_pre_bid"
            )
            ask_quantity_key = (
                "sel_fpr_req" if level == 1 else f"sel_{level}th_pre_req"
            )
            bid_price_key = (
                "buy_fpr_bid" if level == 1 else f"buy_{level}th_pre_bid"
            )
            bid_quantity_key = (
                "buy_fpr_req" if level == 1 else f"buy_{level}th_pre_req"
            )
            levels.extend(
                [
                    {
                        "side": "ask",
                        "level": level,
                        "price": _to_integer(payload.get(ask_price_key)),
                        "quantity": _to_integer(payload.get(ask_quantity_key)),
                        "quantity_change": _to_signed_integer(
                            payload.get(f"sel_{level}th_pre_req_pre")
                        ),
                    },
                    {
                        "side": "bid",
                        "level": level,
                        "price": _to_integer(payload.get(bid_price_key)),
                        "quantity": _to_integer(payload.get(bid_quantity_key)),
                        "quantity_change": _to_signed_integer(
                            payload.get(f"buy_{level}th_pre_req_pre")
                        ),
                    },
                ]
            )
        database_id = self.database.save_order_book(
            stock_code,
            payload,
            levels,
            quote_time=str(payload.get("bid_req_base_tm") or "") or None,
            best_ask_price=_to_integer(payload.get("sel_fpr_bid")),
            best_ask_quantity=_to_integer(payload.get("sel_fpr_req")),
            best_bid_price=_to_integer(payload.get("buy_fpr_bid")),
            best_bid_quantity=_to_integer(payload.get("buy_fpr_req")),
            total_ask_quantity=_to_integer(payload.get("tot_sel_req")),
            total_bid_quantity=_to_integer(payload.get("tot_buy_req")),
        )
        return CollectedOrderBook(stock_code, len(levels), database_id)

    def collect_trade_strength(
        self,
        stock_code: str,
        *,
        max_pages: int = 1,
        priority: ApiCallPriority | str | int = ApiCallPriority.LOW,
    ) -> CollectedTradeStrength:
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")

        received_count = 0
        saved_count = 0
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        collection_date = datetime.now().strftime("%Y%m%d")

        while page_count < max_pages:
            page = _call_with_priority(
                self.client.get_trade_info,
                stock_code,
                cont_yn=cont_yn,
                next_key=next_key,
                priority=priority,
            )
            rows = page.data.get("cntr_infr", [])
            if not isinstance(rows, list):
                raise ValueError("ka10003 응답의 cntr_infr가 목록이 아닙니다.")
            trades = []
            for row in rows:
                raw_time = str(row.get("tm") or "")
                if len(raw_time) != 6 or not raw_time.isdigit():
                    continue
                trade_time = datetime.strptime(
                    collection_date + raw_time,
                    "%Y%m%d%H%M%S",
                ).isoformat(timespec="seconds")
                trades.append(
                    {
                        "trade_time": trade_time,
                        "current_price": _to_integer(row.get("cur_prc")),
                        "trade_quantity": _to_signed_integer(
                            row.get("cntr_trde_qty")
                        ),
                        "accumulated_volume": _to_integer(
                            row.get("acc_trde_qty")
                        ),
                        "accumulated_amount": _to_integer(
                            row.get("acc_trde_prica")
                        ),
                        "strength": _to_float(row.get("cntr_str")),
                        "ask_price": _to_integer(row.get("pri_sel_bid_unit")),
                        "bid_price": _to_integer(row.get("pri_buy_bid_unit")),
                        "exchange_type": str(row.get("stex_tp") or ""),
                    }
                )
            saved_count += self.database.save_trade_strength(
                stock_code,
                page.data,
                trades,
            )
            received_count += len(trades)
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        return CollectedTradeStrength(
            stock_code,
            received_count,
            saved_count,
            page_count,
        )

    def collect_theme_snapshot(
        self,
        *,
        top_groups: int = 10,
        date_type: str = "10",
        component_date_type: str = "2",
        max_pages: int = 1,
    ) -> CollectedThemes:
        if top_groups < 1:
            raise ValueError("테마 그룹 수는 1 이상이어야 합니다.")
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")

        payloads = []
        groups = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_theme_groups(
                date_type=date_type,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            payloads.append(page.data)
            rows = page.data.get("thema_grp", [])
            if not isinstance(rows, list):
                raise ValueError("ka90001 응답의 thema_grp가 목록이 아닙니다.")
            groups.extend(_normalize_theme_group(row) for row in rows)
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        groups.sort(
            key=lambda row: (
                -(row.get("change_rate") or 0),
                -(row.get("day_profit_rate") or 0),
                str(row.get("theme_code") or ""),
            )
        )
        selected_groups = groups[:top_groups]
        saved_group_count = self.database.save_theme_groups(
            {"pages": payloads},
            selected_groups,
        )
        component_count = 0
        saved_component_count = 0
        for group in selected_groups:
            theme_code = str(group["theme_code"])
            component_page = self.client.get_theme_components(
                theme_code,
                date_type=component_date_type,
            )
            rows = component_page.data.get("thema_comp_stk", [])
            if not isinstance(rows, list):
                raise ValueError("ka90002 응답의 thema_comp_stk가 목록이 아닙니다.")
            components = [_normalize_theme_component(row) for row in rows]
            component_count += len(components)
            saved_component_count += self.database.save_theme_components(
                theme_code,
                component_page.data,
                components,
            )

        return CollectedThemes(
            group_count=len(selected_groups),
            saved_group_count=saved_group_count,
            component_count=component_count,
            saved_component_count=saved_component_count,
            page_count=page_count,
        )

    def collect_stock_theme_snapshot(
        self,
        stock_code: str,
        *,
        date_type: str = "10",
        component_date_type: str = "10",
        max_pages: int = 1,
        include_components: bool = True,
    ) -> CollectedStockThemes:
        normalized_code = normalize_stock_code(stock_code)
        api_stock_code = normalized_code[1:] if normalized_code.startswith("A") else normalized_code
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")

        payloads = []
        groups = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_theme_groups(
                query_type="2",
                stock_code=api_stock_code,
                date_type=date_type,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            payloads.append(page.data)
            rows = page.data.get("thema_grp", [])
            if not isinstance(rows, list):
                raise ValueError("ka90001 응답의 thema_grp가 목록이 아닙니다.")
            groups.extend(_normalize_theme_group(row) for row in rows)
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        saved_group_count = self.database.save_theme_groups(
            {"stock_code": normalized_code, "pages": payloads},
            groups,
        )
        component_count = 0
        saved_component_count = 0
        if include_components:
            for group in groups:
                theme_code = str(group["theme_code"])
                component_page = self.client.get_theme_components(
                    theme_code,
                    date_type=component_date_type,
                )
                rows = component_page.data.get("thema_comp_stk", [])
                if not isinstance(rows, list):
                    raise ValueError("ka90002 응답의 thema_comp_stk가 목록이 아닙니다.")
                components = [_normalize_theme_component(row) for row in rows]
                component_count += len(components)
                saved_component_count += self.database.save_theme_components(
                    theme_code,
                    component_page.data,
                    components,
                )

        return CollectedStockThemes(
            stock_code=normalized_code,
            group_count=len(groups),
            saved_group_count=saved_group_count,
            component_count=component_count,
            saved_component_count=saved_component_count,
            page_count=page_count,
        )

    def collect_realtime_item_ranking(
        self,
        *,
        query_type: str = "1",
        max_pages: int = 1,
    ) -> CollectedRealtimeRankings:
        if query_type not in {"1", "2", "3", "4", "5"}:
            raise ValueError("실시간 조회종목 순위 구분은 1~5여야 합니다.")
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")

        payloads = []
        rankings = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_realtime_item_ranking(
                query_type=query_type,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            payloads.append(page.data)
            rows = page.data.get("item_inq_rank", [])
            if not isinstance(rows, list):
                raise ValueError("ka00198 응답의 item_inq_rank가 목록이 아닙니다.")
            rankings.extend(
                _normalize_realtime_ranking(row, query_type)
                for row in rows
            )
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        saved_count = self.database.save_realtime_item_rankings(
            query_type,
            {"pages": payloads},
            rankings,
        )
        rank_items = []
        for page_payload in payloads:
            rows = page_payload.get("item_inq_rank", [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                rank_items.append(
                    _normalize_market_rank_item(
                        row,
                        "realtime_inquiry",
                        len(rank_items),
                    )
                )
        self.database.save_market_rank_snapshot(
            rank_type="realtime_inquiry",
            api_id="ka00198",
            market_type="000",
            query_params={"qry_tp": query_type},
            payload={"pages": payloads},
            items=rank_items,
        )
        return CollectedRealtimeRankings(
            query_type=query_type,
            received_count=len(rankings),
            saved_count=saved_count,
            page_count=page_count,
        )

    def collect_market_ranking(
        self,
        rank_type: str,
        *,
        market_type: str = "000",
        max_pages: int = 1,
    ) -> CollectedMarketRankings:
        if rank_type not in MARKET_RANK_CONFIGS:
            raise ValueError("지원하지 않는 시장 순위 유형입니다.")
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")
        config = MARKET_RANK_CONFIGS[rank_type]
        payloads = []
        items = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            if rank_type == "realtime_inquiry":
                page = self.client.get_realtime_item_ranking(
                    query_type="1",
                    cont_yn=cont_yn,
                    next_key=next_key,
                )
                query_params = {"qry_tp": "1"}
            elif rank_type == "expected_change":
                page = self.client.get_expected_contract_change_ranking(
                    market_type=market_type,
                    cont_yn=cont_yn,
                    next_key=next_key,
                )
                query_params = {"mrkt_tp": market_type, "sort_tp": "1"}
            elif rank_type == "trade_amount":
                page = self.client.get_trade_amount_ranking(
                    market_type=market_type,
                    cont_yn=cont_yn,
                    next_key=next_key,
                )
                query_params = {"mrkt_tp": market_type, "mang_stk_incls": "0"}
            else:
                page = self.client.get_change_rate_ranking(
                    market_type=market_type,
                    cont_yn=cont_yn,
                    next_key=next_key,
                )
                query_params = {"mrkt_tp": market_type, "sort_tp": "1"}
            payloads.append(page.data)
            rows = page.data.get(config["list_key"], [])
            if not isinstance(rows, list):
                raise ValueError(
                    f"{config['api_id']} 응답의 {config['list_key']}가 목록이 아닙니다."
                )
            items.extend(
                _normalize_market_rank_item(row, rank_type, len(items) + index)
                for index, row in enumerate(rows)
            )
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        saved_count = self.database.save_market_rank_snapshot(
            rank_type=rank_type,
            api_id=config["api_id"],
            market_type=market_type,
            query_params=query_params,
            payload={"pages": payloads},
            items=items,
        )
        return CollectedMarketRankings(
            rank_type=rank_type,
            api_id=config["api_id"],
            received_count=len(items),
            saved_count=saved_count,
            page_count=page_count,
        )

    def collect_kosdaq_market_mode(
        self,
        *,
        base_date: str,
        max_pages: int = 1,
    ) -> CollectedMarketMode:
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")

        payloads: list[Dict[str, Any]] = []
        rows: list[Dict[str, Any]] = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = _call_with_priority(
                self.client.get_sector_daily_chart,
                KOSDAQ_SECTOR_CODE,
                base_date,
                cont_yn=cont_yn,
                next_key=next_key,
                priority=ApiCallPriority.NORMAL,
            )
            payloads.append(page.data)
            rows.extend(_sector_daily_rows(page.data))
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        normalized = [_normalize_sector_daily_row(row) for row in rows]
        normalized = [
            row
            for row in normalized
            if row.get("market_date") and row.get("index_value") is not None
        ]
        normalized.sort(key=lambda item: str(item["market_date"]))
        latest = normalized[-1] if normalized else None
        closes = [
            float(row["index_value"])
            for row in normalized[-20:]
            if row.get("index_value") is not None
        ]
        ma20 = sum(closes) / len(closes) if len(closes) >= 20 else None
        index_value = float(latest["index_value"]) if latest is not None else None
        trade_amount = (
            int(latest["trade_amount"])
            if latest is not None and latest.get("trade_amount") is not None
            else None
        )
        above_ma20 = (
            bool(index_value is not None and ma20 is not None and index_value >= ma20)
            if ma20 is not None
            else False
        )
        mode, reliability, reason = classify_market_mode(
            above_ma20=above_ma20 if ma20 is not None else None,
            trade_amount=trade_amount,
        )
        market_date = (
            str(latest["market_date"])
            if latest is not None
            else datetime.strptime(base_date, "%Y%m%d").date().isoformat()
        )
        snapshot_id = self.database.save_market_mode_snapshot(
            market=KOSDAQ_MARKET,
            market_date=market_date,
            index_value=index_value,
            moving_average_20=ma20,
            above_ma20=above_ma20,
            trade_amount=trade_amount,
            mode=mode,
            reliability=reliability,
            reason=reason,
            source="ka20006.kosdaq_daily_chart",
            details={
                "sector_code": KOSDAQ_SECTOR_CODE,
                "base_date": base_date,
                "page_count": page_count,
                "received_count": len(rows),
                "normalized_count": len(normalized),
                "trade_amount_unit": "won",
                "risk_on_trade_amount": 10_000_000_000_000,
                "risk_off_trade_amount": 5_000_000_000_000,
            },
            payload={"pages": payloads},
            api_id="ka20006",
        )
        return CollectedMarketMode(
            market=KOSDAQ_MARKET,
            market_date=market_date,
            index_value=index_value,
            moving_average_20=ma20,
            above_ma20=above_ma20,
            trade_amount=trade_amount,
            mode=mode,
            reliability=reliability,
            reason=reason,
            snapshot_id=snapshot_id,
            received_count=len(rows),
        )

    def collect_today_main_brokers(self, stock_code: str) -> CollectedBrokerFlow:
        normalized_code = normalize_stock_code(stock_code)
        api_stock_code = _to_api_stock_code(normalized_code)
        payload = self.client.get_today_main_brokers(api_stock_code)
        items = _normalize_main_broker_items(payload)
        saved_count = self.database.save_broker_flow_snapshot(
            stock_code=normalized_code,
            api_id="ka10040",
            flow_type="today_main_brokers",
            query_params={"stk_cd": api_stock_code},
            payload=payload,
            items=items,
            foreign_sell_estimated=_to_signed_integer(
                payload.get("frgn_sel_prsm_sum")
            ),
            foreign_buy_estimated=_to_signed_integer(
                payload.get("frgn_buy_prsm_sum")
            ),
        )
        return CollectedBrokerFlow(
            stock_code=normalized_code,
            flow_type="today_main_brokers",
            api_id="ka10040",
            received_count=len(items),
            saved_count=saved_count,
        )

    def collect_foreign_broker_ranking(
        self,
        *,
        market_type: str = "000",
        period: str = "0",
        trade_type: str = "1",
        sort_type: str = "1",
        max_pages: int = 1,
    ) -> CollectedBrokerFlow:
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")
        payloads = []
        items = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_foreign_broker_ranking(
                market_type=market_type,
                period=period,
                trade_type=trade_type,
                sort_type=sort_type,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            payloads.append(page.data)
            rows = page.data.get("frgn_wicket_trde_upper", [])
            if not isinstance(rows, list):
                raise ValueError("ka10037 응답의 frgn_wicket_trde_upper가 목록이 아닙니다.")
            items.extend(
                _normalize_foreign_broker_rank_item(row, len(items) + index)
                for index, row in enumerate(rows)
            )
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key
        saved_count = self.database.save_broker_flow_snapshot(
            stock_code=None,
            api_id="ka10037",
            flow_type="foreign_broker_ranking",
            query_params={
                "mrkt_tp": market_type,
                "dt": period,
                "trde_tp": trade_type,
                "sort_tp": sort_type,
            },
            payload={"pages": payloads},
            items=items,
        )
        return CollectedBrokerFlow(
            stock_code=None,
            flow_type="foreign_broker_ranking",
            api_id="ka10037",
            received_count=len(items),
            saved_count=saved_count,
            page_count=page_count,
        )

    def collect_stock_investor_total(
        self,
        stock_code: str,
        *,
        start_date: str,
        end_date: str,
        amount_quantity_type: str = "1",
        unit_type: str = "1000",
        max_pages: int = 1,
    ) -> CollectedInvestorFlow:
        normalized_code = normalize_stock_code(stock_code)
        api_stock_code = _to_api_stock_code(normalized_code)
        payloads = []
        items = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_stock_investor_total(
                stk_cd=api_stock_code,
                start_date=start_date,
                end_date=end_date,
                amount_quantity_type=amount_quantity_type,
                trade_type="0",
                unit_type=unit_type,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            payloads.append(page.data)
            rows = page.data.get("stk_invsr_orgn_tot", [])
            if not isinstance(rows, list):
                raise ValueError("ka10061 응답의 stk_invsr_orgn_tot가 목록이 아닙니다.")
            items.extend(
                _normalize_investor_flow_item(
                    row,
                    stock_code=normalized_code,
                    source="ka10061.stock_investor_total",
                    amount_quantity_type=amount_quantity_type,
                    unit_type=unit_type,
                )
                for row in rows
            )
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key
        saved_count = self.database.save_investor_flow_snapshot(
            stock_code=normalized_code,
            api_id="ka10061",
            flow_type="stock_investor_total",
            market_type=None,
            query_params={
                "stk_cd": api_stock_code,
                "strt_dt": start_date,
                "end_dt": end_date,
                "amt_qty_tp": amount_quantity_type,
                "trde_tp": "0",
                "unit_tp": unit_type,
            },
            payload={"pages": payloads},
            items=items,
        )
        return CollectedInvestorFlow(
            stock_code=normalized_code,
            flow_type="stock_investor_total",
            api_id="ka10061",
            received_count=len(items),
            saved_count=saved_count,
            page_count=page_count,
        )

    def collect_intraday_investor_trend(
        self,
        stock_code: str,
        *,
        market_type: str = "000",
        amount_quantity_type: str = "1",
        max_pages: int = 1,
    ) -> CollectedInvestorFlow:
        normalized_code = normalize_stock_code(stock_code)
        api_stock_code = _to_api_stock_code(normalized_code)
        payloads = []
        items = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_intraday_investor_trend(
                stk_cd=api_stock_code,
                market_type=market_type,
                amount_quantity_type=amount_quantity_type,
                trade_type="0",
                cont_yn=cont_yn,
                next_key=next_key,
            )
            payloads.append(page.data)
            rows = page.data.get("opmr_invsr_trde_chart", [])
            if not isinstance(rows, list):
                raise ValueError("ka10064 응답의 opmr_invsr_trde_chart가 목록이 아닙니다.")
            items.extend(
                _normalize_investor_flow_item(
                    row,
                    stock_code=normalized_code,
                    source="ka10064.intraday_investor_trend",
                    amount_quantity_type=amount_quantity_type,
                )
                for row in rows
            )
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key
        saved_count = self.database.save_investor_flow_snapshot(
            stock_code=normalized_code,
            api_id="ka10064",
            flow_type="intraday_investor_trend",
            market_type=market_type,
            query_params={
                "mrkt_tp": market_type,
                "amt_qty_tp": amount_quantity_type,
                "trde_tp": "0",
                "stk_cd": api_stock_code,
            },
            payload={"pages": payloads},
            items=items,
        )
        return CollectedInvestorFlow(
            stock_code=normalized_code,
            flow_type="intraday_investor_trend",
            api_id="ka10064",
            received_count=len(items),
            saved_count=saved_count,
            page_count=page_count,
        )

    def collect_stock_daily_program_trade(
        self,
        stock_code: str,
        *,
        date: str = "",
        amount_quantity_type: str = "",
        max_pages: int = 1,
    ) -> CollectedProgramTrade:
        normalized_code = normalize_stock_code(stock_code)
        api_stock_code = _to_api_stock_code(normalized_code)
        payloads = []
        items = []
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_stock_daily_program_trade(
                stk_cd=api_stock_code,
                date=date,
                amount_quantity_type=amount_quantity_type,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            payloads.append(page.data)
            rows = page.data.get("stk_daly_prm_trde_trnsn", [])
            if not isinstance(rows, list):
                raise ValueError(
                    "ka90013 응답의 stk_daly_prm_trde_trnsn가 목록이 아닙니다."
                )
            items.extend(
                _normalize_program_trade_item(row, stock_code=normalized_code)
                for row in rows
            )
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key
        saved_count = self.database.save_program_trade_snapshots(
            stock_code=normalized_code,
            api_id="ka90013",
            query_params={
                "amt_qty_tp": amount_quantity_type,
                "stk_cd": api_stock_code,
                "date": date,
            },
            payload={"pages": payloads},
            items=items,
        )
        return CollectedProgramTrade(
            stock_code=normalized_code,
            api_id="ka90013",
            received_count=len(items),
            saved_count=saved_count,
            page_count=page_count,
        )

    def collect_supply_sources(
        self,
        stock_code: str,
        *,
        start_date: str,
        end_date: str,
        market_type: str = "000",
        amount_quantity_type: str = "1",
        unit_type: str = "1000",
        max_pages: int = 1,
        include_foreign_rank: bool = True,
    ) -> CollectedSupplySources:
        normalized_code = normalize_stock_code(stock_code)
        foreign = (
            self.collect_foreign_broker_ranking(
                market_type=market_type,
                max_pages=max_pages,
            )
            if include_foreign_rank
            else None
        )
        broker = self.collect_today_main_brokers(normalized_code)
        investor_total = self.collect_stock_investor_total(
            normalized_code,
            start_date=start_date,
            end_date=end_date,
            amount_quantity_type=amount_quantity_type,
            unit_type=unit_type,
            max_pages=max_pages,
        )
        intraday = self.collect_intraday_investor_trend(
            normalized_code,
            market_type=market_type,
            amount_quantity_type=amount_quantity_type,
            max_pages=max_pages,
        )
        program_trade = self.collect_stock_daily_program_trade(
            normalized_code,
            date=end_date,
            max_pages=max_pages,
        )
        return CollectedSupplySources(
            stock_code=normalized_code,
            broker=broker,
            investor_total=investor_total,
            intraday_investor=intraday,
            foreign_broker_ranking=foreign,
            program_trade=program_trade,
        )

    def collect_daily_candles(
        self,
        stock_code: str,
        base_date: str,
        *,
        max_pages: int = 1,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> CollectedCandles:
        return self._collect_candles(
            stock_code=stock_code,
            candle_type="daily",
            interval_minutes=0,
            base_date=base_date,
            max_pages=max_pages,
            priority=priority,
        )

    def collect_minute_candles(
        self,
        stock_code: str,
        base_date: str,
        interval_minutes: int,
        *,
        max_pages: int = 1,
        priority: ApiCallPriority | str | int = ApiCallPriority.NORMAL,
    ) -> CollectedCandles:
        if interval_minutes not in {1, 3, 5, 10, 15, 30, 45, 60}:
            raise ValueError("분봉 간격은 1, 3, 5, 10, 15, 30, 45, 60 중 하나여야 합니다.")
        return self._collect_candles(
            stock_code=stock_code,
            candle_type="minute",
            interval_minutes=interval_minutes,
            base_date=base_date,
            max_pages=max_pages,
            priority=priority,
        )

    def _collect_candles(
        self,
        *,
        stock_code: str,
        candle_type: str,
        interval_minutes: int,
        base_date: str,
        max_pages: int,
        priority: ApiCallPriority | str | int,
    ) -> CollectedCandles:
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")

        received_count = 0
        saved_count = 0
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None

        while page_count < max_pages:
            if candle_type == "daily":
                page = _call_with_priority(
                    self.client.get_daily_chart,
                    stock_code,
                    base_date,
                    cont_yn=cont_yn,
                    next_key=next_key,
                    priority=priority,
                )
                api_id = "ka10081"
                list_key = "stk_dt_pole_chart_qry"
                time_key = "dt"
                time_format = "%Y%m%d"
            else:
                page = _call_with_priority(
                    self.client.get_minute_chart,
                    stock_code,
                    interval_minutes,
                    base_date,
                    cont_yn=cont_yn,
                    next_key=next_key,
                    priority=priority,
                )
                api_id = "ka10080"
                list_key = "stk_min_pole_chart_qry"
                time_key = "cntr_tm"
                time_format = "%Y%m%d%H%M%S"

            raw_candles = page.data.get(list_key, [])
            if not isinstance(raw_candles, list):
                raise ValueError(f"{api_id} 응답의 {list_key}가 목록이 아닙니다.")
            candles = [
                {
                    "candle_time": _parse_candle_time(item.get(time_key), time_format),
                    "open_price": _to_integer(item.get("open_pric")),
                    "high_price": _to_integer(item.get("high_pric")),
                    "low_price": _to_integer(item.get("low_pric")),
                    "close_price": _to_integer(item.get("cur_prc")),
                    "volume": _to_integer(item.get("trde_qty")),
                    "accumulated_volume": _to_integer(item.get("acc_trde_qty")),
                    "trade_amount": _to_integer(item.get("trde_prica")),
                }
                for item in raw_candles
            ]
            saved_count += self.database.save_candles(
                api_id,
                stock_code,
                page.data,
                candles,
                candle_type=candle_type,
                interval_minutes=interval_minutes,
            )
            received_count += len(candles)
            page_count += 1

            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        return CollectedCandles(
            stock_code=stock_code,
            candle_type=candle_type,
            interval_minutes=interval_minutes,
            received_count=received_count,
            saved_count=saved_count,
            page_count=page_count,
            has_more=bool(page.continuation),
        )
