"""Read-only, standard-library audit of retained paper trades and archived reports.

Run from the project root: python -X utf8 tools/analyze_restart.py
Never imports the trading application or opens a writable database connection.
"""

import collections
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3


ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "restart-analysis-2026-09-09-data.json"


def decoded(value):
    return json.loads(value) if value else {}


def rows(connection, query, parameters=()):
    return [dict(row) for row in connection.execute(query, parameters)]


def main():
    path = ROOT / "runtime" / "auto_trader.db"
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    fills = rows(connection, """
        SELECT f.*, o.account_id, o.reason, r.decision_id,
               d.strategy_config_id, d.source_data_time, d.score,
               p.id AS plan_id, p.entry_type, p.entry_price AS plan_entry_price,
               p.stop_loss_price, p.first_take_profit_price, p.setup_json
        FROM paper_fills f
        JOIN paper_orders o ON o.id=f.order_id
        LEFT JOIN order_requests r ON r.client_order_id=o.client_order_id
        LEFT JOIN strategy_decisions d ON d.id=r.decision_id
        LEFT JOIN trade_plans p ON p.decision_id=d.id
        ORDER BY f.filled_at,f.id
    """)
    assert len(fills) == len(rows(connection, "SELECT id FROM paper_fills")), "Nonunique fill join"
    daily = collections.defaultdict(lambda: dict(buys=0, sells=0, net_pnl=0, fee_tax=0, slippage=0))
    active = {}
    episodes = []
    buy_evidence = []
    pricing_age = []
    for fill in fills:
        key = (fill["account_id"], fill["stock_code"])
        day = fill["filled_at"][:10]
        row = daily[day]
        row["buys" if fill["side"] == "buy" else "sells"] += 1
        row["net_pnl"] += fill["realized_pnl"]
        row["fee_tax"] += fill["commission"] + fill["tax"]
        row["slippage"] += fill["slippage_amount"]
        pricing_age.append(dict(order_id=fill["order_id"], side=fill["side"], seconds=(dt.datetime.fromisoformat(fill["filled_at"]) - dt.datetime.fromisoformat(fill["price_time"])).total_seconds()))
        if fill["side"] == "buy":
            setup = decoded(fill["setup_json"])
            guard = setup.get("entry_guard_policy") or {}
            previous = rows(connection, """
                SELECT candle_time,high_price,close_price,volume,updated_at
                FROM candles WHERE stock_code=? AND candle_type='daily' AND candle_time<?
                ORDER BY candle_time DESC LIMIT 1
            """, (fill["stock_code"], day))
            previous = previous[0] if previous else None
            evidence = dict(
                order_id=fill["order_id"], stock_code=fill["stock_code"], filled_at=fill["filled_at"],
                quantity=fill["quantity"], market_price=fill["market_price"], fill_price=fill["fill_price"],
                decision_id=fill["decision_id"], config_id=fill["strategy_config_id"],
                plan_id=fill["plan_id"], entry_type=fill["entry_type"],
                plan_entry_price=fill["plan_entry_price"], stop_loss_price=fill["stop_loss_price"],
                first_target=fill["first_take_profit_price"],
                score=fill["score"], grade=guard.get("entry_grade"), guard_status=guard.get("status"),
                playbook=(setup.get("strategy_playbook") or {}).get("name"),
                router_status=(setup.get("router_execution_policy") or {}).get("status"),
                supply_missing=(guard.get("data_availability") or {}).get("supply_status"),
                force_entry=bool((setup.get("chart_trade") or {}).get("force_entry")),
                day_gain_rate=setup.get("day_gain_rate"), prior_daily=previous,
                prior_daily_calendar_age=(dt.date.fromisoformat(day)-dt.date.fromisoformat(previous["candle_time"][:10])).days if previous else None,
                quality=setup.get("entry_candle_quality") or {},
            )
            buy_evidence.append(evidence)
            if key not in active:
                active[key] = dict(account_id=key[0], stock_code=key[1], entry_time=fill["filled_at"],
                    quantity_open=0, bought_quantity=0, buy_count=0, sell_count=0,
                    buy_gross=0, sell_gross=0, fee_tax=0, slippage=0, net_pnl=0,
                    order_ids=[], entry_types=[], grades=[], playbooks=[], config_ids=[])
            episode = active[key]
            episode["quantity_open"] += fill["quantity"]
            episode["bought_quantity"] += fill["quantity"]
            episode["buy_count"] += 1
            episode["buy_gross"] += fill["gross_amount"]
            for field, value in [("entry_types",fill["entry_type"]),("grades",evidence["grade"]),("playbooks",evidence["playbook"]),("config_ids",fill["strategy_config_id"])]:
                if value not in episode[field]: episode[field].append(value)
        else:
            assert key in active, f"Unmatched sell: {fill['order_id']}"
            episode=active[key]
            episode["quantity_open"] -= fill["quantity"]
            assert episode["quantity_open"] >= 0
            episode["sell_count"] += 1
            episode["sell_gross"] += fill["gross_amount"]
            episode["net_pnl"] += fill["realized_pnl"]
            episode["exit_time"] = fill["filled_at"]
            episode["exit_reason"] = fill["reason"]
        episode["order_ids"].append(fill["order_id"])
        episode["fee_tax"] += fill["commission"] + fill["tax"]
        episode["slippage"] += fill["slippage_amount"]
        if episode["quantity_open"] == 0:
            assert episode["net_pnl"] == episode["sell_gross"] - episode["buy_gross"] - episode["fee_tax"]
            episode["gross_pnl_before_all_costs"] = episode["net_pnl"] + episode["fee_tax"] + episode["slippage"]
            episode["return_on_bought_notional"] = episode["net_pnl"] / episode["buy_gross"]
            episode["holding_minutes"] = (dt.datetime.fromisoformat(episode["exit_time"]) - dt.datetime.fromisoformat(episode["entry_time"])).total_seconds()/60
            episodes.append(active.pop(key))
    wins = [e["net_pnl"] for e in episodes if e["net_pnl"] > 0]
    losses = [e["net_pnl"] for e in episodes if e["net_pnl"] < 0]
    accounts=rows(connection,"SELECT * FROM paper_accounts")
    total_pnl=sum(f["realized_pnl"] for f in fills)
    fee_tax=sum(f["commission"]+f["tax"] for f in fills)
    slippage=sum(f["slippage_amount"] for f in fills)
    archived={}
    archived_reviews=[]
    for source in sorted((ROOT/"reports").glob("operations-*.json")):
        report=decoded(source.read_text(encoding="utf-8-sig"))
        day=report["report_date"]
        trading=report["trading"]["accounts"]
        checks=report["validation"].get("checks",[])
        archived[day]=dict(source=source.name, net_pnl=sum(r["realized_pnl"] for r in trading),
            costs=sum(r["total_cost"] for r in trading),accounts=trading,
            validation_status=report["validation"].get("status"),
            failed_checks=[r for r in checks if r.get("status") in ("fail","warning")],
            collection_summary={k:v for k,v in report["collection"].items() if k!="runs"},
            strategy_summary=report["strategy"]["summary"],forward_outcomes=report.get("forward_outcomes"))
        events=report["events"]
        archived[day]["event_summary"]={k:v for k,v in events.items() if not isinstance(v,list)}
    for source in sorted((ROOT/"reports").glob("trading-review-*.json")):
        report=decoded(source.read_text(encoding="utf-8-sig"))
        day=report["report_date"]
        archived_reviews.append(dict(source=source.name,summary=report["summary"],
            missing_supply=report["missing_supply_performance"],force_entry=report["force_entry_performance"],
            entry_type_performance=report["entry_type_performance"],
            preventable_loss_estimate=report["preventable_loss_estimate"],
            blocked_forward_review={k:v for k,v in report["blocked_forward_review"].items() if k!="notable"}))
        if day not in archived:
            archived[day]=dict(source=source.name,net_pnl=report["summary"]["realized_pnl"],costs=report["summary"]["total_cost"])
    for day,item in daily.items():
        if day in archived: assert item["net_pnl"] == archived[day]["net_pnl"], f"Report mismatch {day}"
    orphan_partials=rows(connection,"""
        SELECT p.id plan_id,p.decision_id,p.entry_price,p.planned_quantity,d.action,d.outcome,
               p.partial_exit_order_id,f.fill_price,f.realized_pnl
        FROM trade_plans p JOIN strategy_decisions d ON d.id=p.decision_id
        JOIN paper_fills f ON f.order_id=p.partial_exit_order_id
        WHERE p.partial_exit_executed=1 AND d.outcome!='executed'
    """)
    by_stock=collections.defaultdict(lambda:dict(episodes=0,net_pnl=0))
    by_type=collections.defaultdict(lambda:dict(episodes=0,net_pnl=0))
    for e in episodes:
        for target,key in [(by_stock,e["stock_code"]),(by_type," + ".join(str(x) for x in e["entry_types"]))]:
            target[key]["episodes"]+=1;target[key]["net_pnl"]+=e["net_pnl"]
    output=dict(
        audit_date="2026-09-09",generated_at=dt.datetime.now().astimezone().isoformat(),
        scope="Retained SQLite paper fills; historical JSON reports kept separate. Flat-to-flat position episodes, additions/partial exits combined.",
        database_bytes=path.stat().st_size,accounts=accounts,open_positions=rows(connection,"SELECT * FROM paper_positions"),
        summary=dict(buys=sum(f["side"]=="buy" for f in fills),sells=sum(f["side"]=="sell" for f in fills),
            closed_episodes=len(episodes),wins=len(wins),losses=len(losses),win_rate=len(wins)/len(episodes) if episodes else None,
            average_win=sum(wins)/len(wins) if wins else None,average_loss=sum(losses)/len(losses) if losses else None,
            payoff_ratio=(sum(wins)/len(wins))/abs(sum(losses)/len(losses)) if wins and losses else None,
            profit_factor=sum(wins)/abs(sum(losses)) if losses else None,net_pnl=total_pnl,
            expectancy_per_episode=total_pnl/len(episodes) if episodes else None,fee_and_tax=fee_tax,slippage=slippage,
            gross_pnl_before_all_costs=total_pnl+fee_tax+slippage,
            buy_notional=sum(f["gross_amount"] for f in fills if f["side"]=="buy")),
        daily_retained=dict(daily),episodes=episodes,by_stock=dict(by_stock),by_entry_types=dict(by_type),
        buy_evidence=buy_evidence,pricing_age=pricing_age,unexecuted_plans_used_for_partial_exits=orphan_partials,
        backtests=rows(connection,"SELECT id,strategy_name,start_date,end_date,initial_cash,final_equity,total_return_rate,max_drawdown_rate,trade_count,win_rate,created_at FROM backtest_runs"),
        candle_coverage=rows(connection,"SELECT candle_type,interval_minutes,count(*) count,min(candle_time) first,max(candle_time) last,count(distinct stock_code) symbols FROM candles GROUP BY candle_type,interval_minutes"),
        latest_day_forward_outcomes=rows(connection,"SELECT substr(o.evaluated_at,1,19) evaluated_at,count(*) count,count(distinct o.decision_id) decisions FROM decision_forward_outcomes o JOIN strategy_decisions d ON d.id=o.decision_id WHERE d.decided_at>='2026-07-16' AND d.decided_at<'2026-07-17' GROUP BY substr(o.evaluated_at,1,19)"),
        latest_day_error_events=rows(connection,"SELECT module,event_type,severity,count(*) count FROM operation_events WHERE occurred_at>='2026-07-16' AND occurred_at<'2026-07-17' AND severity IN ('error','warning') GROUP BY module,event_type,severity ORDER BY count DESC"),
        retained_ai_analysis_count=connection.execute("SELECT count(*) FROM ai_shadow_analyses").fetchone()[0],
        retained_real_order_record_count=connection.execute("SELECT count(*) FROM real_order_execution_snapshots").fetchone()[0],
        archived_daily=dict(sorted(archived.items())),archived_reviews=archived_reviews,
        source_code_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ["strategy/breakout.py","risk/manager.py","reports/trading_review.py","backtest/engine.py","config/settings.py"]},
        checks=dict(fill_join_unique=True,all_sells_have_buys=True,episode_cash_reconciliation=True,
            archived_daily_matches_retained=True,unclosed_episodes=len(active),
            account_realized_matches_fills=sum(a["realized_pnl"] for a in accounts)==total_pnl),
    )
    connection.close()
    OUTPUT.write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(output["summary"],ensure_ascii=False,indent=2))
    print("Written:",OUTPUT)


if __name__ == "__main__":
    main()
