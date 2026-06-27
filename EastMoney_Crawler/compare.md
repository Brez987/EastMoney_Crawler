# Stage 2 财富号正文爬取优化方案（改良落地版）

## 0. Codex 改良版结论

> 2026-06-26 实测修订：最终主路径改为 `wap.eastmoney.com/a/{post_source_id}.html`
> 页面背后的 `gbapi.eastmoney.com/content/api/Post/ArticleContent` 接口。
> 参数使用 CSV 中的 `post_id` 作为 `postid`，`post_source_id` 作为 `newsid`。
> `curl_cffi` Chrome TLS 指纹、浏览器 Cookie 预热和 Selenium 导航只保留为兜底层。
> `STAGE2_DETAIL_LIMIT=100` 试跑结果：000001 full stage2 财富号 100/100 成功，
> 平均约 121 条/分钟，无 403、无报错，且未刷新 `stage2.done`。

在保留原方案“curl_cffi Chrome TLS 指纹 + 浏览器 Cookie 预热 + 分批并发”的主方向上，本次落地做 4 点修正：

1. `--detail-workers` 严格作为财富号并发数使用。用户传 `--detail-workers 3` 就按 3 个 worker 试跑，不在内部翻倍，便于复现实测速度和封禁表现。
2. 不在线程之间共享同一个 HTTP Session。浏览器只预热一个主 Cookie 池，每个 worker 使用自己的 curl_cffi Session 并复制 Cookie，降低并发 CookieJar/连接池竞争风险。
3. 失败分层：`http_404`、`http_410`、`invalid_article`、`invalid_page_marker` 才写入永久失败并在后续跳过；`http_403`、`http_429`、验证页、超时、正文容器未识别都视为可重试，不污染 `detail_failed.jsonl`。
4. Stage 2 遇到封禁/限流时返回未完成并保留 checkpoint，不写 `stage2.done`。已成功抓到的正文会先写回 CSV；下次重跑继续补剩余可重试帖子。

试跑 100 条时使用临时环境变量限制本次队列：

```powershell
$env:STAGE2_DETAIL_LIMIT='100'
python auto_pipeline_000001.py --stock 000001 --stage 2 --crawl-mode full --detail-workers 3
Remove-Item Env:\STAGE2_DETAIL_LIMIT
```

不设置 `STAGE2_DETAIL_LIMIT` 时仍然按 full 模式处理全部缺失正文财富号。

# Stage 2 财富号正文爬取优化方案（30分钟目标）

## 1. 问题诊断

### 1.1 当前现象

运行 Stage 2 full 模式（`--detail-workers 3`）后：
- **0-60 条**：全部成功，速度 ~260 条/分
- **60-100 条**：开始出现失败（空/失效数从 0 跳到 38），成功正文仅增 2 条
- **100 条以后**：几乎全部 HTTP 403，速度逐渐下降
- **换 IP 后仍然如此**：40-60 条即触发封禁

### 1.2 失败记录分析

读取 `temp_extract/000001_detail_failed.jsonl`（共 1384 条）：
- **第 374 条起（本次运行）**：**全部是 `http_403`**，无一例外
- 仅 3 条是 `invalid_page_marker`（真正的帖子下架）
- **结论：IP 被 caifuhao.eastmoney.com 的 WAF 封禁，不是帖子下架**

### 1.3 为什么 260 条/分会被封？根因分析

当前代码在 ~260 条/分（3 workers 全速）被封，而 Stage 1 在 ~430 页/分（6 workers）零失败。区别不在速度，而在**请求质量**：

| 缺陷 | 当前 Stage 2 | Stage 1（430页/分零封） |
|------|-------------|----------------------|
| **TLS 指纹** | Python `requests`（JA3 指纹可被 WAF 识别为爬虫） | 同用 requests，但列表 API 反爬弱 |
| **Cookie** | 无（裸 requests.get） | 浏览器预热注入 `.eastmoney.com` Cookie |
| **Session** | 无（每次新建 TCP 连接） | `self.session` keep-alive 复用 |
| **Headers** | 仅 UA/Accept（3 个 header） | 完整 Referer/Accept/Encoding 等 |
| **节奏** | 3 线程全速轰炸，无间隔 | 80 页/批 + 批间暂停 |
| **封禁反应** | 403 后继续发请求 | 检测到 blocked 立刻降级 |

