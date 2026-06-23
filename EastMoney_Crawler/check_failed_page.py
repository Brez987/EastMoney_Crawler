#!/usr/bin/env python3
"""诊断全量模式下某一只股票的特定失败页面。"""
import argparse
import os
import sys
import time
import random

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from crawler import PostCrawler


def diagnose(stock: str, page_num: int, attempts: int = 3):
    crawler = PostCrawler(stock)
    print(f"\n[{stock}] 开始诊断第 {page_num} 页，最多重试 {attempts} 次...")

    # 1. 先看 get_page_num 是否正常
    try:
        max_page = crawler.get_page_num()
        print(f"[{stock}] 列表总页数: {max_page}")
    except Exception as e:
        print(f"[{stock}] get_page_num 失败: {e}")

    # 2. 多次重试该页，分别打印 requests 和 Selenium 结果
    for attempt in range(1, attempts + 1):
        print(f"\n  第 {attempt} 次尝试 (requests)...")
        try:
            rows = crawler._fetch_post_page_fast(page_num)
            print(f"  ✓ requests 成功: {len(rows)} 条")
            if rows:
                print(f"    首条: {rows[0].get('post_date')} {rows[0].get('post_title', '')[:40]}")
                print(f"    末条: {rows[-1].get('post_date')} {rows[-1].get('post_title', '')[:40]}")
            return
        except Exception as e:
            print(f"  ✗ requests 失败: {type(e).__name__}: {e}")
            time.sleep(2 ** attempt + random.random())

    print(f"\n  requests 连续失败，尝试 Selenium 兜底...")
    try:
        from parser import PostParser
        parser = PostParser()
        rows = crawler.fetch_post_page(page_num, parser, stockbar_name=f'{stock}吧')
        print(f"  ✓ Selenium 成功: {len(rows)} 条")
        if rows:
            print(f"    首条: {rows[0].get('post_date')} {rows[0].get('post_title', '')[:40]}")
    except Exception as e:
        print(f"  ✗ Selenium 也失败: {type(e).__name__}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Diagnose a failed list page in full-mode crawl")
    parser.add_argument("--stock", default="000001")
    parser.add_argument("--page", type=int, required=True)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    diagnose(args.stock, args.page, args.attempts)


if __name__ == "__main__":
    main()
