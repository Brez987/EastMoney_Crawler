#!/usr/bin/env python3
"""
分批处理股票数据：解压 → 导入 → 爬取正文 → 导出 → 清理

核心思路：
  RAR 压缩包 → 逐个提取 CSV → 流式导入 MongoDB → 增量爬取正文 → 导出 CSV → 删除临时文件 → 处理下一批

关键原则：
  - 不一次性解压：每次只提取 1 个 CSV 文件（约 20-100 MB）
  - 导入后立即删除：CSV 导入 MongoDB 后立即删除临时文件
  - 分批爬取：每次只处理 1 个股票，完成后立即导出并清理
  - 边处理边清理：始终保持磁盘使用率 < 80%
"""
import os
import csv
import subprocess
import shutil
from mongodb import MongoAPI
from crawler import PostCrawler

# 配置
RAR_FILE = '/home/ubuntu/guba_crawler/EastMoney_Crawler/数据.rar'
TEMP_DIR = '/home/ubuntu/guba_crawler/EastMoney_Crawler/temp_extract'
EXPORT_DIR = '/home/ubuntu/guba_crawler/EastMoney_Crawler/temp_export'
DB_NAME = 'post_info'
STOCK_LIST = '/home/ubuntu/guba_crawler/EastMoney_Crawler/数据_list.csv'


def ensure_dirs():
    """确保临时目录存在"""
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)


def extract_single_stock(stock_code: str) -> str:
    """从 RAR 中提取单个股票的 CSV 文件"""
    csv_path = os.path.join(TEMP_DIR, f'{stock_code}.csv')
    
    # 使用 unrar 提取单个文件
    rar_internal_path = f'数据/{stock_code}.csv'
    cmd = f'unrar x -y "{RAR_FILE}" "{rar_internal_path}" "{TEMP_DIR}/"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f'  ✗ 提取失败: {stock_code}')
        if result.stderr:
            print(f'    错误: {result.stderr.strip()}')
        return None
    
    if not os.path.exists(csv_path):
        print(f'  ✗ 提取后文件不存在: {stock_code}')
        return None
    
    file_size = os.path.getsize(csv_path) / 1024 / 1024
    print(f'  ✓ 提取成功: {stock_code} ({file_size:.1f} MB)')
    return csv_path


def import_csv_to_mongodb(stock_code: str, csv_path: str) -> int:
    """将 CSV 导入 MongoDB"""
    collection_name = f'post_{stock_code}'
    postdb = MongoAPI(DB_NAME, collection_name)
    
    # 清空已有数据
    postdb.drop()
    
    # 读取 CSV 并插入
    batch = []
    batch_size = 1000
    count = 0
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 根据实际 CSV 表头映射字段
            doc = {
                '_id': str(row.get('post_id', '')),
                'post_title': row.get('post_title', ''),
                'post_content': row.get('post_content', ''),
                'post_date': row.get('post_date', ''),
                'post_time': row.get('post_time', ''),
                'post_author': row.get('post_author', ''),
                'post_url': row.get('post_url', ''),
                'post_view': row.get('post_view', 0),
                'comment_num': row.get('comment_num', 0),
            }
            batch.append(doc)
            count += 1
            
            if len(batch) >= batch_size:
                postdb.insert_many(batch)
                batch = []
    
    if batch:
        postdb.insert_many(batch)
    
    print(f'  ✓ 导入 {count} 条记录到 MongoDB')
    return count


def crawl_missing_content(stock_code: str):
    """爬取缺失正文的帖子"""
    print(f'  → 开始爬取正文...')
    post_crawler = PostCrawler(stock_code)
    post_crawler.crawl_post_detail(limit=None, url_type='all')
    print(f'  ✓ 正文爬取完成')


def export_to_csv(stock_code: str) -> str:
    """从 MongoDB 导出为 CSV"""
    collection_name = f'post_{stock_code}'
    postdb = MongoAPI(DB_NAME, collection_name)
    
    export_path = os.path.join(EXPORT_DIR, f'{stock_code}_enhanced.csv')
    
    all_posts = list(postdb.collection.find({}, {'_id': 0}))
    
    if not all_posts:
        print(f'  ✗ 无数据可导出')
        return None
    
    fieldnames = list(all_posts[0].keys())
    
    with open(export_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_posts)
    
    file_size = os.path.getsize(export_path) / 1024 / 1024
    print(f'  ✓ 导出 {len(all_posts)} 条记录 ({file_size:.1f} MB)')
    return export_path


def cleanup(stock_code: str, csv_path: str):
    """清理临时文件"""
    if csv_path and os.path.exists(csv_path):
        os.remove(csv_path)
    
    # 清理 MongoDB 集合（如果不需要保留）
    collection_name = f'post_{stock_code}'
    postdb = MongoAPI(DB_NAME, collection_name)
    postdb.drop()
    
    print(f'  ✓ 临时文件已清理')


def process_single_stock(stock_code: str):
    """处理单个股票的完整流程"""
    print(f'\n{"="*50}')
    print(f'开始处理 {stock_code}')
    print(f'{"="*50}')
    
    # 1. 提取 CSV
    csv_path = extract_single_stock(stock_code)
    if not csv_path:
        return False
    
    # 2. 导入 MongoDB
    import_csv_to_mongodb(stock_code, csv_path)
    
    # 3. 爬取正文
    crawl_missing_content(stock_code)
    
    # 4. 导出增强版 CSV
    export_path = export_to_csv(stock_code)
    
    # 5. 清理
    cleanup(stock_code, csv_path)
    
    print(f'\n{"="*50}')
    print(f'{stock_code} 处理完成')
    print(f'{"="*50}')
    return True


def get_stock_list(start: int = 0, end: int = None) -> list:
    """读取股票列表"""
    with open(STOCK_LIST, 'r') as f:
        stock_codes = [line.strip() for line in f.readlines()[1:]]  # 跳过表头
    
    if end:
        return stock_codes[start:end]
    return stock_codes[start:]


def main():
    ensure_dirs()
    
    # 读取股票列表
    stock_codes = get_stock_list(start=0, end=10)  # 默认处理前 10 个
    
    print(f'共 {len(stock_codes)} 个股票待处理')
    print(f'股票代码: {stock_codes[:5]}...')
    print(f'\n提示: 修改 main() 中的 get_stock_list 参数可调整处理范围')
    
    success_count = 0
    fail_count = 0
    
    for stock_code in stock_codes:
        try:
            if process_single_stock(stock_code):
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            print(f'  ✗ {stock_code} 处理失败: {e}')
            fail_count += 1
            continue
    
    print(f'\n{"="*50}')
    print(f'处理完成: 成功 {success_count} 个, 失败 {fail_count} 个')
    print(f'导出目录: {EXPORT_DIR}')
    print(f'{"="*50}')


if __name__ == '__main__':
    main()
