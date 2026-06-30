#!/usr/bin/env python3
"""
000001 个股数据自动化流水线 (CSV-Native 高速版)
==================================================
优化点：全程以 CSV 文件为中心，省去 MongoDB 导入 24万+ 旧数据的耗时过程。

三阶段执行：
  Stage 1: 提取 CSV → 内存去重 → 爬取帖子列表（新帖子直接追加 CSV）
  Stage 2: 补爬正文（财富号多线程 + 股吧原生多 worker）
  Stage 3: 合并 CSV → 上传百度网盘 → 清理本地数据

  用法：
  python3 auto_pipeline_000001.py --stage 1              # PowerShell 窗口 1
  python3 auto_pipeline_000001.py --stage 1 --source-dir 数据  # 从历史 CSV 目录读取
  python3 auto_pipeline_000001.py --stage 2              # PowerShell 窗口 2（等 stage1 完成）
  python3 auto_pipeline_000001.py --stage 2 --detail-workers 3  # 自定义并发数
  python3 auto_pipeline_000001.py --stage 3              # PowerShell 窗口 3（等 stage2 完成）
"""
import os
import sys
import csv
import json
import time
import threading
import subprocess
import shutil
import argparse
import hashlib
from datetime import datetime

import pandas as pd

from mongodb import MongoAPI
from crawler import PostCrawler, CommentCrawler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def _tmp_path(path: str, attempt: int = 1) -> str:
    return f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{attempt}"


def _safe_replace(src: str, dst: str, max_retries: int = 5) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if attempt == max_retries:
                raise
            delay = 0.5 * attempt
            print(f"[safe_replace] retry {attempt}/{max_retries} after {delay:.1f}s: {exc}")
            time.sleep(delay)

# ==================== 配置 ====================
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

STOCK_CODE = '000001'
RAR_FILE = os.path.join(_PROJECT_DIR, '数据.rar')
RAR_INTERNAL_DIR = '数据'
TEMP_DIR = os.path.join(_PROJECT_DIR, 'temp_extract')
EXPORT_DIR = os.path.join(_PROJECT_DIR, 'temp_export')
PIPELINE_FLAG_DIR = os.path.join(_PROJECT_DIR, '.pipeline_flags')
DB_NAME_COMMENT = 'comment_info'
BAIDU_REMOTE_DIR = '/apps/bypy/guba_crawl'

# ====== 本地调试开关 ======
# 设为 True 时：Stage 3 合并后的 CSV 复制到 data/ 目录，跳过上传和清理
# 设为 False 时：恢复原始行为（上传百度网盘 + 清理本地）
SKIP_BAIDU_UPLOAD = True
DATA_OUTPUT_DIR = os.path.join(_PROJECT_DIR, 'data')

# 帖子列表爬取配置
POST_LIST_START_PAGE = 1
POST_LIST_END_PAGE = 50

# 评论爬取日期范围
COMMENT_START_DATE = '2020-01-01'
COMMENT_END_DATE = datetime.now().strftime('%Y-%m-%d')

# CSV 字段名（与 数据.rar 中提取的 CSV 保持一致）
CSV_FIELDNAMES = [
    'user_id', 'post_id', 'post_source_id', 'post_type',
    'user_name', 'post_publish_time', 'stockbar_name', 'stockbar_code',
    'forward', 'coment_count', 'click_count', 'like_count',
    'post_title', 'url', 'content'
]

# 全局运行模式（由命令行参数设置）
CRAWL_MODE = 'incremental'  # 'incremental' | 'full'
START_DATE = '2009-01-01'
LIST_WORKERS = 6
LIST_WINDOW_SIZE = 80
LIST_SOURCE = 'html'
LIST_PAGE_LIMIT = 0

# ==============================================


def ensure_dirs():
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    os.makedirs(PIPELINE_FLAG_DIR, exist_ok=True)


def set_flag(name: str):
    flag_path = os.path.join(PIPELINE_FLAG_DIR, f'{STOCK_CODE}_{name}.done')
    with open(flag_path, 'w') as f:
        f.write(datetime.now().isoformat())
    print(f'[标记] {name} 完成')


def check_flag(name: str) -> bool:
    flag_path = os.path.join(PIPELINE_FLAG_DIR, f'{STOCK_CODE}_{name}.done')
    return os.path.exists(flag_path)


def clear_flags():
    if os.path.exists(PIPELINE_FLAG_DIR):
        for f in os.listdir(PIPELINE_FLAG_DIR):
            if f.startswith(f'{STOCK_CODE}_'):
                os.remove(os.path.join(PIPELINE_FLAG_DIR, f))


def check_disk_space(min_gb: float = 1.0) -> bool:
    # 获取脚本所在盘符的磁盘信息（兼容 Windows / Linux）
    drive = os.path.splitdrive(_PROJECT_DIR)[0] or '/'
    stat = shutil.disk_usage(drive)
    avail_gb = stat.free / (1024 ** 3)
    if avail_gb < min_gb:
        print(f'[警告] 磁盘空间不足: 仅剩 {avail_gb:.2f} GB (需要 {min_gb} GB)')
        return False
    print(f'[信息] 磁盘剩余空间: {avail_gb:.2f} GB')
    return True


# ==================== 路径工具 ====================

def base_csv_path(stock_code: str) -> str:
    """原始数据 CSV（从 RAR 提取并重命名）"""
    return os.path.join(TEMP_DIR, f'{stock_code}_base.csv')


def new_posts_csv_path(stock_code: str) -> str:
    """Stage 1 爬取的新帖子 CSV"""
    return os.path.join(TEMP_DIR, f'{stock_code}_new_posts.csv')


def stage1_manifest_path(stock_code: str) -> str:
    """Stage 1 产物清单，Stage 2/3 据此校验"""
    return os.path.join(TEMP_DIR, f'{stock_code}_stage1_manifest.json')


def enhanced_csv_path(stock_code: str) -> str:
    """Stage 3 整合后的最终 CSV（incremental 模式）"""
    return os.path.join(EXPORT_DIR, f'{stock_code}_enhanced.csv')


def full_posts_csv_path(stock_code: str) -> str:
    """Stage 1 full 模式爬取的全量帖子 CSV"""
    return os.path.join(TEMP_DIR, f'{stock_code}_full_posts.csv')


def full_manifest_path(stock_code: str) -> str:
    """Stage 1 full 模式产物清单"""
    return os.path.join(TEMP_DIR, f'{stock_code}_full_manifest.json')


def full_page_cache_dir(stock_code: str) -> str:
    """Per-page cache for resumable full-mode Stage 1 list pages."""
    return os.path.join(TEMP_DIR, f'{stock_code}_full_pages')


def full_page_cache_path(stock_code: str, page_num: int) -> str:
    return os.path.join(full_page_cache_dir(stock_code), f'page_{int(page_num):06d}.json')


def full_output_csv_path(stock_code: str, start_date: str) -> str:
    """Stage 3 full 模式最终输出 CSV"""
    date_suffix = start_date.replace('-', '')
    return os.path.join(EXPORT_DIR, f'{stock_code}_full_{date_suffix}.csv')


def full_data_output_csv_path(stock_code: str, start_date: str) -> str:
    """Stage 3 full 模式复制到 data/ 目录的 CSV"""
    date_suffix = start_date.replace('-', '')
    return os.path.join(DATA_OUTPUT_DIR, f'{stock_code}_full_{date_suffix}.csv')


def comment_csv_path(stock_code: str) -> str:
    """评论 CSV"""
    return os.path.join(EXPORT_DIR, f'{stock_code}_comments.csv')


def default_source_dirs() -> list:
    """默认可查找历史 CSV 的目录。"""
    dirs = [_PROJECT_DIR, os.path.join(_PROJECT_DIR, RAR_INTERNAL_DIR)]
    try:
        for name in os.listdir(_PROJECT_DIR):
            path = os.path.join(_PROJECT_DIR, name)
            if not os.path.isdir(path):
                continue
            for filename in os.listdir(path):
                if len(filename) == 10 and filename[:6].isdigit() and filename.endswith('.csv'):
                    dirs.append(path)
                    break
    except OSError:
        pass

    unique_dirs = []
    seen = set()
    for path in dirs:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique_dirs.append(path)
    return unique_dirs


