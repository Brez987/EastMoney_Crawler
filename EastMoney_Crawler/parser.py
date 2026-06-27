from selenium.webdriver.common.by import By
from datetime import datetime
import re
import json
import hashlib
import requests
import time
from bs4 import BeautifulSoup

from browser_utils import create_stealth_chrome


class PostParser(object):

    def __init__(self):
        self.year = None
        self.month = 13
        self.id = 0
        self.detail_browser = None

    def get_detail_browser(self):
        if self.detail_browser is None:
            self.detail_browser = create_stealth_chrome()
        return self.detail_browser

    def restart_detail_browser(self):
        """重启详情页浏览器，用于应对验证页拦截"""
        if self.detail_browser is not None:
            try:
                self.detail_browser.quit()
            except Exception:
                pass
        self.detail_browser = None
        return self.get_detail_browser()

    def close(self):
        if self.detail_browser is not None:
            self.detail_browser.quit()
            self.detail_browser = None

    @staticmethod
    def parse_post_title(html):
        title_element = html.find_element(By.CSS_SELECTOR, 'td:nth-child(3) > div')
        return title_element.text

    @staticmethod
    def parse_post_view(html):
        view_element = html.find_element(By.CSS_SELECTOR, 'td > div')
        return view_element.text  # stay as str structure! as character like '万' exist

    @staticmethod
    def parse_comment_num(html):
        num_element = html.find_element(By.CSS_SELECTOR, 'td:nth-child(2) > div')
        try:
            comment_num = int(num_element.text)  # be converted to int
        except:
            comment_num = int(float(num_element.text[:-1]) * 10000)  # 有时评论个数会过'万'
        return comment_num

    @staticmethod
    def parse_post_url(html):
        url_element = html.find_element(By.CSS_SELECTOR, 'td:nth-child(3) > div > a')
        return url_element.get_attribute('href')

    @staticmethod
    def remove_char(date_str):
        # 使用正则表达式去掉所有汉字字符（处理日期中包含“修改”字符的情况）
        cleaned_str = re.sub(r'[^\d\s:-]', '', date_str)
        return cleaned_str.strip()

    def get_post_year(self, html):
        post_url = self.parse_post_url(html)
        driver = self.get_detail_browser()

        if 'guba.eastmoney.com' in post_url:  # 这是绝大部分的普通帖子
            driver.get(post_url)
            date_str = driver.find_element(By.CSS_SELECTOR, 'div.newsauthor > div.author-info.cl > div.time').text
            self.year = int(self.remove_char(date_str)[:4])
        elif 'caifuhao.eastmoney.com' in post_url:  # 有些热榜帖子会占据第一位，对于这种情况要特殊处理
            driver.get(post_url)
            date_str = driver.find_element(By.CSS_SELECTOR, 'div.article.page-article > div.article-head > '
                                                            'div.article-meta > span.txt').text
            self.year = int(self.remove_char(date_str)[:4])
        else:
            self.year = datetime.now().year

    def _try_api_guba_detail(self, post_url: str, driver) -> dict:
        """方向A：通过浏览器内 JS fetch 获取股吧帖子详情（快 10x）

        不通过 Selenium navigation（driver.get），而是在浏览器中直接执行
        JS fetch 请求帖子页面 HTML，用 DOMParser 解析正文。
        避免了页面渲染、JS 执行、资源加载的开销。

        参考 missing_year_backfill.py 的 API 模式。
        """
        result = {
            'post_content': '',
            'post_title': '',
            'post_date': '',
            'post_time': '',
            'post_author': ''
        }
        try:
            # 通过 JS fetch 获取页面 HTML（复用浏览器 cookies，不触发 navigation 事件）
            script = r"""
            const postUrl = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(postUrl, {
                headers: {'User-Agent': navigator.userAgent},
                credentials: 'include'
            })
            .then(r => r.text())
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                
                // 检查是否被重定向到验证页
                if (doc.title === '验证' || 
                    (doc.title && doc.title.includes('验证') && !doc.title.includes('东方财富'))) {
                    done(JSON.stringify({_validate: true}));
                    return;
                }
                
                // 正文（class 为 newstext）
                const content = doc.querySelector('div.newstext');
                // 标题从 <title> 标签提取（格式: "标题_股票名(代码)股吧_东方财富网股吧"）
                let title = '';
                if (doc.title) {
                    const idx = doc.title.indexOf('_');
                    if (idx > 0) title = doc.title.substring(0, idx);
                }
                // 作者和时间从 body 文本提取
                const bodyText = (doc.body || doc).textContent || '';
                const metaMatch = bodyText.match(/([^\s]{2,30})\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})/);
                let author = '', time = '';
                 if (metaMatch) {
                     author = metaMatch[1].replace(/^[>\s]+/, '');
                     time = metaMatch[2];
                 }
                
                done(JSON.stringify({
                    content: content ? content.textContent.trim() : '',
                    title: title,
                    author: author,
                    time: time,
                }));
            })
            .catch(() => done('{}'));
            """
            result_json = driver.execute_async_script(script, post_url)
            data = json.loads(result_json)

            # 检查是否被重定向到验证页
            if data.get('_validate'):
                return result

            if data.get('content'):
                result['post_content'] = data['content']
            if data.get('title'):
                result['post_title'] = data['title']
            if data.get('time'):
                date_str = self.remove_char(data['time'])
                if len(date_str) >= 16:
                    result['post_date'] = date_str[:10]
                    result['post_time'] = date_str[11:16]
            if data.get('author'):
                result['post_author'] = data['author']

        except Exception:
            pass

        return result

    @staticmethod
    def _looks_like_blocked_html(text: str, url: str = '') -> bool:
        if 'validate' in (url or '').lower() or 'fd_guba_validate' in (url or ''):
            return True
        if not text:
            return True
        return any(
            marker in text
            for marker in (
                'fd_guba_validate',
                '身份核实',
                '请完成安全验证',
                '请输入验证码',
                '<title>验证</title>',
                'HTTP ERROR 403',
                '请求遭到拒绝',
                '未获授权',
            )
        )

    @staticmethod
    def _looks_like_invalid_caifuhao(text: str) -> bool:
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                '文章不存在',
                '内容不存在',
                '该文章已删除',
                '该内容已删除',
                '页面不存在',
                '404',
            )
        )

    @staticmethod
    def _parse_json_or_jsonp(text: str) -> dict:
        text = (text or '').strip()
        if not text:
            return {}
        match = re.match(r'^[\w$]+\((.*)\)\s*;?\s*$', text, flags=re.S)
        if match:
            text = match.group(1)
        return json.loads(text)

    def _clean_caifuhao_api_content(self, raw_content: str) -> str:
        if not raw_content:
            return ''
        soup = BeautifulSoup(raw_content, 'html.parser')
        for node in soup(['script', 'style']):
            node.decompose()
        text = soup.get_text('\n', strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return self.clean_caifuhao_content('\n'.join(lines))

    def _try_wap_caifuhao(
        self,
        post_id: str = '',
        source_id: str = '',
        post_url: str = '',
        session=None,
        proxies=None,
        timeout=(5, 15),
    ) -> dict:
        """Fetch caifuhao article content from the WAP gbapi endpoint.

        The PC article domain can return 403 while the WAP page loads content
        through gbapi. For full-mode CSV rows, post_id is the guba mirror id and
        source_id is the caifuhao news id from /news/{source_id}.
        """
        result = {
            'ok': False,
            'post_content': '',
            'post_title': '',
            'post_date': '',
            'post_time': '',
            'post_author': '',
            'http_status': 0,
            'reason': 'not_started',
        }

        source_id = str(source_id or '').strip()
        post_id = str(post_id or '').strip()
        if not source_id and post_url:
            match = re.search(r'/news/(\d+)', post_url)
            if match:
                source_id = match.group(1)
        if not source_id:
            result['reason'] = 'missing_source_id'
            return result

        fetcher = session if session is not None else requests
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) '
                'Version/17.0 Mobile/15E148 Safari/604.1'
            ),
            'Accept': 'application/json,text/javascript,*/*;q=0.01',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Origin': 'https://wap.eastmoney.com',
            'Referer': f'https://wap.eastmoney.com/a/{source_id}.html',
        }
        base_params = {
            'deviceid': '0d2798cab1716439a343c9965c20c59d',
            'version': '2',
            'product': 'eastmoney',
            'plat': 'wap',
        }

        try:
            if not post_id or not post_id.isdigit() or post_id == source_id:
                brief_params = dict(
                    base_params,
                    postid=source_id,
                    type='1',
                    callback='callback',
                    _=str(int(time.time() * 1000)),
                )
                brief_resp = fetcher.get(
                    'https://gbapi.eastmoney.com/abstract/api/PostShort/ArticleBriefInfo',
                    params=brief_params,
                    headers=headers,
                    timeout=timeout,
                    proxies=proxies,
                )
                result['http_status'] = getattr(brief_resp, 'status_code', 0) or 0
                if brief_resp.status_code in (403, 429):
                    result['reason'] = f'http_{brief_resp.status_code}'
                    return result
                if brief_resp.status_code in (404, 410):
                    result['reason'] = f'http_{brief_resp.status_code}'
                    return result
                if brief_resp.status_code != 200:
                    result['reason'] = f'http_{brief_resp.status_code}'
                    return result

                brief_data = self._parse_json_or_jsonp(brief_resp.text)
                brief_item = ((brief_data.get('re') or [{}])[0] or {})
                mapped_post_id = str(brief_item.get('post_id') or '').strip()
                if mapped_post_id and mapped_post_id.isdigit() and mapped_post_id != '0':
                    post_id = mapped_post_id
                else:
                    result['reason'] = 'missing_post_id'
                    return result

            content_params = dict(
                base_params,
                postid=post_id,
                newsid=source_id,
                pi='',
                ctoken='',
                utoken='',
                IsClick='false',
                _=str(int(time.time() * 1000)),
            )
            resp = fetcher.get(
                'https://gbapi.eastmoney.com/content/api/Post/ArticleContent',
                params=content_params,
                headers=headers,
                timeout=timeout,
                proxies=proxies,
            )
            result['http_status'] = getattr(resp, 'status_code', 0) or 0
            if resp.status_code in (403, 429):
                result['reason'] = f'http_{resp.status_code}'
                return result
            if resp.status_code in (404, 410):
                result['reason'] = f'http_{resp.status_code}'
                return result
            if resp.status_code != 200:
                result['reason'] = f'http_{resp.status_code}'
                return result

            data = self._parse_json_or_jsonp(resp.text)
            post = data.get('post') or {}
            if not data.get('rc') or not post:
                message = str(data.get('me') or data.get('message') or '')
                if any(marker in message for marker in ('不存在', '已删除', '删除', '参数错误')):
                    result['reason'] = 'invalid_article'
                elif '系统繁忙' in message:
                    result['reason'] = 'wap_system_busy'
                else:
                    result['reason'] = 'wap_api_empty'
                return result

            raw_content = post.get('post_content') or post.get('post_abstract') or ''
            result['post_content'] = self._clean_caifuhao_api_content(raw_content)
            result['post_title'] = str(post.get('post_title') or '').strip()

            publish_time = str(
                post.get('post_publish_time')
                or post.get('post_display_time')
                or post.get('post_last_time')
                or ''
            )
            date_str = self.remove_char(publish_time)
            if len(date_str) >= 16:
                result['post_date'] = date_str[:10]
                result['post_time'] = date_str[11:16]

            user = post.get('post_user') or {}
            result['post_author'] = str(
                user.get('user_nickname')
                or user.get('user_name')
                or ''
            ).strip()

            if result['post_content']:
                result['ok'] = True
                result['reason'] = 'ok'
            else:
                result['reason'] = 'body_not_found'

        except Exception as e:
            err_type = type(e).__name__
            if 'Timeout' in err_type:
                result['reason'] = 'timeout'
            else:
                result['reason'] = f'exception:{err_type}'

        return result

    def _try_requests_caifuhao(
        self,
        post_url: str,
        session=None,
        proxies=None,
        timeout=(5, 15),
    ) -> dict:
        """用 HTTP 快速获取财富号正文，并返回可判定的结构化结果。"""
        result = {
            'ok': False,
            'post_content': '',
            'post_title': '',
            'post_date': '',
            'post_time': '',
            'post_author': '',
            'http_status': 0,
            'reason': 'not_started',
        }
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/131.0.0.0 Safari/537.36',
                'Accept': (
                    'text/html,application/xhtml+xml,application/xml;q=0.9,'
                    'image/avif,image/webp,image/apng,*/*;q=0.8'
                ),
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Referer': 'https://guba.eastmoney.com/',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'same-site',
                'Sec-Fetch-User': '?1',
            }
            fetcher = session if session is not None else requests
            resp = fetcher.get(post_url, headers=headers, timeout=timeout, proxies=proxies)
            result['http_status'] = getattr(resp, 'status_code', 0) or 0

            if resp.status_code in (403, 429):
                result['reason'] = f'http_{resp.status_code}'
                return result
            if resp.status_code in (404, 410):
                result['reason'] = f'http_{resp.status_code}'
                return result
            if resp.status_code != 200:
                result['reason'] = f'http_{resp.status_code}'
                return result

            final_url = str(getattr(resp, 'url', '') or '')
            if 'roadshow.eastmoney.com' in final_url:
                result['reason'] = 'invalid_article'
                return result

            text = resp.text or ''
            if self._looks_like_blocked_html(text[:8000], final_url):
                result['reason'] = 'blocked_validation'
                return result
            if self._looks_like_invalid_caifuhao(text[:8000]):
                result['reason'] = 'invalid_article'
                return result

            soup = BeautifulSoup(text, 'html.parser')

            # 提取正文：财富号历史页面有多套模板，按常见容器逐级兼容。
            body = soup.select_one(
                'div.article-body, div.articleContent, div#ContentBody, '
                'div.newsContent, div.newstext, article'
            )
            if body:
                paragraphs = body.find_all(['p', 'div', 'section'])
                content_parts = []
                for p in paragraphs:
                    text = p.get_text(strip=True)
                    if text and len(text) > 5:
                        content_parts.append(text)
                result['post_content'] = '\n'.join(content_parts)

            # 提取标题
            title_el = soup.select_one('h1.article-title, div.article-title, h1.title, h1')
            if title_el:
                result['post_title'] = title_el.get_text(strip=True)

            # 提取时间/作者
            meta = soup.select_one('div.article-meta, div.article-head, div.newsauthor, div.author-info')
            if meta:
                meta_text = meta.get_text(strip=True)
                date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})', meta_text)
                if date_match:
                    result['post_date'] = date_match.group(1)[:10]
                    result['post_time'] = date_match.group(1)[11:]
                author_el = meta.select_one('a.author_name, a[href*="caifuhao"], a')
                if author_el:
                    result['post_author'] = author_el.get_text(strip=True)

            # 清洗内容
            if result['post_content']:
                result['post_content'] = self.clean_caifuhao_content(result['post_content'])
                result['ok'] = True
                result['reason'] = 'ok'
            else:
                result['reason'] = 'body_not_found'

        except Exception as e:
            err_type = type(e).__name__
            if 'Timeout' in err_type:
                result['reason'] = 'timeout'
            else:
                result['reason'] = f'exception:{err_type}'

        return result
    def parse_post_detail(self, post_url: str, retry: bool = True) -> dict:
        """访问帖子详情页，提取完整正文内容
        
        支持两种页面类型：
        1. 原生长文帖子: guba.eastmoney.com/news,...
        2. 财富号转发布长文: caifuhao.eastmoney.com/news/...
        
        方向6优化：财富号帖子优先使用 requests 获取（快5-10倍），
        失败后回退到 Selenium。

        Args:
            post_url: 帖子详情页 URL
            retry: 被拦截时是否尝试重启浏览器重试
        """
        import time

        result = {
            'post_content': '',
            'post_title': '',
            'post_date': '',
            'post_time': '',
            'post_author': ''
        }

        # 方向6：财富号帖子优先用 requests 直接获取
        if 'caifuhao.eastmoney.com' in post_url:
            req_result = self._try_requests_caifuhao(post_url)
            if req_result and req_result.get('post_content'):
                return req_result

        # 方向A：股吧原生帖子优先用浏览器内 JS fetch 获取（快 100x，不触发 navigation）
        if 'guba.eastmoney.com' in post_url:
            driver = self.get_detail_browser()
            api_result = self._try_api_guba_detail(post_url, driver)
            if api_result and api_result.get('post_content'):
                return api_result
            # JS fetch 返回空 = 帖子可能已被删除，Selenium 也无法获取
            # 直接返回空结果，避免浪费时间去 Selenium 渲染
            if api_result:
                return api_result

        driver = self.get_detail_browser()
        driver.get(post_url)
        
        # 等待页面加载完成（给 JavaScript 重定向足够的时间）
        time.sleep(3)

        # 检查是否被重定向到验证页
        # 注意：不能只用 '验证' 判断，因为正常帖子标题可能包含"验证"二字
        is_validate_page = (
            'fd_guba_validate' in driver.current_url or
            driver.title == '验证' or
            '身份核实' in driver.title or
            ('验证' in driver.title and '东方财富' not in driver.title)
        )
        
        if is_validate_page:
            if retry:
                print(f'警告: 页面被重定向到验证页，尝试重启浏览器: {post_url}')
                self.restart_detail_browser()
                time.sleep(10)  # 冷却10秒（平衡反爬与速度）
                # 重新获取新浏览器实例并访问
                driver = self.get_detail_browser()
                driver.get(post_url)
                time.sleep(3)
                # 再次检查
                is_validate_page_retry = (
                    'fd_guba_validate' in driver.current_url or
                    driver.title == '验证' or
                    '身份核实' in driver.title or
                    ('验证' in driver.title and '东方财富' not in driver.title)
                )
                if is_validate_page_retry:
                    print(f'警告: 页面被重定向到验证页，无法提取内容: {post_url}')
                    return result
            else:
                print(f'警告: 页面被重定向到验证页，无法提取内容: {post_url}')
                return result

        if 'guba.eastmoney.com' in post_url:
            result = self._parse_guba_detail(driver, result)
        elif 'caifuhao.eastmoney.com' in post_url:
            result = self._parse_caifuhao_detail(driver, result)

        return result

    def _parse_guba_detail(self, driver, result: dict) -> dict:
        """解析股吧原生帖子详情页"""
        try:
            # 正文内容
            content_el = driver.find_element(By.CSS_SELECTOR, 'div.newsContent')
            result['post_content'] = content_el.text.strip()
        except:
            pass

        try:
            # 标题
            title_el = driver.find_element(By.CSS_SELECTOR, 'h1.title')
            result['post_title'] = title_el.text.strip()
        except:
            pass

        try:
            # 时间
            time_el = driver.find_element(By.CSS_SELECTOR, 'div.newsauthor > div.author-info.cl > div.time')
            date_str = self.remove_char(time_el.text)
            if len(date_str) >= 16:
                result['post_date'] = date_str[:10]
                result['post_time'] = date_str[11:16]
        except:
            pass

        try:
            # 作者
            author_el = driver.find_element(By.CSS_SELECTOR, 'div.newsauthor a.author_name')
            result['post_author'] = author_el.text.strip()
        except:
            pass

        return result

    @staticmethod
    def clean_caifuhao_content(text: str) -> str:
        """清洗财富号帖子正文，删除广告、Markdown 标记和重复内容"""
        if not text:
            return text

        # 删除开头的广告文案（多种变体）
        ad_patterns = [
            r'^在东方财富看资讯行情，选东方财富证券一站式开户交易>>\s*',
            r'^四大权益礼包，开户即送\s*',
            r'^炒股第一步，先开个股票账户\s*',
        ]
        for pattern in ad_patterns:
            text = re.sub(pattern, '', text, flags=re.MULTILINE)

        # 删除文中/文末的广告文案
        text = re.sub(r'想炒股，先开户！选东方财富证券，行情交易一个APP搞定>>\s*', '', text, flags=re.MULTILINE)

        # 删除开头的风险提示（如果出现）
        risk_pattern = r'^风险提示：内容仅为产业逻辑梳理，不构成股票投资建议，股市有风险，投资需谨慎\s*'
        text = re.sub(risk_pattern, '', text, flags=re.MULTILINE)

        # 删除结尾的广告文案
        text = re.sub(r'恭喜解锁.*$', '', text, flags=re.MULTILINE)
        text = re.sub(r'股市如棋局，开户先布局，随时把握投资机遇！\s*$', '', text, flags=re.MULTILINE)

        # 删除文末的风险提示（另一种变体）
        text = re.sub(r'风险提示：本文所提到的观点仅代表个人的意见，所涉及标的不作推荐，据此买卖，风险自负。\s*$', '', text, flags=re.MULTILINE)

        # 清洗 Markdown/HTML 标记
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)       # **bold** → bold
        text = re.sub(r'__(.+?)__', r'\1', text)            # __bold__
        text = re.sub(r'~~(.+?)~~', r'\1', text)            # ~~strikethrough~~
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)  # # headers
        text = re.sub(r'^-{3,}\s*$', '', text, flags=re.MULTILINE)  # --- separator

        # 去除重复段落（同一段内容出现两次时保留第一次）
        paragraphs = text.split('\n')
        seen = set()
        deduped = []
        for p in paragraphs:
            p_clean = p.strip()
            # 去除 markdown 标记后再判断重复
            p_key = re.sub(r'\*{1,3}', '', p_clean).strip()
            if p_key and p_key not in seen:
                seen.add(p_key)
                deduped.append(p_clean)
        text = '\n'.join(deduped)

        return text.strip()

    def _parse_caifuhao_detail(self, driver, result: dict) -> dict:
        """解析财富号转发布帖子详情页"""
        try:
            # 正文内容 - 使用 div.article-body（content 可能在更深层）
            content_el = driver.find_element(By.CSS_SELECTOR, 'div.article-body')
            raw_content = content_el.text.strip()
            result['post_content'] = self.clean_caifuhao_content(raw_content)
        except:
            pass

        try:
            # 标题 - 使用 h1.article-title
            title_el = driver.find_element(By.CSS_SELECTOR, 'h1.article-title')
            result['post_title'] = title_el.text.strip()
        except:
            pass

        try:
            # 时间 - 处理 "修改于 2026年06月06日 08:07" 格式
            time_el = driver.find_element(By.CSS_SELECTOR, 'div.article-meta > span.txt')
            date_str = time_el.text.strip()
            # 提取日期时间：将 "修改于 2026年06月06日 08:07" 转换为标准格式
            match = re.search(r'(\d{4})年(\d{2})月(\d{2})日\s+(\d{2}:\d{2})', date_str)
            if match:
                result['post_date'] = f'{match.group(1)}-{match.group(2)}-{match.group(3)}'
                result['post_time'] = match.group(4)
        except:
            pass

        try:
            # 作者 - 从 article-meta 中提取
            meta_el = driver.find_element(By.CSS_SELECTOR, 'div.article-meta')
            meta_text = meta_el.text.strip()
            # 优先匹配 "来自专栏 作者名" 或 "来自 作者名"
            match = re.search(r'来自(?:专栏)?\s+(\S+)', meta_text)
            if match:
                result['post_author'] = match.group(1)
            else:
                # 回退：匹配 "作者名 YYYY年MM月DD日" 格式（作者名在日期前面）
                match = re.search(r'^(\S+?)\s+\d{4}年\d{2}月\d{2}日', meta_text)
                if match:
                    result['post_author'] = match.group(1)
        except:
            pass

        return result

    @staticmethod
    def judge_post_date(html):  # eastmoney has several fucking inaccurate display dates
        try:
            judge_element = html.find_element(By.CSS_SELECTOR, 'td:nth-child(3) > div > span')
            if judge_element.text == '问董秘':  # is not None
                return False
        except:
            return True

    def parse_post_date(self, html):
        try:
            time_element = html.find_element(By.CSS_SELECTOR, 'div.update.pub_time')
            time_str = time_element.text
            month, day = map(int, time_str.split(' ')[0].split('-'))
        except Exception as e:  # some post is different, just ignore it (very seldom)
            print('Fail to find the date of the post.', '\n', '{}'.format(e))
            return None, None

        if self.judge_post_date(html):
            if self.month < month == 12:
                self.year -= 1
            self.month = month

        if self.year is None:  # get the post year through exact post_url
            self.get_post_year(html)

        date = f'{self.year}-{month:02d}-{day:02d}'
        time = time_str.split(' ')[1]
        return date, time

    @staticmethod
    def parse_post_author(html):
        author_element = html.find_element(By.CSS_SELECTOR, 'td:nth-child(4) > div')
        return author_element.text

    def parse_post_info(self, html, stockbar_name: str = ''):
        title = self.parse_post_title(html)
        view = self.parse_post_view(html)
        num = self.parse_comment_num(html)
        url = self.parse_post_url(html)
        date, time = self.parse_post_date(html)
        author = self.parse_post_author(html)
        user_id = self._parse_user_id(html)
        forward = self._parse_forward(html)
        data_post_id = ''
        post_type = ''
        try:
            url_element = html.find_element(By.CSS_SELECTOR, 'td:nth-child(3) > div > a')
            data_post_id = str(url_element.get_attribute('data-postid') or '')
            post_type = str(url_element.get_attribute('data-posttype') or '')
        except Exception:
            pass
        # 提取 _id：统一使用字符串类型，避免 MongoDB int64 溢出
        # 股吧原生帖子：从 news,xxx,数字.html 中提取数字
        # 财富号帖子：_id 使用股吧 post_id，post_source_id 使用 URL 中的财富号文章号
        post_source_id = ''
        m = re.search(r'news,[^,]+,(\d+)\.html', url)
        if m:
            _id = m.group(1)  # 字符串类型
        else:
            m_caifuhao = re.search(r'/news/(\d+)', url)
            if m_caifuhao:
                post_source_id = m_caifuhao.group(1)
                _id = data_post_id or post_source_id
            else:
                # 兜底：使用 MD5 哈希前16位
                _id = hashlib.md5(url.encode()).hexdigest()[:16]
        post_info = {
            '_id': _id,
            'post_source_id': post_source_id,
            'post_type': post_type,
            'post_title': title,
            'post_view': view,
            'comment_num': num,
            'post_url': url,
            'post_date': date,
            'post_time': time,
            'post_author': author,
            'user_id': user_id,
            'stockbar_name': stockbar_name,
            'forward': forward,
        }
        return post_info

    @staticmethod
    def _parse_user_id(html) -> str:
        """从作者链接中提取用户数字ID"""
        try:
            author_el = html.find_element(By.CSS_SELECTOR, 'td:nth-child(4) a')
            href = author_el.get_attribute('href') or ''
            # href 格式: https://iguba.eastmoney.com/uid_xxxxx 或含 userid 参数
            m = re.search(r'uid[=_](\d+)', href)
            if m:
                return m.group(1)
        except Exception:
            pass
        return ''

    @staticmethod
    def _parse_forward(html) -> str:
        """提取帖子的转发数（整数）"""
        try:
            # 方案1：尝试从转发数所在的 span/em 标签提取
            for selector in [
                'td:nth-child(5) > div',       # 可能在第5列
                'td:nth-child(5)',
                'span.forward_num',
                'em.forward',
                'td.l3 > span',
                'td.l3',
            ]:
                try:
                    el = html.find_element(By.CSS_SELECTOR, selector)
                    text = el.text.strip()
                    if text and text.isdigit():
                        return text
                    # 处理"1.2万"格式
                    if text and '万' in text:
                        num = text.replace('万', '')
                        try:
                            return str(int(float(num) * 10000))
                        except Exception:
                            pass
                except Exception:
                    continue
        except Exception:
            pass
        return '0'


