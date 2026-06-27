from selenium.webdriver.common.by import By
import time
import random
import threading
import json
import re
import math
import hashlib
import urllib.parse
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


class FullCrawlPaused(RuntimeError):
    """Raised when full-mode list crawling should pause instead of retrying."""

    def __init__(self, page_num: int, reason: str, retry_after_seconds: int = 3600):
        super().__init__(f'page {page_num} paused: {reason}')
        self.page_num = page_num
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


def _looks_like_blocked_error(error: Exception | str) -> bool:
    text = str(error)
    return any(
        marker in text
        for marker in (
            'fd_guba_validate',
            '身份核实',
            'validation',
            'HTTP 403',
            'HTTP 429',
            'blocked by validation',
        )
    )


def _looks_like_blocked_response(response_text: str) -> bool:
    if not response_text:
        return False
    return any(
        marker in response_text
        for marker in (
            'fd_guba_validate',
            '身份核实',
            '请完成安全验证',
            '请输入验证码',
        )
    )


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
        'Connection': 'keep-alive',
    }
    POSTS_PER_PAGE = 80
    FAST_LIST_SOURCES = {'html', 'api', 'auto'}

    def __init__(self, stock_symbol: str, proxy: str = ""):
        self.browser = None
        self.symbol = stock_symbol
        self.start = time.time()  # calculate the time cost
        self.proxy = proxy
        self.session = self._new_list_session()
        self._cookie_bootstrap_lock = threading.Lock()
        self._browser_cookie_bootstrapped = False
        self._caifuhao_session = None
        self._caifuhao_cookie_bootstrapped = False

    def _request_proxies(self) -> dict | None:
        if not self.proxy:
            return None
        return {'http': self.proxy, 'https': self.proxy}

    def _new_list_session(self, cookies: dict | None = None) -> requests.Session:
        session = requests.Session()
        session.headers.update(self.LIST_HEADERS)
        if cookies:
            session.cookies.update(cookies)
        return session

    def _new_caifuhao_session(self):
        """创建 curl_cffi Session（Chrome 120 TLS 指纹）用于财富号爬取"""
        from curl_cffi import requests as curl_requests
        session = curl_requests.Session(impersonate="chrome120")
        session.headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,'
                      'image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://guba.eastmoney.com/',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-site',
            'Sec-Fetch-User': '?1',
        })
        if self.proxy:
            session.proxies.update({'http': self.proxy, 'https': self.proxy})
        return session

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

    def _browser_is_blocked(self) -> bool:
        if self.browser is None:
            return False
        try:
            current_url = self.browser.current_url or ''
            title = self.browser.title or ''
            source = self.browser.page_source[:5000] if self.browser.page_source else ''
        except Exception:
            return False
        return (
            'fd_guba_validate' in current_url
            or title == '验证'
            or '身份核实' in title
            or _looks_like_blocked_response(source)
        )

    def get_page_num(self):
        try:
            html = self._fetch_list_html(1, session=self.session)
            payload = self._extract_article_payload(html)
            total_count = int(payload.get('count') or 0)
            if total_count > 0:
                return max(1, math.ceil(total_count / self.POSTS_PER_PAGE))
        except Exception as e:
            if _looks_like_blocked_error(e):
                raise FullCrawlPaused(1, str(e), retry_after_seconds=3600)

        if self.browser is None:
            self.create_webdriver()
        self.browser.get(f'http://guba.eastmoney.com/list,{self.symbol},f_1.html')
        if self._browser_is_blocked():
            raise FullCrawlPaused(1, 'list page redirected to validation', retry_after_seconds=3600)
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

    def _fetch_list_html(self, page_num: int, session: requests.Session | None = None) -> str:
        session = session or self.session
        url = f'https://guba.eastmoney.com/list,{self.symbol},f_{page_num}.html'
        resp = session.get(url, timeout=(3, 12), proxies=self._request_proxies())
        if resp.status_code in (403, 429):
            raise RuntimeError(f'HTTP {resp.status_code}')
        resp.raise_for_status()
        resp.encoding = resp.encoding or 'utf-8'
        text = resp.text
        if 'fd_guba_validate' in resp.url or _looks_like_blocked_response(text[:5000]):
            raise RuntimeError('list page redirected to validation')
        return text

    @staticmethod
    def _extract_json_object(text: str, start_index: int) -> str | None:
        depth = 0
        in_string = False
        escape = False
        for index in range(start_index, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start_index:index + 1]
        return None

    @classmethod
    def _extract_article_payload(cls, html: str) -> dict:
        match = re.search(r'var\s+article_list\s*=\s*\{', html)
        if not match:
            raise ValueError('article_list payload not found')
        json_start = html.find('{', match.start())
        json_text = cls._extract_json_object(html, json_start)
        if not json_text:
            raise ValueError('article_list JSON parse failed')
        return json.loads(json_text)

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

    def _extract_visible_post_keys(self, html: str, page_num: int) -> list[str]:
        soup = BeautifulSoup(html, 'html.parser')
        rows = soup.select('tr.listitem')
        if page_num == 1 and rows:
            rows = rows[1:]
        keys: list[str] = []
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
        source_id = str(article.get('post_source_id') or article.get('source_post_id') or '')
        post_type = str(article.get('post_type') if article.get('post_type') is not None else '')
        stockbar_code = str(article.get('stockbar_code') or self.symbol)
        stockbar_name = article.get('stockbar_name') or default_stockbar_name or f'{self.symbol}吧'
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

    def _bootstrap_list_session_via_browser(self):
        with self._cookie_bootstrap_lock:
            if self._browser_cookie_bootstrapped and self.session.cookies:
                return
            browser = create_stealth_chrome()
            try:
                browser.get(f'https://guba.eastmoney.com/list,{self.symbol},f_1.html')
                time.sleep(2)
                for cookie in browser.get_cookies():
                    name = cookie.get('name')
                    value = cookie.get('value')
                    if name and value is not None:
                        self.session.cookies.set(name, value, domain='.eastmoney.com')
                self._browser_cookie_bootstrapped = True
                print(f'{self.symbol}: warmed requests cookies from browser once')
            finally:
                browser.quit()

    def _bootstrap_caifuhao_session_via_browser(self):
        """启动浏览器访问财富号首页，将 Cookie 注入 curl_cffi Session"""
        with self._cookie_bootstrap_lock:
            if self._caifuhao_cookie_bootstrapped and self._caifuhao_session is not None:
                return
            browser = create_stealth_chrome()
            try:
                browser.get('https://caifuhao.eastmoney.com/')
                time.sleep(3)
                browser.get('https://caifuhao.eastmoney.com/news/')
                time.sleep(2)
                if self._caifuhao_session is None:
                    self._caifuhao_session = self._new_caifuhao_session()
                for cookie in browser.get_cookies():
                    name = cookie.get('name')
                    value = cookie.get('value')
                    domain = cookie.get('domain', '.eastmoney.com')
                    if name and value is not None:
                        self._caifuhao_session.cookies.set(name, value, domain=domain)
                self._caifuhao_cookie_bootstrapped = True
                print(f'{self.symbol}: warmed caifuhao curl_cffi session from browser')
            finally:
                browser.quit()

    def _fetch_article_payload_api(self, page_num: int, session: requests.Session | None = None) -> dict:
        session = session or self.session
        path = 'webarticlelist/api/Article/Articlelist'
        url = (
            f'https://guba.eastmoney.com/api/getData?code={self.symbol}'
            f'&path={urllib.parse.quote(path, safe="")}'
        )
        data = {
            'param': f'code={self.symbol}&type=0&p={page_num}&ps={self.POSTS_PER_PAGE}&sorttype=0',
            'plat': 'Web',
            'path': path,
            'env': '2',
            'origin': '',
            'version': '2022',
            'product': 'Guba',
        }
        headers = dict(self.LIST_HEADERS)
        headers.update({
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': 'https://guba.eastmoney.com',
            'Referer': f'https://guba.eastmoney.com/list,{self.symbol},f_{page_num}.html',
        })
        resp = session.post(url, data=data, headers=headers, timeout=(3, 12), proxies=self._request_proxies())
        if resp.status_code in (403, 429):
            raise RuntimeError(f'HTTP {resp.status_code}')
        resp.raise_for_status()
        if _looks_like_blocked_response(resp.text[:5000]):
            raise RuntimeError('list API redirected to validation')
        payload = resp.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('re'), list):
            raise ValueError(f'Articlelist API payload invalid: {str(payload)[:120]}')
        return payload

    def _fetch_article_payload_api_with_bootstrap(
        self,
        page_num: int,
        session: requests.Session | None = None,
    ) -> dict:
        session = session or self.session
        try:
            return self._fetch_article_payload_api(page_num, session=session)
        except ValueError:
            self._bootstrap_list_session_via_browser()
            session.cookies.update(self.session.cookies.get_dict())
            return self._fetch_article_payload_api(page_num, session=session)

    def _fetch_post_page_fast(self, page_num: int, session: requests.Session | None = None) -> list:
        payload = self._fetch_article_payload_api_with_bootstrap(page_num, session=session)
        default_stockbar_name = payload.get('bar_name') or f'{self.symbol}吧'
        if default_stockbar_name and not str(default_stockbar_name).endswith('吧'):
            default_stockbar_name = f'{default_stockbar_name}吧'

        article_map: dict[str, dict] = {}
        for article in payload.get('re') or []:
            post_id = str(article.get('post_id') or '')
            source_id = str(article.get('post_source_id') or article.get('source_post_id') or '')
            if post_id:
                article_map[post_id] = article
            if source_id:
                article_map[source_id] = article

        visible_keys = []
        if page_num == 1:
            try:
                html = self._fetch_list_html(page_num, session=session)
                visible_keys = self._extract_visible_post_keys(html, page_num)
            except Exception:
                visible_keys = []
        if not visible_keys:
            visible_keys = [
                str(article.get('post_source_id') or article.get('source_post_id') or article.get('post_id'))
                for article in (payload.get('re') or [])
                if article.get('post_id')
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
            pid = str(dic.get('_id') or '')
            if not pid or pid in seen_ids:
                continue
            if 'guba.eastmoney.com/news' in dic['post_url'] or 'caifuhao.eastmoney.com/news' in dic['post_url']:
                seen_ids.add(pid)
                dic_list.append(dic)
        return dic_list

    def fetch_post_page(self, page_num: int, parser: PostParser, stockbar_name: str = ''):
        """Fetch one list page with the upstream Selenium DOM method."""
        if self.browser is None:
            self.create_webdriver()
        url = f'http://guba.eastmoney.com/list,{self.symbol},f_{page_num}.html'
        self.browser.get(url)
        if self._browser_is_blocked():
            raise RuntimeError('list page redirected to validation')
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
        print(f'  - 财富号: {len(caifuhao_posts)} 条（WAP API 主路径 + curl_cffi 兜底）')
        if worker_count > 1:
            print(f'  - 股吧原生: {len(guba_posts)} 条（{worker_count} worker 并发）')
        else:
            print(f'  - 股吧原生: {len(guba_posts)} 条（单线程 Selenium）')

        # 先爬财富号帖子（多线程 requests）
        if caifuhao_posts:
            self._crawl_caifuhao_posts(caifuhao_posts, update_callback, max_workers=worker_count)

        # 爬非财富号帖子
        if guba_posts:
            if worker_count > 1:
                self._crawl_guba_posts_parallel(guba_posts, update_callback, num_workers=worker_count)
            else:
                self._crawl_guba_posts(guba_posts, update_callback)

        print(f'{self.symbol}: 正文爬取完成，共处理 {total} 条帖子')

    def _crawl_caifuhao_posts(self, posts: list, update_callback, max_workers: int = 3):
        """高速财富号正文爬取（WAP API 主路径 + curl_cffi 兜底 + 窗口分批）

        主路径：gbapi.eastmoney.com WAP API（低封禁风险，实测 100/100 成功）
        兜底：curl_cffi Chrome 120 TLS 指纹请求 PC 页面
        每 worker 独立 curl_cffi Session，不共享，避免连接池竞争。
        """
        total = len(posts)
        WINDOW_SIZE = 50          # 每批 50 条
        WORKERS = max(1, min(int(max_workers or 3), 6))
        WINDOW_PAUSE = (2.0, 5.0)   # 批间暂停
        PER_REQ_DELAY = (0.1, 0.4)  # 请求间微延迟
        BLOCK_THRESHOLD = 3         # 连续 403/blocked 触发冷却
        COOLDOWN_TIME = (30.0, 60.0)  # 封禁后冷却

        # 浏览器预热主 Cookie 池（仅一次，供所有 worker 复制）
        self._bootstrap_caifuhao_session_via_browser()
        master_cookies = list(self._caifuhao_session.cookies)
        parser = PostParser()

        def extract_source_id(post: dict) -> str:
            source_id = str(post.get('post_source_id', '') or '').strip()
            if source_id:
                return source_id
            match = re.search(r'/news/(\d+)', post.get('post_url', '') or '')
            return match.group(1) if match else ''

        def caifuhao_url(post: dict) -> str:
            source_id = extract_source_id(post)
            if source_id:
                return f'https://caifuhao.eastmoney.com/news/{source_id}'
            return str(post.get('post_url', '') or '')

        # 每 worker 独立 session（线程本地存储）
        _thread_local = threading.local()

        def _worker_session():
            if getattr(_thread_local, 'session', None) is None:
                s = self._new_caifuhao_session()
                for c in master_cookies:
                    try:
                        s.cookies.set(c.name, c.value, domain=c.domain)
                    except Exception:
                        pass
                _thread_local.session = s
            return _thread_local.session

        success_ids = set()
        permanent_fail_ids = set()
        blocked_ids = set()
        lock = threading.Lock()
        start_time = time.time()
        consecutive_blocks = 0

        # 永久失效原因：文章确实不存在/已删除
        PERMANENT_FAIL_REASONS = {
            'http_404', 'http_410', 'invalid_article', 'invalid_page_marker',
            'missing_source_id', 'missing_post_id',
        }
        # 可重试原因：临时封禁/超时/空正文
        RETRYABLE_REASONS = {
            'http_403', 'http_429', 'blocked_validation', 'timeout',
            'body_not_found', 'wap_system_busy', 'wap_api_empty',
        }

        def crawl_one(post: dict):
            """单条爬取：WAP API 主路径 → curl_cffi PC 页面兜底"""
            post_id = post['_id']
            source_id = extract_source_id(post)
            url = caifuhao_url(post)
            if not url and not source_id:
                return post_id, {'ok': False, 'reason': 'missing_url', 'http_status': 0}, url

            time.sleep(random.uniform(*PER_REQ_DELAY))
            ws = _worker_session()

            # === 主路径：WAP API（gbapi.eastmoney.com，低封禁风险）===
            detail = parser._try_wap_caifuhao(
                post_id=post_id, source_id=source_id, post_url=url,
                session=ws, proxies=self._request_proxies()
            )

            # === 兜底：curl_cffi Chrome 120 请求 PC 页面 ===
            if not detail.get('ok') and detail.get('reason') not in PERMANENT_FAIL_REASONS:
                fallback = parser._try_requests_caifuhao(
                    url, session=ws, proxies=self._request_proxies()
                )
                if fallback.get('ok'):
                    detail = fallback

            return post_id, detail, url

        print(f'{self.symbol}: [CAIFUHAO] WAP API主路径+curl_cffi兜底: total={total}, '
              f'workers={WORKERS}, window={WINDOW_SIZE}')

        window_done = 0
        global_blocked = False

        for wstart in range(0, total, WINDOW_SIZE):
            if global_blocked:
                print(f'{self.symbol}: [CAIFUHAO] 检测到封禁，冷却 {COOLDOWN_TIME[0]}-{COOLDOWN_TIME[1]}s + 刷新Cookie...')
                cooldown = random.uniform(*COOLDOWN_TIME)
                time.sleep(cooldown)
                self._caifuhao_cookie_bootstrapped = False
                self._bootstrap_caifuhao_session_via_browser()
                master_cookies.clear()
                master_cookies.extend(list(self._caifuhao_session.cookies))
                # 清除所有 worker 的 thread-local session，下次自动重建
                global_blocked = False
                consecutive_blocks = 0

            window = posts[wstart:wstart + WINDOW_SIZE]
            window_blocked = 0

            with ThreadPoolExecutor(max_workers=WORKERS) as executor:
                futures = {executor.submit(crawl_one, p): p['_id'] for p in window}
                for future in as_completed(futures):
                    post_id, detail, url = future.result()
                    with lock:
                        window_done += 1
                        reason = detail.get('reason', 'unknown')

                        if detail.get('ok'):
                            update_data = {'post_content': detail['post_content']}
                            for k in ('post_title', 'post_date', 'post_time', 'post_author'):
                                if detail.get(k):
                                    update_data[k] = detail[k]
                            update_callback(post_id, update_data)
                            success_ids.add(post_id)
                            consecutive_blocks = 0
                        elif reason in PERMANENT_FAIL_REASONS:
                            update_callback(post_id, {
                                'post_content': '', '_detail_failed': True,
                                'reason': reason, 'post_url': url
                            })
                            permanent_fail_ids.add(post_id)
                        elif reason in RETRYABLE_REASONS:
                            blocked_ids.add(post_id)
                            window_blocked += 1
                            if reason in ('http_403', 'http_429', 'blocked_validation'):
                                consecutive_blocks += 1
                        else:
                            # 未知原因也归入可重试
                            blocked_ids.add(post_id)
                            window_blocked += 1

            # 进度汇报
            elapsed = time.time() - start_time
            speed = window_done / max(elapsed / 60, 0.01)
            eta = (total - window_done) / max(speed, 0.01)
            print(f'{self.symbol}: [CAIFUHAO] {window_done}/{total} '
                  f'({window_done/total*100:.0f}%) | '
                  f'ok={len(success_ids)} | fail(perm)={len(permanent_fail_ids)} | '
                  f'retry={len(blocked_ids)} | '
                  f'{speed:.0f}条/分 | ETA {eta:.0f}分')

            if consecutive_blocks >= BLOCK_THRESHOLD or window_blocked > len(window) * 0.3:
                global_blocked = True
                print(f'{self.symbol}: [CAIFUHAO] 封禁率过高 ({window_blocked}/{len(window)}), 触发冷却')

            if wstart + WINDOW_SIZE < total and not global_blocked:
                time.sleep(random.uniform(*WINDOW_PAUSE))

        # ===== Pass 2：单线程重试被阻断的帖子（WAP API + 长延迟）=====
        if blocked_ids:
            retry_posts = [p for p in posts if p['_id'] in blocked_ids]
            print(f'\n{self.symbol}: [CAIFUHAO-PASS2] 重试 {len(retry_posts)} 条阻断帖子 '
                  f'(单线程, delay=1-3s, 冷却后刷新Cookie)...')
            time.sleep(random.uniform(30, 60))
            self._caifuhao_cookie_bootstrapped = False
            self._bootstrap_caifuhao_session_via_browser()
            retry_session = self._caifuhao_session

            retry_ok = 0
            retry_fail = 0
            for i, post in enumerate(retry_posts):
                time.sleep(random.uniform(1.0, 3.0))
                post_id = post['_id']
                source_id = extract_source_id(post)
                url = caifuhao_url(post)

                # 主路径：WAP API
                detail = parser._try_wap_caifuhao(
                    post_id=post_id, source_id=source_id, post_url=url,
                    session=retry_session, proxies=self._request_proxies()
                )
                if not detail.get('ok') and detail.get('reason') not in PERMANENT_FAIL_REASONS:
                    detail = parser._try_requests_caifuhao(
                        url, session=retry_session, proxies=self._request_proxies()
                    )

                if detail.get('ok'):
                    update_data = {'post_content': detail['post_content']}
                    for k in ('post_title', 'post_date', 'post_time', 'post_author'):
                        if detail.get(k):
                            update_data[k] = detail[k]
                    update_callback(post_id, update_data)
                    retry_ok += 1
                else:
                    reason = detail.get('reason', 'unknown')
                    update_callback(post_id, {
                        'post_content': '', '_detail_failed': True,
                        'reason': f'retry_failed:{reason}', 'post_url': url
                    })
                    retry_fail += 1
                if (i + 1) % 20 == 0:
                    print(f'{self.symbol}: [CAIFUHAO-PASS2] {i+1}/{len(retry_posts)} '
                          f'| ok={retry_ok} | fail={retry_fail}')

            print(f'{self.symbol}: [CAIFUHAO-PASS2] 完成: ok={retry_ok}, fail={retry_fail}')

        total_elapsed = time.time() - start_time
        final_fail = len(permanent_fail_ids) + len([p for p in posts
                          if p['_id'] in blocked_ids and p['_id'] not in success_ids])
        status = 'OK' if len(blocked_ids) == 0 else f'WARN({len(blocked_ids)}未恢复)'
        print(f'{self.symbol}: 财富号爬取完成 [{status}] '
              f'成功 {len(success_ids)}，永久失效 {len(permanent_fail_ids)}，'
              f'总耗时 {total_elapsed:.0f}s ({total_elapsed/60:.1f}分)')

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
              f'共 {total} 条（成功 {total - progress["empty"]}，空 {progress["empty"]}，错误 {progress["errors"]})')

    # ==================== 全量列表抓取（按 start_date 边界） ====================

    def _page_date_range(self, page_num: int) -> tuple[str | None, str | None, int]:
        """返回某页的 (max_date, min_date, row_count)。

        用于快速判断该页是否可能包含 >= start_date 的帖子。
        """
        parser = PostParser()
        try:
            dic_list = self.fetch_post_page(page_num, parser, stockbar_name=f'{self.symbol}吧')
        except Exception as e:
            if _looks_like_blocked_error(e):
                raise FullCrawlPaused(page_num, str(e), retry_after_seconds=3600)
            return None, None, 0
        finally:
            parser.close()
        dates = [d.get('post_date') for d in dic_list if d.get('post_date')]
        if not dates:
            return None, None, len(dic_list)
        return max(dates), min(dates), len(dic_list)

    def _find_boundary_page_since(self, start_date: str, max_page: int) -> int:
        """二分查找最后一个可能包含 post_date >= start_date 的页面。

        列表页按时间倒序：第 1 页最新，第 max_page 页最旧。
        因此如果第 mid 页的 min_date >= start_date，说明 mid 及之后（页号更大）可能还有目标帖；
        如果第 mid 页的 max_date < start_date，说明 mid 之前（页号更小）才可能有目标帖。
        """
        left, right = 1, max_page
        boundary = 0
        probes = 0
        max_probes = 30
        while left <= right and probes < max_probes:
            mid = (left + right) // 2
            max_date, min_date, count = self._page_date_range(mid)
            probes += 1
            if max_date is None or count == 0:
                # 页面异常，收缩右边界再试
                right = mid - 1
                continue
            if min_date >= start_date:
                # mid 整页都在 start_date 之后，后面页号更大也可能有
                boundary = max(boundary, mid)
                left = mid + 1
            elif max_date < start_date:
                # mid 整页都在 start_date 之前，目标只可能在前半段
                right = mid - 1
            else:
                # mid 页跨边界，它就是目标页之一
                boundary = max(boundary, mid)
                # 为了找更晚的边界页，继续向后探测
                left = mid + 1
        print(f'{self.symbol}: 二分探测 {probes} 次，边界页约为 {boundary}')
        return boundary

    def _fetch_page_with_retry(
        self,
        page_num: int,
        parser: PostParser,
        stockbar_name: str = '',
        retries: int = 3,
        base_delay: float = 1.0,
    ) -> list:
        """带指数退避的 Selenium 页面抓取。"""
        last_error = None
        for attempt in range(retries + 1):
            try:
                return self.fetch_post_page(page_num, parser, stockbar_name=stockbar_name)
            except Exception as e:
                last_error = e
                if _looks_like_blocked_error(e):
                    raise RuntimeError(f'page {page_num} blocked by validation: {e}')
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
        raise RuntimeError(f'page {page_num} failed after Selenium retries: {last_error}')

    def _page_date_range(
        self,
        page_num: int,
        list_source: str = 'html',
        session: requests.Session | None = None,
    ) -> tuple[str | None, str | None, int]:
        try:
            if list_source == 'selenium':
                parser = PostParser()
                try:
                    dic_list = self.fetch_post_page(page_num, parser, stockbar_name=f'{self.symbol}吧')
                finally:
                    parser.close()
            else:
                dic_list = self._fetch_post_page_fast(page_num, session=session)
        except Exception as e:
            if _looks_like_blocked_error(e):
                raise FullCrawlPaused(page_num, str(e), retry_after_seconds=3600)
            return None, None, 0
        dates = [d.get('post_date') for d in dic_list if d.get('post_date')]
        if not dates:
            return None, None, len(dic_list)
        return max(dates), min(dates), len(dic_list)

    def _find_boundary_page_since(self, start_date: str, max_page: int, list_source: str = 'html') -> int:
        left, right = 1, max_page
        boundary = 0
        probes = 0
        max_probes = 30
        while left <= right and probes < max_probes:
            mid = (left + right) // 2
            max_date, min_date, count = self._page_date_range(mid, list_source=list_source, session=self.session)
            probes += 1
            if max_date is None or count == 0:
                right = mid - 1
                continue
            if min_date >= start_date:
                boundary = max(boundary, mid)
                left = mid + 1
            elif max_date < start_date:
                right = mid - 1
            else:
                boundary = max(boundary, mid)
                left = mid + 1
        print(f'{self.symbol}: boundary probes {probes}, boundary_page={boundary}')
        return boundary

    def _fetch_page_with_retry(
        self,
        page_num: int,
        parser: PostParser | None = None,
        stockbar_name: str = '',
        list_source: str = 'html',
        session: requests.Session | None = None,
        allow_selenium_fallback: bool = False,
        retries: int = 3,
        base_delay: float = 0.8,
    ) -> list:
        last_error = None
        for attempt in range(retries + 1):
            try:
                if list_source == 'selenium':
                    if parser is None:
                        parser = PostParser()
                    return self.fetch_post_page(page_num, parser, stockbar_name=stockbar_name)
                return self._fetch_post_page_fast(page_num, session=session)
            except Exception as e:
                last_error = e
                if _looks_like_blocked_error(e):
                    raise RuntimeError(f'page {page_num} blocked by validation: {e}')
                if attempt < retries:
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 0.8)
                    time.sleep(delay)
        if allow_selenium_fallback and list_source != 'selenium':
            fallback_parser = PostParser()
            try:
                return self.fetch_post_page(page_num, fallback_parser, stockbar_name=stockbar_name or f'{self.symbol}吧')
            except Exception as e:
                raise RuntimeError(f'page {page_num} failed after requests and Selenium fallback: {e}')
            finally:
                fallback_parser.close()
        raise RuntimeError(f'page {page_num} failed after requests retries: {last_error}')

    def crawl_post_info_since(
        self,
        start_date: str,
        storage_callback,
        list_workers: int = 6,
        checkpoint_callback=None,
        page_storage_callback=None,
        cached_pages: set[int] | None = None,
        boundary_page: int | None = None,
        list_window_size: int = 30,
        window_pause_range: tuple[float, float] = (20.0, 45.0),
        list_source: str = "html",
        api_page_size: int = 80,
        api_window_size: int = 60,
        api_concurrency: int = 3,
        target_stage1_minutes: int = 25,
    ) -> dict:
        """从网页全量抓取 post_publish_time >= start_date 的帖子列表。

        Args:
            start_date: 起始日期（YYYY-MM-DD），包含该日期。
            storage_callback: 每页数据回调，接收 dic_list（已按 start_date 过滤）。
            list_workers: 兼容旧参数；上游 Selenium 方法按单浏览器顺序翻页。
            checkpoint_callback: 可选回调，每处理完一页调用一次，参数为 (page_num, result)。
            page_storage_callback: 可选页级缓存回调，参数为 (page_num, rows, meta)。
            cached_pages: 已有 ok 页缓存的页码集合；这些页会跳过网络请求。
            boundary_page: 已缓存的边界页；传入时跳过本轮边界探测。
            list_window_size: 兼容旧参数；仅用于进度日志。
            window_pause_range: 兼容旧参数；不再用于并发滑窗。

        Returns:
            汇总信息 dict，包含 max_page, boundary_page, completed_pages, failed_pages,
            rows, unique_post_ids, min_time, max_time 等。
        """
        if list_source != 'html':
            print(f'{self.symbol}: 已恢复为上游 Selenium HTML 抓取，忽略 list_source={list_source}')
        print(f'\n{self.symbol}: 开始全量爬取列表，start_date={start_date}, list_source=html')
        cached_pages = set(cached_pages or set())
        list_window_size = max(1, int(list_window_size or 30))

        try:
            max_page = self.get_page_num()
        except FullCrawlPaused as e:
            end = time.time()
            return {
                'status': 'paused_blocked',
                'max_page': 0,
                'boundary_page': 0,
                'completed_pages': 0,
                'failed_pages': [e.page_num],
                'blocked_pages': [e.page_num],
                'parse_failed_pages': [],
                'transient_failed_pages': [],
                'paused_reason': e.reason,
                'retry_after_seconds': e.retry_after_seconds,
                'skipped_cached_pages': 0,
                'list_window_size': list_window_size,
                'list_source': 'html',
                'rows': 0,
                'unique_post_ids': 0,
                'min_time': '',
                'max_time': '',
                'time_cost_seconds': round(end - self.start, 2),
            }
        print(f'{self.symbol}: 列表总页数 {max_page}')

        status = 'success'
        blocked_pages: list[int] = []
        transient_failed_pages: list[int] = []
        paused_reason = ''
        retry_after_seconds = 0

        if boundary_page is None:
            try:
                boundary_page = self._find_boundary_page_since(start_date, max_page)
            except FullCrawlPaused as e:
                boundary_page = 0
                status = 'paused_blocked'
                blocked_pages.append(e.page_num)
                paused_reason = e.reason
                retry_after_seconds = e.retry_after_seconds
        else:
            boundary_page = min(max(0, int(boundary_page)), max_page)
            print(f'{self.symbol}: 复用边界页 {boundary_page}')
        if boundary_page == 0 and status == 'success':
            status = 'paused_blocked'
            blocked_pages.append(1)
            paused_reason = 'boundary detection failed'
            retry_after_seconds = 3600
        print(f'{self.symbol}: 需抓取 1 ~ {boundary_page} 页')

        completed_pages = len([p for p in cached_pages if 1 <= p <= boundary_page])
        failed_pages: list[int] = []
        total_rows = 0
        seen_ids: set[str] = set()
        min_time = None
        max_time = None

        pages = [p for p in range(1, boundary_page + 1) if p not in cached_pages]
        parser = PostParser()
        stockbar_name = self._extract_stockbar_name()
        progress_interval = max(1, min(5, len(pages) // 20))  # report every ~5% or at least every 5 pages
        last_progress_at = 0
        try:
            for idx, page_num in enumerate(pages, start=1):
                if status == 'paused_blocked':
                    break
                time.sleep(abs(random.normalvariate(0.01, 0.005)))
                try:
                    dic_list = self._fetch_page_with_retry(
                        page_num,
                        parser,
                        stockbar_name=stockbar_name,
                    )
                except Exception as e:
                    failed_pages.append(page_num)
                    if _looks_like_blocked_error(e):
                        status = 'paused_blocked'
                        blocked_pages.append(page_num)
                        paused_reason = str(e)
                        retry_after_seconds = 3600
                        print(f'{self.symbol}: [STOP] 第 {page_num} 页触发验证/限流 → {e}')
                        if hasattr(self, 'browser') and self.browser is not None:
                            try:
                                from browser_utils import session_blocked_screenshot_path
                                self.browser.save_screenshot(str(session_blocked_screenshot_path(self.symbol)))
                                print(f'{self.symbol}: [截图] 已保存验证页截图')
                            except Exception:
                                pass
                        break
                    transient_failed_pages.append(page_num)
                    print(f'{self.symbol}: [失败] 第 {page_num} 页 → {type(e).__name__}: {e}')
                    continue

                kept = [
                    d for d in dic_list
                    if d.get('post_date') and d['post_date'] >= start_date
                ]
                all_before = bool(dic_list) and all(
                    d.get('post_date', '') < start_date
                    for d in dic_list
                    if d.get('post_date')
                )

                if not kept and not all_before:
                    failed_pages.append(page_num)
                    print(f'{self.symbol}: [空页] 第 {page_num} 页无有效数据（非首页空页）')
                else:
                    failed_pages = [p for p in failed_pages if p != page_num]
                    unique_kept = []
                    for d in kept:
                        pid = str(d.get('_id', ''))
                        if pid and pid not in seen_ids:
                            seen_ids.add(pid)
                            unique_kept.append(d)
                    if unique_kept:
                        if storage_callback:
                            storage_callback(unique_kept)
                        if page_storage_callback:
                            page_storage_callback(page_num, unique_kept, {"status": "ok"})
                        total_rows += len(unique_kept)
                        dates = [d['post_date'] for d in unique_kept if d.get('post_date')]
                        if dates:
                            local_min = min(dates)
                            local_max = max(dates)
                            if min_time is None or local_min < min_time:
                                min_time = local_min
                            if max_time is None or local_max > max_time:
                                max_time = local_max
                    completed_pages += 1
                    if checkpoint_callback:
                        checkpoint_callback(page_num, {"kept": len(kept), "all_before": all_before})

                # Real-time progress: report every progress_interval pages or on every page after 50% done
                elapsed = time.time() - self.start
                if (idx - last_progress_at >= progress_interval) or (idx == len(pages)):
                    pct = idx / max(len(pages), 1) * 100
                    speed = idx / max(elapsed / 60, 0.01)
                    eta_min = max(0, (len(pages) - idx) / max(speed, 0.01))
                    status_icon = "[OK]" if not failed_pages else f"[WARN:{len(failed_pages)}]"
                    print(f'{self.symbol}: {status_icon} 进度 {idx}/{len(pages)} 页 ({pct:.0f}%) | '
                          f'成功 {completed_pages} 页 | 累计 {total_rows} 条 | '
                          f'耗时 {elapsed:.0f}s | 速度 {speed:.1f}页/分 | 预计剩余 {eta_min:.0f}分')
                    last_progress_at = idx

                if all_before:
                    boundary_page = min(boundary_page, max(0, page_num - 1))
                    print(f'{self.symbol}: [完成] 第 {page_num} 页全部早于 {start_date}，提前结束')
                    break
        finally:
            parser.close()
            if getattr(self, 'browser', None) is not None:
                self.browser.quit()

        end = time.time()
        time_cost = end - self.start
        summary = {
            'status': status,
            'max_page': max_page,
            'boundary_page': boundary_page,
            'completed_pages': completed_pages,
            'failed_pages': sorted(set(failed_pages)),
            'blocked_pages': sorted(set(blocked_pages)),
            'parse_failed_pages': [],
            'transient_failed_pages': sorted(set(transient_failed_pages)),
            'paused_reason': paused_reason,
            'retry_after_seconds': retry_after_seconds,
            'skipped_cached_pages': len([p for p in cached_pages if 1 <= p <= boundary_page]),
            'list_window_size': list_window_size,
            'list_source': 'html',
            'rows': total_rows,
            'unique_post_ids': len(seen_ids),
            'min_time': min_time or '',
            'max_time': max_time or '',
            'time_cost_seconds': round(time_cost, 2),
        }
        # Clear final summary
        print(f'\n{self.symbol}: {"="*50}')
        print(f'{self.symbol}: 全量列表爬取完成')
        print(f'{self.symbol}: {"="*50}')
        print(f'{self.symbol}:   状态: {status}')
        print(f'{self.symbol}:   总页数: {max_page} | 边界页: {boundary_page}')
        print(f'{self.symbol}:   成功: {completed_pages} 页 | 跳过缓存: {summary["skipped_cached_pages"]} 页')
        print(f'{self.symbol}:   帖子: {total_rows} 条 | 唯一ID: {summary["unique_post_ids"]}')
        if min_time and max_time:
            print(f'{self.symbol}:   时间范围: {min_time} ~ {max_time}')
        print(f'{self.symbol}:   总耗时: {time_cost:.0f}s ({time_cost/60:.1f}分)')
        if failed_pages:
            print(f'{self.symbol}:   [错误] 失败页: {sorted(set(failed_pages))} ({len(failed_pages)}页)')
        if blocked_pages:
            print(f'{self.symbol}:   [错误] 被限流页: {blocked_pages}')
        if transient_failed_pages:
            print(f'{self.symbol}:   [警告] 瞬时失败: {sorted(set(transient_failed_pages))} ({len(transient_failed_pages)}页)')
        if paused_reason:
            print(f'{self.symbol}:   [原因] {paused_reason}')
        if status == 'paused_blocked':
            print(f'{self.symbol}:   [提示] 请先运行 --manual-verify 完成验证，再重新执行 Stage 1 断点续爬')
        print(f'{self.symbol}: {"="*50}')
        return summary


    def crawl_post_info_since(
        self,
        start_date: str,
        storage_callback,
        list_workers: int = 6,
        checkpoint_callback=None,
        page_storage_callback=None,
        cached_pages: set[int] | None = None,
        boundary_page: int | None = None,
        list_window_size: int = 80,
        window_pause_range: tuple[float, float] = (3.0, 8.0),
        list_source: str = "html",
        api_page_size: int = 80,
        api_window_size: int = 60,
        api_concurrency: int = 3,
        target_stage1_minutes: int = 25,
        page_limit: int | None = None,
    ) -> dict:
        effective_source = 'selenium' if list_source == 'selenium' else 'html'
        if list_source in {'api', 'auto'}:
            print(f'{self.symbol}: list_source={list_source} uses fast requests HTML path')
        print(f'\n{self.symbol}: start full list crawl, start_date={start_date}, list_source={effective_source}')

        cached_pages = set(cached_pages or set())
        list_window_size = max(1, int(list_window_size or 80))
        list_workers = max(1, int(list_workers or 1))
        if effective_source == 'selenium':
            list_workers = 1

        try:
            max_page = self.get_page_num()
        except FullCrawlPaused as e:
            end = time.time()
            return {
                'status': 'paused_blocked',
                'max_page': 0,
                'boundary_page': 0,
                'completed_pages': 0,
                'failed_pages': [e.page_num],
                'blocked_pages': [e.page_num],
                'parse_failed_pages': [],
                'transient_failed_pages': [],
                'paused_reason': e.reason,
                'retry_after_seconds': e.retry_after_seconds,
                'skipped_cached_pages': 0,
                'list_window_size': list_window_size,
                'list_workers': list_workers,
                'list_source': effective_source,
                'partial': bool(page_limit),
                'page_limit': page_limit or 0,
                'rows': 0,
                'unique_post_ids': 0,
                'min_time': '',
                'max_time': '',
                'time_cost_seconds': round(end - self.start, 2),
            }
        print(f'{self.symbol}: max_page={max_page}')

        partial = bool(page_limit)
        if page_limit:
            boundary_page = min(max_page, max(1, int(page_limit)))
            print(f'{self.symbol}: trial page_limit={page_limit}, crawl pages 1~{boundary_page}')
        elif boundary_page is None:
            try:
                boundary_page = self._find_boundary_page_since(start_date, max_page, list_source=effective_source)
            except FullCrawlPaused as e:
                end = time.time()
                return {
                    'status': 'paused_blocked',
                    'max_page': max_page,
                    'boundary_page': 0,
                    'completed_pages': 0,
                    'failed_pages': [e.page_num],
                    'blocked_pages': [e.page_num],
                    'parse_failed_pages': [],
                    'transient_failed_pages': [],
                    'paused_reason': e.reason,
                    'retry_after_seconds': e.retry_after_seconds,
                    'skipped_cached_pages': 0,
                    'list_window_size': list_window_size,
                    'list_workers': list_workers,
                    'list_source': effective_source,
                    'partial': partial,
                    'page_limit': page_limit or 0,
                    'rows': 0,
                    'unique_post_ids': 0,
                    'min_time': '',
                    'max_time': '',
                    'time_cost_seconds': round(end - self.start, 2),
                }
        else:
            boundary_page = min(max(0, int(boundary_page)), max_page)
            print(f'{self.symbol}: reuse boundary_page={boundary_page}')

        if not boundary_page:
            end = time.time()
            return {
                'status': 'paused_blocked',
                'max_page': max_page,
                'boundary_page': 0,
                'completed_pages': 0,
                'failed_pages': [1],
                'blocked_pages': [1],
                'parse_failed_pages': [],
                'transient_failed_pages': [],
                'paused_reason': 'boundary detection failed',
                'retry_after_seconds': 3600,
                'skipped_cached_pages': 0,
                'list_window_size': list_window_size,
                'list_workers': list_workers,
                'list_source': effective_source,
                'partial': partial,
                'page_limit': page_limit or 0,
                'rows': 0,
                'unique_post_ids': 0,
                'min_time': '',
                'max_time': '',
                'time_cost_seconds': round(end - self.start, 2),
            }

        print(f'{self.symbol}: crawl page range 1~{boundary_page}')

        completed_pages = len([p for p in cached_pages if 1 <= p <= boundary_page])
        failed_pages: set[int] = set()
        blocked_pages: set[int] = set()
        transient_failed_pages: set[int] = set()
        seen_ids: set[str] = set()
        total_rows = 0
        min_time = None
        max_time = None
        pages = [p for p in range(1, boundary_page + 1) if p not in cached_pages]
        total_to_fetch = len(pages)
        thread_local = threading.local()
        base_cookies = self.session.cookies.get_dict()

        def session_for_thread() -> requests.Session:
            session = getattr(thread_local, 'session', None)
            if session is None:
                session = self._new_list_session(base_cookies)
                thread_local.session = session
            return session

        def fetch_job(page_num: int, allow_fallback: bool = False) -> dict:
            try:
                time.sleep(random.uniform(0.02, 0.18))
                parser = PostParser() if effective_source == 'selenium' else None
                try:
                    dic_list = self._fetch_page_with_retry(
                        page_num,
                        parser=parser,
                        stockbar_name=f'{self.symbol}吧',
                        list_source=effective_source,
                        session=session_for_thread(),
                        allow_selenium_fallback=allow_fallback,
                    )
                finally:
                    if parser is not None:
                        parser.close()
                kept = [
                    d for d in dic_list
                    if d.get('post_date') and d['post_date'] >= start_date
                ]
                all_before = bool(dic_list) and all(
                    d.get('post_date', '') < start_date
                    for d in dic_list
                    if d.get('post_date')
                )
                if not kept and not all_before:
                    raise RuntimeError('empty parsed page')
                return {'page': page_num, 'ok': True, 'kept': kept, 'all_before': all_before}
            except Exception as e:
                return {
                    'page': page_num,
                    'ok': False,
                    'kept': [],
                    'all_before': False,
                    'blocked': _looks_like_blocked_error(e),
                    'error': f'{type(e).__name__}: {e}',
                }

        def record_success(page_num: int, kept: list, all_before: bool):
            nonlocal completed_pages, total_rows, min_time, max_time, boundary_page
            failed_pages.discard(page_num)
            blocked_pages.discard(page_num)
            transient_failed_pages.discard(page_num)
            unique_kept = []
            for d in kept:
                pid = str(d.get('_id', ''))
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    unique_kept.append(d)
            if unique_kept:
                if storage_callback:
                    storage_callback(unique_kept)
                if page_storage_callback:
                    page_storage_callback(page_num, unique_kept, {
                        'status': 'ok',
                        'list_source': effective_source,
                    })
                total_rows += len(unique_kept)
                dates = [d['post_date'] for d in unique_kept if d.get('post_date')]
                if dates:
                    local_min = min(dates)
                    local_max = max(dates)
                    if min_time is None or local_min < min_time:
                        min_time = local_min
                    if max_time is None or local_max > max_time:
                        max_time = local_max
            completed_pages += 1
            if checkpoint_callback:
                checkpoint_callback(page_num, {'kept': len(kept), 'all_before': all_before})
            if all_before and not partial:
                boundary_page = min(boundary_page, max(0, page_num - 1))

        def run_pass(page_list: list[int], workers: int, label: str, allow_fallback: bool = False) -> set[int]:
            if not page_list:
                return set()
            workers = max(1, min(workers, len(page_list)))
            print(f'{self.symbol}: {label}, pages={len(page_list)}, workers={workers}')
            pass_failed: set[int] = set()
            fetched_in_pass = 0
            for start in range(0, len(page_list), list_window_size):
                window = page_list[start:start + list_window_size]
                if effective_source == 'selenium':
                    workers = 1
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(fetch_job, page_num, allow_fallback): page_num
                        for page_num in window
                    }
                    for future in as_completed(futures):
                        result = future.result()
                        page_num = result['page']
                        fetched_in_pass += 1
                        if result.get('ok'):
                            record_success(page_num, result['kept'], result['all_before'])
                        else:
                            failed_pages.add(page_num)
                            pass_failed.add(page_num)
                            if result.get('blocked'):
                                blocked_pages.add(page_num)
                            else:
                                transient_failed_pages.add(page_num)
                            if len(pass_failed) <= 5:
                                print(f'{self.symbol}: page {page_num} failed in {label}: {result.get("error")}')

                elapsed = time.time() - self.start
                done_now = completed_pages - len([p for p in cached_pages if 1 <= p <= boundary_page])
                speed = max(done_now, 1) / max(elapsed / 60, 0.01)
                eta_min = max(0, (total_to_fetch - done_now) / max(speed, 0.01))
                status_icon = '[OK]' if not failed_pages else f'[WARN:{len(failed_pages)}]'
                print(f'{self.symbol}: {status_icon} progress {completed_pages}/{boundary_page} pages | '
                      f'new_rows {total_rows} | elapsed {elapsed:.0f}s | speed {speed:.1f} pages/min | '
                      f'eta {eta_min:.0f} min')

                if start + list_window_size < len(page_list):
                    low, high = window_pause_range
                    time.sleep(random.uniform(max(0, low), max(low, high)))
            return pass_failed

        first_failed = run_pass(pages, list_workers, 'pass1-fast', allow_fallback=False)
        if first_failed:
            retry_workers = max(1, list_workers // 2)
            second_failed = run_pass(sorted(first_failed), retry_workers, 'retry-lower-concurrency', allow_fallback=False)
        else:
            second_failed = set()
        if second_failed:
            run_pass(sorted(second_failed), 1, 'retry-single-with-fallback', allow_fallback=True)

        status = 'success'
        paused_reason = ''
        retry_after_seconds = 0
        if blocked_pages:
            status = 'paused_blocked'
            paused_reason = 'blocked by validation after retries'
            retry_after_seconds = 3600
        elif failed_pages:
            status = 'partial_failed'
            paused_reason = 'some pages failed after retries'

        if getattr(self, 'browser', None) is not None:
            try:
                self.browser.quit()
            except Exception:
                pass

        end = time.time()
        time_cost = end - self.start
        summary = {
            'status': status,
            'max_page': max_page,
            'boundary_page': boundary_page,
            'completed_pages': completed_pages,
            'failed_pages': sorted(failed_pages),
            'blocked_pages': sorted(blocked_pages),
            'parse_failed_pages': [],
            'transient_failed_pages': sorted(transient_failed_pages),
            'paused_reason': paused_reason,
            'retry_after_seconds': retry_after_seconds,
            'skipped_cached_pages': len([p for p in cached_pages if 1 <= p <= boundary_page]),
            'list_window_size': list_window_size,
            'list_workers': list_workers,
            'list_source': effective_source,
            'partial': partial,
            'page_limit': page_limit or 0,
            'rows': total_rows,
            'unique_post_ids': len(seen_ids),
            'min_time': min_time or '',
            'max_time': max_time or '',
            'time_cost_seconds': round(time_cost, 2),
        }
        print(f'\n{self.symbol}: full list crawl done, status={status}, '
              f'completed={completed_pages}, failed={summary["failed_pages"]}, '
              f'blocked={summary["blocked_pages"]}, rows={total_rows}, seconds={time_cost:.1f}')
        return summary


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
