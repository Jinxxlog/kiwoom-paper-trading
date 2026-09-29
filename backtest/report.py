from collections import defaultdict
from decimal import Decimal
from typing import Callable

from backtest.repository import BacktestRepository


class BacktestReportService:
    def __init__(self, repository: BacktestRepository) -> None:
        self.repository = repository

    def build(self, run_id: int) -> dict:
        run = self.repository.get_run(run_id)
        if run is None:
            raise ValueError(f"저장된 백테스트를 찾을 수 없습니다: {run_id}")
        trades = self.repository.list_trades(run_id)
        wins = [trade for trade in trades if trade["realized_pnl"] > 0]
        losses = [trade for trade in trades if trade["realized_pnl"] < 0]
        total_commission = sum(
            trade["entry_commission"] + trade["exit_commission"]
            for trade in trades
        )
        total_tax = sum(trade["tax"] for trade in trades)
        total_slippage = sum(trade["slippage_amount"] for trade in trades)
        return {
            "run": run,
            "summary": {
                "net_pnl": sum(trade["realized_pnl"] for trade in trades),
                "average_win": self._average_pnl(wins),
                "average_loss": self._average_pnl(losses),
                "total_commission": total_commission,
                "total_tax": total_tax,
                "total_slippage": total_slippage,
                "total_cost": total_commission + total_tax + total_slippage,
            },
            "by_stock": self._group(
                trades,
                lambda trade: trade["stock_code"],
                "stock_code",
            ),
            "by_date": self._group(
                trades,
                lambda trade: trade["exit_time"][:10],
                "date",
            ),
            "by_exit_reason": self._group(
                trades,
                lambda trade: trade["exit_reason"],
                "exit_reason",
            ),
        }

    @staticmethod
    def _average_pnl(trades: list[dict]) -> int:
        if not trades:
            return 0
        return int(
            Decimal(sum(trade["realized_pnl"] for trade in trades))
            / Decimal(len(trades))
        )

    @staticmethod
    def _group(
        trades: list[dict],
        key_function: Callable[[dict], str],
        key_name: str,
    ) -> list[dict]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for trade in trades:
            groups[key_function(trade)].append(trade)
        results = []
        for key in sorted(groups):
            rows = groups[key]
            wins = sum(1 for row in rows if row["realized_pnl"] > 0)
            losses = sum(1 for row in rows if row["realized_pnl"] < 0)
            trade_count = len(rows)
            commission = sum(
                row["entry_commission"] + row["exit_commission"]
                for row in rows
            )
            tax = sum(row["tax"] for row in rows)
            slippage = sum(row["slippage_amount"] for row in rows)
            results.append(
                {
                    key_name: key,
                    "trade_count": trade_count,
                    "win_count": wins,
                    "loss_count": losses,
                    "win_rate": (
                        Decimal(wins) / Decimal(trade_count)
                        if trade_count
                        else Decimal("0")
                    ),
                    "net_pnl": sum(row["realized_pnl"] for row in rows),
                    "average_return_rate": (
                        sum(
                            Decimal(str(row["return_rate"]))
                            for row in rows
                        )
                        / Decimal(trade_count)
                        if trade_count
                        else Decimal("0")
                    ),
                    "commission": commission,
                    "tax": tax,
                    "slippage": slippage,
                    "total_cost": commission + tax + slippage,
                }
            )
        return results
