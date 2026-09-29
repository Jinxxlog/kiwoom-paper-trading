"""Isolate the partial-exit branch using stored plans and synthetic declining bars.

No application instance, writable database, order submission, or network call.
This demonstrates a branch mismatch, not a counterfactual trading return.
"""

import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from risk.manager import RiskManager


def main():
    connection = sqlite3.connect((ROOT / "runtime/auto_trader.db").as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    manager = object.__new__(RiskManager)
    manager._recent_minute_bars = lambda stock_code, limit=4: [
        {"close_price": 2170, "high_price": 2190},
        {"close_price": 2160, "high_price": 2180},
    ]
    result = {}
    for plan_id in (350, 354):
        plan = dict(connection.execute("SELECT * FROM trade_plans WHERE id=?", (plan_id,)).fetchone())
        result[str(plan_id)] = manager._partial_take_profit_trigger(
            trade_plan=plan,
            row={"highest_price": 2210, "cost_basis": 41939, "quantity": 19},
            stock_code="A122350", current_price=2160, timestamp="2026-07-16T11:52:07",
        )
    connection.close()
    assert result["350"] is None
    assert result["354"]["trigger_price"] == 2160
    payload = {
        "fixture": "Stored plans 350/354 and observed highest/current prices; two synthetic declining bars isolate the branch, not a historical replay.",
        "observed": "Original executed plan does not trigger; the zero-quantity observation plan triggers partial_take_profit below actual purchase cost.",
        "result": result,
    }
    (ROOT / "reports/restart-analysis-2026-09-09-reproduction.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
