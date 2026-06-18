import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests
from tqdm.auto import tqdm

from browser_utils import create_stealth_chrome


OUTPUT_FIELDS = [
    "user_id",
    "post_id",
    "post_type",
    "user_name",
    "post_publish_time",
    "post_title",
    "stockbar_name",
    "stockbar_code",
    "forward_count",
    "comment_count",
    "click_count",
    "page",
    "total_pages",
    "stock_code",
    "post_url",
]
DEFAULT_OUTPUT_BASE_DIR = "/data1/wuzixin/eastmoney_missing_year_results"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
}
ARTICLE_LIST_PATTERN = re.compile(r"var\s+article_list\s*=\s*\{")
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
YEAR_TOKEN_PATTERN = re.compile(r"\b\d{4}\b")
RESTARTABLE_RUN_ERROR_PATTERNS = (
    "session not created",
    "chrome not reachable",
    "invalid session id",
    "disconnected: not connected to devtools",
    "unable to discover open pages",
    "target window already closed",
    "tab crashed",
)


def create_csv_writer(handle) -> csv.DictWriter:
    return csv.DictWriter(
        handle,
        fieldnames=OUTPUT_FIELDS,
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )


def normalize_stock_code(raw_code: str) -> Optional[str]:
    code = str(raw_code or "").strip()
    if not code or not code.isdigit():
        return None
    return code.zfill(6)


