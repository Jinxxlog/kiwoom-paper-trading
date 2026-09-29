"""Data-only candle refresh; no strategy, account, order, or notification APIs.

Examples:
  python tools/refresh_candles.py --kind both
  python tools/refresh_candles.py --kind daily --universe
  python tools/refresh_candles.py --kind both --codes A005930 A000660 --check
"""

import argparse
import gzip
import json
import pathlib
import sys
import time
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import load_database_path, load_settings
from data.api_budget import ApiBudgetManager
from data.candle_freshness import CandleFreshness, CandleRefreshService
from data.collector import MarketDataCollector
from data.market_calendar import SEOUL_TIMEZONE
from data.scheduler import ProcessLock
from data.watchlist import normalize_stock_code
from db.database import Database
from kiwoom_rest_client import KiwoomRestClient
from screening.market_product import is_market_product_name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("daily", "minute", "both"), default="both")
    parser.add_argument("--codes", nargs="+")
    parser.add_argument("--universe", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--interval", type=int, choices=(1, 3, 5, 10, 15, 30, 45, 60), default=5)
    parser.add_argument("--minute-pages", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    if args.codes and args.universe:
        parser.error("--codes and --universe are mutually exclusive")
    if args.delay < 0.5 or args.minute_pages < 1 or (args.limit is not None and args.limit < 1):
        parser.error("delay >= 0.5, minute-pages >= 1, limit >= 1 required")
    db_path = load_database_path().resolve()
    if not db_path.exists() or not db_path.stat().st_size:
        parser.error("Existing initialized database required")
    database = Database(db_path)
    freshness = CandleFreshness(database)
    with database.connect() as connection:
        if args.codes:
            codes = list(dict.fromkeys(normalize_stock_code(c) for c in args.codes))
        elif args.universe:
            rows = connection.execute("""SELECT stock_code,stock_name FROM stock_universe
                WHERE tradable=1 AND management=0 AND halted=0 AND overheated=0
                AND market IN ('KOSPI','KOSDAQ') ORDER BY stock_code""").fetchall()
            codes = [r["stock_code"] for r in rows if not is_market_product_name(r["stock_name"])]
        else:
            rows = connection.execute("""SELECT stock_code FROM paper_positions
                UNION SELECT stock_code FROM watchlist WHERE enabled=1
                UNION SELECT stock_code FROM paper_fills WHERE side='buy'
                UNION SELECT stock_code FROM candidate_scores
                    WHERE run_id=(SELECT MAX(id) FROM candidate_runs) AND selected=1
                ORDER BY stock_code""").fetchall()
            codes = [r["stock_code"] for r in rows]
    if args.limit:
        codes = codes[:args.limit]
    started = datetime.now(SEOUL_TIMEZONE)
    run_id = started.strftime("%Y%m%d-%H%M%S-%f")
    report_path = args.report or ROOT / "reports" / f"candle-refresh-{run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": started.isoformat(), "status": "running", "kind": args.kind,
              "check_only": args.check, "total_symbols": len(codes), "processed_symbols": 0,
              "rows": [], "backup_directory": None}
    secrets = []
    service = None
    if not args.check:
        settings = load_settings()
        secrets = [settings.app_key, settings.secret_key]
        client = KiwoomRestClient(settings.app_key, settings.secret_key, use_mock=settings.use_mock,
                                  timeout=15, max_retries=1, api_budget_manager=ApiBudgetManager(database))
        service = CandleRefreshService(database, MarketDataCollector(client, database))
        backup = db_path.parent / "backups" / f"candles-before-{run_id}"
        backup.mkdir(parents=True, exist_ok=False)
        report["backup_directory"] = str(backup)
        report["server"] = "mock" if settings.use_mock else "production"

    def save():
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(report_path)

    save()
    print(f"symbols={len(codes)} report={report_path}", flush=True)
    kinds = ("daily", "minute") if args.kind == "both" else (args.kind,)
    try:
        with ProcessLock(db_path.parent / "candle-refresh.lock"):
            for index, code in enumerate(codes, 1):
                for kind in kinds:
                    now = datetime.now(SEOUL_TIMEZONE)
                    try:
                        if args.check:
                            result = freshness.daily_status(code, now) if kind == "daily" else freshness.minute_status(code, now, args.interval)
                        else:
                            # Preserve old normalized candles before a vendor adjustment can overwrite them.
                            before = freshness.daily_status(code, now) if kind == "daily" else None
                            if before is None or not before["ready"]:
                                with database.connect() as connection:
                                    old = connection.execute("SELECT * FROM candles WHERE stock_code=? AND candle_type=? AND interval_minutes=?",
                                        (code, kind, 0 if kind == "daily" else args.interval)).fetchall()
                                with gzip.open(backup / f"{code}-{kind}.json.gz", "wt", encoding="utf-8") as stream:
                                    json.dump([dict(r) for r in old], stream, ensure_ascii=False)
                            result = service.daily(code, now) if kind == "daily" else service.minute(code, now, interval=args.interval, max_pages=args.minute_pages)
                            if result.get("action") != "already_current":
                                time.sleep(args.delay)
                        report["rows"].append(result)
                    except Exception as exc:
                        message = str(exc)
                        for secret in secrets + ([client.token] if not args.check else []):
                            if secret:
                                message = message.replace(secret, "[REDACTED]")
                        report["rows"].append({"stock_code": code, "kind": kind, "ready": False,
                                               "reason": "refresh_failed", "message": message[:500]})
                        if not args.check:
                            time.sleep(1)
                report["processed_symbols"] = index
                save()
                if index == 1 or index % 10 == 0 or index == len(codes):
                    print(f"{index}/{len(codes)} ready={sum(r['ready'] for r in report['rows'])}/{len(report['rows'])}", flush=True)
            report["status"] = "complete" if all(r["ready"] for r in report["rows"]) and codes else "needs_attention"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except Exception:
        report["status"] = "failed"
        raise
    finally:
        report["finished_at"] = datetime.now(SEOUL_TIMEZONE).isoformat()
        save()
    print(report["status"], flush=True)
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
