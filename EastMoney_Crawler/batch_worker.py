#!/usr/bin/env python3
"""Batch worker for the CSV-native EastMoney pipeline.

The worker discovers stock CSV files, atomically claims one stock at a time,
runs Stage 1 -> Stage 2 -> Stage 3, and writes progress files that make the
whole run resumable after crashes or reboots.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_DIR = Path(__file__).resolve().parent
AUTO_PIPELINE = PROJECT_DIR / "auto_pipeline_000001.py"
DEFAULT_PROGRESS_DIR = PROJECT_DIR / "batch_progress"
DEFAULT_PROGRESS_FULL_DIR = PROJECT_DIR / "batch_progress_full_20090101"
DEFAULT_LOG_DIR = PROJECT_DIR / "batch_logs"
DEFAULT_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_TEMP_DIR = PROJECT_DIR / "temp_extract"
STOCK_CSV_RE = re.compile(r"^\d{6}\.csv$")
REQUIRED_CSV_COLUMNS = {"post_id", "post_publish_time", "post_title", "url", "content"}
TERMINAL_SUFFIXES = (".done", ".failed", ".failed_upload")


@dataclass
class StageResult:
    returncode: int
    seconds: float
    output_tail: str


@dataclass
class ClaimedStock:
    stock: str
    source_csv: Path
    lock_path: Path


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_stock(stock: str) -> str:
    return str(stock).strip().zfill(6)


def unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def default_source_dirs() -> list[Path]:
    candidates = [PROJECT_DIR]
    for child in PROJECT_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            has_stock_csv = any(
                item.is_file() and STOCK_CSV_RE.match(item.name)
                for item in child.iterdir()
            )
        except OSError:
            has_stock_csv = False
        if has_stock_csv:
            candidates.append(child)
    return candidates


def collect_source_dirs(cli_source_dirs: list[str] | None) -> list[Path]:
    candidates: list[Path] = []
    for value in cli_source_dirs or []:
        candidates.append(Path(value))
    candidates.extend(default_source_dirs())
    return unique_paths(path for path in candidates if path.exists())


def discover_stock_csvs(source_dirs: list[Path]) -> dict[str, Path]:
    stocks: dict[str, Path] = {}
    for source_dir in source_dirs:
        if not source_dir.is_dir():
            continue
        for csv_path in source_dir.iterdir():
            if not csv_path.is_file():
                continue
            if not STOCK_CSV_RE.match(csv_path.name):
                continue
            stocks.setdefault(csv_path.stem, csv_path.resolve())
    return dict(sorted(stocks.items()))


def load_stock_list(path: Path) -> dict[str, Path | None]:
    """从单列股票代码清单加载任务。

    每行一个代码，支持空行和以 # 开头的注释行。
    返回有序 dict: {stock_code: source_csv_or_None}。
    full 模式下 source_csv 为 None，不需要历史 CSV。
    """
    stocks: dict[str, Path | None] = {}
    if not path.exists():
        return stocks
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            code = normalize_stock(line)
            if code and code not in stocks:
                stocks[code] = None
    return stocks


def filter_stocks(stocks: dict[str, Path | None], requested: list[str] | None) -> dict[str, Path | None]:
    if not requested:
        return stocks
    filtered: dict[str, Path | None] = {}
    for stock in requested:
        normalized = normalize_stock(stock)
        if normalized in stocks:
            filtered[normalized] = stocks[normalized]
        else:
            filtered[normalized] = None
    return filtered


def validate_source_csv(csv_path: Path) -> tuple[bool, str]:
    if not csv_path.exists():
        return False, "missing_source_csv"
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = set(reader.fieldnames or [])
    except UnicodeDecodeError:
        return False, "invalid_csv_encoding"
    except OSError:
        return False, "source_csv_unreadable"

    missing = sorted(REQUIRED_CSV_COLUMNS - fieldnames)
    if missing:
        return False, f"invalid_csv_schema:missing={','.join(missing)}"
    return True, ""


def state_path(progress_dir: Path, stock: str, suffix: str) -> Path:
    return progress_dir / f"{stock}{suffix}"


def has_terminal_state(progress_dir: Path, stock: str) -> bool:
    return any(state_path(progress_dir, stock, suffix).exists() for suffix in TERMINAL_SUFFIXES)


def has_done_state(progress_dir: Path, stock: str) -> bool:
    return state_path(progress_dir, stock, ".done").exists()


def has_failed_state(progress_dir: Path, stock: str) -> bool:
    return any(
        state_path(progress_dir, stock, suffix).exists()
        for suffix in (".failed", ".failed_upload")
    )


def is_stale(lock_path: Path, stale_seconds: float) -> bool:
    if not lock_path.exists():
        return False
    age = time.time() - lock_path.stat().st_mtime
    return age > stale_seconds


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def mark_state(progress_dir: Path, stock: str, suffix: str, payload: dict) -> None:
    payload = dict(payload)
    payload.setdefault("stock", stock)
    payload.setdefault("updated_at", now_iso())
    write_json(state_path(progress_dir, stock, suffix), payload)


def remove_state(progress_dir: Path, stock: str, suffix: str) -> None:
    path = state_path(progress_dir, stock, suffix)
    if path.exists():
        path.unlink()


def claim_stock(
    stock: str,
    source_csv: Path | None,
    progress_dir: Path,
    worker_id: str,
    stale_seconds: float,
    retry_failed: bool = False,
) -> ClaimedStock | None:
    progress_dir.mkdir(parents=True, exist_ok=True)

    if has_done_state(progress_dir, stock):
        return None
    if has_failed_state(progress_dir, stock) and not retry_failed:
        return None

    lock_path = state_path(progress_dir, stock, ".lock")
    if lock_path.exists() and is_stale(lock_path, stale_seconds):
        try:
            lock_path.unlink()
            print(f"[{worker_id}] reclaimed stale lock: {stock}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[{worker_id}] cannot reclaim lock {stock}: {exc}")
            return None

    payload: dict = {
        "stock": stock,
        "worker_id": worker_id,
        "pid": os.getpid(),
        "created_at": now_iso(),
    }
    if source_csv is not None:
        payload["source_csv"] = str(source_csv)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
    except FileExistsError:
        return None

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return ClaimedStock(stock=stock, source_csv=source_csv or Path(), lock_path=lock_path)


def heartbeat(lock_path: Path, stop_event: threading.Event, interval: float) -> None:
    while not stop_event.wait(interval):
        try:
            os.utime(lock_path, None)
        except OSError:
            return


def disk_free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)


def output_csv_size_mb(stock: str) -> float:
    candidates = [
        DEFAULT_DATA_DIR / f"{stock}_enhanced.csv",
        PROJECT_DIR / "temp_export" / f"{stock}_enhanced.csv",
    ]
    for path in candidates:
        if path.exists():
            return round(path.stat().st_size / 1024 / 1024, 2)
    return 0.0


def detail_failed_count(stock: str) -> int:
    path = DEFAULT_TEMP_DIR / f"{stock}_detail_failed.jsonl"
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        return sum(1 for line in handle if line.strip())


def clean_per_stock_temp(stock: str, crawl_mode: str = "incremental") -> None:
    """处理某只股票前清理该股票的旧临时产物，避免过期 base/new 被复用。"""
    patterns = [
        DEFAULT_TEMP_DIR / f"{stock}_base.csv",
        DEFAULT_TEMP_DIR / f"{stock}_new_posts.csv",
        DEFAULT_TEMP_DIR / f"{stock}_full_posts.csv",
        DEFAULT_TEMP_DIR / f"{stock}_stage1_manifest.json",
        DEFAULT_TEMP_DIR / f"{stock}_full_manifest.json",
        DEFAULT_TEMP_DIR / f"{stock}_detail_failed.jsonl",
        DEFAULT_TEMP_DIR / f"{stock}_content_delta.jsonl",
        DEFAULT_TEMP_DIR / f"{stock}_detail_checkpoint.json",
        PROJECT_DIR / "temp_export" / f"{stock}_enhanced.csv",
        PROJECT_DIR / "temp_export" / f"{stock}_full_20090101.csv",
        PROJECT_DIR / "temp_export" / f"{stock}_comments.csv",
        PROJECT_DIR / ".pipeline_flags" / f"{stock}_stage1.done",
        PROJECT_DIR / ".pipeline_flags" / f"{stock}_stage2.done",
    ]
    if crawl_mode == "incremental":
        patterns.extend([
            PROJECT_DIR / "batch_progress" / f"{stock}.lock",
            PROJECT_DIR / "batch_progress" / f"{stock}.retrying",
        ])
    else:
        patterns.extend([
            PROJECT_DIR / "batch_progress_full_20090101" / f"{stock}.lock",
            PROJECT_DIR / "batch_progress_full_20090101" / f"{stock}.retrying",
        ])
    removed = []
    for path in patterns:
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError:
            pass
    if removed:
        print(f"[{stock}] cleaned stale temp files: {', '.join(removed)}")


def stream_subprocess(cmd: list[str], stage: int, stock: str) -> StageResult:
    started = time.time()
    print(f"[{stock}][stage {stage}] running: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(f"[{stock}][stage {stage}] {line}", end="")
        tail.append(line)
        if len(tail) > 200:
            tail = tail[-200:]
    returncode = process.wait()
    seconds = time.time() - started
    print(f"[{stock}][stage {stage}] exit={returncode}, seconds={seconds:.1f}")
    return StageResult(returncode=returncode, seconds=seconds, output_tail="".join(tail))


def build_stage_cmd(
    python_bin: str,
    stock: str,
    stage: int,
    source_dirs: list[Path],
    detail_workers: int,
    crawl_mode: str = "incremental",
    start_date: str = "2009-01-01",
    list_workers: int = 6,
    list_source: str = "html",
    list_page_limit: int = 0,
) -> list[str]:
    cmd = [
        python_bin,
        str(AUTO_PIPELINE),
        "--stock",
        stock,
        "--stage",
        str(stage),
        "--crawl-mode",
        crawl_mode,
    ]
    if crawl_mode == "full":
        cmd.extend([
            "--start-date", start_date,
            "--list-workers", str(list_workers),
            "--list-source", list_source,
        ])
        if list_page_limit:
            cmd.extend(["--list-page-limit", str(list_page_limit)])
    if stage == 1 and crawl_mode == "incremental":
        for source_dir in source_dirs:
            cmd.extend(["--source-dir", str(source_dir)])
    if stage == 2:
        cmd.extend(["--detail-workers", str(detail_workers)])
    return cmd


def classify_stage_failure(stage: int, result: StageResult) -> str:
    tail = result.output_tail
    if stage == 3 and ("上传失败" in tail or "upload" in tail.lower()):
        return "upload_failed"
    return f"stage{stage}_exit_{result.returncode}"


def output_csv_size_mb(stock: str, crawl_mode: str = "incremental") -> float:
    if crawl_mode == "full":
        candidates = [
            DEFAULT_DATA_DIR / f"{stock}_full_20090101.csv",
            PROJECT_DIR / "temp_export" / f"{stock}_full_20090101.csv",
        ]
    else:
        candidates = [
            DEFAULT_DATA_DIR / f"{stock}_enhanced.csv",
            PROJECT_DIR / "temp_export" / f"{stock}_enhanced.csv",
        ]
    for path in candidates:
        if path.exists():
            return round(path.stat().st_size / 1024 / 1024, 2)
    return 0.0


def run_stock_pipeline(
    stock: str,
    source_csv: Path | None,
    args: argparse.Namespace,
    source_dirs: list[Path],
) -> tuple[bool, dict, str]:
    crawl_mode = getattr(args, "crawl_mode", "incremental")
    start_date = getattr(args, "start_date", "2009-01-01")
    list_workers = getattr(args, "list_workers", 6)
    list_page_limit = getattr(args, "list_page_limit", 0)

    summary = {
        "stock": stock,
        "worker_id": args.worker_id,
        "crawl_mode": crawl_mode,
        "source_csv": str(source_csv) if source_csv else "",
        "started_at": now_iso(),
        "stage1_seconds": 0.0,
        "stage2_seconds": 0.0,
        "stage3_seconds": 0.0,
        "total_seconds": 0.0,
        "output_csv_mb": 0.0,
        "detail_failed_count": 0,
        "exit_code": 1,
        "attempts": 0,
    }

    if crawl_mode == "incremental":
        if source_csv is None or not source_csv.exists():
            reason = "missing_source_csv"
            summary.update({"failed_reason": reason, "finished_at": now_iso()})
            return False, summary, reason
        valid, reason = validate_source_csv(source_csv)
        if not valid:
            summary.update({"failed_reason": reason, "finished_at": now_iso()})
            return False, summary, reason

    # 每次尝试前都清理该股票的旧临时产物，保证 Stage 1 从干净状态开始
    clean_per_stock_temp(stock, crawl_mode=crawl_mode)

    total_started = time.time()
    final_reason = ""

    for attempt in range(1, args.max_retries + 2):
        summary["attempts"] = attempt
        if attempt > 1:
            mark_state(
                args.progress_dir,
                stock,
                ".retrying",
                {
                    "worker_id": args.worker_id,
                    "attempt": attempt,
                    "max_retries": args.max_retries,
                    "previous_reason": final_reason,
                },
            )

        failed_stage = None
        failed_result = None
        for stage in (1, 2, 3):
            result = stream_subprocess(
                build_stage_cmd(
                    args.python,
                    stock,
                    stage,
                    source_dirs=source_dirs,
                    detail_workers=args.detail_workers,
                    crawl_mode=crawl_mode,
                    start_date=start_date,
                    list_workers=list_workers,
                    list_source=getattr(args, "list_source", "html"),
                    list_page_limit=list_page_limit,
                ),
                stage=stage,
                stock=stock,
            )
            summary[f"stage{stage}_seconds"] = round(
                summary[f"stage{stage}_seconds"] + result.seconds, 2
            )
            if result.returncode != 0:
                failed_stage = stage
                failed_result = result
                final_reason = classify_stage_failure(stage, result)
                break

        if failed_stage is None:
            summary.update(
                {
                    "total_seconds": round(time.time() - total_started, 2),
                    "output_csv_mb": output_csv_size_mb(stock, crawl_mode=crawl_mode),
                    "detail_failed_count": detail_failed_count(stock),
                    "exit_code": 0,
                    "finished_at": now_iso(),
                }
            )
            remove_state(args.progress_dir, stock, ".retrying")
            return True, summary, ""

        if failed_stage == 3 and final_reason == "upload_failed":
            summary.update(
                {
                    "total_seconds": round(time.time() - total_started, 2),
                    "output_csv_mb": output_csv_size_mb(stock, crawl_mode=crawl_mode),
                    "detail_failed_count": detail_failed_count(stock),
                    "exit_code": failed_result.returncode if failed_result else 1,
                    "failed_reason": final_reason,
                    "finished_at": now_iso(),
                }
            )
            remove_state(args.progress_dir, stock, ".retrying")
            return False, summary, final_reason

        print(
            f"[{stock}] attempt {attempt}/{args.max_retries + 1} failed: {final_reason}"
        )

    summary.update(
        {
            "total_seconds": round(time.time() - total_started, 2),
            "output_csv_mb": output_csv_size_mb(stock, crawl_mode=crawl_mode),
            "detail_failed_count": detail_failed_count(stock),
            "exit_code": 1,
            "failed_reason": final_reason,
            "finished_at": now_iso(),
        }
    )
    remove_state(args.progress_dir, stock, ".retrying")
    return False, summary, final_reason


def write_summary(progress_dir: Path, stock: str, summary: dict) -> None:
    write_json(state_path(progress_dir, stock, ".summary.json"), summary)


def process_claim(claim: ClaimedStock, args: argparse.Namespace, source_dirs: list[Path]) -> bool:
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=heartbeat,
        args=(claim.lock_path, stop_event, args.heartbeat_seconds),
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        success, summary, reason = run_stock_pipeline(
            claim.stock,
            claim.source_csv,
            args,
            source_dirs=source_dirs,
        )
        write_summary(args.progress_dir, claim.stock, summary)
        if success:
            mark_state(
                args.progress_dir,
                claim.stock,
                ".done",
                {"worker_id": args.worker_id, "summary": f"{claim.stock}.summary.json"},
            )
            return True
        if reason == "upload_failed":
            mark_state(
                args.progress_dir,
                claim.stock,
                ".failed_upload",
                {
                    "worker_id": args.worker_id,
                    "reason": reason,
                    "summary": f"{claim.stock}.summary.json",
                },
            )
            return False
        mark_state(
            args.progress_dir,
            claim.stock,
            ".failed",
            {
                "worker_id": args.worker_id,
                "reason": reason,
                "summary": f"{claim.stock}.summary.json",
            },
        )
        return False
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=2)
        try:
            claim.lock_path.unlink()
        except FileNotFoundError:
            pass


def find_next_claim(
    stocks: dict[str, Path | None],
    args: argparse.Namespace,
) -> ClaimedStock | None:
    stale_seconds = args.stale_lock_hours * 3600
    for stock, source_csv in stocks.items():
        claim = claim_stock(
            stock,
            source_csv,
            args.progress_dir,
            args.worker_id,
            stale_seconds=stale_seconds,
            retry_failed=args.retry_failed,
        )
        if claim is not None:
            return claim
    return None


def run_dry_run(stocks: dict[str, Path | None], limit: int | None) -> int:
    items = list(stocks.items())
    if limit is not None:
        items = items[:limit]
    print(f"[dry-run] will process {len(items)} stock(s)")
    for stock, source_csv in items:
        print(f"[dry-run] {stock} <- {source_csv or '(no source CSV required)'}")
    return 0


def worker_loop(args: argparse.Namespace, stocks: dict[str, Path | None], source_dirs: list[Path]) -> int:
    args.progress_dir.mkdir(parents=True, exist_ok=True)
    processed = 0
    successes = 0
    failures = 0

    while args.limit is None or processed < args.limit:
        free_gb = disk_free_gb(PROJECT_DIR)
        if free_gb < args.min_free_gb:
            print(
                f"[{args.worker_id}] disk free {free_gb:.2f}GB < {args.min_free_gb:.2f}GB; "
                f"waiting {args.disk_wait_seconds}s before claiming new stock"
            )
            time.sleep(args.disk_wait_seconds)
            continue

        claim = find_next_claim(stocks, args)
        if claim is None:
            print(f"[{args.worker_id}] no pending stock left")
            break

        print(f"[{args.worker_id}] claimed {claim.stock}: {claim.source_csv}")
        processed += 1
        if process_claim(claim, args, source_dirs=source_dirs):
            successes += 1
        else:
            failures += 1

    print(
        f"[{args.worker_id}] finished: processed={processed}, success={successes}, failure={failures}"
    )
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch worker for EastMoney CSV pipeline")
    parser.add_argument("--worker-id", default=f"worker_{os.getpid()}")
    parser.add_argument("--source-dir", action="append", default=[],
                        help="Directory containing stock CSV files; may be provided multiple times (incremental mode only)")
    parser.add_argument("--stock-list", type=Path, default=None,
                        help="Path to a single-column stock code list file (full mode)")
    parser.add_argument("--crawl-mode", choices=["incremental", "full"], default="incremental",
                        help="Crawl mode: incremental uses historical CSVs, full crawls from start-date on the web")
    parser.add_argument("--start-date", default="2009-01-01",
                        help="Full mode start date (YYYY-MM-DD)")
    parser.add_argument("--list-workers", type=int, default=6,
                        help="Full mode Stage 1 list-page requests workers")
    parser.add_argument("--list-source", choices=["html", "api", "auto", "selenium"], default="html",
                        help="Full mode Stage 1 list source; html/api/auto use fast requests HTML")
    parser.add_argument("--list-page-limit", type=int, default=0,
                        help="Full mode trial page limit; 0 means no limit")
    parser.add_argument("--progress-dir", type=Path, default=None)
    parser.add_argument("--detail-workers", type=int, default=3)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--stale-lock-hours", type=float, default=3.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=60.0)
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--disk-wait-seconds", type=float, default=300.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--stock", action="append", default=None,
                        help="Restrict processing to one stock; may be provided multiple times")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Allow claiming stocks with .failed/.failed_upload state")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--python", default=sys.executable)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.progress_dir is None:
        args.progress_dir = (
            DEFAULT_PROGRESS_FULL_DIR
            if args.crawl_mode == "full"
            else DEFAULT_PROGRESS_DIR
        )
    args.progress_dir = args.progress_dir.expanduser().resolve()

    if args.crawl_mode == "full":
        if args.stock_list is None:
            default_list = PROJECT_DIR / "数据_list.csv"
            if default_list.exists():
                args.stock_list = default_list
            else:
                print("[error] full mode requires --stock-list or 数据_list.csv in project dir")
                return 1
        stocks = load_stock_list(args.stock_list)
        stocks = filter_stocks(stocks, args.stock)
        source_dirs: list[Path] = []
    else:
        source_dirs = collect_source_dirs(args.source_dir)
        if not source_dirs:
            print("[error] no source directories found")
            return 1
        stocks = discover_stock_csvs(source_dirs)
        stocks = filter_stocks(stocks, args.stock)

    if args.limit is not None:
        stocks = dict(list(stocks.items())[:args.limit])

    if not stocks:
        print("[error] no stock tasks discovered")
        return 1

    print(f"[{args.worker_id}] mode: {args.crawl_mode}")
    if args.crawl_mode == "incremental":
        print(f"[{args.worker_id}] source dirs:")
        for source_dir in source_dirs:
            print(f"  - {source_dir}")
    print(f"[{args.worker_id}] discovered {len(stocks)} stock(s)")

    if args.dry_run:
        return run_dry_run(stocks, args.limit)

    return worker_loop(args, stocks, source_dirs=source_dirs)


if __name__ == "__main__":
    raise SystemExit(main())
