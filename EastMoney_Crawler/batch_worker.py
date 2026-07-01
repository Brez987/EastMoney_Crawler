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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


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
DEFERRED_SUFFIX = ".deferred"
OWNED_STATE_SUFFIXES = TERMINAL_SUFFIXES + (".retrying", DEFERRED_SUFFIX)
SPACE_ESTIMATE_CACHE: dict[tuple[str, str], tuple[float, float]] = {}


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


def read_json_file(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {}


def read_lock_worker_id(lock_path: Path) -> str | None:
    data = read_json_file(lock_path)
    if data is None:
        return None
    if not data:
        return ""
    return str(data.get("worker_id") or "")


def deferred_retry_after_epoch(progress_dir: Path, stock: str, now: float | None = None) -> float | None:
    path = state_path(progress_dir, stock, DEFERRED_SUFFIX)
    data = read_json_file(path)
    if data is None:
        return None
    retry_after = float(data.get("retry_after_epoch") or 0)
    if retry_after > (time.time() if now is None else now):
        return retry_after
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return time.time() + 30.0
    return None


def is_deferred_state_active(progress_dir: Path, stock: str, now: float | None = None) -> bool:
    return deferred_retry_after_epoch(progress_dir, stock, now=now) is not None


def active_deferred_status(
    stocks: dict[str, Path | None],
    progress_dir: Path,
    retry_failed: bool = False,
) -> tuple[int, float | None]:
    count = 0
    next_retry: float | None = None
    now = time.time()
    for stock in stocks:
        if has_done_state(progress_dir, stock):
            continue
        if has_failed_state(progress_dir, stock) and not retry_failed:
            continue
        retry_after = deferred_retry_after_epoch(progress_dir, stock, now=now)
        if retry_after is None:
            continue
        count += 1
        if next_retry is None or retry_after < next_retry:
            next_retry = retry_after
    return count, next_retry


def is_retryable_failure_reason(reason: str) -> bool:
    if reason == "upload_failed":
        return True
    if reason.startswith("stage"):
        return True
    return reason in {
        "timeout",
        "network",
        "blocked",
        "resource",
    }


def write_json(path: Path, payload: dict, max_retries: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        tmp_path = path.with_name(
            f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{attempt}"
        )
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_path, path)
            return
        except (PermissionError, FileNotFoundError) as exc:
            last_error = exc
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            if attempt == max_retries:
                raise
            delay = 0.5 * attempt
            print(f"[write_json] retry {attempt}/{max_retries} after {delay:.1f}s: {exc}")
            time.sleep(delay)
    if last_error is not None:
        raise last_error


def mark_state(progress_dir: Path, stock: str, suffix: str, payload: dict) -> bool:
    payload = dict(payload)
    payload.setdefault("stock", stock)
    payload.setdefault("updated_at", now_iso())
    worker_id = str(payload.get("worker_id") or "")
    if worker_id and suffix in OWNED_STATE_SUFFIXES:
        lock_path = state_path(progress_dir, stock, ".lock")
        lock_worker_id = read_lock_worker_id(lock_path)
        if lock_worker_id != worker_id:
            owner = lock_worker_id if lock_worker_id else "missing"
            print(f"[{worker_id}] skip {stock}{suffix}: lock owner is {owner}")
            return False
    write_json(state_path(progress_dir, stock, suffix), payload)
    return True


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
    if is_deferred_state_active(progress_dir, stock):
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


def heartbeat(
    lock_path: Path,
    worker_id: str,
    stop_event: threading.Event,
    interval: float,
) -> None:
    while not stop_event.wait(interval):
        try:
            if read_lock_worker_id(lock_path) != worker_id:
                return
            os.utime(lock_path, None)
        except OSError:
            return


def disk_free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / (1024 ** 3)


def estimate_stock_space_gb(crawl_mode: str, ttl_seconds: float = 300.0) -> float:
    cache_key = (str(DEFAULT_DATA_DIR.resolve()), crawl_mode)
    now = time.time()
    cached = SPACE_ESTIMATE_CACHE.get(cache_key)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]

    if crawl_mode == "full":
        patterns = ("*_full_*.csv",)
        default_gb = 0.5
    else:
        patterns = ("*_enhanced.csv", "*.csv")
        default_gb = 0.1

    sizes: list[int] = []
    if DEFAULT_DATA_DIR.exists():
        candidates: list[Path] = []
        for pattern in patterns:
            candidates.extend(DEFAULT_DATA_DIR.glob(pattern))
        try:
            candidates = sorted(
                candidates,
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            candidates = list(candidates)
        for path in candidates[:200]:
            try:
                if path.is_file():
                    size = path.stat().st_size
                    if size > 0:
                        sizes.append(size)
            except OSError:
                continue

    if sizes:
        average_gb = (sum(sizes) / len(sizes)) / (1024 ** 3)
        estimate = max(default_gb, average_gb * 2)
    else:
        estimate = default_gb

    SPACE_ESTIMATE_CACHE[cache_key] = (now, estimate)
    return estimate


def enough_disk_for_stock(args: argparse.Namespace) -> tuple[bool, float, float]:
    free_gb = disk_free_gb(PROJECT_DIR)
    estimated_gb = estimate_stock_space_gb(getattr(args, "crawl_mode", "incremental"))
    required_gb = max(float(args.min_free_gb), estimated_gb * 2)
    return free_gb >= required_gb, free_gb, required_gb


def detail_failed_count(stock: str) -> int:
    path = DEFAULT_TEMP_DIR / f"{stock}_detail_failed.jsonl"
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        return sum(1 for line in handle if line.strip())


def clean_per_stock_temp(
    stock: str,
    crawl_mode: str = "incremental",
    from_stage: int = 1,
) -> None:
    """处理某只股票前清理该股票的旧临时产物，避免过期 base/new 被复用。"""
    patterns: list[Path] = []
    if from_stage <= 1:
        patterns.extend([
            DEFAULT_TEMP_DIR / f"{stock}_base.csv",
            DEFAULT_TEMP_DIR / f"{stock}_new_posts.csv",
            DEFAULT_TEMP_DIR / f"{stock}_full_posts.csv",
            DEFAULT_TEMP_DIR / f"{stock}_stage1_manifest.json",
            DEFAULT_TEMP_DIR / f"{stock}_full_manifest.json",
            PROJECT_DIR / ".pipeline_flags" / f"{stock}_stage1.done",
            PROJECT_DIR / ".pipeline_flags" / f"{stock}_stage2.done",
        ])
    elif from_stage <= 2:
        patterns.append(PROJECT_DIR / ".pipeline_flags" / f"{stock}_stage2.done")

    if from_stage <= 3:
        patterns.extend([
            PROJECT_DIR / "temp_export" / f"{stock}_enhanced.csv",
            PROJECT_DIR / "temp_export" / f"{stock}_full_20090101.csv",
            PROJECT_DIR / "temp_export" / f"{stock}_comments.csv",
        ])
    removed = []
    for path in patterns:
        try:
            if path.is_dir():
                import shutil as _shutil
                _shutil.rmtree(path, ignore_errors=True)
                removed.append(path.name + '/')
            elif path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError:
            pass
    if removed:
        print(f"[{stock}] cleaned stale temp files: {', '.join(removed)}")


# ── 实时进度解析 ──
# Stage 1 full (实际英文格式): "000001: [OK] progress 50/500 pages | new_rows 4000 | elapsed 120s | speed 25.0 pages/min | eta 18 min"
_RE_FULL_S1 = re.compile(
    r"(\d{6}):\s+\[(?:OK|WARN:\d+)\]\s+progress\s+(\d+)/(\d+)\s+pages\s*\|"
    r"\s*new_rows\s+(\d+)\s*\|"
    r"\s*elapsed\s+(\d+)s\s*\|"
    r"\s*speed\s+(\d+\.?\d*)\s*pages/min\s*\|"
    r"\s*eta\s+(\d+\.?\d*)\s*min"
)
# Stage 1 full (兼容旧中文格式): "000001: [OK] 进度 50/500 页 | 成功 50 页 | 累计 4000 条 | 耗时 120s | 速度 25.0页/分 | 预计剩余 18分"
_RE_FULL_S1_CN = re.compile(
    r"(\d{6}):\s+\[(?:OK|WARN:\d+)\]\s+进度\s+(\d+)/(\d+)\s+页"
    r"(?:\s*\((\d+)%\))?"
    r"\s*\|\s*(?:成功\s+)?(\d+)\s*页?\s*\|"
    r"\s*(?:累计\s+)?(\d+)\s*条"
    r"\s*\|\s*耗时\s+(\d+)s\s*\|"
    r"\s*速度\s+(\d+\.?\d*)\s*页/分\s*\|"
    r"\s*预计剩余\s+(\d+\.?\d*)\s*分"
)
# Stage 2 caifuhao: "000001: [CAIFUHAO] 50/100 (50%) | ok=50 | fail(perm)=0 | retry=0 | 419条/分 | ETA 0分"
_RE_CAIFUHAO = re.compile(
    r"(\d{6}):\s+\[CAIFUHAO(?:-PASS2)?\]\s+(\d+)/(\d+)\s+\((\d+)%\)"
    r"\s*\|\s*ok=(\d+)\s*\|"
    r"\s*fail(?:\(perm\))?=(\d+)"
    r"(?:\s*\|\s*retry=(\d+))?"
    r"\s*\|\s*(\d+\.?\d*)\s*条/分\s*\|"
    r"\s*ETA\s+(\d+\.?\d*)\s*分"
)
# Stage 2 guba parallel: "000001: 股吧 50/200 (25.0%) [worker-1] 成功, 空3, 延迟1.5s"
_RE_GUBA_PARALLEL = re.compile(
    r"(\d{6}):\s+股吧\s+(\d+)/(\d+)\s+\((\d+\.?\d*)%\)"
    r"(?:\s+\[worker-\d+\])?"
    r"\s+成功,?\s*空(\d+)"
)
# Stage 2 guba single: "000001: 股吧 50/200 (25.0%) 成功, 空3, 延迟1.5s"
_RE_GUBA_SINGLE = re.compile(
    r"(\d{6}):\s+股吧\s+(\d+)/(\d+)\s+\((\d+\.?\d*)%\)\s+成功,?\s*空(\d+)"
)
# Stage 1 incremental: "000001: 已经成功爬取第 5 页帖子基本信息"
_RE_INC_S1 = re.compile(
    r"(\d{6}):\s+已经成功爬取第\s+(\d+)\s+页帖子基本信息"
)
# Stage done markers
_RE_S1_DONE = re.compile(
    r"(\d{6}):\s+(?:全量列表爬取完成|full list crawl done|成功爬取.*共\s+\d+\s+页帖子|正文爬取完成.*共处理)"
    r"|\[Stage 1(?:\s+full)?\]\s+(?:complete|完成)"
)
_RE_S2_DONE = re.compile(
    r"(\d{6}):\s+(?:财富号爬取完成|正文爬取完成|guba.*爬取完成|股吧原生帖子.*爬取完成|股吧原生帖子.*并发完成|共处理\s+\d+\s+条帖子)"
    r"|\[Stage 2(?:\s+full)?\]\s+完成"
)
_RE_S3_DONE = re.compile(r"\[Stage\s+3(?:\s+full)?\]\s+(?:全部完成|完成|all complete|done)")
_RE_STAGE_DONE = re.compile(r"\[Stage\s+(\d)(?:\s+full)?\]\s+(?:完成！|完成|全部完成|complete|done)")

# ── 进度文件写入 ──
PROGRESS_SUFFIX = ".progress.json"


def write_live_progress(progress_dir: Path, stock: str, payload: dict) -> None:
    """写入实时进度文件（原子性写入，防读取撕裂）"""
    if progress_dir is None:
        return
    payload.setdefault("stock", stock)
    payload.setdefault("updated_at", now_iso())
    path = progress_dir / f"{stock}{PROGRESS_SUFFIX}"
    write_json(path, payload)


def parse_progress_line(line: str, stage: int, stock: str) -> dict | None:
    """从 auto_pipeline 的一行输出中提取进度信息。"""
    if stage == 1:
        # 优先匹配英文格式（当前实际输出）
        m = _RE_FULL_S1.search(line)
        if m:
            done_pages = int(m.group(2))
            total_pages = int(m.group(3))
            pct = int(done_pages / total_pages * 100) if total_pages > 0 else 0
            return {
                "stage": 1, "stage_label": "Stage 1",
                "status": "running",
                "progress": f"{m.group(2)}/{m.group(3)}",
                "pct": pct,
                "ok_pages": done_pages,
                "total_rows": int(m.group(4)),
                "elapsed": f"{m.group(5)}s",
                "speed": f"{m.group(6)}页/分",
                "eta": f"{m.group(7)}分",
            }
        # 兼容旧中文格式
        m = _RE_FULL_S1_CN.search(line)
        if m:
            pct = int(m.group(4)) if m.group(4) else (
                int(int(m.group(2)) / int(m.group(3)) * 100) if m.group(3) and int(m.group(3)) > 0 else 0
            )
            return {
                "stage": 1, "stage_label": "Stage 1",
                "status": "running",
                "progress": f"{m.group(2)}/{m.group(3)}",
                "pct": pct,
                "ok_pages": int(m.group(5)),
                "total_rows": int(m.group(6)),
                "elapsed": f"{m.group(7)}s",
                "speed": f"{m.group(8)}页/分",
                "eta": f"{m.group(9)}分",
            }
        m = _RE_INC_S1.search(line)
        if m:
            return {
                "stage": 1, "stage_label": "Stage 1",
                "status": "running",
                "progress": f"第{m.group(2)}页",
                "pct": 0,
            }
        if _RE_S1_DONE.search(line):
            return {"stage": 1, "stage_label": "Stage 1", "status": "done"}

    if stage == 2:
        m = _RE_CAIFUHAO.search(line)
        if m:
            return {
                "stage": 2, "stage_label": "Stage 2",
                "status": "running",
                "progress": f"{m.group(2)}/{m.group(3)}",
                "pct": int(m.group(4)),
                "ok": int(m.group(5)),
                "fail_perm": int(m.group(6)),
                "retry": int(m.group(7)) if m.group(7) else 0,
                "speed": f"{m.group(8)}条/分",
                "eta": f"{m.group(9)}分",
            }
        # 股吧并发/单线程进度
        m = _RE_GUBA_PARALLEL.search(line)
        if not m:
            m = _RE_GUBA_SINGLE.search(line)
        if m:
            done = int(m.group(2))
            total = int(m.group(3))
            pct = int(float(m.group(4)))
            return {
                "stage": 2, "stage_label": "Stage 2",
                "status": "running",
                "progress": f"{m.group(2)}/{m.group(3)}",
                "pct": pct,
                "ok": done,
                "empty": int(m.group(5)),
                "speed": "",
                "eta": "",
            }
        if _RE_S2_DONE.search(line):
            return {"stage": 2, "stage_label": "Stage 2", "status": "done"}

    if stage == 3:
        if _RE_S3_DONE.search(line):
            return {"stage": 3, "stage_label": "Stage 3", "status": "done"}

    # 通用 Stage 完成检测
    m = _RE_STAGE_DONE.search(line)
    if m:
        s = int(m.group(1))
        return {"stage": s, "stage_label": f"Stage {s}", "status": "done"}

    return None


def stream_subprocess(
    cmd: list[str],
    stage: int,
    stock: str,
    progress_dir: Path | None = None,
    idle_timeout: float = 3600.0,
) -> StageResult:
    """Run subprocess with idle-timeout detection.

    If no output is produced for `idle_timeout` seconds, the process is killed
    and the result is marked as a timeout failure.
    """
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
    last_line_time = time.time()
    done_event = threading.Event()
    reader_error: list[Exception | None] = [None]

    def reader() -> None:
        nonlocal last_line_time, tail
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(f"[{stock}][stage {stage}] {line}", end="")
                tail.append(line)
                if len(tail) > 200:
                    tail = tail[-200:]
                last_line_time = time.time()
                # 实时进度解析
                if progress_dir is not None:
                    progress = parse_progress_line(line, stage, stock)
                    if progress is not None:
                        write_live_progress(progress_dir, stock, progress)
        except Exception as exc:
            reader_error[0] = exc
        finally:
            done_event.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    # Poll for idle timeout
    while not done_event.is_set():
        reader_thread.join(timeout=5)
        if not done_event.is_set():
            idle = time.time() - last_line_time
            if idle > idle_timeout:
                print(
                    f"[{stock}][stage {stage}] TIMEOUT: no output for {idle:.0f}s "
                    f"(limit={idle_timeout:.0f}s), killing process..."
                )
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                seconds = time.time() - started
                tail.append(f"\n[TIMEOUT] idle for {idle:.0f}s, killed by batch_worker\n")
                return StageResult(
                    returncode=-1,
                    seconds=seconds,
                    output_tail="".join(tail),
                )

    if reader_error[0] is not None:
        print(f"[{stock}][stage {stage}] reader error: {reader_error[0]}")

    returncode = process.wait()
    if process.stdout is not None:
        process.stdout.close()
    seconds = time.time() - started
    print(f"[{stock}][stage {stage}] exit={returncode}, seconds={seconds:.1f}")
    return StageResult(returncode=returncode, seconds=seconds, output_tail="".join(tail))


def build_stage_cmd(
    python_bin: str,
    stock: str,
    stage: int | str,
    source_dirs: list[Path],
    detail_workers: int,
    crawl_mode: str = "incremental",
    start_date: str = "2009-01-01",
    list_workers: int = 6,
    list_window_pause_min: float = 0.3,
    list_window_pause_max: float = 1.2,
    list_source: str = "html",
    list_page_limit: int = 0,
) -> list[str]:
    stage_value = str(stage)
    cmd = [
        python_bin,
        str(AUTO_PIPELINE),
        "--stock",
        stock,
        "--stage",
        stage_value,
        "--crawl-mode",
        crawl_mode,
    ]
    if crawl_mode == "full":
        cmd.extend([
            "--start-date", start_date,
            "--list-workers", str(list_workers),
            "--list-window-pause-min", str(list_window_pause_min),
            "--list-window-pause-max", str(list_window_pause_max),
            "--list-source", list_source,
        ])
        if list_page_limit:
            cmd.extend(["--list-page-limit", str(list_page_limit)])
    if stage_value in {"1", "all"} and crawl_mode == "incremental":
        for source_dir in source_dirs:
            cmd.extend(["--source-dir", str(source_dir)])
    if stage_value in {"2", "all"}:
        cmd.extend(["--detail-workers", str(detail_workers)])
    return cmd


def classify_stage_failure(stage: int | str, result: StageResult) -> str:
    tail = result.output_tail
    tail_lower = tail.lower()
    stage_label = str(stage)
    if result.returncode == -1 and "TIMEOUT" in tail:
        return f"stage{stage_label}_timeout"
    if "validate" in tail_lower or "blocked" in tail_lower or "captcha" in tail_lower:
        return f"stage{stage_label}_blocked"
    if (
        "connectionerror" in tail_lower
        or "timeout" in tail_lower
        or "read timed out" in tail_lower
        or "connection aborted" in tail_lower
    ):
        return f"stage{stage_label}_network"
    if (
        "memoryerror" in tail_lower
        or "no space left" in tail_lower
        or "disk" in tail_lower
        or "winerror 112" in tail_lower
    ):
        return f"stage{stage_label}_resource"
    if stage_label in {"3", "all"} and ("上传失败" in tail or "upload" in tail_lower):
        return "upload_failed"
    return f"stage{stage_label}_exit_{result.returncode}"


def extract_stage_timings(output_tail: str) -> dict[int, float]:
    timings: dict[int, float] = {}
    for match in re.finditer(r"\[STAGE_TIMING\]\s+stage=(\d)\s+seconds=(\d+(?:\.\d+)?)", output_tail):
        timings[int(match.group(1))] = float(match.group(2))
    return timings


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
    list_window_pause_min = getattr(args, "list_window_pause_min", 0.3)
    list_window_pause_max = getattr(args, "list_window_pause_max", 1.2)
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

    total_started = time.time()
    final_reason = ""
    restart_from_stage = 1

    for attempt in range(1, args.max_retries + 2):
        clean_per_stock_temp(
            stock,
            crawl_mode=crawl_mode,
            from_stage=restart_from_stage,
        )
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

        stock_timeout = getattr(args, "stock_timeout_minutes", 60) * 60.0
        failed_stage = None
        failed_result = None
        if getattr(args, "single_process_stock", False) and restart_from_stage == 1:
            single_list_source = getattr(args, "list_source", "html")
            if (
                crawl_mode == "full"
                and attempt >= 3
                and single_list_source != "selenium"
            ):
                single_list_source = "selenium"
                print(f"[{stock}][stage all] fallback to selenium after repeated failures")
            result = stream_subprocess(
                build_stage_cmd(
                    args.python,
                    stock,
                    "all",
                    source_dirs=source_dirs,
                    detail_workers=args.detail_workers,
                    crawl_mode=crawl_mode,
                    start_date=start_date,
                    list_workers=list_workers,
                    list_window_pause_min=list_window_pause_min,
                    list_window_pause_max=list_window_pause_max,
                    list_source=single_list_source,
                    list_page_limit=list_page_limit,
                ),
                stage=1,
                stock=stock,
                progress_dir=args.progress_dir,
                idle_timeout=stock_timeout,
            )
            timings = extract_stage_timings(result.output_tail)
            for stage_num, seconds in timings.items():
                summary[f"stage{stage_num}_seconds"] = round(seconds, 2)
            if result.returncode != 0:
                failed_stage = "all"
                failed_result = result
                final_reason = classify_stage_failure("all", result)
                restart_from_stage = 1
        else:
            for stage in range(restart_from_stage, 4):
                stage_list_source = getattr(args, "list_source", "html")
                if (
                    stage == 1
                    and crawl_mode == "full"
                    and attempt >= 3
                    and stage_list_source != "selenium"
                ):
                    stage_list_source = "selenium"
                    print(f"[{stock}][stage 1] fallback to selenium after repeated failures")
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
                        list_window_pause_min=list_window_pause_min,
                        list_window_pause_max=list_window_pause_max,
                        list_source=stage_list_source,
                        list_page_limit=list_page_limit,
                    ),
                    stage=stage,
                    stock=stock,
                    progress_dir=args.progress_dir,
                    idle_timeout=stock_timeout,
                )
                summary[f"stage{stage}_seconds"] = round(
                    summary[f"stage{stage}_seconds"] + result.seconds, 2
                )
                if result.returncode != 0:
                    failed_stage = stage
                    failed_result = result
                    final_reason = classify_stage_failure(stage, result)
                    restart_from_stage = stage
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