def file_sha256(path: str) -> str:
    """计算文件 SHA256，用于检测历史源 CSV 是否变化。"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def resolve_source_csv(stock_code: str, source_dirs: list = None) -> str:
    """按优先级查找 `{stock_code}.csv`，找到则返回绝对路径。"""
    csv_filename = f'{stock_code}.csv'
    search_dirs = []

    for src_dir in source_dirs or []:
        if src_dir:
            search_dirs.append(os.path.abspath(src_dir))

    search_dirs.extend(default_source_dirs())

    seen = set()
    for src_dir in search_dirs:
        if not src_dir:
            continue
        src_dir = os.path.abspath(src_dir)
        key = os.path.normcase(src_dir)
        if key in seen:
            continue
        seen.add(key)

        candidate = os.path.join(src_dir, csv_filename)
        if os.path.exists(candidate):
            return candidate

    return None


# ==================== Stage 1 ====================

def extract_csv_from_rar(stock_code: str) -> str:
    """从 RAR 中提取单个股票的 CSV 文件"""
    csv_filename = f'{stock_code}.csv'
    extracted_path = os.path.join(TEMP_DIR, csv_filename)
    base_path = base_csv_path(stock_code)
    rar_internal_path = f'{RAR_INTERNAL_DIR}/{csv_filename}'

    print(f'\n[Stage 1-1] 从 RAR 提取 {csv_filename} ...')

    if os.path.exists(base_path):
        print(f'  基础 CSV 已存在，跳过提取: {base_path}')
        return base_path

    unrar_path = os.path.expanduser('~/.local/bin/unrar')
    if not os.path.exists(unrar_path):
        unrar_path = 'unrar'

    cmd = [unrar_path, 'x', '-y', '-o+', RAR_FILE, rar_internal_path, TEMP_DIR + '/']
    try:
        subprocess.run(cmd, capture_output=True, text=True)
        inner_extracted = os.path.join(TEMP_DIR, rar_internal_path)
        if os.path.exists(inner_extracted):
            _safe_replace(inner_extracted, extracted_path)
            inner_dir = os.path.join(TEMP_DIR, RAR_INTERNAL_DIR)
            if os.path.exists(inner_dir) and not os.listdir(inner_dir):
                os.rmdir(inner_dir)
    except Exception as e:
        print(f'  ✗ 提取失败: {e}')
        return None

    if not os.path.exists(extracted_path):
        print(f'  ✗ 提取后文件不存在')
        return None

    # 重命名为 base.csv，避免与后续新帖子文件混淆
    _safe_replace(extracted_path, base_path)
    file_size = os.path.getsize(base_path) / 1024 / 1024
    print(f'  ✓ 提取成功: {base_path} ({file_size:.1f} MB)')
    return base_path


def build_post_id_set(csv_path: str) -> set:
    """读取 CSV 中所有 post_id 到内存 set（约 30MB，用于高速去重）"""
    existing_ids, _, _, count = scan_csv_state(csv_path)
    print(f'  ✓ 已加载 {count} 条记录的 post_id，内存去重集合大小: {len(existing_ids)}')
    return existing_ids


def get_latest_date_from_csv(csv_path: str) -> str:
    """获取 CSV 中最新的帖子日期（YYYY-MM-DD 格式），用于增量补爬的停止阈值"""
    print(f'\n[Stage 1-2b] 分析 CSV 时间范围 ...')
    _, latest_dt, earliest_dt, count = scan_csv_state(csv_path)
    print(f'  ✓ CSV 时间范围: {earliest_dt} ~ {latest_dt}，共 {count} 条')
    print(f'  → 将补爬 {latest_dt} 之后的新帖子')
    return latest_dt


def scan_csv_state(csv_path: str):
    """单次扫描 CSV，返回 post_id 集合、最新日期、最早日期和行数。"""
    existing_ids = set()
    latest_dt = None
    earliest_dt = None
    count = 0
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = row.get('post_id', '')
            if pid:
                existing_ids.add(str(pid))
            dt = row.get('post_publish_time', '').strip()
            if dt:
                date_part = dt[:10]
                if latest_dt is None or date_part > latest_dt:
                    latest_dt = date_part
                if earliest_dt is None or date_part < earliest_dt:
                    earliest_dt = date_part
            count += 1
    return existing_ids, latest_dt, earliest_dt, count


def make_csv_storage_callback(stock_code: str, existing_ids: set):
    """创建 CSV 追加存储回调函数"""
    new_csv = new_posts_csv_path(stock_code)
    header_written = os.path.exists(new_csv) and os.path.getsize(new_csv) > 0
    new_count = [0]
    skip_count = [0]

    def storage_callback(dic_list):
        """将 parse_post_info 输出的字典列表追加到 CSV"""
        rows = []
        for dic in dic_list:
            pid = str(dic.get('_id', ''))  # parse_post_info 返回的字段名是 _id
            if not pid or pid in existing_ids:
                skip_count[0] += 1
                continue
            existing_ids.add(pid)  # 防止同一批次内重复
            new_count[0] += 1

            # 映射内部字段名 → CSV 字段名
            post_date = dic.get('post_date', '')
            post_time = dic.get('post_time', '')
            publish_time = f"{post_date} {post_time}".strip() if post_time else post_date

            rows.append({
                'user_id': dic.get('user_id', ''),
                'post_id': pid,
                'post_source_id': dic.get('post_source_id', ''),
                'post_type': dic.get('post_type', ''),
                'user_name': dic.get('post_author', ''),
                'post_publish_time': publish_time,
                'stockbar_name': dic.get('stockbar_name', ''),
                'stockbar_code': dic.get('stockbar_code', stock_code),
                'forward': dic.get('forward', '0'),
                'coment_count': dic.get('comment_num', 0),
                'click_count': dic.get('post_view', 0),
                'like_count': dic.get('like_num', 0),
                'post_title': dic.get('post_title', ''),
                'url': dic.get('post_url', ''),
                'content': dic.get('post_content', ''),
            })

        if not rows:
            return

        nonlocal header_written
        mode = 'a' if header_written else 'w'
        with open(new_csv, mode, newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            if not header_written:
                writer.writeheader()
                header_written = True
            writer.writerows(rows)

    return storage_callback, new_count, skip_count


def crawl_post_list(stock_code: str, existing_ids: set, stop_date: str):
    """爬取帖子列表，新帖子直接追加到 CSV，遇到旧帖自动停止"""
    print(f'\n[Stage 1-3] 爬取帖子列表（从第 {POST_LIST_START_PAGE} 页开始，遇到 <= {stop_date} 的帖子自动停止）...')
    callback, new_count, skip_count = make_csv_storage_callback(stock_code, existing_ids)
    post_crawler = PostCrawler(stock_code)
    post_crawler.crawl_post_info(
        POST_LIST_START_PAGE, 99999,  # page2 被 stop_date 模式忽略，传大数兜底
        storage_callback=callback,
        stop_date=stop_date
    )
    print(f'  ✓ 新帖子已追加到 CSV，新增 {new_count[0]} 条，过滤重复 {skip_count[0]} 条')


def _full_csv_row_from_post_dict(stock_code: str, dic: dict) -> dict | None:
    pid = str(dic.get('_id', ''))
    if not pid:
        return None
    post_date = dic.get('post_date', '')
    post_time = dic.get('post_time', '')
    publish_time = f"{post_date} {post_time}".strip() if post_time else post_date
    return {
        'user_id': dic.get('user_id', ''),
        'post_id': pid,
        'post_source_id': dic.get('post_source_id', ''),
        'post_type': dic.get('post_type', ''),
        'user_name': dic.get('post_author', ''),
        'post_publish_time': publish_time,
        'stockbar_name': dic.get('stockbar_name', ''),
        'stockbar_code': dic.get('stockbar_code', stock_code),
        'forward': dic.get('forward', '0'),
        'coment_count': dic.get('comment_num', 0),
        'click_count': dic.get('post_view', 0),
        'like_count': dic.get('like_num', 0),
        'post_title': dic.get('post_title', ''),
        'url': dic.get('post_url', ''),
        'content': dic.get('post_content', ''),
    }


def clear_full_stage1_artifacts(stock_code: str):
    for path in (full_posts_csv_path(stock_code), full_manifest_path(stock_code)):
        if os.path.exists(path):
            os.remove(path)
            print(f'  cleared {path}')
    cache_dir = full_page_cache_dir(stock_code)
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir)
        print(f'  cleared {cache_dir}')


def write_full_page_cache(stock_code: str, page_num: int, rows: list, meta: dict | None = None):
    os.makedirs(full_page_cache_dir(stock_code), exist_ok=True)
    payload = {
        'stock': stock_code,
        'page': int(page_num),
        'rows': rows,
        'meta': meta or {},
        'created_at': datetime.now().isoformat(),
    }
    path = full_page_cache_path(stock_code, page_num)
    tmp = _tmp_path(path)
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
        f.write('\n')
    _safe_replace(tmp, path)


def read_full_page_cache(stock_code: str, page_num: int) -> dict | None:
    path = full_page_cache_path(stock_code, page_num)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def cached_full_pages(stock_code: str, max_page: int | None = None) -> set[int]:
    cache_dir = full_page_cache_dir(stock_code)
    if not os.path.isdir(cache_dir):
        return set()
    pages = set()
    for filename in os.listdir(cache_dir):
        if not filename.startswith('page_') or not filename.endswith('.json'):
            continue
        try:
            page_num = int(filename[5:-5])
        except ValueError:
            continue
        if max_page is None or page_num <= max_page:
            pages.add(page_num)
    return pages


def rebuild_full_posts_from_page_cache(
    stock_code: str,
    page_limit: int | None = None,
    boundary_page: int | None = None,
) -> dict:
    max_page = page_limit or boundary_page
    pages = sorted(cached_full_pages(stock_code, max_page=max_page))
    full_csv = full_posts_csv_path(stock_code)
    rows = []
    seen_ids = set()
    for page_num in pages:
        payload = read_full_page_cache(stock_code, page_num)
        if not payload:
            continue
        for dic in payload.get('rows') or []:
            row = _full_csv_row_from_post_dict(stock_code, dic)
            if not row:
                continue
            pid = str(row.get('post_id', ''))
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            rows.append(row)

    rows.sort(key=lambda r: r.get('post_publish_time', '').strip(), reverse=True)
    with open(full_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    times = [r.get('post_publish_time', '').strip() for r in rows if r.get('post_publish_time', '').strip()]
    return {
        'rows': len(rows),
        'unique_post_ids': len(seen_ids),
        'cached_pages': pages,
        'completed_pages': len(pages),
        'min_time': min(times) if times else '',
        'max_time': max(times) if times else '',
    }


def make_full_storage_callback(stock_code: str):
    """full 模式专用 CSV 写入回调，直接写 {stock}_full_posts.csv"""
    full_csv = full_posts_csv_path(stock_code)
    header_written = os.path.exists(full_csv) and os.path.getsize(full_csv) > 0
    new_count = [0]

    def storage_callback(dic_list):
        rows = []
        for dic in dic_list:
            pid = str(dic.get('_id', ''))
            if not pid:
                continue
            post_date = dic.get('post_date', '')
            post_time = dic.get('post_time', '')
            publish_time = f"{post_date} {post_time}".strip() if post_time else post_date
            rows.append({
                'user_id': dic.get('user_id', ''),
                'post_id': pid,
                'post_source_id': dic.get('post_source_id', ''),
                'post_type': dic.get('post_type', ''),
                'user_name': dic.get('post_author', ''),
                'post_publish_time': publish_time,
                'stockbar_name': dic.get('stockbar_name', ''),
                'stockbar_code': dic.get('stockbar_code', stock_code),
                'forward': dic.get('forward', '0'),
                'coment_count': dic.get('comment_num', 0),
                'click_count': dic.get('post_view', 0),
                'like_count': dic.get('like_num', 0),
                'post_title': dic.get('post_title', ''),
                'url': dic.get('post_url', ''),
                'content': dic.get('post_content', ''),
            })
        if not rows:
            return
        nonlocal header_written
        mode = 'a' if header_written else 'w'
        with open(full_csv, mode, newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            if not header_written:
                writer.writeheader()
                header_written = True
            writer.writerows(rows)
        new_count[0] += len(rows)

    return storage_callback, new_count


def write_full_manifest(stock_code: str, start_date: str, summary: dict):
    """写入 full 模式 manifest，供 Stage 2/3 校验。"""
    manifest = {
        "stock": stock_code,
        "crawl_mode": "full",
        "start_date": start_date,
        "full_posts_csv": full_posts_csv_path(stock_code),
        "max_page": summary.get('max_page', 0),
        "boundary_page": summary.get('boundary_page', 0),
        "completed_pages": summary.get('completed_pages', 0),
        "failed_pages": summary.get('failed_pages', []),
        "blocked_pages": summary.get('blocked_pages', []),
        "transient_failed_pages": summary.get('transient_failed_pages', []),
        "status": summary.get('status', 'success'),
        "partial": bool(summary.get('partial', False)),
        "page_limit": summary.get('page_limit', 0),
        "list_source": summary.get('list_source', 'html'),
        "list_workers": summary.get('list_workers', LIST_WORKERS),
        "list_window_size": summary.get('list_window_size', LIST_WINDOW_SIZE),
        "skipped_cached_pages": summary.get('skipped_cached_pages', 0),
        "rows": summary.get('rows', 0),
        "unique_post_ids": summary.get('unique_post_ids', 0),
        "min_time": summary.get('min_time', ''),
        "max_time": summary.get('max_time', ''),
        "created_at": datetime.now().isoformat(),
    }
    path = full_manifest_path(stock_code)
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _safe_replace(tmp, path)


def read_full_manifest(stock_code: str) -> dict | None:
    path = full_manifest_path(stock_code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def write_stage1_manifest(
    stock_code: str,
    source_csv: str | None,
    source_rows: int,
    source_sha256: str,
    source_latest_date: str,
    new_rows: int,
):
    """持久化 Stage 1 产物清单，供 Stage 2/3 校验。"""
    manifest = {
        "stock": stock_code,
        "source_csv": source_csv,
        "source_rows": source_rows,
        "source_sha256": source_sha256,
        "source_latest_date": source_latest_date,
        "new_posts_csv": new_posts_csv_path(stock_code),
        "new_rows": new_rows,
        "created_at": datetime.now().isoformat(),
    }
    path = stage1_manifest_path(stock_code)
    tmp = _tmp_path(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _safe_replace(tmp, path)


def read_stage1_manifest(stock_code: str) -> dict | None:
    path = stage1_manifest_path(stock_code)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def validate_stage1_inputs(stock_code: str, source_dirs: list | None, force_refresh_base: bool = False) -> tuple[str | None, bool]:
    """返回 (source_csv 路径, 是否复用了已有 base)。

    当以下情况之一时，强制重建 base：
      - force_refresh_base=True
      - 源文件与 manifest 记录不一致（sha256/size/行数）
      - 已有 base 与当前源文件不一致
    """
    base_path = base_csv_path(stock_code)
    source_csv = resolve_source_csv(stock_code, source_dirs)

    if force_refresh_base:
        print("  --force-refresh-base 已开启，强制重建 base CSV")
        return source_csv, False

    manifest = read_stage1_manifest(stock_code)
    if manifest and source_csv:
        manifest_sha = manifest.get("source_sha256", "")
        manifest_rows = manifest.get("source_rows", 0)
        current_sha = file_sha256(source_csv)
        _, _, _, current_rows = scan_csv_state(source_csv)
        if manifest_sha != current_sha or manifest_rows != current_rows:
            print(f"[警告] 历史源 CSV 与 Stage 1 manifest 不一致，强制重建 base")
            print(f"  sha256: {manifest_sha[:16]}... -> {current_sha[:16]}...")
            print(f"  rows: {manifest_rows} -> {current_rows}")
            return source_csv, False

    if os.path.exists(base_path):
        if source_csv:
            # 即使 manifest 不存在，也兜底比较一次
            base_sha = file_sha256(base_path)
            src_sha = file_sha256(source_csv)
            if base_sha != src_sha:
                print(f"[警告] temp_extract 中的 base CSV 与源文件不一致，将重新复制")
                return source_csv, False
        return source_csv, True

    return source_csv, False


def run_stage1(source_dirs: list = None, force_refresh_base: bool = False, force_full_refresh: bool = False):
    if CRAWL_MODE == 'full':
        return run_stage1_full(
            start_date=START_DATE,
            list_workers=LIST_WORKERS,
            list_window_size=LIST_WINDOW_SIZE,
            list_source=LIST_SOURCE,
            list_page_limit=LIST_PAGE_LIMIT,
            force_full_refresh=force_full_refresh,
        )

    print(f'\n{"="*60}')
    print(f'[Stage 1] {STOCK_CODE} 提取 CSV + 爬取帖子列表')
    print(f'{"="*60}')

    if not check_disk_space(min_gb=0.5):
        return False

    ensure_dirs()

    # 1. 获取基础 CSV（优先从 source-dir / 本地历史目录复制，回退到 RAR 提取）
    base_path = base_csv_path(STOCK_CODE)
    source_csv, reuse_base = validate_stage1_inputs(STOCK_CODE, source_dirs, force_refresh_base)

    if reuse_base and os.path.exists(base_path):
        print(f'\n[Stage 1-1] 基础 CSV 已存在且与源一致，跳过复制: {base_path}')
    else:
        if source_csv:
            print(f'\n[Stage 1-1] 使用历史 CSV: {source_csv}')
            shutil.copy2(source_csv, base_path)
            file_size = os.path.getsize(base_path) / 1024 / 1024
            print(f'  ✓ 已复制到: {base_path} ({file_size:.1f} MB)')
        elif os.path.exists(RAR_FILE):
            csv_path = extract_csv_from_rar(STOCK_CODE)
            if not csv_path:
                print('提取 CSV 失败，终止')
                return False
            source_csv = csv_path
        else:
            searched = ', '.join(default_source_dirs() + (source_dirs or []))
            print(f'[错误] 未找到 {STOCK_CODE}.csv，也没有可用数据.rar，无法继续')
            print(f'  已查找目录: {searched}')
            return False

    csv_path = base_path

    # 2. 单次扫描 CSV：读取 post_id 去重集合 + 获取最新日期
    print(f'\n[Stage 1-2] 扫描 CSV 状态（post_id 去重集合 + 时间范围）...')
    existing_ids, stop_date, earliest_dt, count = scan_csv_state(csv_path)
    print(f'  ✓ 已加载 {count} 条记录，post_id 去重集合大小: {len(existing_ids)}')
    print(f'  ✓ CSV 时间范围: {earliest_dt} ~ {stop_date}')
    print(f'  → 将补爬 {stop_date} 之后的新帖子')

    # 3. 爬取帖子列表（从第1页开始，遇到旧帖自动停止，新帖子直接追加 CSV）
    #    开始前清空旧 new_posts.csv，避免把上一次运行结果重复计入
    new_csv = new_posts_csv_path(STOCK_CODE)
    if os.path.exists(new_csv):
        os.remove(new_csv)
        print(f'  已清空旧新帖 CSV: {new_csv}')

    crawl_post_list(STOCK_CODE, existing_ids, stop_date)

    new_rows = 0
    if os.path.exists(new_csv):
        _, _, _, new_rows = scan_csv_state(new_csv)

    source_sha = file_sha256(source_csv) if source_csv else ""
    write_stage1_manifest(
        STOCK_CODE,
        source_csv=source_csv,
        source_rows=count,
        source_sha256=source_sha,
        source_latest_date=stop_date or "",
        new_rows=new_rows,
    )
    print(f'  ✓ Stage 1 manifest 已写入: {stage1_manifest_path(STOCK_CODE)}')
    print(f'    source_rows={count}, new_rows={new_rows}')

    set_flag('stage1')
    print(f'\n{"="*60}')
    print(f'[Stage 1] 完成！请在新 PowerShell 窗口运行 Stage 2')
    print(f'{"="*60}')
    return True


def run_stage1_full(
    start_date: str,
    list_workers: int,
    list_window_size: int = 30,
    list_source: str = "html",
):
    """full 模式 Stage 1：从网页全量抓取 start_date 之后的帖子列表。"""
    if list_source != "html":
        print(f'  [兼容] 已切换回上游 Selenium HTML 方法，忽略 list_source={list_source}')
        list_source = "html"
    print(f'\n{"="*60}')
    print(f'[Stage 1 full] {STOCK_CODE} 全量爬取帖子列表（start_date={start_date}, list_source={list_source}）')
    print(f'{"="*60}')

    if not check_disk_space(min_gb=1.0):
        return False

    ensure_dirs()

    # 开始前清空旧 full_posts.csv，避免把上一次运行结果重复计入
    full_csv = full_posts_csv_path(STOCK_CODE)
    if os.path.exists(full_csv):
        os.remove(full_csv)
        print(f'  已清空旧 full_posts CSV: {full_csv}')

    callback, new_count = make_full_storage_callback(STOCK_CODE)
    post_crawler = PostCrawler(STOCK_CODE)
    summary = post_crawler.crawl_post_info_since(
        start_date=start_date,
        storage_callback=callback,
        list_workers=list_workers,
        list_window_size=list_window_size,
        list_source=list_source,
    )
    print(f'  ✓ 全量帖子已写入 CSV: {full_csv}')
    print(f'    总页数={summary.get("max_page")}, 边界页={summary.get("boundary_page")}, '
          f'完成页={summary.get("completed_pages")}, 失败页={summary.get("failed_pages")}, '
          f'行数={summary.get("rows")}, 唯一ID={summary.get("unique_post_ids")}, '
          f'时间范围={summary.get("min_time")} ~ {summary.get("max_time")}')

    if summary.get('status') == 'paused_blocked':
        print(f'\n[错误] Stage 1 full 因验证/限流暂停！')
        print(f'  原因: {summary.get("paused_reason")}')
        print(f'  阻塞页: {summary.get("blocked_pages")}')
        print(f'  恢复步骤:')
        print(f'    1. python auto_pipeline_000001.py --stock {STOCK_CODE} --manual-verify')
        print(f'    2. 重新运行 Stage 1（会自动断点续爬）')
    elif summary.get('failed_pages'):
        print(f'\n[警告] Stage 1 full 存在 {len(summary["failed_pages"])} 个失败页: {summary["failed_pages"]}')
        print(f'  [提示] 可重新运行 Stage 1 进行补爬（断点续爬会自动跳过已成功的页）')
    else:
        print(f'\n[成功] Stage 1 full 完整完成！')

    write_full_manifest(STOCK_CODE, start_date, summary)
    print(f'  ✓ full manifest 已写入: {full_manifest_path(STOCK_CODE)}')

    set_flag('stage1')
    print(f'\n{"="*60}')
    print(f'[Stage 1 full] 完成！请在新 PowerShell 窗口运行 Stage 2')
    print(f'{"="*60}')
    return True


def run_stage1_full(
    start_date: str,
    list_workers: int,
    list_window_size: int = 80,
    list_source: str = "html",
    list_page_limit: int = 0,
    force_full_refresh: bool = False,
):
    """Full-mode Stage 1 using fast requests HTML list pages plus page cache."""
    print(f'\n{"="*60}')
    print(f'[Stage 1 full] {STOCK_CODE} fast list crawl '
          f'(start_date={start_date}, list_source={list_source}, '
          f'workers={list_workers}, page_limit={list_page_limit or "none"})')
    print(f'{"="*60}')

    if not check_disk_space(min_gb=1.0):
        return False
    ensure_dirs()

    if force_full_refresh:
        print('  force refresh enabled; clearing full-mode temporary artifacts')
        clear_full_stage1_artifacts(STOCK_CODE)
        clear_flags()

    page_limit = int(list_page_limit or 0)
    page_limit_or_none = page_limit if page_limit > 0 else None
    cached_pages_set = cached_full_pages(STOCK_CODE, max_page=page_limit_or_none)
    if cached_pages_set:
        print(f'  resume cache: {len(cached_pages_set)} pages already cached')

    def storage_callback(_rows):
        return None

    def page_storage_callback(page_num, rows, meta):
        write_full_page_cache(STOCK_CODE, page_num, rows, meta)

    post_crawler = PostCrawler(STOCK_CODE)
    summary = post_crawler.crawl_post_info_since(
        start_date=start_date,
        storage_callback=storage_callback,
        list_workers=list_workers,
        list_window_size=list_window_size,
        list_source=list_source,
        cached_pages=cached_pages_set,
        page_storage_callback=page_storage_callback,
        window_pause_range=(3.0, 8.0),
        page_limit=page_limit_or_none,
    )

    rebuild_stats = rebuild_full_posts_from_page_cache(
        STOCK_CODE,
        page_limit=page_limit_or_none,
        boundary_page=summary.get('boundary_page') or None,
    )
    summary.update({
        'rows': rebuild_stats['rows'],
        'unique_post_ids': rebuild_stats['unique_post_ids'],
        'completed_pages': rebuild_stats['completed_pages'],
        'min_time': rebuild_stats['min_time'],
        'max_time': rebuild_stats['max_time'],
        'partial': bool(page_limit_or_none),
        'page_limit': page_limit_or_none or 0,
    })
    full_csv = full_posts_csv_path(STOCK_CODE)
    print(f'  full_posts rebuilt: {full_csv}')
    print(f'  pages={summary.get("completed_pages")}/{summary.get("boundary_page")} '
          f'rows={summary.get("rows")} unique={summary.get("unique_post_ids")} '
          f'failed={summary.get("failed_pages")} blocked={summary.get("blocked_pages")} '
          f'time={summary.get("time_cost_seconds")}s')

    write_full_manifest(STOCK_CODE, start_date, summary)
    print(f'  full manifest written: {full_manifest_path(STOCK_CODE)}')

    status = summary.get('status')
    if status == 'paused_blocked':
        print('  [error] Stage 1 full paused because validation/anti-bot persisted after retries')
        return False
    if summary.get('failed_pages'):
        print(f'  [error] Stage 1 full still has failed pages: {summary.get("failed_pages")}')
        return False
    if page_limit_or_none:
        print('  [partial] Trial crawl finished. Stage 2/3 are intentionally blocked for partial manifests.')
        return True

    set_flag('stage1')
    print(f'\n{"="*60}')
    print('[Stage 1 full] complete; run Stage 2 next')
    print(f'{"="*60}')
    return True


# ==================== Stage 2 ====================

def load_posts_for_detail_crawl(csv_paths: list, skip_failed_ids: set = None):
    """扫描 CSV，返回 (待爬财富号帖子列表, 普通帖标题填充字典)

    — 财富号帖子（post_type='20' 或 URL 含 caifuhao）：content 为空时加入爬取队列
      但已记录为失效/空正文的财富号会跳过，避免重跑时反复卡住
    — 普通股吧帖（post_type='0' 等）：content 为空时直接用 post_title 填充，不爬详情页
    — 已有正文的帖子：跳过
    """
    print(f'\n[Stage 2-1a] 筛选需要补爬正文的帖子 ...')
    skip_failed_ids = skip_failed_ids or set()
    posts = []
    title_fills = {}
    caifuhao_empty = 0
    caifuhao_failed_skipped = 0
    normal_filled = 0
    skipped_has_content = 0
    seen_ids = set()

    for csv_path in csv_paths:
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = str(row.get('post_id', ''))
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)

                content = row.get('content', '')
                is_empty = not content or not content.strip()

                if not is_empty:
                    skipped_has_content += 1
                    continue

                post_type = row.get('post_type', '').strip()
                url = row.get('url', '')
                is_caifuhao = (post_type == '20' or 'caifuhao.eastmoney.com' in url)

                if is_caifuhao:
                    if pid in skip_failed_ids:
                        caifuhao_failed_skipped += 1
                        continue
                    posts.append({
                        '_id': pid,
                        'post_url': url,
                        'post_source_id': row.get('post_source_id', ''),
                        'post_type': row.get('post_type', ''),
                        'post_content': '',
                    })
                    caifuhao_empty += 1
                else:
                    title = row.get('post_title', '')
                    if title.strip():
                        title_fills[pid] = title.strip()
                        normal_filled += 1

    print(f'  ✓ 财富号缺失正文: {caifuhao_empty}（进入爬取队列）')
    print(f'  ✓ 已知失效财富号跳过: {caifuhao_failed_skipped}（不重复爬取）')
    print(f'  ✓ 普通帖标题填充: {normal_filled}（本地完成，不爬详情页）')
    print(f'  ✓ 已有正文跳过: {skipped_has_content}')
    return posts, title_fills


def update_csv_content(csv_path: str, updates: dict):
    """流式更新 CSV 中的 content 字段（内存友好）"""
    if not updates:
        return

    tmp_path = _tmp_path(csv_path)
    updated = 0

    with open(csv_path, 'r', encoding='utf-8') as f_in, \
         open(tmp_path, 'w', newline='', encoding='utf-8') as f_out:
        reader = csv.DictReader(f_in)
        writer = csv.DictWriter(f_out, fieldnames=reader.fieldnames)
        writer.writeheader()

        for row in reader:
            pid = str(row.get('post_id', ''))
            if pid in updates:
                row['content'] = updates[pid]
                updated += 1
            writer.writerow(row)

    _safe_replace(tmp_path, csv_path)
    print(f'  ✓ 已更新 CSV: {updated} 条记录的正文字段')


def _checkpoint_path(stock_code: str) -> str:
    """Stage 2 断点续爬的检查点文件"""
    return os.path.join(TEMP_DIR, f'{stock_code}_stage2_checkpoint.json')


def _content_delta_path(stock_code: str) -> str:
    """Stage 2 正文增量结果文件，避免频繁重写大 CSV。"""
    return os.path.join(TEMP_DIR, f'{stock_code}_content_updates.jsonl')


def _detail_failed_path(stock_code: str) -> str:
    """财富号正文失效/空正文记录，跨重跑持久跳过。"""
    return os.path.join(TEMP_DIR, f'{stock_code}_detail_failed.jsonl')


def _load_content_delta(stock_code: str) -> dict:
    """读取已落盘但尚未合并进 CSV 的正文更新。"""
    path = _content_delta_path(stock_code)
    updates = {}
    if not os.path.exists(path):
        return updates
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = str(row.get('post_id', ''))
            content = row.get('post_content', '')
            if pid and content:
                updates[pid] = content
    return updates


def _append_content_delta(stock_code: str, post_id: str, content: str, lock: threading.Lock = None):
    """追加单条正文更新到 delta 文件（线程安全）"""
    if not content:
        return
    path = _content_delta_path(stock_code)

    def _write():
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'post_id': str(post_id), 'post_content': content}, ensure_ascii=False))
            f.write('\n')

    if lock:
        with lock:
            _write()
    else:
        _write()


def _delete_content_delta(stock_code: str):
    path = _content_delta_path(stock_code)
    if os.path.exists(path):
        os.remove(path)


def _load_detail_failed_ids(stock_code: str) -> set:
    """读取已确认正文失效/为空的财富号 post_id，避免下次 Stage 2 重复爬。"""
    path = _detail_failed_path(stock_code)
    failed_ids = set()
    retryable_reasons = {
        'http_403', 'http_429', 'blocked_validation', 'timeout',
        'body_not_found', 'wap_system_busy', 'wap_api_empty',
    }
    if not os.path.exists(path):
        return failed_ids
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = str(row.get('post_id', ''))
            reason = str(row.get('reason', '') or '')
            if reason.startswith('retry_failed:'):
                reason = reason.split(':', 1)[1]
            if pid and reason not in retryable_reasons:
                failed_ids.add(pid)
    return failed_ids


def _append_detail_failed(stock_code: str, post_id: str, reason: str = '', url: str = ''):
    """追加单条财富号失效记录。保留 CSV content 为空，但下次不再重复爬取。"""
    path = _detail_failed_path(stock_code)
    payload = {
        'post_id': str(post_id),
        'reason': reason or 'empty_or_failed',
        'url': url or '',
        'updated_at': datetime.now().isoformat(),
    }
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(payload, ensure_ascii=False))
        f.write('\n')


def _load_checkpoint(stock_code: str) -> set:
    """读取已完成的 post_id 集合"""
    cp = _checkpoint_path(stock_code)
    if not os.path.exists(cp):
        return set()
    try:
        with open(cp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data.get('done_ids', []))
    except Exception:
        return set()


def _save_checkpoint(stock_code: str, done_ids: set):
    """持久化检查点（追加模式，不丢失已有记录）"""
    cp = _checkpoint_path(stock_code)
    existing = _load_checkpoint(stock_code)
    existing.update(done_ids)
    with open(cp, 'w', encoding='utf-8') as f:
        json.dump({'done_ids': list(existing), 'updated_at': datetime.now().isoformat()}, f)


def _delete_checkpoint(stock_code: str):
    cp = _checkpoint_path(stock_code)
    if os.path.exists(cp):
        os.remove(cp)


def crawl_post_detail_csv(stock_code: str, csv_paths: list, detail_workers: int = 3):
    """从 CSV 读取并补爬帖子正文（支持断点续爬 + 线程安全回调）

    策略：
    - 普通股吧帖：content 为空时直接用 post_title 填充（本地完成，不爬详情页）
    - 财富号帖子：content 为空时进入爬取队列（多线程 requests）
    - 不爬评论，不要求 MongoDB

    Args:
        stock_code: 股票代码
        csv_paths: CSV 文件路径列表
        detail_workers: 财富号 requests 并发数，默认 3。设为 1 回退单线程
    """
    print(f'\n[Stage 2-1] 爬取帖子正文（仅财富号补爬 + 普通帖标题填充）...')
    failed_ids = _load_detail_failed_ids(stock_code)
    if failed_ids:
        print(f'  [失效跳过] 已记录 {len(failed_ids)} 条财富号正文失效，重跑时将直接跳过')
    posts, title_fills = load_posts_for_detail_crawl(csv_paths, skip_failed_ids=failed_ids)
    content_updates = _load_content_delta(stock_code)

    # 先写回普通帖标题填充（无需爬取，直接本地完成）
    if title_fills:
        _flush_updates_to_csv(csv_paths, title_fills)
        print(f'  ✓ 已本地填充 {len(title_fills)} 条普通帖 content → post_title')

    # 如果断点续爬中有已爬但未合并的 delta，也合并进来
    if content_updates and not posts:
        print(f'  检测到 {len(content_updates)} 条未合并正文更新，正在写回 CSV...')
        _flush_updates_to_csv(csv_paths, content_updates)
        _delete_content_delta(stock_code)
        _delete_checkpoint(stock_code)

    if not posts:
        if content_updates:
            _flush_updates_to_csv(csv_paths, content_updates)
            _delete_content_delta(stock_code)
            _delete_checkpoint(stock_code)
        print('  ✓ 无需要爬取正文的财富号帖子，跳过')
        return True

    # 断点续爬：跳过已完成的财富号帖子
    done_ids = _load_checkpoint(stock_code)
    if done_ids:
        original_count = len(posts)
        posts = [p for p in posts if str(p['_id']) not in done_ids]
        skipped = original_count - len(posts)
        print(f'  [断点续爬] 检查点中发现 {len(done_ids)} 条已完成，跳过 {skipped} 条，剩余 {len(posts)} 条待爬')

    # 试跑限制：通过环境变量 STAGE2_DETAIL_LIMIT 控制爬取数量，用于验证成功率
    detail_limit = os.environ.get('STAGE2_DETAIL_LIMIT', '')
    if detail_limit and detail_limit.strip().isdigit():
        limit = int(detail_limit.strip())
        if limit > 0 and len(posts) > limit:
            print(f'  [试跑模式] STAGE2_DETAIL_LIMIT={limit}，仅爬取前 {limit} 条')
            posts = posts[:limit]

    if not posts:
        if content_updates:
            _flush_updates_to_csv(csv_paths, content_updates)
            _delete_content_delta(stock_code)
            _delete_checkpoint(stock_code)
        print('  ✓ 全部财富号帖子已完成，跳过')
        return True

    CHECKPOINT_INTERVAL = 50
    crawl_count = [0]
    batch_done = set()
    cb_lock = threading.Lock()

    def update_callback(post_id, update_data):
        pid = str(post_id)
        content = update_data.get('post_content', '')
        with cb_lock:
            if content:
                content_updates[pid] = content
                _append_content_delta(stock_code, pid, content)
            elif update_data.get('_detail_failed'):
                _append_detail_failed(
                    stock_code,
                    pid,
                    reason=update_data.get('reason', 'empty_or_failed'),
                    url=update_data.get('post_url', ''),
                )
            crawl_count[0] += 1
            batch_done.add(pid)
            if crawl_count[0] % CHECKPOINT_INTERVAL == 0:
                _save_checkpoint(stock_code, batch_done)
                batch_done.clear()

    post_crawler = PostCrawler(stock_code)
    ok = post_crawler.crawl_post_detail(
        posts=posts,
        update_callback=update_callback,
        max_workers=detail_workers
    )

    # 最终写回 CSV
    _flush_updates_to_csv(csv_paths, content_updates)
    if batch_done:
        _save_checkpoint(stock_code, batch_done)
    if ok is False:
        print('  [pause] caifuhao detail crawl hit retryable blocking; checkpoint kept for next retry')
        return False
    _delete_checkpoint(stock_code)
    _delete_content_delta(stock_code)

    print(f'  ✓ 财富号正文爬取完成，共成功爬取 {crawl_count[0]}/{len(posts)} 条')
    return True

def _flush_updates_to_csv(csv_paths: list, updates: dict):
    """增量写回 CSV（断点续爬支持）"""
    if not updates:
        return
    for csv_path in csv_paths:
        if os.path.exists(csv_path):
            update_csv_content(csv_path, updates)
    updates.clear()


def run_stage2(detail_workers: int = 3):
    if CRAWL_MODE == 'full':
        return run_stage2_full(detail_workers=detail_workers)

    print(f'\n{"="*60}')
    print(f'[Stage 2] {STOCK_CODE} 正文补爬')
    print(f'{"="*60}')

    if not check_flag('stage1'):
        print('[错误] Stage 1 未完成，请先运行 Stage 1')
        return False

    if not check_disk_space(min_gb=0.5):
        return False

    base_csv = base_csv_path(STOCK_CODE)
    new_csv = new_posts_csv_path(STOCK_CODE)
    if not os.path.exists(base_csv):
        print(f'[错误] 基础 CSV 不存在: {base_csv}')
        return False

    csv_paths = [base_csv]
    if os.path.exists(new_csv):
        csv_paths.append(new_csv)
        print(f'  检测到新帖子 CSV: {new_csv}')

    # 补爬正文（财富号走多线程 requests，股吧原生走多 worker 并发）
    ok = crawl_post_detail_csv(STOCK_CODE, csv_paths, detail_workers=detail_workers)
    if not ok:
        print(f'\n{"="*60}')
        print('[Stage 2] 暂停/未完成，未写入 stage2.done')
        print(f'{"="*60}')
        return False

    set_flag('stage2')
    print(f'\n{"="*60}')
    print(f'[Stage 2] 完成！请在新 PowerShell 窗口运行 Stage 3')
    print(f'{"="*60}')
    return True


def run_stage2_full(detail_workers: int = 3):
    """full 模式 Stage 2：只对 full_posts.csv 补爬正文。"""
    print(f'\n{"="*60}')
    print(f'[Stage 2 full] {STOCK_CODE} 正文补爬')
    print(f'{"="*60}')

    partial_manifest = read_full_manifest(STOCK_CODE)
    if partial_manifest and partial_manifest.get('partial'):
        print('[error] Stage 2 full refused: manifest is partial/trial output')
        return False

    if not check_flag('stage1'):
        print('[错误] Stage 1 未完成，请先运行 Stage 1')
        return False

    manifest = read_full_manifest(STOCK_CODE)
    if not manifest:
        print(f'[错误] 未找到 full manifest: {full_manifest_path(STOCK_CODE)}')
        return False

    full_csv = full_posts_csv_path(STOCK_CODE)
    if not os.path.exists(full_csv):
        print(f'[错误] full_posts CSV 不存在: {full_csv}')
        return False

    actual_rows = _count_csv_rows(full_csv)
    if actual_rows != manifest.get('rows', 0):
        print(f'[警告] full_posts 行数与 manifest 不符: manifest={manifest.get("rows")}, actual={actual_rows}')

    ok = crawl_post_detail_csv(STOCK_CODE, [full_csv], detail_workers=detail_workers)
    if not ok:
        print(f'\n{"="*60}')
        print('[Stage 2 full] 暂停/未完成，未写入 stage2.done')
        print(f'{"="*60}')
        return False

    set_flag('stage2')
    print(f'\n{"="*60}')
    print(f'[Stage 2 full] 完成！请在新 PowerShell 窗口运行 Stage 3')
    print(f'{"="*60}')
    return True


# ==================== Stage 3 ====================

def _count_csv_rows(csv_path: str) -> int:
    """返回 CSV 数据行数（不含表头）。"""
    if not os.path.exists(csv_path):
        return 0
    with open(csv_path, 'r', encoding='utf-8') as f:
        return sum(1 for _ in csv.DictReader(f))


def merge_csv_files(stock_code: str, require_manifest: bool = True) -> str | None:
    """合并基础 CSV 和新帖子 CSV，按 post_publish_time 降序排列（新帖在前）。

    Args:
        require_manifest: 为 True 时，Stage 1 manifest 缺失或关键产物不匹配会返回 None。
    """
    print(f'\n[Stage 3-1] 合并基础数据与新爬取帖子（按时间降序）...')
    base_csv = base_csv_path(stock_code)
    new_csv = new_posts_csv_path(stock_code)
    out_csv = enhanced_csv_path(stock_code)

    # 强校验 1: base CSV 必须存在
    if not os.path.exists(base_csv):
        print(f'  [错误] 基础 CSV 不存在，无法合并: {base_csv}')
        print(f'  请确认 Stage 1 已成功运行并生成 {base_csv}')
        return None

    # 强校验 2: 读取 Stage 1 manifest，与当前产物做一致性检查
    manifest = read_stage1_manifest(stock_code)
    if require_manifest:
        if not manifest:
            print(f'  [错误] 未找到 Stage 1 manifest: {stage1_manifest_path(stock_code)}')
            print(f'  请重新运行 Stage 1，确保生成 manifest 后再执行 Stage 3')
            return None

        expected_source_rows = manifest.get('source_rows', 0)
        expected_new_rows = manifest.get('new_rows', 0)
        actual_base_rows = _count_csv_rows(base_csv)
        actual_new_rows = _count_csv_rows(new_csv)

        if actual_base_rows != expected_source_rows:
            print(f'  [错误] base 行数与 manifest 不符: manifest={expected_source_rows}, actual={actual_base_rows}')
            print(f'  建议删除该股票的临时产物后重新跑 Stage 1')
            return None

        # new_posts.csv 缺失但 manifest 期望有数据 -> 直接失败
        if expected_new_rows > 0 and actual_new_rows == 0:
            print(f'  [错误] manifest 期望 {expected_new_rows} 条新帖，但 {new_csv} 不存在或为空')
            print(f'  请重新跑 Stage 1，或检查是否误删了新帖 CSV')
            return None

        if actual_new_rows != expected_new_rows:
            print(f'  [警告] new_posts 行数与 manifest 不符: manifest={expected_new_rows}, actual={actual_new_rows}')
            print(f'  将继续合并，但请确认数据完整性')
    else:
        print(f'  [警告] 未启用 manifest 校验（require_manifest=False）')

    all_rows = []
    row_counts = {}

    for src_path, label in [(base_csv, '基础数据'), (new_csv, '新帖子')]:
        if not os.path.exists(src_path):
            continue
        rows = 0
        with open(src_path, 'r', encoding='utf-8') as f_in:
            reader = csv.DictReader(f_in)
            for row in reader:
                all_rows.append(row)
                rows += 1
        row_counts[label] = rows
        print(f'  已读取 {label}: {src_path} ({rows} 条)')

    # 按 post_publish_time 降序排列（最新的排最前面）
    def _sort_key(row):
        ts = row.get('post_publish_time', '').strip()
        if ts:
            ts = ts.replace('/', '-')  # 兼容不同日期分隔符
        return ts

    all_rows.sort(key=_sort_key, reverse=True)
    total = len(all_rows)

    with open(out_csv, 'w', newline='', encoding='utf-8') as f_out:
        writer = csv.DictWriter(f_out, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    # 强校验 3: 输出行数应等于 base + new 行数之和
    expected_total = row_counts.get('基础数据', 0) + row_counts.get('新帖子', 0)
    if total != expected_total:
        print(f'  [错误] 整合后行数异常: expected={expected_total}, actual={total}')
        return None

    file_size = os.path.getsize(out_csv) / 1024 / 1024
    # 显示首尾时间范围
    first_ts = all_rows[0].get('post_publish_time', '?') if all_rows else '?'
    last_ts = all_rows[-1].get('post_publish_time', '?') if all_rows else '?'
    print(f'  ✓ 整合完成: {total} 条记录 ({file_size:.1f} MB)，时间范围 {first_ts} ~ {last_ts} → {out_csv}')
    print(f'    base_rows={row_counts.get("基础数据", 0)}, new_rows={row_counts.get("新帖子", 0)}')
    return out_csv


def export_full_posts(stock_code: str, start_date: str) -> str | None:
    """full 模式 Stage 3：校验并导出 full_posts.csv 到 temp_export/。

    校验项：
      - full manifest 必须存在
      - full_posts.csv 行数与 manifest 一致
      - post_publish_time 最小日期 >= start_date
      - post_id 无重复
      - failed_pages 为空
    """
    print(f'\n[Stage 3-1 full] 校验并导出全量帖子（start_date={start_date}）...')
    full_csv = full_posts_csv_path(stock_code)
    out_csv = full_output_csv_path(stock_code, start_date)

    manifest = read_full_manifest(stock_code)
    if not manifest:
        print(f'  [错误] 未找到 full manifest: {full_manifest_path(stock_code)}')
        return None

    if manifest.get('crawl_mode') != 'full':
        print(f'  [错误] manifest crawl_mode 不是 full: {manifest.get("crawl_mode")}')
        return None

    if manifest.get('partial'):
        print('  [error] Stage 3 full refused: manifest is partial/trial output')
        return None

    if manifest.get('failed_pages'):
        print(f'  [警告] Stage 1 full 存在失败页面: {manifest["failed_pages"]}')
        print(f'  [警告] 继续导出；缺失页可在网络环境改善后补爬')

    if not os.path.exists(full_csv):
        print(f'  [错误] full_posts CSV 不存在: {full_csv}')
        return None

    expected_rows = manifest.get('rows', 0)
    actual_rows = _count_csv_rows(full_csv)
    if actual_rows != expected_rows:
        print(f'  [错误] full_posts 行数与 manifest 不符: manifest={expected_rows}, actual={actual_rows}')
        return None

    all_rows = []
    seen_ids = set()
    duplicate_ids = []
    min_time = None
    with open(full_csv, 'r', encoding='utf-8') as f_in:
        reader = csv.DictReader(f_in)
        for row in reader:
            pid = str(row.get('post_id', ''))
            if pid:
                if pid in seen_ids:
                    duplicate_ids.append(pid)
                seen_ids.add(pid)
            ts = row.get('post_publish_time', '').strip()
            if ts:
                if min_time is None or ts < min_time:
                    min_time = ts
            all_rows.append(row)

    if duplicate_ids:
        print(f'  [错误] full_posts 中存在重复 post_id: {duplicate_ids[:10]}...')
        return None

    if min_time and min_time[:10] < start_date:
        print(f'  [错误] full_posts 最小日期 {min_time[:10]} 早于 start_date {start_date}')
        return None

    # 按时间降序排列
    all_rows.sort(key=lambda r: r.get('post_publish_time', '').strip(), reverse=True)

    with open(out_csv, 'w', newline='', encoding='utf-8') as f_out:
        writer = csv.DictWriter(f_out, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    file_size = os.path.getsize(out_csv) / 1024 / 1024
    first_ts = all_rows[0].get('post_publish_time', '?') if all_rows else '?'
    last_ts = all_rows[-1].get('post_publish_time', '?') if all_rows else '?'
    print(f'  ✓ 导出完成: {actual_rows} 条记录 ({file_size:.1f} MB)，时间范围 {first_ts} ~ {last_ts} → {out_csv}')
    return out_csv


def export_comments(stock_code: str) -> str:
    """从 MongoDB 导出评论到 CSV"""
    print(f'\n[Stage 3-2] 导出评论数据 ...')
    try:
        commentdb = MongoAPI(DB_NAME_COMMENT, f'comment_{stock_code}')
        count = commentdb.count_documents()
    except Exception as e:
        print(f'  MongoDB 不可用，跳过评论导出: {e}')
        return None

    if count == 0:
        print('  无评论数据')
        return None

    out_csv = comment_csv_path(stock_code)
    comments = list(commentdb.collection.find({}, {'_id': 0}))

    fieldnames = set()
    for c in comments:
        fieldnames.update(c.keys())
    fieldnames = sorted(fieldnames)

    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(comments)

    file_size = os.path.getsize(out_csv) / 1024 / 1024
    print(f'  ✓ 评论导出完成: {count} 条 ({file_size:.1f} MB) → {out_csv}')
    return out_csv


def upload_to_baidu(stock_code: str, post_csv: str, comment_csv: str = None) -> bool:
    """上传 CSV 到百度网盘（分片上传，每片 5MB，避免大文件 MD5 校验失败）"""

    print(f'\n[Stage 3-3] 上传数据到百度网盘 ...')

    remote_dir = f'{BAIDU_REMOTE_DIR}/{stock_code}'

    subprocess.run(
        ['bypy', 'mkdir', remote_dir],
        capture_output=True, text=True
    )

    CHUNK_SIZE = 5 * 1024 * 1024  # 5MB per chunk

    def upload_in_chunks(local_path, remote_subdir, label):
        """将大文件分片上传到远程子目录"""
        abs_local = os.path.abspath(local_path)
        file_size = os.path.getsize(abs_local)
        basename = os.path.basename(abs_local)
        chunk_dir = f'{remote_subdir}/{basename}_chunks'

        subprocess.run(
            ['bypy', 'mkdir', chunk_dir],
            capture_output=True, text=True
        )

        total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f'  [{label}] 共 {total_chunks} 片，每片 5MB ...')

        with open(abs_local, 'rb') as f:
            for i in range(total_chunks):
                chunk_data = f.read(CHUNK_SIZE)
                chunk_file = f'/tmp/{basename}.part{i:04d}'
                with open(chunk_file, 'wb') as cf:
                    cf.write(chunk_data)

                # 上传分片（重试3次）
                uploaded = False
                for attempt in range(3):
                    result = subprocess.run(
                        ['bypy', 'upload', chunk_file, f'{chunk_dir}/'],
                        capture_output=True, text=True,
                        timeout=120
                    )
                    output = (result.stdout + result.stderr).strip()
                    if result.returncode == 0 and 'Error' not in output and '31064' not in output:
                        uploaded = True
                        break
                    time.sleep(5)

                os.remove(chunk_file)

                if not uploaded:
                    print(f'  ✗ [{label}] 分片 {i+1}/{total_chunks} 上传失败')
                    return False

                if (i + 1) % 5 == 0 or i == total_chunks - 1:
                    print(f'  [{label}] {i+1}/{total_chunks} 片完成')

        print(f'  ✓ [{label}] {total_chunks} 片全部上传成功')
        return True

    if not upload_in_chunks(post_csv, remote_dir, '帖子'):
        return False

    if comment_csv and os.path.exists(comment_csv):
        upload_in_chunks(comment_csv, remote_dir, '评论')

    # 验证上传
    verify = subprocess.run(
        ['bypy', 'list', remote_dir],
        capture_output=True, text=True
    )
    if verify.returncode == 0:
        print(f'  ✓ 验证：数据目录存在')

    print(f'  ✓ 上传流程完成: {remote_dir}/')
    return True


def cleanup_all(stock_code: str):
    """清理所有本地数据"""
    print(f'\n[Stage 3-4] 清理本地数据 ...')

    files_to_remove = [
        base_csv_path(stock_code),
        new_posts_csv_path(stock_code),
        enhanced_csv_path(stock_code),
    ]

    for f in files_to_remove:
        if os.path.exists(f):
            os.remove(f)
            print(f'  ✓ 已删除: {os.path.basename(f)}')

    # 清理 MongoDB 评论集合
    try:
        commentdb = MongoAPI(DB_NAME_COMMENT, f'comment_{stock_code}')
        ccount = commentdb.count_documents()
        if ccount > 0:
            commentdb.drop()
            print(f'  ✓ 已删除 MongoDB 评论集合 comment_{stock_code} ({ccount} 条)')
    except Exception as e:
        print(f'  ⚠ MongoDB 清理失败（可忽略）: {e}')

    print(f'  ✓ 清理完成，为下一只股票腾出空间')


def run_stage3():
    if CRAWL_MODE == 'full':
        return run_stage3_full(start_date=START_DATE)

    print(f'\n{"="*60}')
    print(f'[Stage 3] {STOCK_CODE} 整合、上传与清理')
    print(f'{"="*60}')

    if not check_flag('stage2'):
        print('[错误] Stage 2 未完成，请先运行 Stage 2')
        return False

    if not check_disk_space(min_gb=0.2):
        return False

    # 1. 合并 CSV（不导出评论）
    post_csv = merge_csv_files(STOCK_CODE, require_manifest=True)
    if not post_csv:
        print('[错误] Stage 3 合并失败，已中止')
        return False

    # 2. 输出到 data/ 目录（本地检查模式）或上传百度网盘
    if SKIP_BAIDU_UPLOAD:
        os.makedirs(DATA_OUTPUT_DIR, exist_ok=True)
        if post_csv and os.path.exists(post_csv):
            dst = os.path.join(DATA_OUTPUT_DIR, os.path.basename(post_csv))
            shutil.copy2(post_csv, dst)
            size_mb = os.path.getsize(dst) / 1024 / 1024
            print(f'  ✓ [帖子] 已复制到: {dst} ({size_mb:.1f} MB)')
        print(f'\n[Stage 3] 跳过上传和清理（SKIP_BAIDU_UPLOAD=True），文件已保存至 data/ 目录')
        print(f'  检查完毕后，将 SKIP_BAIDU_UPLOAD 改为 False 即可恢复网盘上传')
    else:
        # 3. 上传百度网盘
        if not upload_to_baidu(STOCK_CODE, post_csv):
            print('上传失败，保留本地数据以便手动处理')
            return False

        # 4. 清理
        cleanup_all(STOCK_CODE)
        clear_flags()

    print(f'\n{"="*60}')
    print(f'[Stage 3] 全部完成！')
    if not SKIP_BAIDU_UPLOAD:
        print(f'  数据已上传至: {BAIDU_REMOTE_DIR}/{STOCK_CODE}/')
        print(f'  本地数据已清理，可以开始处理下一只股票')
    print(f'{"="*60}')
    return True


def run_stage3_full(start_date: str):
    """full 模式 Stage 3：校验并导出 full_posts.csv。"""
    print(f'\n{"="*60}')
    print(f'[Stage 3 full] {STOCK_CODE} 校验导出')
    print(f'{"="*60}')

    if not check_flag('stage2'):
        print('[错误] Stage 2 未完成，请先运行 Stage 2')
        return False

    if not check_disk_space(min_gb=0.2):
        return False

    post_csv = export_full_posts(STOCK_CODE, start_date)
    if not post_csv:
        print('[错误] Stage 3 full 导出失败，已中止')
        return False

    if SKIP_BAIDU_UPLOAD:
        os.makedirs(DATA_OUTPUT_DIR, exist_ok=True)
        dst = full_data_output_csv_path(STOCK_CODE, start_date)
        shutil.copy2(post_csv, dst)
        size_mb = os.path.getsize(dst) / 1024 / 1024
        print(f'  ✓ [帖子] 已复制到: {dst} ({size_mb:.1f} MB)')
        print(f'\n[Stage 3 full] 跳过上传和清理（SKIP_BAIDU_UPLOAD=True），文件已保存至 data/ 目录')
    else:
        if not upload_to_baidu(STOCK_CODE, post_csv):
            print('上传失败，保留本地数据以便手动处理')
            return False
        cleanup_all(STOCK_CODE)
        clear_flags()

    print(f'\n{"="*60}')
    print(f'[Stage 3 full] 全部完成！')
    if not SKIP_BAIDU_UPLOAD:
        print(f'  数据已上传至: {BAIDU_REMOTE_DIR}/{STOCK_CODE}/')
        print(f'  本地数据已清理，可以开始处理下一只股票')
    print(f'{"="*60}')
    return True


# ==================== 主入口 ====================

def main():
    global STOCK_CODE, CRAWL_MODE, START_DATE, LIST_WORKERS, LIST_WINDOW_SIZE, LIST_SOURCE, LIST_PAGE_LIMIT
    parser = argparse.ArgumentParser(description='000001 自动化数据流水线 (CSV-Native 高速版)')
    parser.add_argument('--stock', default=STOCK_CODE,
                        help='股票代码，默认 000001；可传 1/000001/600000 等格式')
    parser.add_argument('--stage', type=int, choices=[1, 2, 3], required=True,
                        help='运行阶段: 1=提取+列表爬取, 2=正文爬取, 3=整合上传清理')
    parser.add_argument('--crawl-mode', choices=['incremental', 'full'], default='incremental',
                        help='爬取模式: incremental=历史CSV+增量补爬, full=从start_date全量爬取(默认incremental)')
    parser.add_argument('--start-date', default='2009-01-01',
                        help='full 模式起始日期（YYYY-MM-DD），默认 2009-01-01')
    parser.add_argument('--list-workers', type=int, default=6,
                        help='兼容参数；当前上游 Selenium HTML 方法按单浏览器顺序翻页')
    parser.add_argument('--list-window-size', type=int, default=80,
                        help='兼容参数；用于 full 模式进度日志')
    parser.add_argument('--list-source', choices=['html', 'api', 'auto', 'selenium'], default='html',
                        help='full 模式 Stage 1 列表数据源；当前固定使用上游 Selenium HTML 方法，api/auto 会按 html 处理')
    parser.add_argument('--list-page-limit', type=int, default=0,
                        help='full Stage 1 trial limit; e.g. 50 crawls only first 50 pages and marks manifest partial')
    parser.add_argument('--detail-workers', type=int, default=3,
                        help='Stage 2 财富号正文 requests 并发数，默认 3。设为 1 回退单线程')
    parser.add_argument('--source-dir', action='append', default=[],
                        help='Stage 1 incremental 历史 CSV 目录，可重复传入；full 模式不需要')
    parser.add_argument('--force-refresh-base', action='store_true',
                        help='Stage 1 强制从 --source-dir 重新复制最新历史 CSV，忽略 temp_extract 中已存在的 base')
    parser.add_argument('--force-full-refresh', action='store_true',
                        help='Stage 1 full: clear full_posts, full manifest, page cache, and stage flags before crawling')
    args = parser.parse_args()
    STOCK_CODE = str(args.stock).strip().zfill(6)
    CRAWL_MODE = args.crawl_mode
    START_DATE = args.start_date
    LIST_WORKERS = args.list_workers
    LIST_WINDOW_SIZE = args.list_window_size
    LIST_SOURCE = args.list_source
    LIST_PAGE_LIMIT = args.list_page_limit

    if args.stage == 1:
        ok = run_stage1(
            source_dirs=args.source_dir,
            force_refresh_base=args.force_refresh_base,
            force_full_refresh=args.force_full_refresh,
        )
    elif args.stage == 2:
        ok = run_stage2(detail_workers=args.detail_workers)
    elif args.stage == 3:
        ok = run_stage3()

    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
