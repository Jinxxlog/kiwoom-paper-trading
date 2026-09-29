import tempfile
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from data.account_collector import AccountDataCollector
from data.collector import MarketDataCollector
from db.database import Database
from kiwoom_rest_client import ApiPage
from screening.supply import SupplyScoringEngine


class FakeExtendedClient:
    def get_hoga(self, stock_code: str) -> dict:
        payload = {
            "stk_cd": stock_code,
            "bid_req_base_tm": "153000",
            "tot_sel_req": "1,000",
            "tot_buy_req": "900",
        }
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
            payload[ask_price_key] = str(71000 + level * 100)
            payload[ask_quantity_key] = str(level * 10)
            payload[f"sel_{level}th_pre_req_pre"] = str(-level)
            payload[bid_price_key] = str(71000 - level * 100)
            payload[bid_quantity_key] = str(level * 20)
            payload[f"buy_{level}th_pre_req_pre"] = str(level)
        return payload

    def get_trade_info(
        self,
        stock_code: str,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "cntr_infr": [
                    {
                        "tm": "153000",
                        "cur_prc": "-71000",
                        "cntr_trde_qty": "-15",
                        "acc_trde_qty": "12345",
                        "acc_trde_prica": "987654",
                        "cntr_str": "121.5",
                        "pri_sel_bid_unit": "-71100",
                        "pri_buy_bid_unit": "-71000",
                        "stex_tp": "1",
                    }
                ]
            },
            continuation=False,
            next_key=None,
        )

    def get_balance_details(self) -> dict:
        return {
            "entr": "10,000,000",
            "pymn_alow_amt": "9,000,000",
            "ord_alow_amt": "8,000,000",
        }

    def get_account_evaluation(self) -> dict:
        return {
            "tot_pur_amt": "1,000,000",
            "tot_evlt_amt": "1,100,000",
            "tot_evlt_pl": "100,000",
            "tot_prft_rt": "10.0",
            "prsm_dpst_aset_amt": "10,100,000",
            "acnt_evlt_remn_indv_tot": [
                {
                    "stk_cd": "005930",
                    "stk_nm": "삼성전자",
                    "rmnd_qty": "10",
                    "trde_able_qty": "8",
                    "cur_prc": "-71000",
                    "pur_pric": "70000",
                    "pur_amt": "700000",
                    "evlt_amt": "710000",
                    "evltv_prft": "10000",
                    "prft_rt": "1.43",
                }
            ],
        }

    def get_order_execution_details(
        self,
        order_date: str = "",
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "acnt_ord_cntr_prps_dtl": [
                    {
                        "ord_no": "1001",
                        "stk_cd": "005930",
                        "stk_nm": "삼성전자",
                        "trde_tp": "2",
                        "io_tp_nm": "현금매수",
                        "ord_qty": "10",
                        "ord_uv": "70000",
                        "cntr_qty": "10",
                        "cntr_uv": "69900",
                        "ord_remnq": "0",
                        "ord_tm": "091500",
                        "acpt_tp": "체결",
                        "ori_ord": "",
                        "dmst_stex_tp": "KRX",
                    }
                ]
            },
            continuation=False,
            next_key=None,
        )

    def get_today_main_brokers(self, stock_code: str) -> dict:
        if stock_code != "005930":
            raise AssertionError(f"unexpected API stock code: {stock_code}")
        return {
            "sel_trde_ori_qty_1": "-5000",
            "sel_trde_ori_1": "모건스탠리",
            "sel_trde_ori_cd_1": "036",
            "sel_trde_ori_irds_1": "-200",
            "buy_trde_ori_1": "키움증권",
            "buy_trde_ori_cd_1": "050",
            "buy_trde_ori_qty_1": "+9000",
            "buy_trde_ori_irds_1": "+1000",
            "frgn_sel_prsm_sum": "-5000",
            "frgn_buy_prsm_sum": "+9000",
            "tdy_main_trde_ori": [],
        }

    def get_foreign_broker_ranking(
        self,
        *,
        market_type: str = "000",
        period: str = "0",
        trade_type: str = "1",
        sort_type: str = "1",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        return ApiPage(
            data={
                "frgn_wicket_trde_upper": [
                    {
                        "rank": "3",
                        "stk_cd": "005930_AL",
                        "stk_nm": "삼성전자",
                        "netprps_trde_qty": "+4000",
                        "netprps_prica": "+2500",
                        "sel_trde_qty": "-5000",
                        "buy_trde_qty": "+9000",
                    }
                ]
            },
            continuation=False,
            next_key=None,
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
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        if stk_cd != "005930":
            raise AssertionError(f"unexpected API stock code: {stk_cd}")
        return ApiPage(
            data={
                "stk_invsr_orgn_tot": [
                    {
                        "frgnr_invsr": "+1200",
                        "orgn": "+2500",
                        "ind_invsr": "-3700",
                        "fnnc_invt": "+1000",
                        "invtrt": "+500",
                    }
                ]
            },
            continuation=False,
            next_key=None,
        )

    def get_intraday_investor_trend(
        self,
        *,
        stk_cd: str,
        market_type: str = "000",
        amount_quantity_type: str = "1",
        trade_type: str = "0",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        if stk_cd != "005930":
            raise AssertionError(f"unexpected API stock code: {stk_cd}")
        return ApiPage(
            data={
                "opmr_invsr_trde_chart": [
                    {
                        "tm": "100000",
                        "frgnr_invsr": "+300",
                        "orgn": "+700",
                        "invtrt": "+100",
                    }
                ]
            },
            continuation=False,
            next_key=None,
        )

    def get_stock_daily_program_trade(
        self,
        *,
        stk_cd: str,
        date: str = "",
        amount_quantity_type: str = "",
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> ApiPage:
        if stk_cd != "005930":
            raise AssertionError(f"unexpected API stock code: {stk_cd}")
        return ApiPage(
            data={
                "stk_daly_prm_trde_trnsn": [
                    {
                        "dt": date or "20260620",
                        "cur_prc": "+71000",
                        "pred_pre": "+1000",
                        "flu_rt": "+1.43",
                        "trde_qty": "1000000",
                        "prm_sell_amt": "1000",
                        "prm_buy_amt": "3000",
                        "prm_netprps_amt": "+2000",
                        "prm_netprps_amt_irds": "+500",
                        "prm_sell_qty": "10000",
                        "prm_buy_qty": "30000",
                        "prm_netprps_qty": "+20000",
                        "prm_netprps_qty_irds": "+5000",
                        "base_pric_tm": "153000",
                        "stex_tp": "통합",
                    }
                ]
            },
            continuation=False,
            next_key=None,
        )


class ExtendedCollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.client = FakeExtendedClient()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_order_book_saves_ten_levels_per_side(self) -> None:
        result = MarketDataCollector(
            self.client,
            self.database,
        ).collect_order_book("A005930")

        self.assertEqual(result.level_count, 20)
        with self.database.connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM order_book_snapshots"
            ).fetchone()
            levels = connection.execute(
                "SELECT * FROM order_book_levels ORDER BY side, level"
            ).fetchall()
        self.assertEqual(snapshot["best_ask_price"], 71100)
        self.assertEqual(snapshot["best_bid_quantity"], 20)
        self.assertEqual(len(levels), 20)

    def test_trade_strength_preserves_signed_trade_quantity(self) -> None:
        collector = MarketDataCollector(self.client, self.database)
        first = collector.collect_trade_strength("A005930")
        second = collector.collect_trade_strength("A005930")

        self.assertEqual(first.received_count, 1)
        self.assertEqual(second.received_count, 1)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trade_strength_snapshots"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_quantity"], -15)
        self.assertEqual(rows[0]["current_price"], 71000)
        self.assertEqual(rows[0]["strength"], 121.5)

    def test_account_snapshot_and_order_execution_are_saved(self) -> None:
        collector = AccountDataCollector(self.client, self.database)
        account = collector.collect_snapshot()
        orders = collector.collect_orders("20260610")

        self.assertEqual(account.position_count, 1)
        self.assertEqual(orders.saved_count, 1)
        with self.database.connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM real_account_snapshots"
            ).fetchone()
            position = connection.execute(
                "SELECT * FROM real_positions"
            ).fetchone()
            order = connection.execute(
                "SELECT * FROM real_order_execution_snapshots"
            ).fetchone()
        self.assertEqual(snapshot["total_profit_loss"], 100000)
        self.assertEqual(position["stock_code"], "A005930")
        self.assertEqual(position["current_price"], 71000)
        self.assertEqual(order["executed_price"], 69900)

    def test_sensitive_account_data_is_pruned_with_raw_payloads(self) -> None:
        old_time = datetime.now() - timedelta(days=100)
        self.database.save_real_account_snapshot(
            {"entr": "1"},
            {"acnt_evlt_remn_indv_tot": []},
            [],
            {"deposit": 1},
            collected_at=old_time,
        )
        self.database.save_real_order_executions(
            {"acnt_ord_cntr_prps_dtl": []},
            [],
            order_date="20260101",
            collected_at=old_time,
        )

        deleted = self.database.prune_sensitive_account_data(90)

        self.assertEqual(deleted["account_snapshots"], 1)
        self.assertEqual(deleted["raw_snapshots"], 3)
        with self.database.connect() as connection:
            raw_count = connection.execute(
                "SELECT COUNT(*) FROM api_snapshots"
            ).fetchone()[0]
        self.assertEqual(raw_count, 0)

    def test_supply_flow_collection_and_scoring(self) -> None:
        collector = MarketDataCollector(self.client, self.database)

        broker = collector.collect_today_main_brokers("A005930")
        foreign = collector.collect_foreign_broker_ranking()
        total = collector.collect_stock_investor_total(
            "A005930",
            start_date="20260613",
            end_date="20260620",
        )
        intraday = collector.collect_intraday_investor_trend("A005930")
        result = SupplyScoringEngine(self.database).score(["A005930"])

        self.assertEqual(broker.saved_count, 2)
        self.assertEqual(foreign.saved_count, 1)
        self.assertEqual(total.saved_count, 1)
        self.assertEqual(intraday.saved_count, 1)
        self.assertEqual(result.saved_count, 1)
        with self.database.connect() as connection:
            broker_snapshot = connection.execute(
                "SELECT * FROM broker_flow_snapshots WHERE api_id = 'ka10040'"
            ).fetchone()
            broker_items = connection.execute(
                "SELECT * FROM broker_flow_items ORDER BY side"
            ).fetchall()
            investor = connection.execute(
                "SELECT * FROM investor_flow_items WHERE observed_key = '100000'"
            ).fetchone()
            score = connection.execute("SELECT * FROM supply_scores").fetchone()
        self.assertEqual(broker_snapshot["foreign_net_estimated"], 4000)
        self.assertEqual(len(broker_items), 3)
        self.assertEqual(investor["foreigner"], 300)
        self.assertGreater(score["total_score"], 70)
        self.assertEqual(score["blocked"], 0)

    def test_supply_sources_are_collected_with_standard_metadata(self) -> None:
        collector = MarketDataCollector(self.client, self.database)

        sources = collector.collect_supply_sources(
            "A005930",
            start_date="20260613",
            end_date="20260620",
        )
        SupplyScoringEngine(self.database).score(["A005930"])

        self.assertEqual(sources.stock_code, "A005930")
        self.assertEqual(sources.broker.api_id, "ka10040")
        self.assertEqual(sources.investor_total.api_id, "ka10061")
        self.assertEqual(sources.intraday_investor.api_id, "ka10064")
        self.assertIsNotNone(sources.foreign_broker_ranking)
        self.assertEqual(sources.program_trade.api_id, "ka90013")
        self.assertEqual(sources.program_trade.saved_count, 1)
        with self.database.connect() as connection:
            raw_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM api_snapshots
                WHERE api_id IN (
                    'ka10037', 'ka10040', 'ka10061', 'ka10064', 'ka90013'
                )
                """
            ).fetchone()[0]
            broker_item = connection.execute(
                """
                SELECT metadata_json
                FROM broker_flow_items
                WHERE side = 'buy'
                LIMIT 1
                """
            ).fetchone()
            investor_item = connection.execute(
                """
                SELECT metadata_json
                FROM investor_flow_items
                WHERE observed_key = '100000'
                LIMIT 1
                """
            ).fetchone()
            program_item = connection.execute(
                """
                SELECT *
                FROM program_trade_snapshots
                WHERE stock_code = 'A005930'
                LIMIT 1
                """
            ).fetchone()
            supply_score = connection.execute(
                "SELECT * FROM supply_scores WHERE stock_code = 'A005930'"
            ).fetchone()
        broker_metadata = json.loads(broker_item["metadata_json"])
        investor_metadata = json.loads(investor_item["metadata_json"])
        self.assertEqual(raw_count, 5)
        self.assertEqual(program_item["net_buy_amount"], 2000)
        self.assertGreater(supply_score["program_score"], 0)
        self.assertEqual(
            broker_metadata["normalized_source"],
            "ka10040.today_main_brokers",
        )
        self.assertEqual(broker_metadata["unit"], "shares")
        self.assertEqual(
            investor_metadata["normalized_source"],
            "ka10064.intraday_investor_trend",
        )
        self.assertEqual(investor_metadata["amount_quantity_type"], "1")

    def test_program_consecutive_small_sells_are_watch_not_hard_block(self) -> None:
        rows = []
        for observed_date in (
            "20260624",
            "20260623",
            "20260622",
            "20260619",
        ):
            rows.append(
                {
                    "stock_code": "A005930",
                    "observed_date": observed_date,
                    "sell_amount": 1_010,
                    "buy_amount": 990,
                    "net_buy_amount": -20,
                    "net_buy_amount_change": 10,
                    "sell_quantity": 1_010,
                    "buy_quantity": 990,
                    "net_buy_quantity": -20,
                    "metadata": {},
                }
            )
        self.database.save_program_trade_snapshots(
            stock_code="A005930",
            api_id="ka90013",
            query_params={"stk_cd": "005930"},
            payload={"batch": 1},
            items=rows,
        )
        self.database.save_program_trade_snapshots(
            stock_code="A005930",
            api_id="ka90013",
            query_params={"stk_cd": "005930"},
            payload={"batch": 2},
            items=rows,
        )

        result = SupplyScoringEngine(self.database).score(["A005930"])

        with self.database.connect() as connection:
            score = connection.execute(
                "SELECT * FROM supply_scores ORDER BY id DESC LIMIT 1"
            ).fetchone()
        program = json.loads(score["components_json"])["program"]
        self.assertEqual(result.program_validation["watch_count"], 1)
        self.assertEqual(result.program_validation["hard_block_count"], 0)
        self.assertFalse(score["blocked"])
        self.assertEqual(program["policy_state"], "watch")
        self.assertTrue(program["legacy_block_candidate"])
        self.assertTrue(program["legacy_block_relaxed"])
        self.assertEqual(program["unique_date_count"], 4)

    def test_program_persistent_concentrated_sells_hard_block(self) -> None:
        rows = [
            {
                "stock_code": "A005930",
                "observed_date": observed_date,
                "sell_amount": 1_500,
                "buy_amount": 500,
                "net_buy_amount": -1_000,
                "net_buy_amount_change": -100,
                "sell_quantity": 1_500,
                "buy_quantity": 500,
                "net_buy_quantity": -1_000,
                "metadata": {},
            }
            for observed_date in ("20260624", "20260623", "20260622")
        ]
        self.database.save_program_trade_snapshots(
            stock_code="A005930",
            api_id="ka90013",
            query_params={"stk_cd": "005930"},
            payload={"test": True},
            items=rows,
        )

        result = SupplyScoringEngine(self.database).score(["A005930"])

        with self.database.connect() as connection:
            score = connection.execute(
                "SELECT * FROM supply_scores ORDER BY id DESC LIMIT 1"
            ).fetchone()
        program = json.loads(score["components_json"])["program"]
        self.assertEqual(result.program_validation["hard_block_count"], 1)
        self.assertTrue(score["blocked"])
        self.assertEqual(score["blocked_reason"], "프로그램 당일 매도 집중")
        self.assertEqual(program["policy_state"], "block")
        self.assertTrue(program["hard_block"])

    def test_supply_score_blocks_leader_candidate_when_flow_leaves(self) -> None:
        evaluated_at = "2026-06-20T10:00:00"
        self.database.save_leader_stock_scores(
            [
                {
                    "trade_date": "2026-06-20",
                    "stock_code": "A005930",
                    "stock_name": "삼성전자",
                    "theme_code": "101",
                    "theme_name": "반도체",
                    "role": "leader",
                    "total_score": 90.0,
                    "rank_score": 20.0,
                    "trade_amount_score": 35.0,
                    "theme_score": 20.0,
                    "persistence_score": 10.0,
                    "chart_score": 5.0,
                    "risk_score": 0.0,
                    "blocked": False,
                    "blocked_reason": None,
                    "reasons": ["대장주 후보"],
                    "components": {},
                }
            ],
            scored_at=datetime.fromisoformat(evaluated_at),
        )
        self.database.save_broker_flow_snapshot(
            stock_code="A005930",
            api_id="ka10040",
            flow_type="today_main_brokers",
            query_params={"stk_cd": "A005930"},
            payload={"test": True},
            items=[
                {"side": "buy", "rank": 1, "broker_name": "A", "quantity": 100},
                {"side": "sell", "rank": 1, "broker_name": "B", "quantity": -300},
            ],
            foreign_sell_estimated=-300,
            foreign_buy_estimated=100,
            observed_at=evaluated_at,
        )
        self.database.save_investor_flow_snapshot(
            stock_code="A005930",
            api_id="ka10064",
            flow_type="intraday_investor_trend",
            market_type="000",
            query_params={"stk_cd": "A005930"},
            payload={"test": True},
            items=[
                {
                    "stock_code": "A005930",
                    "observed_key": "100000",
                    "foreigner": -200,
                    "institution": -100,
                }
            ],
            observed_at=evaluated_at,
        )

        result = SupplyScoringEngine(self.database).score(
            ["A005930"],
            evaluated_at=evaluated_at,
        )

        self.assertEqual(result.saved_count, 1)
        with self.database.connect() as connection:
            score = connection.execute(
                "SELECT * FROM supply_scores WHERE stock_code = 'A005930'"
            ).fetchone()
        components = json.loads(score["components_json"])
        self.assertEqual(score["blocked"], 1)
        self.assertEqual(score["blocked_reason"], "대장주 후보 수급 이탈")
        self.assertEqual(components["leader_context"]["role"], "leader")
        self.assertEqual(components["program"]["status"], "not_collected")


if __name__ == "__main__":
    unittest.main()