def write_summary(progress_dir: Path, stock: str, summary: dict) -> bool:
    worker_id = str(summary.get("worker_id") or "")
    if worker_id:
        lock_worker_id = read_lock_worker_id(state_path(progress_dir, stock, ".lock"))
        if lock_worker_id != worker_id:
            owner = lock_worker_id if lock_worker_id else "missing"
            print(f"[{worker_id}] skip {stock}.summary.json: lock owner is {owner}")
            return False
    write_json(state_path(progress_dir, stock, ".summary.json"), summary)
    return True


def defer_claim(
    claim: ClaimedStock,
    args: argparse.Namespace,
    reason: str,
    summary: dict,
) -> bool:
    retry_seconds = float(getattr(args, "deferred_retry_seconds", 900.0))
    retry_after = time.time() + max(1.0, retry_seconds)
    remove_state(args.progress_dir, claim.stock, ".retrying")
    return mark_state(
        args.progress_dir,
        claim.stock,
        DEFERRED_SUFFIX,
        {
            "worker_id": args.worker_id,
            "reason": reason,
            "summary": f"{claim.stock}.summary.json",
            "retry_after_epoch": retry_after,
            "retry_after": datetime.fromtimestamp(retry_after).isoformat(timespec="seconds"),
            "attempts": summary.get("attempts", 0),
        },
    )


