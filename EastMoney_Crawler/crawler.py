from selenium.webdriver.common.by import By
import time
import random
import threading
import json
import re
import math
import hashlib
import pandas as pd
import os
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from mongodb import MongoAPI
from parser import PostParser
from parser import CommentParser
from browser_utils import create_stealth_chrome


class PostCrawler(object):
    LIST_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': 'https://guba.eastmoney.com/',
    }
    POSTS_PER_PAGE = 80

    def __init__(self, stock_symbol: str):
        self.browser = None
        self.symbol = stock_symbol
        self.start = time.time()  # calculate the time cost
        self.session = requests.Session()
        self.session.headers.update(self.LIST_HEADERS)

    def create_webdriver(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))  # hide the features of crawler/selenium
        js_file_path = os.path.join(current_dir, 'stealth.min.js')
        self.browser = create_stealth_chrome(stealth_script_path=js_file_path)

    def restart_webdriver(self):
        if self.browser is not None:
            try:
                self.browser.quit()
            except Exception:
                pass
        self.create_webdriver()

    def get_page_num(self):
        try:
            html = self._fetch_list_html(1)
            payload = self._extract_article_payload(html)
            total_count = int(payload.get('count') or 0)
            if total_count > 0:
                return max(1, math.ceil(total_count / self.POSTS_PER_PAGE))
        except Exception:
            pass

        if self.browser is None:
            self.create_webdriver()
        self.browser.get(f'http://guba.eastmoney.com/list,{self.symbol},f_1.html')
        try:
            page_element = self.browser.find_element(By.CSS_SELECTOR, 'ul.paging > li:nth-child(7) > a > span')
            return int(page_element.text)
        except Exception:
            page_elements = self.browser.find_elements(By.CSS_SELECTOR, 'ul.paging a span')
            page_numbers = []
            for element in page_elements:
                text = element.text.strip()
                if text.isdigit():
                    page_numbers.append(int(text))
            return max(page_numbers) if page_numbers else 1

    def _extract_stockbar_name(self) -> str:
        """从股吧列表页提取股票吧名称（如'平安银行吧'）"""
        try:
            # 页面标题格式: 平安银行(000001)股吧_平安银行吧_东方财富网股吧
            title_el = self.browser.find_element(By.CSS_SELECTOR, 'div.stock_name a, div.stockcode a, h1.stock_name')
            text = title_el.text.strip()
            if text:
                return text
        except Exception:
            pass
        try:
            title = self.browser.title
            # 从标题提取: "股吧_XXX吧_东方财富网股吧" 或 "XXX吧(000001)股吧"
            m = re.search(r'([^\s_]+吧)', title)
            if m:
                return m.group(1)
        except Exception:
            pass
        return f'{self.symbol}吧'

    def _fetch_list_html(self, page_num: int) -> str:
        url = f'https://guba.eastmoney.com/list,{self.symbol},f_{page_num}.html'
        resp = self.session.get(url, timeout=(3, 12))
        resp.raise_for_status()
        if 'fd_guba_validate' in resp.url or '身份核实' in resp.text[:5000]:
            raise RuntimeError('list page redirected to validation')
        resp.encoding = resp.encoding or 'utf-8'
        return resp.text

    @staticmethod
    def _extract_article_payload(html: str) -> dict:
        match = re.search(r'var\s+article_list\s*=\s*(\{.*?\});\s*var\s+other_list', html, re.S)
        if not match:
            match = re.search(r'var\s+article_list\s*=\s*(\{.*?\});', html, re.S)
        if not match:
            raise ValueError('article_list payload not found')
        return json.loads(match.group(1))

    @staticmethod
    def _extract_post_key_from_href(href: str) -> str:
        if not href:
            return ''
        match = re.search(r'/news/(\d+)', href)
        if match:
            return match.group(1)
        match = re.search(r'news,[^,]+,(\d+)\.html', href)
        if match:
            return match.group(1)
        return ''

    def _extract_visible_post_keys(self, html: str, page_num: int) -> list:
        soup = BeautifulSoup(html, 'html.parser')
        rows = soup.select('tr.listitem')
        if page_num == 1 and rows:
            rows = rows[1:]  # keep legacy behavior: skip first cross-bar/pinned row

        keys = []
        for row in rows:
            link = row.select_one('td:nth-child(3) a')
            if not link:
                continue
            key = self._extract_post_key_from_href(link.get('href', '')) or link.get('data-postid', '')
            if key:
                keys.append(str(key))
        return keys

    @staticmethod
    def _normalise_count(value) -> int:
        if value is None:
            return 0
        text = str(value).strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            if '万' in text:
                try:
                    return int(float(text.replace('万', '')) * 10000)
                except ValueError:
                    return 0
        return 0

    def _article_to_post_info(self, article: dict, default_stockbar_name: str = '') -> dict:
        post_id = str(article.get('post_id') or '')
        source_id = str(article.get('post_source_id') or '')
        post_type = str(article.get('post_type') if article.get('post_type') is not None else '')
        stockbar_code = str(article.get('stockbar_code') or self.symbol)
        stockbar_name = (
            article.get('stockbar_name')
            or default_stockbar_name
            or f'{self.symbol}吧'
        )
        publish_time = article.get('post_publish_time') or article.get('post_display_time') or ''
        post_date = publish_time[:10] if len(publish_time) >= 10 else ''
        post_time = publish_time[11:16] if len(publish_time) >= 16 else ''

        user = article.get('post_user') or {}
        user_id = article.get('user_id') or user.get('user_id') or ''
        author = article.get('user_nickname') or user.get('user_nickname') or ''

        if post_type == '20' and source_id:
            post_url = f'https://caifuhao.eastmoney.com/news/{source_id}'
        else:
            post_url = f'https://guba.eastmoney.com/news,{stockbar_code},{post_id}.html'

        return {
            '_id': post_id,
            'post_source_id': source_id,
            'post_type': post_type,
            'post_title': article.get('post_title') or '',
            'post_view': self._normalise_count(article.get('post_click_count')),
            'comment_num': self._normalise_count(article.get('post_comment_count')),
            'post_url': post_url,
            'post_date': post_date,
            'post_time': post_time,
            'post_author': author,
            'user_id': str(user_id),
            'stockbar_name': stockbar_name,
            'stockbar_code': stockbar_code,
            'forward': str(self._normalise_count(article.get('post_forward_count'))),
        }

    def _fetch_post_page_fast(self, page_num: int) -> list:
        html = self._fetch_list_html(page_num)
        payload = self._extract_article_payload(html)
        default_stockbar_name = (payload.get('bar_name') or self.symbol)
        if default_stockbar_name and not default_stockbar_name.endswith('吧'):
            default_stockbar_name = f'{default_stockbar_name}吧'

        article_map = {}
        for article in payload.get('re') or []:
            post_id = str(article.get('post_id') or '')
            source_id = str(article.get('post_source_id') or '')
            if post_id:
                article_map[post_id] = article
            if source_id:
                article_map[source_id] = article

        visible_keys = self._extract_visible_post_keys(html, page_num)
        if not visible_keys:
            visible_keys = [
                str(a.get('post_source_id') or a.get('post_id'))
                for a in (payload.get('re') or [])
                if a.get('post_id')
            ]
            if page_num == 1 and visible_keys:
                visible_keys = visible_keys[1:]

        dic_list = []
        seen_ids = set()
        for key in visible_keys:
            article = article_map.get(str(key))
            if not article:
                continue
            dic = self._article_to_post_info(article, default_stockbar_name=default_stockbar_name)
            if not dic.get('_id') or dic['_id'] in seen_ids:
                continue
            if 'guba.eastmoney.com/news' in dic['post_url'] or 'caifuhao.eastmoney.com/news' in dic['post_url']:
                seen_ids.add(dic['_id'])
                dic_list.append(dic)
        return dic_list

    def fetch_post_page(self, page_num: int, parser: PostParser, stockbar_name: str = ''):
        try:
            return self._fetch_post_page_fast(page_num)
        except Exception as fast_error:
            print(f'{self.symbol}: 第 {page_num} 页快速解析失败，回退 Selenium: {fast_error}')

        if self.browser is None:
            self.create_webdriver()
        url = f'http://guba.eastmoney.com/list,{self.symbol},f_{page_num}.html'
        self.browser.get(url)
        dic_list = []
        list_item = self.browser.find_elements(By.CSS_SELECTOR, '.listitem')  # includes all posts on one page
        if page_num == 1:
            list_item = list_item[1:]  # 剔除首页的置顶帖（开户广告hhh）
        for li in list_item:  # get each post respectively
            dic = parser.parse_post_info(li, stockbar_name=stockbar_name)
            # 保留股吧原生帖子和财富号转发布帖子
            if 'guba.eastmoney.com/news' in dic['post_url'] or 'caifuhao.eastmoney.com/news' in dic['post_url']:
                dic_list.append(dic)
        return dic_list

    def crawl_post_info(self, page1: int, page2: int, storage_callback=None, stop_date: str = None):
        """爬取帖子列表

        Args:
            page1: 起始页码
            page2: 结束页码
            storage_callback: 外部存储回调函数，接收 dic_list 参数。
                              如果为 None，则默认存入 MongoDB。
            stop_date: 停止爬取的日期阈值（YYYY-MM-DD 格式）。
                       当一页中所有帖子的 post_date <= stop_date 时停止爬取，
                       用于增量补爬最新帖子。None 表示不启用日期停止逻辑。
        """
        max_page = self.get_page_num()  # confirm the maximum page number to crawl
        current_page = page1  # start page
        # 如果启用了 stop_date，不设上限，直到触发日期停止逻辑或爬完所有页
        stop_page = max_page if stop_date else min(page2, max_page)

        parser = PostParser()  # must be created out of the 'while', as it contains the function about date
        postdb = None if storage_callback else MongoAPI('post_info', f'post_{self.symbol}')
        stockbar_name = self._extract_stockbar_name() if self.browser is not None else f'{self.symbol}吧'
        total_inserted = 0
        stopped_by_date = False

        while current_page <= stop_page:  # use 'while' instead of 'for' is crucial for exception handling
            time.sleep(abs(random.normalvariate(0.01, 0.005)))  # random sleep time
            url = f'http://guba.eastmoney.com/list,{self.symbol},f_{current_page}.html'

            try:
                dic_list = self.fetch_post_page(current_page, parser, stockbar_name=stockbar_name)

                # 如果启用了日期停止逻辑，检查该页所有帖子的日期
                if stop_date and dic_list:
                    all_old = all(
                        dic.get('post_date', '') <= stop_date
                        for dic in dic_list
                        if dic.get('post_date')
                    )
                    if all_old:
                        print(f'{self.symbol}: 第 {current_page} 页所有帖子日期 <= {stop_date}，停止爬取')
                        stopped_by_date = True
                        break

                if storage_callback:
                    storage_callback(dic_list)
                    total_inserted += len(dic_list)
                else:
                    postdb.insert_many(dic_list)
                print(f'{self.symbol}: 已经成功爬取第 {current_page} 页帖子基本信息，'
                      f'进度 {(current_page - page1 + 1)*100/(stop_page - page1 + 1):.2f}%')
                current_page += 1

            except Exception as e:
                print(f'{self.symbol}: 第 {current_page} 页出现了错误 {e}')
                time.sleep(0.01)
                if self.browser is not None:
                    try:
                        self.browser.refresh()
                        self.browser.delete_all_cookies()
                    except Exception:
                        pass
                    self.restart_webdriver()  # restart it again!

        end = time.time()
        time_cost = end - self.start  # calculate the time cost
        parser.close()
        if self.browser is not None:
            self.browser.quit()

        actual_pages = current_page - page1
        if stopped_by_date:
            print(f'成功爬取 {self.symbol}股吧共 {actual_pages} 页帖子（因日期阈值 {stop_date} 提前停止）')
        else:
            print(f'成功爬取 {self.symbol}股吧共 {actual_pages} 页帖子')

        if storage_callback:
            row_count = total_inserted
            print(f'总计新增 {row_count} 条，花费 {time_cost/60:.2f} 分钟')
        else:
            start_date = postdb.find_last()['post_date']
            end_date = postdb.find_first()['post_date']
            row_count = postdb.count_documents()
            print(f'总计 {row_count} 条，花费 {time_cost/60:.2f} 分钟')
            print(f'帖子的时间范围从 {start_date} 到 {end_date}')

    def crawl_post_detail(self, limit: int = None, url_type: str = 'all',
                          posts: list = None, update_callback=None,
                          max_workers: int = 3):
        """爬取帖子详情页的完整正文内容（混合策略版）

        策略：
        - 财富号帖子：多线程 requests（无浏览器，不触发验证页）
        - 非财富号帖子：多 worker 并发（每 worker 独立浏览器 + JS fetch）

        Args:
            limit: 限制爬取的帖子数量，None 表示爬取所有帖子
            url_type: 筛选帖子类型
            posts: 外部传入的帖子列表
            update_callback: 外部更新回调（线程安全）
            max_workers: 并发 worker 数，默认 3。值为 1 时回退单线程模式
        """
        worker_count = max(1, max_workers)
        use_external = posts is not None
        if use_external:
            all_posts = posts
        else:
            postdb = MongoAPI('post_info', f'post_{self.symbol}')
            all_posts = list(postdb.collection.find({}, {'_id': 1, 'post_url': 1, 'post_content': 1}))

        if url_type == 'caifuhao':
            filtered_posts = [p for p in all_posts if 'caifuhao' in p.get('post_url', '')]
        elif url_type == 'guba':
            filtered_posts = [p for p in all_posts if 'guba.eastmoney.com/news' in p.get('post_url', '')]
        else:
            filtered_posts = all_posts

        posts_to_crawl = [p for p in filtered_posts if not p.get('post_content')]

        if limit and len(posts_to_crawl) > limit:
            posts_to_crawl = posts_to_crawl[:limit]

        total = len(posts_to_crawl)
        if total == 0:
            print(f'{self.symbol}: 未找到需要爬取正文的帖子')
            return

        # 分离财富号和非财富号帖子
        caifuhao_posts = [p for p in posts_to_crawl if 'caifuhao' in p.get('post_url', '')]
        guba_posts = [p for p in posts_to_crawl if 'caifuhao' not in p.get('post_url', '')]

        print(f'{self.symbol}: 共 {total} 条帖子待爬取')
        print(f'  - 财富号: {len(caifuhao_posts)} 条（多线程 requests）')
        if worker_count > 1:
            print(f'  - 股吧原生: {len(guba_posts)} 条（{worker_count} worker 并发）')
        else:
            print(f'  - 股吧原生: {len(guba_posts)} 条（单线程 Selenium）')

        # 先爬财富号帖子（多线程 requests）
        if caifuhao_posts:
            self._crawl_caifuhao_posts(caifuhao_posts, update_callback, max_workers=min(worker_count * 2, 8))

        # 爬非财富号帖子
        if guba_posts:
            if worker_count > 1:
                self._crawl_guba_posts_parallel(guba_posts, update_callback, num_workers=worker_count)
            else:
                self._crawl_guba_posts(guba_posts, update_callback)

        print(f'{self.symbol}: 正文爬取完成，共处理 {total} 条帖子')

    def _crawl_caifuhao_posts(self, posts: list, update_callback, max_workers: int = 2):
        """多线程爬取财富号帖子（仅使用 requests，无浏览器兜底）

        财富号历史文章可能已删除/失效。此处不能调用 parse_post_detail()，
        因为它在 requests 为空时会回退 Selenium，失效文章多时会导致 Stage 2 长时间卡住。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(posts)
        print(f'{self.symbol}: 开始多线程爬取 {total} 条财富号帖子...')

        parser = PostParser()
        success_count = 0
        error_count = 0
        lock = threading.Lock()

        def crawl_one(post):
            nonlocal success_count, error_count
            post_id = post['_id']
            post_url = post['post_url']

            try:
                detail = parser._try_requests_caifuhao(post_url)

                if detail['post_content']:
                    update_data = {'post_content': detail['post_content']}
                    if detail.get('post_title'):
                        update_data['post_title'] = detail['post_title']
                    if detail.get('post_date'):
                        update_data['post_date'] = detail['post_date']
                    if detail.get('post_time'):
                        update_data['post_time'] = detail['post_time']
                    if detail.get('post_author'):
                        update_data['post_author'] = detail['post_author']

                    with lock:
                        if update_callback:
                            update_callback(post_id, update_data)
                        success_count += 1
                        if success_count % 20 == 0:
                            print(f'{self.symbol}: 财富号已爬取 {success_count}/{total} 条')
                    return True
                else:
                    # 正文为空/文章失效 → 持久记录为失败，后续重跑直接跳过。
                    with lock:
                        if update_callback:
                            update_callback(
                                post_id,
                                {
                                    'post_content': '',
                                    '_detail_failed': True,
                                    'reason': 'caifuhao_empty_or_invalid',
                                    'post_url': post_url,
                                }
                            )
                        error_count += 1
                    return False

            except Exception as e:
                with lock:
                    if update_callback:
                        update_callback(
                            post_id,
                            {
                                'post_content': '',
                                '_detail_failed': True,
                                'reason': f'caifuhao_exception:{type(e).__name__}',
                                'post_url': post_url,
                            }
                        )
                    error_count += 1
                return False

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(crawl_one, post) for post in posts]
            for future in as_completed(futures):
                future.result()  # 等待所有任务完成
                processed = success_count + error_count
                if processed % 20 == 0 or processed == total:
                    print(f'{self.symbol}: 财富号进度 {processed}/{total}，成功 {success_count}，失效/失败 {error_count}')

        print(f'{self.symbol}: 财富号爬取完成，成功 {success_count}，失败 {error_count}')

    def _crawl_guba_posts(self, posts: list, update_callback):
        """单线程爬取股吧原生帖子（JS fetch API + 自适应延迟）

        先通过 JS fetch 获取帖子正文（快 100x），失败时回退 Selenium。
        正文为空视为正常结果（帖子可能已被删除），不增加延迟。
        """
        total = len(posts)
        print(f'{self.symbol}: 开始单线程爬取 {total} 条股吧原生帖子...')

        parser = PostParser()
        # 初始化浏览器并建立 cookies
        driver = parser.get_detail_browser()
        driver.get(f'https://guba.eastmoney.com/list,{self.symbol},f_1.html')
        time.sleep(1)

        consecutive_errors = 0
        adaptive_delay = 0.0  # JS fetch 无需延迟
        RESTART_INTERVAL = 20  # 重启间隔放宽到 20 条
        empty_count = 0

        for i, post in enumerate(posts):
            post_id = post['_id']
            post_url = post['post_url']

            try:
                time.sleep(adaptive_delay)

                detail = parser.parse_post_detail(post_url)

                if detail['post_content']:
                    update_data = {'post_content': detail['post_content']}
                    if detail.get('post_title'):
                        update_data['post_title'] = detail['post_title']
                    if detail.get('post_date'):
                        update_data['post_date'] = detail['post_date']
                    if detail.get('post_time'):
                        update_data['post_time'] = detail['post_time']
                    if detail.get('post_author'):
                        update_data['post_author'] = detail['post_author']

                    if update_callback:
                        update_callback(post_id, update_data)

                    if (i + 1) % 10 == 0 or (i + 1) == total:
                        pct = (i+1)*100/total
                        print(f'{self.symbol}: 股吧 {i+1}/{total} ({pct:.1f}%) 成功, 空{empty_count}, 延迟{adaptive_delay:.1f}s')

                    consecutive_errors = 0
                    adaptive_delay = max(0.2, adaptive_delay * 0.85)

                else:
                    # 正文为空属于正常情况（帖子已删除或无法访问），不算错误
                    empty_count += 1
                    # 也通知回调（断点续爬标记为已处理，避免重启后重复尝试）
                    if update_callback:
                        update_callback(post_id, {'post_content': ''})
                    if (i + 1) % 10 == 0 or (i + 1) == total:
                        pct = (i+1)*100/total
                        print(f'{self.symbol}: 股吧 {i+1}/{total} ({pct:.1f}%) 空{empty_count}, 延迟{adaptive_delay:.1f}s')

            except Exception as e:
                consecutive_errors += 1
                adaptive_delay = min(3.0, adaptive_delay * 1.5)
                if (i + 1) % 10 == 0 or (i + 1) == total:
                    print(f'{self.symbol}: 爬取帖子 {post_id} 出错: {e}')
                continue

            # 每 20 条重启浏览器
            if (i + 1) % RESTART_INTERVAL == 0:
                try:
                    parser.restart_detail_browser()
                    time.sleep(1)
                except Exception as e:
                    print(f'{self.symbol}: 浏览器重启失败（跳过，将继续尝试）: {e}')
                    consecutive_errors += 1
                    time.sleep(2)

            # 连续 5 次真正异常才冷却（更宽容）
            if consecutive_errors >= 5:
                print(f'{self.symbol}: 连续 {consecutive_errors} 次异常，重启浏览器，冷却 3 秒...')
                try:
                    parser.restart_detail_browser()
                    time.sleep(3)
                    consecutive_errors = 0
                    adaptive_delay = 1.5
                except Exception as e:
                    print(f'{self.symbol}: 浏览器重启失败，将重试: {e}')
                    time.sleep(5)

        parser.close()
        print(f'{self.symbol}: 股吧原生帖子爬取完成，共 {total} 条（成功 {total - empty_count}，为空 {empty_count}）')

    def _crawl_guba_posts_parallel(self, posts: list, update_callback, num_workers: int = 3):
        """多 worker 并发爬取股吧原生帖子（保守策略）

        每个 worker 独立创建 PostParser + 浏览器实例。
        每个 worker 启动后先访问一次列表页建立 cookies。
        正文获取优先使用 JS fetch，保留重启/空正文/异常冷却逻辑。
        """
        total = len(posts)
        num_workers = max(1, min(num_workers, 8))  # 限制 1~8
        print(f'{self.symbol}: 启动 {num_workers} 个 worker 并发爬取 {total} 条股吧原生帖子...')

        # 均匀分片
        chunks = [posts[i::num_workers] for i in range(num_workers)]
        # 过滤空分片
        chunks = [c for c in chunks if c]
        num_workers = len(chunks)
        print(f'{self.symbol}: 实际 {num_workers} 个 worker，每 worker {len(chunks[0])}~{len(chunks[-1])} 条')

        progress_lock = threading.Lock()
        progress = {'done': 0, 'empty': 0, 'errors': 0}

        def worker(thread_id: int, chunk: list):
            parser = PostParser()
            RESTART_INTERVAL = 20
            consecutive_errors = 0
            adaptive_delay = 0.0
            empty_count = 0

            # 建立 cookies：先访问一次列表页
            try:
                driver = parser.get_detail_browser()
                driver.get(f'https://guba.eastmoney.com/list,{self.symbol},f_1.html')
                time.sleep(1.5)
            except Exception as e:
                print(f'{self.symbol}: worker-{thread_id} 初始化失败: {e}')
                parser.close()
                return

            for i, post in enumerate(chunk):
                post_id = post['_id']
                post_url = post['post_url']

                try:
                    time.sleep(adaptive_delay + random.uniform(0, 0.1))

                    detail = parser.parse_post_detail(post_url)

                    if detail['post_content']:
                        update_data = {'post_content': detail['post_content']}
                        if detail.get('post_title'):
                            update_data['post_title'] = detail['post_title']
                        if detail.get('post_date'):
                            update_data['post_date'] = detail['post_date']
                        if detail.get('post_time'):
                            update_data['post_time'] = detail['post_time']
                        if detail.get('post_author'):
                            update_data['post_author'] = detail['post_author']

                        if update_callback:
                            update_callback(post_id, update_data)

                        consecutive_errors = 0
                        adaptive_delay = max(0.2, adaptive_delay * 0.85)
                    else:
                        empty_count += 1
                        if update_callback:
                            update_callback(post_id, {'post_content': ''})

                    with progress_lock:
                        progress['done'] += 1
                        if progress['done'] % 10 == 0 or progress['done'] == total:
                            pct = progress['done'] * 100 / total
                            print(f'{self.symbol}: 股吧 {progress["done"]}/{total} ({pct:.1f}%) '
                                  f'[worker-{thread_id}] 成功, 空{progress["empty"]}, 延迟{adaptive_delay:.1f}s')

                except Exception as e:
                    consecutive_errors += 1
                    adaptive_delay = min(3.0, adaptive_delay * 1.5)
                    with progress_lock:
                        progress['done'] += 1
                        progress['errors'] += 1
                    if progress['errors'] <= 5:
                        print(f'{self.symbol}: worker-{thread_id} 帖子 {post_id} 出错: {e}')
                    continue

                # 每 20 条重启浏览器
                if (i + 1) % RESTART_INTERVAL == 0:
                    try:
                        parser.restart_detail_browser()
                        time.sleep(1)
                    except Exception as e:
                        print(f'{self.symbol}: worker-{thread_id} 浏览器重启失败: {e}')
                        consecutive_errors += 1
                        time.sleep(2)

                # 连续 5 次异常冷却
                if consecutive_errors >= 5:
                    print(f'{self.symbol}: worker-{thread_id} 连续 {consecutive_errors} 次异常，重启冷却...')
                    try:
                        parser.restart_detail_browser()
                        time.sleep(3)
                        consecutive_errors = 0
                        adaptive_delay = 1.5
                    except Exception as e:
                        print(f'{self.symbol}: worker-{thread_id} 重启失败: {e}')
                        time.sleep(5)

            parser.close()

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = []
            for tid in range(num_workers):
                futures.append(executor.submit(worker, tid, chunks[tid]))
            for f in as_completed(futures):
                f.result()

        print(f'{self.symbol}: 股吧原生帖子 {num_workers} worker 并发完成，'
              f'共 {total} 条（成功 {total - progress["empty"]}，空 {progress["empty"]}，错误 {progress["errors"]}）')


class CommentCrawler(object):
    COMMENT_API_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': 'https://guba.eastmoney.com/',
    }
    COMMENT_API_PAGE_SIZE = 50
    COMMENT_API_MAX_PAGES = 5
    COMMENT_API_WORKERS = 8

    def __init__(self, stock_symbol: str):
        self.browser = None
        self.symbol = stock_symbol
        self.start = time.time()
        self.post_df = None  # dataframe about the post_url and post_id
        self.current_num = 0
        self.comment_session = requests.Session()
        self.comment_session.headers.update(self.COMMENT_API_HEADERS)

    def create_webdriver(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))  # hide the features of crawler/selenium
        js_file_path = os.path.join(current_dir, 'stealth.min.js')
        self.browser = create_stealth_chrome(stealth_script_path=js_file_path)
        # self.browser.set_page_load_timeout(2)  # set the timeout restrict

    def restart_webdriver(self):
        if self.browser is not None:
            try:
                self.browser.quit()
            except Exception:
                pass
        self.create_webdriver()

    def _ensure_webdriver(self):
        if self.browser is None:
            self.create_webdriver()

    def find_by_date(self, start_date, end_date):
        # get comment urls through date (used for the first crawl)
        """
        :param start_date: '2003-07-21' 字符串格式 ≥
        :param end_date: '2024-07-21' 字符串格式 ≤
        """
        postdb = MongoAPI('post_info', f'post_{self.symbol}')
        time_query = {
            'post_date': {'$gte': start_date, '$lte': end_date},
            'comment_num': {'$ne': 0}  # avoid fetching urls with no comment
        }
        post_info = postdb.find(time_query, {'_id': 1, 'post_url': 1})  # , 'post_date': 1
        self.post_df = pd.DataFrame(post_info)

    def find_by_id(self, start_id: int, end_id: int):
        # get comment urls through post_id (used when crawler is paused accidentally) crawl in batches
        """
        :param start_id: 721 整数 ≥
        :param end_id: 2003 整数 ≤
        """
        postdb = MongoAPI('post_info', f'post_{self.symbol}')
        id_query = {
            '_id': {'$gte': start_id, '$lte': end_id},
            'comment_num': {'$ne': 0}  # avoid fetching urls with no comment
        }
        post_info = postdb.find(id_query, {'_id': 1, 'post_url': 1})  # , 'post_date': 1
        self.post_df = pd.DataFrame(post_info)

    def crawl_comment_info(self, checkpoint_path: str = None):
        """爬取评论（API 并发优先，Selenium 兜底，支持断点续爬）"""
        import json as _json

        url_df = self.post_df['post_url']
        id_df = self.post_df['_id']
        comment_num_df = self.post_df['comment_num'] if 'comment_num' in self.post_df.columns else None
        total_num = self.post_df.shape[0]

        start_idx = 0
        done_ids = set()
        if checkpoint_path and os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, 'r', encoding='utf-8') as f:
                    data = _json.load(f)
                done_ids = set(str(x) for x in data.get('done_ids', []))
                if done_ids:
                    print(f'{self.symbol}: 评论断点续爬，跳过 {len(done_ids)} 帖')
                else:
                    start_idx = data.get('comment_idx', 0)
                    if start_idx < total_num:
                        print(f'{self.symbol}: 评论断点续爬，跳过前 {start_idx} 帖')
                    else:
                        print(f'{self.symbol}: 评论已全部完成，跳过')
                        return
            except Exception:
                pass

        commentdb = MongoAPI('comment_info', f'comment_{self.symbol}')
        jobs = []
        for idx in range(start_idx, total_num):
            current_post_id = id_df.iloc[idx]
            if hasattr(current_post_id, 'item'):
                current_post_id = current_post_id.item()
            current_post_id = str(current_post_id)
            if current_post_id in done_ids:
                continue
            expected_count = 0
            if comment_num_df is not None:
                try:
                    expected_count = int(comment_num_df.iloc[idx])
                except Exception:
                    expected_count = 0
            jobs.append((idx, str(url_df.iloc[idx]), current_post_id, expected_count))

        total_jobs = len(jobs)
        if total_jobs == 0:
            print(f'{self.symbol}: 评论已全部完成，跳过')
            return

        done_batch = set()

        def save_comment_checkpoint(force: bool = False):
            if not checkpoint_path:
                return
            if not force and len(done_batch) < 50:
                return
            existing_done = set(done_ids)
            if os.path.exists(checkpoint_path):
                try:
                    with open(checkpoint_path, 'r', encoding='utf-8') as f:
                        existing_done.update(str(x) for x in _json.load(f).get('done_ids', []))
                except Exception:
                    pass
            existing_done.update(done_batch)
            try:
                with open(checkpoint_path, 'w', encoding='utf-8') as f:
                    _json.dump({'done_ids': sorted(existing_done), 'updated_at': time.time()}, f)
                done_ids.update(done_batch)
                done_batch.clear()
            except Exception:
                pass

        def api_job(job):
            idx, url, post_id, expected_count = job
            ok, dic_list = self._fetch_comments_via_api(post_id, referer=url)
            if ok and expected_count > 0 and not dic_list:
                ok = False
            return idx, url, post_id, ok, dic_list

        api_success = 0
        api_fail = 0
        fallback_jobs = []

        print(f'{self.symbol}: 评论 API 并发爬取 {total_jobs} 帖，workers={self.COMMENT_API_WORKERS}')
        with ThreadPoolExecutor(max_workers=self.COMMENT_API_WORKERS) as executor:
            futures = [executor.submit(api_job, job) for job in jobs]
            for future in as_completed(futures):
                idx, url, current_post_id, ok, dic_list = future.result()

                if ok:
                    api_success += 1
                    if dic_list:
                        commentdb.insert_many(dic_list)
                    self.current_num += 1
                    done_batch.add(current_post_id)
                else:
                    api_fail += 1
                    fallback_jobs.append((idx, url, current_post_id))

                if self.current_num % 50 == 0 or self.current_num == total_jobs:
                    pct = self.current_num * 100 / total_jobs
                    print(f'{self.symbol}: 评论 API {self.current_num}/{total_jobs} ({pct:.1f}%) '
                          f'成功{api_success} 回退待处理{api_fail}')
                save_comment_checkpoint()

        save_comment_checkpoint(force=True)

        selenium_success = 0
        if fallback_jobs:
            print(f'{self.symbol}: 评论 API 失败 {len(fallback_jobs)} 帖，使用 Selenium 兜底')
            self._ensure_webdriver()

        for idx, url, current_post_id in fallback_jobs:
            try:
                dic_list = self._fetch_comments_via_selenium(url, current_post_id)
                if dic_list:
                    commentdb.insert_many(dic_list)
                selenium_success += 1
                self.current_num += 1
                done_batch.add(current_post_id)
                if selenium_success % 20 == 0 or selenium_success == len(fallback_jobs):
                    print(f'{self.symbol}: Selenium 评论兜底 {selenium_success}/{len(fallback_jobs)}')
                save_comment_checkpoint()
            except Exception as e:
                print(f'{self.symbol}: 帖子 {current_post_id} 评论兜底出错 {type(e).__name__}: {e}')
                try:
                    self.restart_webdriver()
                    time.sleep(1)
                except Exception as re:
                    print(f'{self.symbol}: 浏览器重启也失败: {re}，跳过继续...')
                    time.sleep(2)

        save_comment_checkpoint(force=True)
        end = time.time()
        time_cost = end - self.start
        row_count = commentdb.count_documents()
        if self.browser is not None:
            self.browser.quit()
        print(f'{self.symbol}: 评论爬取完成，{self.current_num} 帖，{row_count} 条，{time_cost/60:.1f}分 '
              f'(API成功{api_success}，API失败{api_fail}，Selenium兜底{selenium_success})')

    @staticmethod
    def _parse_jsonp(text: str) -> dict:
        match = re.search(r'\((.*)\)\s*$', text.strip(), re.S)
        if not match:
            raise ValueError('invalid JSONP response')
        return json.loads(match.group(1))

    @staticmethod
    def _comment_from_api_reply(reply: dict, post_id: str, sub_comment: bool = False) -> dict:
        publish_time = reply.get('reply_publish_time') or reply.get('reply_time') or ''
        return {
            'post_id': str(post_id),
            'comment_content': reply.get('reply_text') or '',
            'comment_like': int(reply.get('reply_like_count') or 0),
            'comment_date': publish_time[:10] if len(publish_time) >= 10 else '',
            'comment_time': publish_time[11:16] if len(publish_time) >= 16 else '',
            'sub_comment': int(sub_comment),
        }

    def _fetch_comments_via_api(self, post_id: str, referer: str = '') -> tuple:
        """通过东方财富评论 JSONP 接口获取评论，失败时返回 (False, [])."""
        comments = []
        seen_reply_ids = set()
        page = 1
        total_count = None

        while page <= self.COMMENT_API_MAX_PAGES:
            now_ms = int(time.time() * 1000)
            callback = f'jQuery{random.randint(100000000, 999999999)}_{now_ms}'
            params = {
                'callback': callback,
                'plat': 'web',
                'version': '300',
                'product': 'guba',
                'h': hashlib.md5(str(post_id).encode('utf-8')).hexdigest(),
                'postid': str(post_id),
                'sort': '1',
                'sorttype': '1',
                'p': str(page),
                'ps': str(self.COMMENT_API_PAGE_SIZE),
                'type': '0',
                '_': str(now_ms),
            }
            headers = dict(self.COMMENT_API_HEADERS)
            if referer:
                headers['Referer'] = referer

            try:
                resp = requests.get(
                    'https://gbapi.eastmoney.com/reply/JSONP/ArticleNewReplyList',
                    params=params,
                    headers=headers,
                    timeout=(3, 10),
                )
                resp.raise_for_status()
                data = self._parse_jsonp(resp.text)
            except Exception:
                return False, []

            if data.get('rc') not in (0, 1, None):
                return False, []

            replies = data.get('re') or []
            if total_count is None:
                total_count = int(data.get('reply_total_count') or data.get('count') or len(replies))

            for reply in replies:
                reply_id = str(reply.get('reply_id') or '')
                if reply_id and reply_id not in seen_reply_ids:
                    seen_reply_ids.add(reply_id)
                    comments.append(self._comment_from_api_reply(reply, post_id, sub_comment=False))

                for child in reply.get('child_replys') or []:
                    child_id = str(child.get('reply_id') or '')
                    if child_id and child_id not in seen_reply_ids:
                        seen_reply_ids.add(child_id)
                        comments.append(self._comment_from_api_reply(child, post_id, sub_comment=True))

            if not replies or len(replies) < self.COMMENT_API_PAGE_SIZE:
                break
            if total_count is not None and page * self.COMMENT_API_PAGE_SIZE >= total_count:
                break
            page += 1

        return True, comments

    def _fetch_comments_via_js(self, post_url: str, post_id: str) -> list:
        """方向C：通过浏览器内 JS fetch 快速判断帖子是否有评论

        由于东方财富评论是动态加载的（不在初始 HTML 中），JS fetch 无法直接提取评论。
        此方法作为快速预检：检查页面是否可访问，不可访问时返回空列表让调用方回退。
        实际评论提取由 _fetch_comments_via_selenium 完成。
        """
        # 评论由 JS 动态加载，JS fetch HTML 无法获取，直接返回空列表
        # 让调用方使用 Selenium 方式（已做优化：减少等待时间、批量处理）
        return []

    def _fetch_comments_via_selenium(self, post_url: str, post_id: str) -> list:
        """原始 Selenium 方式获取评论（作为 JS fetch 失败时的回退）"""
        dic_list = []
        try:
            try:
                # 临时取消页面加载超时，避免慢页面崩溃 WebDriver
                self.browser.set_page_load_timeout(15)
                self.browser.get(post_url)
            except Exception:
                # 页面加载失败（超时/重定向/验证页），恢复超时后直接跳过该帖
                self.browser.set_page_load_timeout(5)
                return dic_list

            self.browser.set_page_load_timeout(5)

            WebDriverWait(self.browser, 0.2, poll_frequency=0.1).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div.reply_item.cl')))
        except TimeoutException:
            self.browser.refresh()
        finally:
            try:
                reply_items = self.browser.find_elements(
                    By.CSS_SELECTOR,
                    'div.allReplyList > div.replylist_content > div.reply_item.cl'
                )
            except Exception:
                reply_items = []

        parser = CommentParser()
        for item in reply_items:
            dic = parser.parse_comment_info(item, post_id)
            dic_list.append(dic)

            if parser.judge_sub_comment(item):
                sub_reply_items = item.find_elements(By.CSS_SELECTOR, 'li.reply_item_l2')
                for subitem in sub_reply_items:
                    dic = parser.parse_comment_info(subitem, post_id, True)
                    dic_list.append(dic)

        return dic_list