**核心发现：caifuhao.eastmoney.com 的 WAF 比 guba 列表 API 严格得多。裸 requests（TLS 指纹 + 无 Cookie + 无 Referer）即使低速也会被封；但如果请求在 TLS 层和 HTTP 层都"看起来像 Chrome 浏览器"，可以在高并发下稳定通过。**

---

## 2. 提速核心：curl_cffi TLS 指纹伪装

### 2.1 为什么 curl_cffi 是关键

WAF 识别爬虫分多层：
1. **TLS 层（最关键）**：ClientHello 中的 cipher suites、extensions、EC 点格式等构成 JA3/JA4 指纹。Python `requests` 使用 `urllib3` + Python `ssl` 模块，其指纹与 Chrome/Firefox 完全不同，WAF 可在 TCP 握手阶段直接标记为爬虫。**这是为什么换 IP 仍然在 60 条内被封——WAF 看到的还是 Python 的 TLS 指纹。**
2. **HTTP/2 层**：帧顺序、SETTINGS 参数、WINDOW_UPDATE 行为
3. **HTTP 层**：Header 顺序、大小写、缺失的 Sec-Fetch-* 头
4. **行为层**：请求频率、Cookie 存在性、Referer 链

`curl_cffi` 通过绑定 curl-impersonate，在 TLS 和 HTTP/2 层完美模拟 Chrome 120 的指纹，包括：
- TLS ClientHello 扩展顺序和值
- HTTP/2 SETTINGS 帧和优先级
- Header 顺序和大小写（Chrome 使用小写 header名）
- Accept-Encoding 顺序

```bash
pip install curl_cffi
```

使用方式与 requests 几乎一致：

```python
from curl_cffi import requests as curl_requests

session = curl_requests.Session(impersonate="chrome120")
resp = session.get(url, headers=headers)  # TLS/HTTP2 指纹 = Chrome 120
```

### 2.2 速度目标推算

8372 条 / 30 分钟 = **~280 条/分** = ~4.7 条/秒

| 参数 | 保守值 | 目标值 |
|------|--------|--------|
| 单请求响应时间 | ~400ms（服务器处理+网络） | ~300ms |
| 单请求 jitter 延迟 | 0.2-0.5s | 0.1-0.3s |
| 每 worker 吞吐 | 60/(0.4+0.35) ≈ 80 条/分 | 60/(0.3+0.2) ≈ 120 条/分 |
| 需要 workers | 280/80 = **4 workers** | 280/120 = **3 workers** |
| 窗口大小 | 80 条/批 | 100 条/批 |
| 窗口间暂停 | 3-8 秒 | 2-5 秒 |
| **预计总耗时** | **~37 分（保守）** | **~25 分（乐观）** |

---

## 3. 具体代码改动

### 3.1 改动总览（仅 2 个文件）