def process_claim(claim: ClaimedStock, args: argparse.Namespace, source_dirs: list[Path]) -> str:
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=heartbeat,
        args=(claim.lock_path, args.worker_id, stop_event, args.heartbeat_seconds),
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
            remove_state(args.progress_dir, claim.stock, DEFERRED_SUFFIX)
            mark_state(
                args.progress_dir,
                claim.stock,
                ".done",
                {"worker_id": args.worker_id, "summary": f"{claim.stock}.summary.json"},
            )
            return "success"
        if is_retryable_failure_reason(reason):
            defer_claim(claim, args, reason, summary)
            print(
                f"[{args.worker_id}] deferred {claim.stock} after retryable failure: {reason}"
            )
            return "deferred"
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
            return "failed"
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
        return "failed"
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=2)
        try:
            if read_lock_worker_id(claim.lock_path) == args.worker_id:
                claim.lock_path.unlink()
            else:
                print(f"[{args.worker_id}] skip lock cleanup for {claim.stock}: lock not owned")
        except FileNotFoundError:
            pass
        # 清理实时进度文件
        for suff in (PROGRESS_SUFFIX, PROGRESS_SUFFIX + ".tmp"):
            try:
                (args.progress_dir / f"{claim.stock}{suff}").unlink()
            except (FileNotFoundError, OSError):
                pass
        for tmp_progress in args.progress_dir.glob(f"{claim.stock}{PROGRESS_SUFFIX}.tmp.*"):
            try:
                tmp_progress.unlink()
            except (FileNotFoundError, OSError):
                pass