class CommentParser(object):

    @staticmethod
    def judge_sub_comment(html):  # identify whether it has sub-comments
        sub = html.find_elements(By.CSS_SELECTOR, 'ul.replyListL2')  # must use '_elements' instead of '_element'
        return bool(sub)  # if not null return True, vice versa, return False

    @staticmethod
    def parse_comment_content(html, sub_bool):
        if sub_bool:  # situation to deal with sub-comments
            content_element = html.find_element(By.CSS_SELECTOR, 'div.reply_title > span')
        else:
            content_element = html.find_element(By.CSS_SELECTOR, 'div.recont_right.fl > div.reply_title > span')
        return content_element.text

    @staticmethod
    def parse_comment_like(html, sub_bool):
        if sub_bool:  # situation to deal with sub-comments
            like_element = html.find_element(By.CSS_SELECTOR, 'span.likemodule')
        else:
            like_element = html.find_element(By.CSS_SELECTOR, 'ul.bottomright > li:nth-child(4) > span')

        if like_element.text == '点赞':  # website display text instead of '0'
            return 0
        else:
            return int(like_element.text)

    @staticmethod
    def parse_comment_date(html, sub_bool):
        if sub_bool:  # situation to deal with sub-comments
            date_element = html.find_element(By.CSS_SELECTOR, 'span.pubtime')
        else:
            date_element = html.find_element(By.CSS_SELECTOR, 'div.publishtime > span.pubtime')
        date_str = date_element.text
        date = date_str.split(' ')[0]
        time = date_str.split(' ')[1][:5]
        return date, time

    def parse_comment_info(self, html, post_id, sub_bool: bool = False):  # sub_pool is used to distinguish sub-comments
        content = self.parse_comment_content(html, sub_bool)
        like = self.parse_comment_like(html, sub_bool)
        date, time = self.parse_comment_date(html, sub_bool)
        whether_subcomment = int(sub_bool)  # '1' means it is sub-comment, '0' means it is not
        comment_info = {
            'post_id': post_id,
            'comment_content': content,
            'comment_like': like,
            'comment_date': date,
            'comment_time': time,
            'sub_comment': whether_subcomment,
        }
        return comment_info
