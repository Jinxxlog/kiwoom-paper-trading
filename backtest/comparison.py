from typing import Any, Optional

from backtest.report import BacktestReportService


class BacktestComparisonService:
    def __init__(self, report_service: BacktestReportService) -> None:
        self.report_service = report_service

    def compare(self, run_ids: list[int]) -> dict:
        unique_ids = list(dict.fromkeys(run_ids))
        if len(unique_ids) < 2:
            raise ValueError("비교할 백테스트 실행 ID를 2개 이상 지정해야 합니다.")

        rows = []
        for run_id in unique_ids:
            report = self.report_service.build(run_id)
            run = report["run"]
            summary = report["summary"]
            rows.append(
                {
                    "run_id": run_id,
                    "strategy_name": run["strategy_name"],
                    "start_date": run["start_date"],
                    "end_date": run["end_date"],
                    "stock_codes": run["stock_codes"],
                    "initial_cash": run["initial_cash"],
                    "interval_minutes": run["interval_minutes"],
                    "total_return_rate": run["total_return_rate"],
                    "max_drawdown_rate": run["max_drawdown_rate"],
                    "trade_count": run["trade_count"],
                    "win_rate": run["win_rate"],
                    "payoff_ratio": run["payoff_ratio"],
                    "net_pnl": summary["net_pnl"],
                    "total_cost": summary["total_cost"],
                    "strategy_settings": run["strategy_settings"],
                    "risk_settings": run["risk_settings"],
                }
            )

        return {
            "runs": rows,
            "leaders": {
                "highest_return": self._select(
                    rows,
                    "total_return_rate",
                    highest=True,
                ),
                "lowest_drawdown": self._select(
                    rows,
                    "max_drawdown_rate",
                    highest=False,
                ),
                "highest_win_rate": self._select(
                    rows,
                    "win_rate",
                    highest=True,
                ),
                "highest_payoff_ratio": self._select(
                    rows,
                    "payoff_ratio",
                    highest=True,
                    ignore_none=True,
                ),
                "lowest_cost": self._select(
                    rows,
                    "total_cost",
                    highest=False,
                ),
            },
            "warnings": self._comparison_warnings(rows),
        }

    @staticmethod
    def _select(
        rows: list[dict],
        key: str,
        *,
        highest: bool,
        ignore_none: bool = False,
    ) -> Optional[dict]:
        candidates = (
            [row for row in rows if row[key] is not None]
            if ignore_none
            else rows
        )
        if not candidates:
            return None
        selected = (
            max(candidates, key=lambda row: (row[key], -row["run_id"]))
            if highest
            else min(candidates, key=lambda row: (row[key], row["run_id"]))
        )
        return {
            "run_id": selected["run_id"],
            "value": selected[key],
        }

    @staticmethod
    def _comparison_warnings(rows: list[dict]) -> list[str]:
        checks: list[tuple[str, str]] = [
            ("기간", "period"),
            ("초기자금", "initial_cash"),
            ("종목 구성", "stock_codes"),
            ("분봉 간격", "interval_minutes"),
            ("전략 설정", "strategy_settings"),
            ("리스크 설정", "risk_settings"),
        ]
        normalized: dict[str, list[Any]] = {
            "period": [
                (row["start_date"], row["end_date"])
                for row in rows
            ],
            "initial_cash": [row["initial_cash"] for row in rows],
            "stock_codes": [
                tuple(row["stock_codes"])
                for row in rows
            ],
            "interval_minutes": [
                row["interval_minutes"]
                for row in rows
            ],
            "strategy_settings": [
                tuple(sorted(row["strategy_settings"].items()))
                for row in rows
            ],
            "risk_settings": [
                tuple(sorted(row["risk_settings"].items()))
                for row in rows
            ],
        }
        return [
            f"{label}이 서로 달라 직접 비교에 주의가 필요합니다."
            for label, key in checks
            if len(set(normalized[key])) > 1
        ]
