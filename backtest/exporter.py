import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtest.report import BacktestReportService
from backtest.repository import BacktestRepository


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"JSON으로 변환할 수 없는 값입니다: {type(value).__name__}")


class BacktestReportExporter:
    SUMMARY_FIELDS = [
        "net_pnl",
        "average_win",
        "average_loss",
        "total_commission",
        "total_tax",
        "total_slippage",
        "total_cost",
    ]
    GROUP_FIELDS = [
        "trade_count",
        "win_count",
        "loss_count",
        "win_rate",
        "net_pnl",
        "average_return_rate",
        "commission",
        "tax",
        "slippage",
        "total_cost",
    ]
    TRADE_FIELDS = [
        "sequence",
        "stock_code",
        "entry_time",
        "exit_time",
        "quantity",
        "entry_market_price",
        "exit_market_price",
        "entry_price",
        "exit_price",
        "entry_commission",
        "exit_commission",
        "tax",
        "slippage_amount",
        "realized_pnl",
        "return_rate",
        "exit_reason",
    ]
    EQUITY_FIELDS = [
        "sequence",
        "point_time",
        "cash_balance",
        "market_value",
        "total_equity",
    ]

    def __init__(self, repository: BacktestRepository) -> None:
        self.repository = repository
        self.report_service = BacktestReportService(repository)

    def export(
        self,
        run_id: int,
        export_format: str,
        output: Path | None = None,
    ) -> list[Path]:
        normalized_format = export_format.strip().lower()
        if normalized_format == "json":
            return [self._export_json(run_id, output)]
        if normalized_format == "csv":
            return self._export_csv(run_id, output)
        raise ValueError("내보내기 형식은 json 또는 csv여야 합니다.")

    def _export_json(self, run_id: int, output: Path | None) -> Path:
        path = output or Path("reports") / f"backtest-{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._payload(run_id)
        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
            encoding="utf-8",
        )
        return path

    def _export_csv(
        self,
        run_id: int,
        output: Path | None,
    ) -> list[Path]:
        directory = output or Path("reports") / f"backtest-{run_id}"
        directory.mkdir(parents=True, exist_ok=True)
        payload = self._payload(run_id)
        run = dict(payload["run"])
        run["stock_codes"] = ",".join(run["stock_codes"])
        run["strategy_settings"] = json.dumps(
            run["strategy_settings"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        run["risk_settings"] = json.dumps(
            run["risk_settings"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        files = [
            self._write_csv(directory / "run.csv", [run]),
            self._write_csv(
                directory / "summary.csv",
                [payload["summary"]],
                self.SUMMARY_FIELDS,
            ),
            self._write_csv(
                directory / "by_stock.csv",
                payload["by_stock"],
                ["stock_code", *self.GROUP_FIELDS],
            ),
            self._write_csv(
                directory / "by_date.csv",
                payload["by_date"],
                ["date", *self.GROUP_FIELDS],
            ),
            self._write_csv(
                directory / "by_exit_reason.csv",
                payload["by_exit_reason"],
                ["exit_reason", *self.GROUP_FIELDS],
            ),
            self._write_csv(
                directory / "trades.csv",
                payload["trades"],
                self.TRADE_FIELDS,
            ),
            self._write_csv(
                directory / "equity.csv",
                payload["equity_curve"],
                self.EQUITY_FIELDS,
            ),
        ]
        return files

    def _payload(self, run_id: int) -> dict:
        report = self.report_service.build(run_id)
        return {
            **report,
            "trades": self.repository.list_trades(run_id),
            "equity_curve": self.repository.list_equity_points(run_id),
        }

    @staticmethod
    def _write_csv(
        path: Path,
        rows: list[dict],
        fieldnames: list[str] | None = None,
    ) -> Path:
        columns = fieldnames or list(rows[0].keys())
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        key: (
                            float(value)
                            if isinstance(value, Decimal)
                            else value
                        )
                        for key, value in row.items()
                    }
                )
        return path