def sanitize_text(value) -> str:
    text = str(value or "")
    text = CONTROL_CHAR_PATTERN.sub(" ", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return re.sub(r" +", " ", text).strip()


def load_missing_year_targets(
    csv_path: str,
    selected_stocks: Optional[Sequence[str]] = None,
) -> Dict[str, Set[int]]:
    selected = {
        normalized
        for normalized in (normalize_stock_code(code) for code in (selected_stocks or []))
        if normalized
    }
    targets: Dict[str, Set[int]] = {}

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            stock_code = normalize_stock_code(row.get("code", ""))
            target_years = extract_target_years(row)
            if not stock_code or not target_years:
                continue
            if selected and stock_code not in selected:
                continue
            targets.setdefault(stock_code, set()).update(target_years)

    return targets


def extract_target_years(row: Dict[str, str]) -> Set[int]:
    year_text = str(row.get("year", "")).strip()
    if year_text.isdigit():
        return {int(year_text)}

    missing_years_text = str(row.get("missing_years", "")).strip()
    if not missing_years_text:
        return set()

    return {int(token) for token in YEAR_TOKEN_PATTERN.findall(missing_years_text)}


def build_stock_jobs(targets: Dict[str, Set[int]]) -> List[Tuple[str, Set[int]]]:
    return [(stock_code, targets[stock_code]) for stock_code in sorted(targets)]


def load_existing_stock_summaries(output_dir: str) -> List[Dict]:
    summary_path = os.path.join(output_dir, "missing_year_summary.json")
    if not os.path.exists(summary_path):
        return []

    with open(summary_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    stocks = payload.get("stocks", [])
    return list(stocks) if isinstance(stocks, list) else []


def get_completed_stock_codes(summaries: Sequence[Dict]) -> Set[str]:
    completed = set()
    for item in summaries:
        stock_code = normalize_stock_code(item.get("stock_code", ""))
        if stock_code:
            completed.add(stock_code)
    return completed


def build_pending_stock_jobs(
    stock_jobs: Sequence[Tuple[str, Set[int]]],
    completed_stock_codes: Set[str],
) -> List[Tuple[str, Set[int]]]:
    return [
        (stock_code, target_years)
        for stock_code, target_years in stock_jobs
        if stock_code not in completed_stock_codes
    ]


def get_target_year_bounds(target_years: Set[int]) -> Tuple[int, int]:
    if not target_years:
        raise ValueError("target_years 不能为空")
    return min(target_years), max(target_years)


def classify_page_years(page_years: Set[int], target_min_year: int, target_max_year: int) -> str:
    if not page_years:
        return "unknown"
    if min(page_years) > target_max_year:
        return "newer"
    return "target_or_older"


def estimate_initial_seek_step(total_pages: int, newest_year: int, target_min_year: int) -> int:
    year_span = max(newest_year - target_min_year, 1)
    return max(total_pages // year_span, 1)


def seek_target_start_page(
    total_pages: int,
    target_min_year: int,
    target_max_year: int,
    first_page_years: Set[int],
    lookup_page_years,
) -> Tuple[int, List[int]]:
    if total_pages <= 1:
        return 1, [1]

    first_state = classify_page_years(first_page_years, target_min_year, target_max_year)
    probed_pages = [1]
    if first_state != "newer":
        return 1, probed_pages

    initial_step = min(
        total_pages - 1,
        estimate_initial_seek_step(total_pages, max(first_page_years), target_min_year),
    )
    step = max(initial_step, 1)
    lower_page = 1
    current_page = 1

    while True:
        probe_page = min(current_page + step, total_pages)
        if probe_page == current_page:
            return total_pages + 1, probed_pages

        if probe_page not in probed_pages:
            probed_pages.append(probe_page)

        page_years = lookup_page_years(probe_page)
        page_state = classify_page_years(page_years, target_min_year, target_max_year)

        if page_state == "newer":
            lower_page = probe_page
            current_page = probe_page
            if probe_page >= total_pages:
                return total_pages + 1, probed_pages
            continue

        if step == 1:
            return probe_page, probed_pages

        step = max(step // 2, 1)
        current_page = lower_page


def should_enable_progress(show_progress: Optional[bool] = None) -> bool:
    if show_progress is not None:
        return bool(show_progress)
    return sys.stderr.isatty()


def emit_log(message: str, progress_enabled: bool = False):
    if progress_enabled:
        tqdm.write(message)
    else:
        print(message)


def resolve_run_output_dir(output_dir: Optional[str] = None) -> str:
    return output_dir or os.path.join(
        DEFAULT_OUTPUT_BASE_DIR,
        datetime.now().strftime("%Y%m%d_%H%M%S"),
    )


def should_restart_run(exc: BaseException) -> bool:
    pending = [exc]
    seen = set()

    while pending:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue

        seen.add(id(current))
        message = str(current).lower()
        if any(pattern in message for pattern in RESTARTABLE_RUN_ERROR_PATTERNS):
            return True

        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))

    return False


def format_post_publish_time(post: Dict) -> Optional[str]:
    post_date = str(post.get("post_date") or "").strip()
    post_time = str(post.get("post_time") or "").strip()
    if not post_date or not post_time:
        return None

    if len(post_time) >= 5:
        post_time = post_time[:5]
    return f"{post_date} {post_time}"


def extract_post_year(post: Dict) -> Optional[int]:
    post_date = str(post.get("post_date") or "").strip()
    if len(post_date) < 4 or not post_date[:4].isdigit():
        return None
    return int(post_date[:4])


def filter_posts_by_years(
    posts: Iterable[Dict],
    target_years: Set[int],
) -> Tuple[List[Dict], Set[int], int]:
    matched_posts: List[Dict] = []
    page_years: Set[int] = set()
    invalid_count = 0

    for post in posts:
        year = extract_post_year(post)
        if year is None:
            invalid_count += 1
            continue

        page_years.add(year)
        if year in target_years:
            matched_posts.append(post)

    return matched_posts, page_years, invalid_count


def should_stop_for_min_year(page_years: Set[int], min_year: int) -> bool:
    return bool(page_years) and max(page_years) < min_year


def build_output_row(post: Dict, page_num: int, total_pages: int) -> Dict:
    stockbar_code = str(post.get("stockbar_code", "") or "")
    return {
        "user_id": post.get("user_id", ""),
        "post_id": post.get("_id"),
        "post_type": post.get("post_type", 0),
        "user_name": post.get("post_author", ""),
        "post_publish_time": format_post_publish_time(post) or "",
        "post_title": post.get("post_title", ""),
        "stockbar_name": "",
        "stockbar_code": stockbar_code,
        "forward_count": post.get("forward_count", 0),
        "comment_count": post.get("comment_num", 0),
        "click_count": post.get("post_view", ""),
        "page": page_num,
        "total_pages": total_pages,
        "stock_code": stockbar_code,
        "post_url": post.get("post_url", ""),
    }


def extract_page_posts(page_data: Dict, page_num: int) -> List[Dict]:
    posts = [convert_article_to_post(article) for article in page_data.get("re", [])]
    if not posts:
        raise ValueError(f"第 {page_num} 页解析结果为空")
    return posts


def build_list_page_url(stock_code: str, page_num: int) -> str:
    return f"https://guba.eastmoney.com/list,{stock_code},f_{page_num}.html"


def extract_json_object(text: str, start_index: int) -> Optional[str]:
    depth = 0
    in_string = False
    escape = False

    for index in range(start_index, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start_index:index + 1]

    return None


def extract_article_list_data(html: str) -> Dict:
    match = ARTICLE_LIST_PATTERN.search(html)
    if not match:
        raise ValueError("页面中未找到 article_list")

    json_start = html.find("{", match.start())
    json_text = extract_json_object(html, json_start)
    if not json_text:
        raise ValueError("article_list JSON 解析失败")

    return json.loads(json_text)


def convert_article_to_post(article: Dict) -> Dict:
    stockbar_code = normalize_stock_code(article.get("stockbar_code", "")) or ""
    post_id = int(article.get("post_id") or 0)
    publish_time = str(article.get("post_publish_time") or "").strip()
    if " " in publish_time:
        post_date, post_time = publish_time.split(" ", 1)
    else:
        post_date, post_time = "", ""

    return {
        "_id": post_id,
        "user_id": str(article.get("user_id") or ""),
        "post_type": int(article.get("post_type", 0) or 0),
        "post_title": sanitize_text(article.get("post_title", "")),
        "post_view": str(article.get("post_click_count", "")),
        "forward_count": int(article.get("post_forward_count", 0) or 0),
        "comment_num": int(article.get("post_comment_count", 0) or 0),
        "stockbar_code": stockbar_code,
        "post_url": f"https://guba.eastmoney.com/news,{stockbar_code},{post_id}.html",
        "post_date": post_date,
        "post_time": post_time,
        "post_author": sanitize_text(article.get("user_nickname", "")),
    }


def validate_article_page_payload(payload: Dict) -> Dict:
    if not isinstance(payload, dict) or "re" not in payload:
        raise ValueError(f"接口返回异常: {payload}")
    if not isinstance(payload.get("re"), list):
        raise ValueError(f"接口返回的 re 不是列表: {payload}")

    total_count = int(payload.get("count", 0) or 0)
    if total_count > 0 and not payload["re"]:
        raise ValueError(f"接口返回空帖子列表: count={total_count}")

    return payload


def create_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch_article_page_data(
    session: requests.Session,
    stock_code: str,
    page_num: int,
    page_retry_limit: int,
) -> Dict:
    url = build_list_page_url(stock_code, page_num)
    last_error = None

    for attempt in range(1, page_retry_limit + 1):
        try:
            response = session.get(url, timeout=20)
            response.raise_for_status()
            return extract_article_list_data(response.text)
        except Exception as exc:
            last_error = exc
            if attempt < page_retry_limit:
                time.sleep(0.5)

    raise RuntimeError(f"第 {page_num} 页抓取失败: {last_error}")


def fetch_article_page_data_via_browser(
    browser,
    stock_code: str,
    page_num: int,
    page_retry_limit: int,
    page_size: int = 80,
) -> Dict:
    script = """
    const stockCode = arguments[0];
    const pageNum = arguments[1];
    const pageSize = arguments[2];
    const done = arguments[arguments.length - 1];
    const path = 'webarticlelist/api/Article/Articlelist';
    const body = new URLSearchParams({
      param: `code=${stockCode}&type=0&p=${pageNum}&ps=${pageSize}&sorttype=0`,
      plat: 'Web',
      path,
      env: '2',
      origin: '',
      version: '2022',
      product: 'Guba',
    });
    fetch(`/api/getData?code=${stockCode}&path=${path}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
      body: body.toString(),
      credentials: 'same-origin',
    })
      .then(response => response.text())
      .then(text => done({ok: true, text}))
      .catch(error => done({ok: false, error: String(error)}));
    """

    last_error = None
    for attempt in range(1, page_retry_limit + 1):
        try:
            result = browser.execute_async_script(script, stock_code, page_num, page_size)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "浏览器 fetch 失败")
            payload = json.loads(result["text"])
            return validate_article_page_payload(payload)
        except Exception as exc:
            last_error = exc
            if attempt < page_retry_limit:
                try:
                    browser.get(build_list_page_url(stock_code, 1))
                except Exception:
                    pass
                time.sleep(0.5)

    raise RuntimeError(f"第 {page_num} 页抓取失败: {last_error}")


def bootstrap_browser_for_stock(stock_code: str, page_retry_limit: int):
    browser = create_stealth_chrome()
    browser.get(build_list_page_url(stock_code, 1))
    first_page_data = fetch_article_page_data_via_browser(
        browser,
        stock_code,
        page_num=1,
        page_retry_limit=page_retry_limit,
    )
    return browser, first_page_data


def parse_stock_args(stock_args: Optional[Sequence[str]]) -> List[str]:
    if not stock_args:
        return []

    raw_codes: List[str] = []
    for item in stock_args:
        raw_codes.extend(part.strip() for part in str(item).split(","))

    normalized_codes = []
    seen = set()
    for code in raw_codes:
        normalized = normalize_stock_code(code)
        if normalized and normalized not in seen:
            normalized_codes.append(normalized)
            seen.add(normalized)
    return normalized_codes


def append_rows(output_file: str, rows: List[Dict]):
    if not rows:
        return

    with open(output_file, "a", newline="", encoding="utf-8-sig") as handle:
        writer = create_csv_writer(handle)
        writer.writerows(rows)


def crawl_missing_year_posts(
    stock_code: str,
    target_years: Set[int],
    output_file: str,
    max_pages: Optional[int] = None,
    page_retry_limit: int = 3,
    progress_enabled: bool = False,
    stock_index: Optional[int] = None,
    stock_total: Optional[int] = None,
) -> Dict:
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", newline="", encoding="utf-8-sig") as handle:
        writer = create_csv_writer(handle)
        writer.writeheader()

    summary = {
        "stock_code": stock_code,
        "target_years": sorted(target_years),
        "scanned_pages": 0,
        "seek_start_page": None,
        "seek_probe_count": 0,
        "matched_rows": 0,
        "matched_years": set(),
        "invalid_timestamp_count": 0,
        "failed_pages": [],
        "stop_reason": "exhausted_pages",
        "output_file": output_file,
    }

    target_min_year, target_max_year = get_target_year_bounds(target_years)

    browser = None
    page_progress = None
    previous_page_years: Optional[Set[int]] = None
    page_cache: Dict[int, Dict] = {}
    try:
        browser, first_page_data = bootstrap_browser_for_stock(stock_code, page_retry_limit)
        total_posts = int(first_page_data.get("count", 0) or 0)
        page_size = len(first_page_data.get("re", [])) or 80
        total_pages = max((total_posts + page_size - 1) // page_size, 1)
        stop_page = min(total_pages, max_pages) if max_pages else total_pages
        stock_label = (
            f"{stock_index}/{stock_total} {stock_code}"
            if stock_index is not None and stock_total is not None
            else stock_code
        )
        emit_log(
            f"{stock_code}: 检测到共 {total_pages} 页，计划扫描到第 {stop_page} 页",
            progress_enabled=progress_enabled,
        )

        def fetch_page_snapshot(page_num: int, cache_result: bool = True) -> Dict:
            nonlocal browser, first_page_data

            if page_num in page_cache:
                return page_cache[page_num]

            try:
                page_data = (
                    first_page_data
                    if page_num == 1
                    else fetch_article_page_data_via_browser(
                        browser,
                        stock_code,
                        page_num=page_num,
                        page_retry_limit=page_retry_limit,
                    )
                )
                posts = extract_page_posts(page_data, page_num)
            except Exception as exc:
                try:
                    if browser is not None:
                        browser.quit()
                except Exception:
                    pass
                browser = None
                try:
                    browser, first_page_data = bootstrap_browser_for_stock(stock_code, page_retry_limit)
                    page_data = fetch_article_page_data_via_browser(
                        browser,
                        stock_code,
                        page_num=page_num,
                        page_retry_limit=page_retry_limit,
                    )
                    posts = extract_page_posts(page_data, page_num)
                    emit_log(
                        f"{stock_code}: 第 {page_num} 页切换新浏览器后恢复成功",
                        progress_enabled=progress_enabled,
                    )
                except Exception as retry_exc:
                    summary["failed_pages"].append(page_num)
                    emit_log(
                        f"{stock_code}: 第 {page_num} 页最终失败，跳过。最后错误: {retry_exc}",
                        progress_enabled=progress_enabled,
                    )
                    raise RuntimeError(str(retry_exc)) from retry_exc

            matched_posts, page_years, invalid_count = filter_posts_by_years(posts, target_years)
            snapshot = {
                "posts": posts,
                "page_years": page_years,
                "invalid_count": invalid_count,
                "rows": [build_output_row(post, page_num, total_pages) for post in matched_posts],
            }
            if cache_result:
                page_cache[page_num] = snapshot
            return snapshot

        first_snapshot = fetch_page_snapshot(1, cache_result=True)
        seek_logged_pages = {1}

        def lookup_page_years(page_num: int) -> Set[int]:
            snapshot = fetch_page_snapshot(page_num, cache_result=True)
            if page_num not in seek_logged_pages:
                seek_logged_pages.add(page_num)
                emit_log(
                    f"{stock_code}: 定位探测第 {page_num}/{stop_page} 页，页面年份 {sorted(snapshot['page_years'])}",
                    progress_enabled=progress_enabled,
                )
            return snapshot["page_years"]

        try:
            start_page, probed_pages = seek_target_start_page(
                total_pages=stop_page,
                target_min_year=target_min_year,
                target_max_year=target_max_year,
                first_page_years=first_snapshot["page_years"],
                lookup_page_years=lookup_page_years,
            )
        except RuntimeError as exc:
            start_page, probed_pages = 1, [1]
            emit_log(
                f"{stock_code}: 定位阶段失败，回退到顺序扫描。错误: {exc}",
                progress_enabled=progress_enabled,
            )
        summary["seek_start_page"] = start_page
        summary["seek_probe_count"] = len(probed_pages)

        if start_page > stop_page:
            summary["stop_reason"] = "target_window_not_found"
            emit_log(
                f"{stock_code}: 在前 {stop_page} 页中未定位到目标年份窗口，停止抓取",
                progress_enabled=progress_enabled,
            )
            summary["matched_years"] = sorted(summary["matched_years"])
            return summary

        emit_log(
            f"{stock_code}: 定位到目标起始页 {start_page}，探测 {len(probed_pages)} 页",
            progress_enabled=progress_enabled,
        )
        page_progress = tqdm(
            total=stop_page - start_page + 1,
            desc=stock_label,
            unit="page",
            leave=False,
            dynamic_ncols=True,
            disable=not progress_enabled,
            position=1 if progress_enabled else 0,
        )

        for page_num in range(start_page, stop_page + 1):
            try:
                snapshot = fetch_page_snapshot(page_num, cache_result=False)
            except RuntimeError:
                if page_progress is not None:
                    page_progress.update(1)
                    page_progress.set_postfix(
                        hit=summary["matched_rows"],
                        fail=len(summary["failed_pages"]),
                    )
                continue

            rows = snapshot["rows"]
            page_years = snapshot["page_years"]
            invalid_count = snapshot["invalid_count"]
            append_rows(output_file, rows)
            summary["scanned_pages"] += 1
            summary["matched_rows"] += len(rows)
            summary["matched_years"].update(page_years & target_years)
            summary["invalid_timestamp_count"] += invalid_count

            should_log_page = (
                page_num == start_page
                or page_num % 50 == 0
                or page_years != (previous_page_years or set())
                or page_num == stop_page
            )
            if should_log_page:
                page_year_list = sorted(page_years)
                emit_log(
                    f"{stock_code}: 第 {page_num}/{stop_page} 页，解析 {len(snapshot['posts'])} 条，"
                    f"命中 {len(rows)} 条，页面年份 {page_year_list if page_year_list else '[]'}",
                    progress_enabled=progress_enabled,
                )
            previous_page_years = set(page_years)
            if page_progress is not None:
                page_progress.update(1)
                if should_log_page:
                    year_label = (
                        f"{min(page_years)}-{max(page_years)}" if page_years else "unknown"
                    )
                    page_progress.set_postfix(
                        hit=summary["matched_rows"],
                        fail=len(summary["failed_pages"]),
                        year=year_label,
                    )

            page_cache.pop(page_num, None)

            if should_stop_for_min_year(page_years, target_min_year):
                summary["stop_reason"] = "year_boundary_reached"
                emit_log(
                    f"{stock_code}: 第 {page_num} 页最新年份已早于 {target_min_year}，停止后续抓取",
                    progress_enabled=progress_enabled,
                )
                break
    finally:
        if page_progress is not None:
            page_progress.close()
        if browser is not None:
            browser.quit()

    summary["matched_years"] = sorted(summary["matched_years"])
    return summary


def save_summary(output_dir: str, summaries: List[Dict]) -> str:
    summary_path = os.path.join(output_dir, "missing_year_summary.json")
    payload = {
        "created_at": datetime.now().isoformat(),
        "stocks": summaries,
    }
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return summary_path


def run_backfill(
    missing_years_file: str,
    selected_stocks: Optional[Sequence[str]] = None,
    output_dir: Optional[str] = None,
    max_pages: Optional[int] = None,
    show_progress: Optional[bool] = None,
) -> Tuple[str, List[Dict]]:
    targets = load_missing_year_targets(missing_years_file, selected_stocks=selected_stocks)
    if not targets:
        raise ValueError("缺失年份清单中没有可处理的股票")

    run_output_dir = resolve_run_output_dir(output_dir)
    Path(run_output_dir).mkdir(parents=True, exist_ok=True)

    progress_enabled = should_enable_progress(show_progress)
    stock_jobs = build_stock_jobs(targets)
    summaries = load_existing_stock_summaries(run_output_dir)
    completed_stock_codes = get_completed_stock_codes(summaries)
    pending_stock_jobs = build_pending_stock_jobs(stock_jobs, completed_stock_codes)
    if completed_stock_codes:
        emit_log(
            f"检测到已有进度，跳过 {len(completed_stock_codes)} 只已完成股票，剩余 {len(pending_stock_jobs)} 只",
            progress_enabled=progress_enabled,
        )
    save_summary(run_output_dir, summaries)
    stock_progress = tqdm(
        total=len(pending_stock_jobs),
        desc="Stocks",
        unit="stock",
        leave=True,
        dynamic_ncols=True,
        disable=not progress_enabled,
        position=0,
    )
    try:
        for stock_index, (stock_code, target_years) in enumerate(pending_stock_jobs, start=1):
            if progress_enabled:
                stock_progress.set_postfix_str(
                    f"{stock_code} {min(target_years)}-{max(target_years)}"
                )
            output_file = os.path.join(run_output_dir, f"{stock_code}.csv")
            summary = crawl_missing_year_posts(
                stock_code=stock_code,
                target_years=target_years,
                output_file=output_file,
                max_pages=max_pages,
                progress_enabled=progress_enabled,
                stock_index=stock_index,
                stock_total=len(pending_stock_jobs),
            )
            summaries.append(summary)
            save_summary(run_output_dir, summaries)
            stock_progress.update(1)
    finally:
        stock_progress.close()

    return run_output_dir, summaries


def run_with_auto_restart(
    missing_years_file: str,
    selected_stocks: Optional[Sequence[str]] = None,
    output_dir: Optional[str] = None,
    max_pages: Optional[int] = None,
    show_progress: Optional[bool] = None,
    max_run_restarts: int = 3,
    restart_delay_seconds: float = 5.0,
) -> Tuple[str, List[Dict]]:
    run_output_dir = resolve_run_output_dir(output_dir)
    progress_enabled = should_enable_progress(show_progress)
    restart_budget = max(max_run_restarts, 0)
    restart_delay = max(restart_delay_seconds, 0.0)
    restart_count = 0

    while True:
        try:
            return run_backfill(
                missing_years_file=missing_years_file,
                selected_stocks=selected_stocks,
                output_dir=run_output_dir,
                max_pages=max_pages,
                show_progress=show_progress,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not should_restart_run(exc) or restart_count >= restart_budget:
                raise

            restart_count += 1
            emit_log(
                f"检测到浏览器异常，{restart_delay:.1f} 秒后重启整个补爬任务 "
                f"({restart_count}/{restart_budget})。错误: {exc}",
                progress_enabled=progress_enabled,
            )
            time.sleep(restart_delay)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="东方财富股吧缺失年份补爬")
    parser.add_argument(
        "--missing-years-file",
        required=True,
        help="缺失年份清单 CSV 路径，兼容 code/year 与 code/missing_years 两种格式",
    )
    parser.add_argument(
        "--stocks",
        nargs="+",
        help="指定股票代码，支持空格或逗号分隔",
    )
    parser.add_argument(
        "--output-dir",
        help="输出目录，默认写到 /data1/wuzixin/eastmoney_missing_year_results/<时间戳>/",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="最多扫描多少页，主要用于调试",
    )
    parser.add_argument(
        "--max-run-restarts",
        type=int,
        default=3,
        help="浏览器会话崩溃时，整个补爬任务最多自动重启多少次",
    )
    parser.add_argument(
        "--restart-delay-seconds",
        type=float,
        default=5.0,
        help="整个补爬任务自动重启前等待多少秒",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()
    selected_stocks = parse_stock_args(args.stocks)
    output_dir, summaries = run_with_auto_restart(
        missing_years_file=args.missing_years_file,
        selected_stocks=selected_stocks,
        output_dir=args.output_dir,
        max_pages=args.max_pages,
        max_run_restarts=args.max_run_restarts,
        restart_delay_seconds=args.restart_delay_seconds,
    )
    print(f"输出目录: {output_dir}")
    for summary in summaries:
        print(
            f"{summary['stock_code']}: 扫描 {summary['scanned_pages']} 页，"
            f"命中 {summary['matched_rows']} 条，命中年份 {summary['matched_years']}，"
            f"失败页 {summary['failed_pages']}，停止原因 {summary['stop_reason']}"
        )


if __name__ == "__main__":
    main()
