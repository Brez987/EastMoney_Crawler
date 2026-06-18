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
from datetime import datetime

import pandas as pd

from mongodb import MongoAPI
from crawler import PostCrawler, CommentCrawler

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
    'forward', 'coment_count', 'click_count',
    'post_title', 'url', 'content'
]

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


def enhanced_csv_path(stock_code: str) -> str:
    """Stage 3 整合后的最终 CSV"""
    return os.path.join(EXPORT_DIR, f'{stock_code}_enhanced.csv')


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
            shutil.move(inner_extracted, extracted_path)
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
    shutil.move(extracted_path, base_path)
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


def run_stage1(source_dirs: list = None):
    print(f'\n{"="*60}')
    print(f'[Stage 1] {STOCK_CODE} 提取 CSV + 爬取帖子列表')
    print(f'{"="*60}')

    if not check_disk_space(min_gb=0.5):
        return False

    ensure_dirs()

    # 1. 获取基础 CSV（优先从 source-dir / 本地历史目录复制，回退到 RAR 提取）
    base_path = base_csv_path(STOCK_CODE)
    if not os.path.exists(base_path):
        source_csv = resolve_source_csv(STOCK_CODE, source_dirs)
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
        else:
            searched = ', '.join(default_source_dirs() + (source_dirs or []))
            print(f'[错误] 未找到 {STOCK_CODE}.csv，也没有可用数据.rar，无法继续')
            print(f'  已查找目录: {searched}')
            return False
    else:
        print(f'\n[Stage 1-1] 基础 CSV 已存在，跳过: {base_path}')

    csv_path = base_path

    # 2. 单次扫描 CSV：读取 post_id 去重集合 + 获取最新日期
    print(f'\n[Stage 1-2] 扫描 CSV 状态（post_id 去重集合 + 时间范围）...')
    existing_ids, stop_date, earliest_dt, count = scan_csv_state(csv_path)
    print(f'  ✓ 已加载 {count} 条记录，post_id 去重集合大小: {len(existing_ids)}')
    print(f'  ✓ CSV 时间范围: {earliest_dt} ~ {stop_date}')
    print(f'  → 将补爬 {stop_date} 之后的新帖子')

    # 3. 爬取帖子列表（从第1页开始，遇到旧帖自动停止，新帖子直接追加 CSV）
    crawl_post_list(STOCK_CODE, existing_ids, stop_date)

    set_flag('stage1')
    print(f'\n{"="*60}')
    print(f'[Stage 1] 完成！请在新 tmux 会话中运行 Stage 2')
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

    tmp_path = csv_path + '.tmp'
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

    shutil.move(tmp_path, csv_path)
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
            if pid:
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
    — 普通股吧帖：content 为空时直接用 post_title 填充（本地完成，不爬详情页）
    — 财富号帖子：content 为空时进入爬取队列（多线程 requests）
    — 不爬评论，不要求 MongoDB

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
    post_crawler.crawl_post_detail(
        posts=posts,
        update_callback=update_callback,
        max_workers=detail_workers
    )

    # 最终写回 CSV
    _flush_updates_to_csv(csv_paths, content_updates)
    if batch_done:
        _save_checkpoint(stock_code, batch_done)
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
    crawl_post_detail_csv(STOCK_CODE, csv_paths, detail_workers=detail_workers)

    set_flag('stage2')
    print(f'\n{"="*60}')
    print(f'[Stage 2] 完成！请在新 PowerShell 窗口运行 Stage 3')
    print(f'{"="*60}')
    return True


# ==================== Stage 3 ====================

def merge_csv_files(stock_code: str) -> str:
    """合并基础 CSV 和新帖子 CSV，按 post_publish_time 降序排列（新帖在前）"""
    print(f'\n[Stage 3-1] 合并基础数据与新爬取帖子（按时间降序）...')
    base_csv = base_csv_path(stock_code)
    new_csv = new_posts_csv_path(stock_code)
    out_csv = enhanced_csv_path(stock_code)

    all_rows = []

    for src_path, label in [(base_csv, '基础数据'), (new_csv, '新帖子')]:
        if not os.path.exists(src_path):
            continue
        with open(src_path, 'r', encoding='utf-8') as f_in:
            reader = csv.DictReader(f_in)
            for row in reader:
                all_rows.append(row)
        print(f'  已读取 {label}: {src_path}')

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

    file_size = os.path.getsize(out_csv) / 1024 / 1024
    # 显示首尾时间范围
    first_ts = all_rows[0].get('post_publish_time', '?') if all_rows else '?'
    last_ts = all_rows[-1].get('post_publish_time', '?') if all_rows else '?'
    print(f'  ✓ 整合完成: {total} 条记录 ({file_size:.1f} MB)，时间范围 {first_ts} ~ {last_ts} → {out_csv}')
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
    print(f'\n{"="*60}')
    print(f'[Stage 3] {STOCK_CODE} 整合、上传与清理')
    print(f'{"="*60}')

    if not check_flag('stage2'):
        print('[错误] Stage 2 未完成，请先运行 Stage 2')
        return False

    if not check_disk_space(min_gb=0.2):
        return False

    # 1. 合并 CSV（不导出评论）
    post_csv = merge_csv_files(STOCK_CODE)

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


# ==================== 主入口 ====================

def main():
    global STOCK_CODE
    parser = argparse.ArgumentParser(description='000001 自动化数据流水线 (CSV-Native 高速版)')
    parser.add_argument('--stock', default=STOCK_CODE,
                        help='股票代码，默认 000001；可传 1/000001/600000 等格式')
    parser.add_argument('--stage', type=int, choices=[1, 2, 3], required=True,
                        help='运行阶段: 1=提取+列表爬取, 2=正文爬取, 3=整合上传清理')
    parser.add_argument('--detail-workers', type=int, default=3,
                        help='Stage 2 财富号正文 requests 并发数，默认 3。设为 1 回退单线程')
    parser.add_argument('--source-dir', action='append', default=[],
                        help='Stage 1 历史 CSV 目录，可重复传入；优先查找 {stock}.csv')
    args = parser.parse_args()
    STOCK_CODE = str(args.stock).strip().zfill(6)

    if args.stage == 1:
        ok = run_stage1(source_dirs=args.source_dir)
    elif args.stage == 2:
        ok = run_stage2(detail_workers=args.detail_workers)
    elif args.stage == 3:
        ok = run_stage3()

    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