| 文件 | 改动 | 影响 |
|------|------|------|
| [crawler.py](file:///e:/guba_project/EastMoney_Crawler/crawler.py) | `_crawl_caifuhao_posts()` 重写：curl_cffi session + 浏览器 Cookie 预热 + 窗口分批（100条/批）+ 高并发（4 workers）+ 微延迟（0.1-0.5s）+ 即时封禁检测 | 核心提速 |
| [parser.py](file:///e:/guba_project/EastMoney_Crawler/parser.py) | `_try_requests_caifuhao()` 改为接受 session 参数，返回结构化结果（含 http_status/reason），完整 Chrome headers | 精确区分封禁 vs 真下架 |

### 3.2 第一步：安装 curl_cffi

```bash
pip install curl_cffi
```

### 3.3 改动一：crawler.py — 新增 curl_cffi Session 创建方法

在 `_new_list_session` 方法附近新增：

```python
def _new_caifuhao_session(self) -> "curl_requests.Session":
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
```

### 3.4 改动二：crawler.py — 财富号 Cookie 预热（caifuhao 域）

在 `_bootstrap_list_session_via_browser` 之后新增：

```python
def _bootstrap_caifuhao_session_via_browser(self):
    """启动浏览器访问财富号首页，将 Cookie 注入 curl_cffi Session"""
    with self._cookie_bootstrap_lock:
        if getattr(self, '_caifuhao_cookie_bootstrapped', False) and hasattr(self, '_caifuhao_session'):
            return
        browser = create_stealth_chrome()
        try:
            browser.get('https://caifuhao.eastmoney.com/')
            time.sleep(3)
            # 额外访问一篇文章建立文章页 Cookie
            browser.get('https://caifuhao.eastmoney.com/news/')
            time.sleep(2)
            if not hasattr(self, '_caifuhao_session'):
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
```

在 `__init__` 中添加：
```python
self._caifuhao_session = None
self._caifuhao_cookie_bootstrapped = False
```

### 3.5 改动三：parser.py — _try_requests_caifuhao 接受 session + 返回结构化结果

```python
def _try_requests_caifuhao(self, post_url: str, session=None, proxies=None, timeout=(5, 15)) -> dict:
    """获取财富号文章正文，返回结构化结果

    Returns:
        {
            'ok': bool,
            'post_content': str, 'post_title': str,
            'post_date': str, 'post_time': str, 'post_author': str,
            'http_status': int,
            'reason': str  # 'ok' | 'http_403' | 'http_404' | 'http_429'
                           # | 'blocked_validation' | 'invalid_article'
                           # | 'body_not_found' | 'timeout' | 'exception:XXX'
        }
    """
    result = {
        'ok': False, 'post_content': '', 'post_title': '',
        'post_date': '', 'post_time': '', 'post_author': '',
        'http_status': 0, 'reason': 'not_started',
    }

    headers = {
        'Referer': 'https://guba.eastmoney.com/',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-site',
        'Sec-Fetch-User': '?1',
    }

    fetcher = session if session is not None else requests
    try:
        resp = fetcher.get(post_url, headers=headers, timeout=timeout, proxies=proxies)
        result['http_status'] = resp.status_code

        if resp.status_code in (403, 429):
            result['reason'] = f'http_{resp.status_code}'
            return result
        if resp.status_code in (404, 410):
            result['reason'] = 'http_404'
            return result
        if resp.status_code != 200:
            result['reason'] = f'http_{resp.status_code}'
            return result

        text = resp.text[:8000] if len(resp.text) > 8000 else resp.text
        if self._looks_like_blocked_html(text, getattr(resp, 'url', '')):
            result['reason'] = 'blocked_validation'
            return result
        if self._looks_like_invalid_caifuhao(text):
            result['reason'] = 'invalid_article'
            return result

        soup = BeautifulSoup(resp.text, 'html.parser')

        # 正文（多 selector 兼容）
        body = soup.select_one(
            'div.article-body, div.articleContent, div#ContentBody, '
            'div.newstext, div.newsContent, article'
        )
        if body:
            paragraphs = body.find_all(['p', 'div', 'section'])
            content_parts = [p.get_text(strip=True) for p in paragraphs
                             if p.get_text(strip=True) and len(p.get_text(strip=True)) > 5]
            result['post_content'] = '\n'.join(content_parts)

        # 标题
        title_el = soup.select_one('h1.article-title, div.article-title, h1.title, h1')
        if title_el:
            result['post_title'] = title_el.get_text(strip=True)

        # 时间/作者
        meta = soup.select_one('div.article-meta, div.article-head, div.newsauthor, div.author-info')
        if meta:
            meta_text = meta.get_text(strip=True)
            date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})', meta_text)
            if date_match:
                result['post_date'] = date_match.group(1)[:10]
                result['post_time'] = date_match.group(1)[11:]
            author_el = meta.select_one('a.author_name, a[href*="caifuhao"]')
            if author_el:
                result['post_author'] = author_el.get_text(strip=True)

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
```

### 3.6 改动四（核心）：crawler.py — 重写 _crawl_caifuhao_posts

```python
def _crawl_caifuhao_posts(self, posts: list, update_callback, max_workers: int = 4):
    """高速财富号正文爬取（curl_cffi Chrome指纹 + 浏览器Cookie + 窗口分批）

    目标：8000+ 条在 30 分钟内完成（~280 条/分），不触发 403 封禁。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(posts)
    # ===== 高速参数 =====
    WINDOW_SIZE = 100         # 每批 100 条
    WORKERS = max(1, min(int(max_workers or 4), 6))  # 最多 6 并发
    WINDOW_PAUSE = (2.0, 5.0)  # 批间暂停 2-5 秒
    PER_REQ_DELAY = (0.1, 0.4) # 请求间微延迟 0.1-0.4s（防突发）
    BLOCK_THRESHOLD = 3        # 连续 403 触发冷却
    COOLDOWN_TIME = (30.0, 60.0)  # 封禁后冷却 30-60s

    # 初始化 curl_cffi session（Chrome 120 指纹）+ 浏览器 Cookie 预热
    self._bootstrap_caifuhao_session_via_browser()
    session = self._caifuhao_session
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

    success_ids = set()
    permanent_fail_ids = set()  # 404/invalid → 不再重试
    blocked_ids = set()         # 403/timeout/body_not_found → Pass2 重试
    lock = threading.Lock()
    start_time = time.time()
    consecutive_blocks = 0

    def crawl_one(post: dict):
        post_id = post['_id']
        url = caifuhao_url(post)
        if not url:
            return post_id, {'ok': False, 'reason': 'missing_url'}
        # 微延迟（在 worker 线程内）
        time.sleep(random.uniform(*PER_REQ_DELAY))
        detail = parser._try_requests_caifuhao(
            url, session=session, proxies=self._request_proxies()
        )
        return post_id, detail, url

    # 断点续爬
    already_done = _load_checkpoint(self.symbol) if '_load_checkpoint' in dir() else set()
    already_failed = set()
    try:
        failed_path = os.path.join(TEMP_DIR, f'{self.symbol}_detail_failed.jsonl')
        if os.path.exists(failed_path):
            with open(failed_path, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        already_failed.add(str(obj.get('post_id', '')))
                    except:
                        pass
    except:
        pass

    skip_ids = already_done | already_failed
    remaining = [p for p in posts if str(p['_id']) not in skip_ids]
    print(f'{self.symbol}: [CAIFUHAO-HIGH-SPEED] total={total}, '
          f'已完成={len(already_done)}, 已知失效={len(already_failed)}, '
          f'待爬={len(remaining)}, workers={WORKERS}, window={WINDOW_SIZE}')

    window_done = 0
    batch_for_checkpoint = set()
    global_blocked = False

    for wstart in range(0, len(remaining), WINDOW_SIZE):
        if global_blocked:
            print(f'{self.symbol}: [CAIFUHAO] 检测到全局封禁，暂停当前批次，进入冷却...')
            cooldown = random.uniform(*COOLDOWN_TIME)
            time.sleep(cooldown)
            # 刷新 Cookie
            self._caifuhao_cookie_bootstrapped = False
            self._bootstrap_caifuhao_session_via_browser()
            session = self._caifuhao_session
            global_blocked = False
            consecutive_blocks = 0

        window = remaining[wstart:wstart + WINDOW_SIZE]
        window_blocked = 0
        window_success = 0
        window_perm_fail = 0

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
                        batch_for_checkpoint.add(str(post_id))
                        window_success += 1
                        consecutive_blocks = 0
                    elif reason in ('http_403', 'http_429', 'blocked_validation'):
                        blocked_ids.add(post_id)
                        window_blocked += 1
                        consecutive_blocks += 1
                    elif reason in ('http_404', 'invalid_article'):
                        update_callback(post_id, {
                            'post_content': '', '_detail_failed': True,
                            'reason': reason, 'post_url': url
                        })
                        permanent_fail_ids.add(post_id)
                        window_perm_fail += 1
                    else:
                        # body_not_found / timeout / exception → 重试
                        blocked_ids.add(post_id)
                        window_blocked += 1
                        consecutive_blocks += 1

        # 进度汇报
        elapsed = time.time() - start_time
        done_total = len(already_done) + window_done
        speed = done_total / max(elapsed / 60, 0.01)
        eta = (total - done_total) / max(speed, 0.01)
        print(f'{self.symbol}: [CAIFUHAO] {done_total}/{total} '
              f'({done_total/total*100:.0f}%) | '
              f'ok={len(success_ids)} | '
              f'fail(perm)={len(permanent_fail_ids)} | '
              f'retry={len(blocked_ids)} | '
              f'{speed:.0f}条/分 | ETA {eta:.0f}分')

        # 断点保存
        if batch_for_checkpoint:
            all_done = batch_for_checkpoint | {str(pid) for pid in permanent_fail_ids}
            _save_checkpoint(self.symbol, all_done) if '_save_checkpoint' in dir() else None
            batch_for_checkpoint.clear()

        # 全局封禁检测
        if consecutive_blocks >= BLOCK_THRESHOLD or window_blocked > len(window) * 0.3:
            global_blocked = True
            print(f'{self.symbol}: [CAIFUHAO] 窗口封禁率过高 '
                  f'({window_blocked}/{len(window)}), 触发冷却')

        # 窗口间暂停
        if wstart + WINDOW_SIZE < len(remaining) and not global_blocked:
            time.sleep(random.uniform(*WINDOW_PAUSE))

    # ===== Pass 2：重试被阻断的帖子（单线程 + 稍长延迟 + 新Cookie）=====
    if blocked_ids:
        retry_posts = [p for p in posts if p['_id'] in blocked_ids]
        print(f'\n{self.symbol}: [CAIFUHAO-PASS2] 重试 {len(retry_posts)} 条阻断帖子 '
              f'(单线程, delay=1-3s, 冷却后进行)...')
        time.sleep(random.uniform(30, 60))
        self._caifuhao_cookie_bootstrapped = False
        self._bootstrap_caifuhao_session_via_browser()
        session = self._caifuhao_session

        retry_ok = 0
        retry_fail = 0
        for i, post in enumerate(retry_posts):
            time.sleep(random.uniform(1.0, 3.0))
            post_id = post['_id']
            url = caifuhao_url(post)
            detail = parser._try_requests_caifuhao(
                url, session=session, proxies=self._request_proxies()
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
                if reason in ('http_404', 'invalid_article'):
                    update_callback(post_id, {
                        'post_content': '', '_detail_failed': True,
                        'reason': reason, 'post_url': url
                    })
                else:
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
    print(f'{self.symbol}: 财富号爬取完成！'
          f'成功 {len(success_ids)}，永久失效 {len(permanent_fail_ids)}，'
          f'总耗时 {total_elapsed:.0f}s ({total_elapsed/60:.1f}分)')
```

---

## 4. 参数预设与运行方式

### 4.1 推荐默认参数（硬编码即可，无需额外 CLI 参数）

| 参数 | 值 | 说明 |
|------|-----|------|
| `max_workers` | 4 | 4 个 curl_cffi 并发连接（Chrome 正常加载页面也会并发请求资源） |
| `WINDOW_SIZE` | 100 | 每批 100 条（比 Stage 1 的 80 页略大，因为 curl_cffi 伪装后更安全） |
| `WINDOW_PAUSE` | 2-5 秒 | 批间微暂停（Stage 1 在 80 页批间停 20-45s，是因为裸 requests；curl_cffi 不需要那么长） |
| `PER_REQ_DELAY` | 0.1-0.4 秒 | 请求间微抖动（防固定频率被识别） |
| `BLOCK_THRESHOLD` | 连续 3 次 403 | 触发冷却 |
| `COOLDOWN_TIME` | 30-60 秒 | 封禁后冷却 + 刷新 Cookie |

### 4.2 运行命令（不变）

```powershell
# Stage 2 full（curl_cffi + Chrome指纹 + Cookie预热，预计25-35分钟完成）
python auto_pipeline_000001.py --stock 000001 --stage 2 --crawl-mode full --detail-workers 4
```

### 4.3 分档策略

| 场景 | workers | 预计速度 | 预计耗时 | 风险 |
|------|---------|---------|---------|------|
| 首次试跑（验证） | 2 | ~150条/分 | ~56分 | 极低 |
| **推荐全量** | **4** | **~280条/分** | **~30分** | **低** |
| 激进模式 | 6 | ~400条/分 | ~21分 | 中（可能触发冷却） |

**建议先用 `--detail-workers 4` 跑。如果一个窗口内 blocked 始终为 0 且速度 >300条/分，可尝试 workers=6。**

---

## 5. 关键设计决策解释

### 5.1 为什么 curl_cffi 比"加延迟"更重要？

之前的方案靠"加延迟、降并发"把速度压到 22 条/分来避免封禁，但这导致需要 6 小时。实际上 WAF 封禁的核心判据不是"请求频率"，而是**请求特征**：
- 一个真实 Chrome 用户打开一个页面可能在 1 秒内发出 30-50 个请求（加载 JS/CSS/图片/广告），但不会被封
- Python requests 即使 1 秒 1 个请求也会被封，因为 TLS 指纹暴露了
- **curl_cffi 让我们在 TLS/HTTP2 层变成 Chrome**，配合浏览器 Cookie 和完整 Headers，WAF 无法区分我们的请求和真实用户浏览

### 5.2 为什么窗口间只停 2-5 秒而不是 15-30 秒？

Stage 1 的 20-45 秒窗口暂停是因为裸 requests 的窗口内高并发（80页/6workers≈13页/worker，几乎无延迟）。有了 curl_cffi 的 Chrome 伪装 + Cookie 后，WAF 的"滑动窗口频率计数器"阈值高得多，2-5 秒足以让计数器衰减。如果不加这个暂停，连续的高速请求可能在 ~5-10 分钟后触发累计频率限制。

### 5.3 为什么不直接用 Selenium（真实浏览器）？

Selenium 打开一个页面需要 3-5 秒（渲染+JS执行+资源加载），即使 3 个浏览器并发也只能 ~36 条/分，8372 条需要 ~4 小时。curl_cffi 只获取 HTML 不渲染，每个请求 ~300ms，速度快 10 倍以上。

### 5.4 封禁检测与恢复机制

1. **实时检测**：每个窗口统计 blocked 数量，>30% 或连续 3 次 403 立即触发冷却
2. **自动恢复**：冷却 30-60 秒后重新打开浏览器获取新 Cookie，继续爬取
3. **二遍补爬**：Pass 1 中被阻断的帖子在 Pass 2 单线程+长延迟重试
4. **断点续爬**：每 50 条保存 checkpoint，中断后重跑自动跳过已完成

---

## 6. 预期效果

| 指标 | 当前（裸 requests） | 优化后（curl_cffi） | Stage 1 参考 |
|------|-------------------|-------------------|-------------|
| TLS 指纹 | Python ssl（可识别） | **Chrome 120（curl_cffi）** | Python ssl（API 反爬弱） |
| Cookie 预热 | ❌ 无 | ✅ 浏览器注入 caifuhao 域 Cookie | ✅ 浏览器注入 |
| Headers | 3 个极简 header | **完整 Chrome headers（含 Sec-Fetch-*）** | 完整 headers |
| Session | ❌ 无 keep-alive | ✅ curl_cffi Session 复用 | ✅ requests Session |
| 并发 | 3 workers | **4 workers**（可试 6） | 6 workers |
| 请求间延迟 | 0s | 0.1-0.4s 微抖动 | 窗口间暂停 |
| 窗口大小 | 无（全部提交） | 100条/批 | 80页/批 |
| 窗口暂停 | 无 | 2-5s | 20-45s |
| 封禁检测 | ❌ | ✅ 实时 403 检测+冷却+Cookie刷新 | ✅ |
| Pass2 重试 | ❌ | ✅ 单线程+长延迟 | ✅ |
| 触发封禁 | ~60条 | **预期不封禁**（偶发冷却30-60s） | 3380页未封禁 |
| 速度 | 260条/分（10秒后失效） | **~280条/分（持续稳定）** | ~430页/分 |
| **8372条耗时** | 无法完成 | **~25-35分钟** | N/A |
