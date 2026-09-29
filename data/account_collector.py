from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from data.collector import _to_float, _to_integer, _to_signed_integer
from db.database import Database
from kiwoom_rest_client import KiwoomRestClient


def _first_value(payload: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _clean_stock_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return code if code.startswith("A") else f"A{code}"


@dataclass(frozen=True)
class CollectedAccount:
    snapshot_id: int
    position_count: int


@dataclass(frozen=True)
class CollectedOrders:
    received_count: int
    saved_count: int
    page_count: int


class AccountDataCollector:
    def __init__(self, client: KiwoomRestClient, database: Database) -> None:
        self.client = client
        self.database = database

    def collect_snapshot(self) -> CollectedAccount:
        balance = self.client.get_balance_details()
        holdings = self.client.get_account_evaluation()
        raw_positions = holdings.get("acnt_evlt_remn_indv_tot", [])
        if not isinstance(raw_positions, list):
            raise ValueError("kt00018 응답의 보유 종목 목록이 올바르지 않습니다.")

        summary = {
            "deposit": _to_signed_integer(
                _first_value(balance, ("entr", "deposit"))
            ),
            "withdrawable_amount": _to_signed_integer(
                _first_value(
                    balance,
                    ("pymn_alow_amt", "wthd_alow_amt", "withdrawable_amount"),
                )
            ),
            "orderable_cash": _to_signed_integer(
                _first_value(
                    balance,
                    (
                        "ord_alowa",
                        "ord_alow_amt",
                        "nxdy_ord_alow_amt",
                        "orderable_cash",
                    ),
                )
            ),
            "total_purchase_amount": _to_signed_integer(
                _first_value(holdings, ("tot_pur_amt", "total_purchase_amount"))
            ),
            "total_evaluation_amount": _to_signed_integer(
                _first_value(holdings, ("tot_evlt_amt", "total_evaluation_amount"))
            ),
            "total_profit_loss": _to_signed_integer(
                _first_value(holdings, ("tot_evlt_pl", "total_profit_loss"))
            ),
            "total_profit_rate": _to_float(
                _first_value(holdings, ("tot_prft_rt", "total_profit_rate"))
            ),
            "estimated_assets": _to_signed_integer(
                _first_value(
                    holdings,
                    ("prsm_dpst_aset_amt", "estimated_assets"),
                )
            ),
        }
        positions = [
            {
                "stock_code": _clean_stock_code(row.get("stk_cd")),
                "stock_name": row.get("stk_nm"),
                "quantity": _to_integer(row.get("rmnd_qty")),
                "tradable_quantity": _to_integer(row.get("trde_able_qty")),
                "current_price": _to_integer(row.get("cur_prc")),
                "purchase_price": _to_integer(row.get("pur_pric")),
                "purchase_amount": _to_signed_integer(row.get("pur_amt")),
                "evaluation_amount": _to_signed_integer(row.get("evlt_amt")),
                "evaluation_profit_loss": _to_signed_integer(
                    row.get("evltv_prft")
                ),
                "profit_rate": _to_float(row.get("prft_rt")),
            }
            for row in raw_positions
            if row.get("stk_cd")
        ]
        snapshot_id = self.database.save_real_account_snapshot(
            balance,
            holdings,
            positions,
            summary,
        )
        return CollectedAccount(snapshot_id, len(positions))

    def collect_orders(
        self,
        order_date: str = "",
        *,
        max_pages: int = 1,
    ) -> CollectedOrders:
        if max_pages < 1:
            raise ValueError("수집 페이지 수는 1 이상이어야 합니다.")

        received_count = 0
        saved_count = 0
        page_count = 0
        cont_yn: Optional[str] = None
        next_key: Optional[str] = None
        while page_count < max_pages:
            page = self.client.get_order_execution_details(
                order_date,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            rows = page.data.get("acnt_ord_cntr_prps_dtl", [])
            if not isinstance(rows, list):
                raise ValueError("kt00007 응답의 주문·체결 목록이 올바르지 않습니다.")
            orders = [
                {
                    "order_number": row.get("ord_no"),
                    "stock_code": _clean_stock_code(row.get("stk_cd")),
                    "stock_name": row.get("stk_nm"),
                    "trade_type": row.get("trde_tp"),
                    "order_type_name": row.get("io_tp_nm"),
                    "order_quantity": _to_integer(row.get("ord_qty")),
                    "order_price": _to_integer(row.get("ord_uv")),
                    "executed_quantity": _to_integer(row.get("cntr_qty")),
                    "executed_price": _to_integer(row.get("cntr_uv")),
                    "remaining_quantity": _to_integer(row.get("ord_remnq")),
                    "order_time": row.get("ord_tm"),
                    "status": row.get("acpt_tp"),
                    "original_order_number": row.get("ori_ord"),
                    "exchange_type": row.get("dmst_stex_tp"),
                }
                for row in rows
            ]
            saved_count += self.database.save_real_order_executions(
                page.data,
                orders,
                order_date=order_date or None,
            )
            received_count += len(orders)
            page_count += 1
            if not page.continuation:
                break
            cont_yn = "Y"
            next_key = page.next_key

        return CollectedOrders(received_count, saved_count, page_count)