def release_claim(claim: ClaimedStock, worker_id: str) -> None:
    try:
        if read_lock_worker_id(claim.lock_path) == worker_id:
            claim.lock_path.unlink()
        else:
            print(f"[{worker_id}] skip releasing {claim.stock}: lock not owned")
    except FileNotFoundError:
        pass


def find_next_claim(
    stocks: dict[str, Path | None],
    args: argparse.Namespace,
    max_wait_seconds: float = 120.0,
    retry_seconds: float = 30.0,
) -> ClaimedStock | None:
    stale_seconds = args.stale_lock_hours * 3600
    started_at = time.time()
    while True:
        pending_or_busy = 0
        for stock, source_csv in stocks.items():
            if is_deferred_state_active(args.progress_dir, stock):
                continue
            if not has_done_state(args.progress_dir, stock) and (
                args.retry_failed or not has_failed_state(args.progress_dir, stock)
            ):
                pending_or_busy += 1
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

        if pending_or_busy == 0:
            return None

        elapsed = time.time() - started_at
        if elapsed >= max_wait_seconds:
            print(
                f"[{args.worker_id}] no claimable stock after {elapsed:.0f}s wait; "
                f"{pending_or_busy} pending/busy"
            )
            return None

        delay = min(retry_seconds, max_wait_seconds - elapsed)
        print(
            f"[{args.worker_id}] all pending stocks locked/busy "
            f"({pending_or_busy}); retrying in {delay:.0f}s"
        )
        time.sleep(delay)


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
    deferred = 0
    consecutive_failures = 0
    max_consecutive = getattr(args, "max_consecutive_failures", 5)

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
            deferred_count, next_retry = active_deferred_status(
                stocks,
                args.progress_dir,
                retry_failed=args.retry_failed,
            )
            if deferred_count and next_retry is not None:
                wait_seconds = max(1.0, min(next_retry - time.time(), args.disk_wait_seconds))
                retry_at = datetime.fromtimestamp(next_retry).isoformat(timespec="seconds")
                print(
                    f"[{args.worker_id}] {deferred_count} deferred stock(s); "
                    f"next retry at {retry_at}, waiting {wait_seconds:.0f}s"
                )
                time.sleep(wait_seconds)
                continue
            print(f"[{args.worker_id}] no pending stock left")
            break

        print(f"[{args.worker_id}] claimed {claim.stock}: {claim.source_csv}")
        enough_space, free_gb, required_gb = enough_disk_for_stock(args)
        if not enough_space:
            print(
                f"[{args.worker_id}] disk free {free_gb:.2f}GB < projected "
                f"{required_gb:.2f}GB for {claim.stock}; releasing claim and waiting "
                f"{args.disk_wait_seconds}s"
            )
            release_claim(claim, args.worker_id)
            time.sleep(args.disk_wait_seconds)
            continue

        processed += 1
        status = process_claim(claim, args, source_dirs=source_dirs)
        if status == "success":
            successes += 1
            consecutive_failures = 0
        elif status == "deferred":
            deferred += 1
            consecutive_failures = 0
        else:
            failures += 1
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive:
                print(
                    f"[{args.worker_id}] {consecutive_failures} consecutive failures "
                    f"(max={max_consecutive}); stopping to avoid IP ban. "
                    f"Restart with --retry-failed after resolving network issues."
                )
                break

    print(
        f"[{args.worker_id}] finished: processed={processed}, success={successes}, "
        f"deferred={deferred}, failure={failures}"
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
    parser.add_argument("--list-window-pause-min", type=float, default=0.3,
                        help="Fast-path Stage 1 window pause lower bound when no failures occur")
    parser.add_argument("--list-window-pause-max", type=float, default=1.2,
                        help="Fast-path Stage 1 window pause upper bound when no failures occur")
    parser.add_argument("--list-source", choices=["html", "api", "auto", "selenium"], default="html",
                        help="Full mode Stage 1 list source; html/api/auto use fast requests HTML")
    parser.add_argument("--list-page-limit", type=int, default=0,
                        help="Full mode trial page limit; 0 means no limit")
    parser.add_argument("--progress-dir", type=Path, default=None)
    parser.add_argument("--detail-workers", type=int, default=3)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--stale-lock-hours", type=float, default=1.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=60.0)
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--disk-wait-seconds", type=float, default=300.0)
    parser.add_argument("--deferred-retry-seconds", type=float, default=900.0,
                        help="Seconds before retrying retryable stock failures (default 900)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--stock-timeout-minutes", type=float, default=60.0,
                        help="Max idle minutes per stock stage before killing (default 60)")
    parser.add_argument("--max-consecutive-failures", type=int, default=5,
                        help="Max consecutive failures before stopping worker to avoid IP ban (default 5)")
    parser.add_argument("--stock", action="append", default=None,
                        help="Restrict processing to one stock; may be provided multiple times")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Allow claiming stocks with .failed/.failed_upload state")
    parser.add_argument("--single-process-stock", action="store_true",
                        help="Run Stage 1/2/3 for each stock inside one auto_pipeline.py process")
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
            list_candidates = sorted(PROJECT_DIR.glob("*_list.csv"))
            if list_candidates:
                args.stock_list = list_candidates[0]
            else:
                print("[error] full mode requires --stock-list or *_list.csv in project dir")
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
