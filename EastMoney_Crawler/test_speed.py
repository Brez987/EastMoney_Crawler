#!/usr/bin/env python3
"""速度对比测试：爬取 20 条财富号帖子正文，测试优化效果"""
import os
import csv
import time
import sys

from crawler import PostCrawler

STOCK_CODE = '000001'
BASE_CSV = f'/home/ubuntu/guba_crawler/EastMoney_Crawler/temp_extract/{STOCK_CODE}_base.csv'
TEST_LIMIT = 20


def load_test_posts():
    """加载 20 条财富号帖子用于测试"""
    posts = []
    seen = set()
    for csv_path in [BASE_CSV]:
        if not os.path.exists(csv_path):
            continue
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = row.get('url', '')
                pid = str(row.get('post_id', ''))
                if not pid or pid in seen:
                    continue
                if 'caifuhao' not in url:
                    continue
                seen.add(pid)
                posts.append({
                    '_id': pid,
                    'post_url': url,
                })
                if len(posts) >= TEST_LIMIT:
                    break
        if len(posts) >= TEST_LIMIT:
            break
    return posts


def test_single_thread(posts):
    """方向1+2+4+5+6：单线程优化版"""
    print(f'\n{"="*60}')
    print(f'[测试1] 单线程优化版（方向1+2+4+5+6）')
    print(f'  方向1: 延迟 0.5s→0.2s')
    print(f'  方向2: 重启间隔 5→15条')
    print(f'  方向4: 页面加载超时 5秒')
    print(f'  方向5: 日志每10条打印')
    print(f'  方向6: requests 兜底财富号')
    print(f'{"="*60}')

    updates = {}
    def callback(post_id, data):
        updates[str(post_id)] = data.get('post_content', '')

    crawler = PostCrawler(STOCK_CODE)
    t0 = time.time()
    crawler.crawl_post_detail(
        limit=TEST_LIMIT,
        url_type='caifuhao',
        posts=posts,
        update_callback=callback,
        parallel=False
    )
    elapsed = time.time() - t0

    success = sum(1 for v in updates.values() if v)
    return elapsed, success, len(posts)


def test_parallel(posts):
    """方向3：多线程并行版（2 workers）"""
    print(f'\n{"="*60}')
    print(f'[测试2] 多线程并行版（方向3: 2 workers）')
    print(f'  方向1+2+4+5+6 全部启用')
    print(f'  方向3: ThreadPoolExecutor max_workers=2')
    print(f'{"="*60}')

    updates = {}
    def callback(post_id, data):
        updates[str(post_id)] = data.get('post_content', '')

    crawler = PostCrawler(STOCK_CODE)
    t0 = time.time()
    crawler.crawl_post_detail(
        limit=TEST_LIMIT,
        url_type='caifuhao',
        posts=posts,
        update_callback=callback,
        parallel=True,
        max_workers=2
    )
    elapsed = time.time() - t0

    success = sum(1 for v in updates.values() if v)
    return elapsed, success, len(posts)


def main():
    posts = load_test_posts()
    if not posts:
        print(f'[错误] 未找到财富号帖子！请先完成 Stage 1。')
        sys.exit(1)

    print(f'\n加载测试帖子: {len(posts)} 条财富号帖子')
    print(f'首帖URL示例: {posts[0]["post_url"][:80]}...')

    results = {}

    # 测试1：单线程优化版
    t1, s1, total1 = test_single_thread(posts)
    results['单线程优化版'] = {
        'time': t1,
        'success': s1,
        'total': total1,
        'speed': total1 / t1 if t1 > 0 else 0
    }
    print(f'\n[结果1] 单线程优化版: {t1:.1f}秒, 成功 {s1}/{total1}, 速度 {total1/t1:.2f} 条/秒')

    # 测试2：并行版
    t2, s2, total2 = test_parallel(posts)
    results['多线程并行版'] = {
        'time': t2,
        'success': s2,
        'total': total2,
        'speed': total2 / t2 if t2 > 0 else 0
    }
    print(f'\n[结果2] 多线程并行版: {t2:.1f}秒, 成功 {s2}/{total2}, 速度 {total2/t2:.2f} 条/秒')

    # 汇总
    print(f'\n{"="*60}')
    print(f'[速度对比汇总]')
    print(f'{"="*60}')
    print(f'{"方案":<20} {"耗时":>8} {"成功率":>8} {"速度":>10}')
    print(f'{"-"*50}')
    for name, r in results.items():
        print(f'{name:<20} {r["time"]:>7.1f}s {r["success"]}/{r["total"]:>3}   {r["speed"]:>7.2f}条/秒')

    # 与旧版对比（假设旧版 0.07 条/秒 = 600条/40分钟 = 0.25条/秒）
    old_speed = 600 / (40 * 60)  # 0.25 条/秒
    print(f'\n旧版速度: {old_speed:.2f} 条/秒 (600条/40分钟)')
    for name, r in results.items():
        improvement = r['speed'] / old_speed
        print(f'{name} 提速: {improvement:.1f}x')


if __name__ == '__main__':
    main()
