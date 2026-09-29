import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from backtest.engine import BacktestConfig, BacktestResult
from config.settings import RiskSettings, StrategySettings
from db.database import Database


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"JSON으로 변환할 수 없는 값입니다: {type(value).__name__}")


class BacktestRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(
        self,
        stock_codes: list[str],
        config: BacktestConfig,
        strategy_settings: StrategySettings,
        risk_settings: RiskSettings,
        result: BacktestResult,
    ) -> int:
        timestamp = datetime.now().isoformat(timespec="seconds")
        codes = sorted(set(stock_codes))
        strategy_json = json.dumps(
            asdict(strategy_settings),
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        )
        risk_json = json.dumps(
            asdict(risk_settings),
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        )
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO backtest_runs(
                    strategy_name, start_date, end_date, interval_minutes,
                    stock_codes_json, initial_cash, commission_rate,
                    sell_tax_rate, slippage_rate, strategy_settings_json,
                    risk_settings_json, final_equity, total_return_rate,
                    max_drawdown_rate, trade_count, win_count, loss_count,
                    win_rate, payoff_ratio, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "breakout",
                    config.start_date.isoformat(),
                    config.end_date.isoformat(),
                    config.interval_minutes,
                    json.dumps(codes, separators=(",", ":")),
                    config.initial_cash,
                    float(config.commission_rate),
                    float(config.sell_tax_rate),
                    float(config.slippage_rate),
                    strategy_json,
                    risk_json,
                    result.final_equity,
                    float(result.total_return_rate),
                    float(result.max_drawdown_rate),
                    result.trade_count,
                    result.win_count,
                    result.loss_count,
                    float(result.win_rate),
                    (
                        float(result.payoff_ratio)
                        if result.payoff_ratio is not None
                        else None
                    ),
                    timestamp,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO backtest_trades(
                    run_id, sequence, stock_code, entry_time, exit_time,
                    quantity, entry_market_price, exit_market_price,
                    entry_price, exit_price, entry_commission,
                    exit_commission, tax, slippage_amount, realized_pnl,
                    return_rate, exit_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        sequence,
                        trade.stock_code,
                        trade.entry_time,
                        trade.exit_time,
                        trade.quantity,
                        trade.entry_market_price,
                        trade.exit_market_price,
                        trade.entry_price,
                        trade.exit_price,
                        trade.entry_commission,
                        trade.exit_commission,
                        trade.tax,
                        trade.slippage_amount,
                        trade.realized_pnl,
                        float(trade.return_rate),
                        trade.exit_reason,
                    )
                    for sequence, trade in enumerate(result.trades, start=1)
                ],
            )
            connection.executemany(
                """
                INSERT INTO backtest_equity_points(
                    run_id, sequence, point_time, cash_balance,
                    market_value, total_equity
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        sequence,
                        point.point_time,
                        point.cash_balance,
                        point.market_value,
                        point.total_equity,
                    )
                    for sequence, point in enumerate(
                        result.equity_curve,
                        start=1,
                    )
                ],
            )
            return run_id

    def list_runs(self, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("조회 건수는 1 이상이어야 합니다.")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, strategy_name, start_date, end_date,
                       interval_minutes, stock_codes_json, initial_cash,
                       final_equity, total_return_rate, max_drawdown_rate,
                       trade_count, win_rate, payoff_ratio, created_at
                FROM backtest_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._decode_run(row) for row in rows]

    def get_run(self, run_id: int) -> Optional[dict]:
        if run_id < 1:
            raise ValueError("백테스트 실행 ID는 1 이상이어야 합니다.")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM backtest_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return self._decode_run(row) if row is not None else None

    def list_trades(self, run_id: int) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, stock_code, entry_time, exit_time,
                       quantity,
                       COALESCE(entry_market_price, entry_price)
                           AS entry_market_price,
                       COALESCE(exit_market_price, exit_price)
                           AS exit_market_price,
                       entry_price, exit_price, entry_commission,
                       exit_commission, tax,
                       COALESCE(slippage_amount, 0) AS slippage_amount,
                       realized_pnl,
                       return_rate, exit_reason
                FROM backtest_trades
                WHERE run_id = ?
                ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_equity_points(
        self,
        run_id: int,
        limit: Optional[int] = None,
    ) -> list[dict]:
        if limit is not None and limit < 1:
            raise ValueError("자산곡선 조회 건수는 1 이상이어야 합니다.")
        query = """
            SELECT sequence, point_time, cash_balance,
                   market_value, total_equity
            FROM backtest_equity_points
            WHERE run_id = ?
            ORDER BY sequence
        """
        parameters: tuple[Any, ...] = (run_id,)
        if limit is not None:
            query += " LIMIT ?"
            parameters = (run_id, limit)
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _decode_run(row: Any) -> dict:
        result = dict(row)
        result["stock_codes"] = json.loads(result.pop("stock_codes_json"))
        if "strategy_settings_json" in result:
            result["strategy_settings"] = json.loads(
                result.pop("strategy_settings_json")
            )
        if "risk_settings_json" in result:
            result["risk_settings"] = json.loads(
                result.pop("risk_settings_json")
            )
        return result
