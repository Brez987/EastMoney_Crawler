# Stage 2 爬取方式对比分析

## 1. 三种 Stage 2 入口的代码路径

| 入口 | 函数 | 调用链 |
|------|------|--------|
| 增量模式 `--stage 2` | `run_stage2()` | `crawl_post_detail_csv()` → `PostCrawler.crawl_post_detail()` → `_crawl_caifuhao_posts()` |
| full 模式 `--stage 2 --crawl-mode full` | `run_stage2_full()` | `crawl_post_detail_csv()` → `PostCrawler.crawl_post_detail()` → `_crawl_caifuhao_posts()` |
| 文档描述的 Stage 2 (L92-108) | — | 同上，文档描述的是行为而非实现差异 |

**结论：增量模式和 full 模式的 Stage 2 走完全相同的代码路径，两者没有实现差异。**

## 2. 核心爬取机制对比

### 2.1 财富号帖子爬取：`_crawl_caifuhao_posts()`（crawler.py L598-706）

```
当前实现：
  - 并发数：max_workers = min(detail_workers * 2, 8) = 6（当 detail_workers=3）
  - 请求方式：requests.get()，无延迟，无 rate-limiting
  - 超时设置：timeout=(3, 5)
  - 失败阈值：MAX_CONSECUTIVE_HTTP_FAIL = 50
  - 停止策略：连续 50 次失败后停止

原始 GitHub 实现（推测）：
  - 并发数：通常为 1-2（更保守）
  - 请求方式：requests.get() + time.sleep() 延迟
  - 更低或更敏感的失败检测
```

### 2.2 关键差异

| 维度 | 当前 full 模式 | 文档描述 (L92-108) | 影响 |
|------|---------------|-------------------|------|
| 输入 CSV | `full_posts.csv`（单文件） | base + new（多文件） | full 模式一次性处理全部历史帖子 |
| 待爬数量 | 全量历史（9447 条） | 增量少量（通常 < 1000） | full 模式下请求量暴增 |
| 财富号并发 | 6 workers | 描述为 `--detail-workers 3` | 实际并发是文档描述的 2 倍 |
| 请求延迟 | **无** | 文档未提及 | 关键缺失 |
| 失败检测 | 50 次连续失败 | 文档未提及 | 阈值过高，已造成大量无效请求 |

## 3. 终端运行日志分析（Terminal L190-227）

```
000001: 财富号已爬取 20/9447 条          ← 前 20 条正常
000001: 财富号进度 20/9447，成功 20
000001: 财富号已爬取 40/9447 条          ← 40 条正常
000001: 财富号进度 40/9447，成功 40
000001: 财富号已爬取 60/9447 条          ← 60 条正常
000001: 财富号进度 60/9447，成功 60
000001: 财富号进度 80/9447，成功 62，失效/失败 18  ← 失败开始出现
000001: 财富号进度 100/9447，成功 62，失效/失败 38  ← 失败率飙升
000001: 财富号请求连续失败 54 次，疑似 IP 已被全局拦截  ← 触发停止
```

**失败模式分析：**
- 前 60 条全部成功 → IP 未被封
- 60-80 条之间 18 条失败 → IP 开始被限流
- 80-100 条之间 20 条失败 → IP 已被全局拦截
- 连续失败 54 次 → 触发停止

## 4. 根因分析：为什么会 IP 被全局拦截？

### 直接原因：`_crawl_caifuhao_posts()` 缺少请求限速

```python
# crawler.py L598-677 关键代码段
def _crawl_caifuhao_posts(self, posts, update_callback, max_workers=2):
    # max_workers 实际传入 min(detail_workers * 2, 8) = 6
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(crawl_one, post) for post in posts]
        # 6 个 worker 同时发起 requests.get()，没有任何延迟
```

6 个并发 worker × 无延迟 ≈ **每秒 6+ 个请求**，远超东方财富的反爬阈值。

### 深层原因

1. **并发数过高**：`detail_workers=3` 时财富号实际使用 6 个并发，是用户预期的 2 倍
2. **无请求间隔**：`crawl_one` 函数内没有 `time.sleep()`，请求发出后立即处理下一条
3. **失败阈值过高**：`MAX_CONSECUTIVE_HTTP_FAIL = 50`，IP 被封后仍发送了 50 个无效请求，进一步加重封禁
4. **full 模式放大效应**：9447 条待爬帖子，即使成功率 60%，也会在短时间内发出数千个请求

### 不是 full 模式特有

增量模式同样使用 `crawl_post_detail_csv()`，如果新帖子数达到数千条，同样会触发 IP 拦截。区别仅在于：

- 增量模式：待爬帖子少，在 IP 被封前就能完成
- full 模式：待爬帖子多，必然在中间触发 IP 拦截

## 7. Stage 1 成功经验 → Stage 2 改进方案

### 7.1 Stage 1 为什么没有触发 IP 拦截？

从 Terminal L37-189 的实际运行日志可以看到，Stage 1 以 **6 workers、~430 页/分钟** 的速度完成 3380 页爬取，**零失败、零阻塞**。对比 Stage 2 在 100 条后就触发 IP 拦截，核心差异在于：

| 维度 | Stage 1（fast HTML） | Stage 2（`_crawl_caifuhao_posts`） | 影响 |
|------|---------------------|-----------------------------------|------|
| **Cookie 来源** | 浏览器预热 → `self.session.cookies` | 无，裸 `requests.get()` | Stage 1 的身份更像真实浏览器 |
| **Session 复用** | 共享 `requests.Session`（keep-alive） | 每次 `requests.get()` 新建连接 | Stage 1 TCP 连接复用，减少握手 |
| **窗口分批** | 每 80 页一批，窗口之间暂停 20-45s | 全部 9447 条一次性提交 | Stage 1 有自然冷却周期 |
| **请求延迟** | 窗口间 `random.uniform(20, 45)` 秒 | 无任何延迟 | 关键差异 |
| **并发降级** | 失败时自动降级 workers → Selenium | 无降级，连败 50 次才停 | Stage 1 自适应，Stage 2 硬撞 |
| **目标页面** | 列表页（`guba.eastmoney.com/list`） | 文章详情页（`caifuhao.eastmoney.com`） | 详情页反爬更严 |

### 7.2 Stage 1 关键代码路径（crawler.py L300-319）

```python
def _bootstrap_list_session_via_browser(self):
    """启动浏览器 → 获取 EastMoney cookies → 注入 requests.Session"""
    browser = create_stealth_chrome()
    browser.get(f'https://guba.eastmoney.com/list,{self.symbol},f_1.html')
    time.sleep(2)
    for cookie in browser.get_cookies():
        self.session.cookies.set(name, value, domain='.eastmoney.com')
    browser.quit()
    # 之后所有 requests 都带着真实浏览器的 cookies
```

```python
# run_pass 中的窗口分批（crawler.py L1530-1559）
for start in range(0, len(page_list), list_window_size):
    window = page_list[start:start + list_window_size]  # 80 页一批
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # ... 并发抓取当前窗口 ...
    # 窗口之间暂停 20-45 秒
    time.sleep(random.uniform(*window_pause_range))
```

### 7.3 Stage 2 改进方案

将 Stage 1 的成功模式移植到 Stage 2 的财富号爬取：

#### 改进 1：浏览器 Cookie 预热（P0）

```python
# 在 _crawl_caifuhao_posts 开始前
def _crawl_caifuhao_posts(self, posts, update_callback, max_workers=2):
    # 复用 Stage 1 的 cookie 预热机制
    self._bootstrap_list_session_via_browser()
    
    # 使用共享 session 而非裸 requests
    session = self.session  # 已注入 EastMoney cookies
```

#### 改进 2：窗口分批 + 冷却暂停（P0）

```python
CAIFUHAO_WINDOW_SIZE = 50      # 每 50 条为一批
CAIFUHAO_PAUSE_RANGE = (10, 20)  # 批间暂停 10-20 秒

for start in range(0, total, CAIFUHAO_WINDOW_SIZE):
    window = posts[start:start + CAIFUHAO_WINDOW_SIZE]
    with ThreadPoolExecutor(max_workers=1) as executor:  # 单线程
        for post in window:
            time.sleep(random.uniform(0.5, 1.5))  # 请求间延迟
            executor.submit(crawl_one, post, session)
    # 窗口完成，暂停冷却
    time.sleep(random.uniform(*CAIFUHAO_PAUSE_RANGE))
```

#### 改进 3：降低并发数（P0）

```python
# 当前: max_workers = min(detail_workers * 2, 8)  # = 6 when detail_workers=3
# 改为: max_workers = max(1, detail_workers // 2)  # = 1 when detail_workers=3
# 财富号页面比列表页更敏感，应使用更低并发
```

#### 改进 4：降低失败阈值（P1）

```python
# 当前: MAX_CONSECUTIVE_HTTP_FAIL = 50
# 改为: MAX_CONSECUTIVE_HTTP_FAIL = 10
# IP 被封后应立即停止，50 次无效请求只会加重封禁
```

#### 改进 5：自适应降级（P1）

```python
# 当连续失败超过 5 次时，自动：
# 1. 增加请求间延迟（0.5s → 2.0s → 5.0s）
# 2. 降低并发数（2 → 1）
# 3. 增加窗口暂停时间（20s → 60s）
# 参考 Stage 1 的 adaptive fallback 机制
```

#### 改进 6：断点续爬恢复（P2）

```python
# 当 IP 拦截触发停止后，保存进度：
# 1. 记录已成功爬取的 post_id 列表
# 2. 记录失败 post_id 列表（detail_failed.jsonl 已有）
# 3. 下次运行时跳过已成功和已失败的帖子
# 4. 自动加载浏览器 cookies 恢复会话
```

### 7.4 改进后的预期效果

| 指标 | 当前 Stage 2 | 改进后 Stage 2 | Stage 1 参考值 |
|------|-------------|---------------|---------------|
| 并发数 | 6 | 1-2 | 6（但针对列表页） |
| 请求间隔 | 0s | 0.5-1.5s | 窗口间 20-45s |
| 窗口暂停 | 无 | 10-20s / 50条 | 20-45s / 80页 |
| Cookie | 无 | 浏览器预热 | 浏览器预热 |
| 失败阈值 | 50 | 10 | 自适应降级 |
| 预计速度 | ~360条/分（10秒内被封） | ~30-40条/分（稳定） | ~430页/分 |
| 9447条预计耗时 | 无法完成 | ~4-5小时 | N/A |

### 7.5 总结

Stage 1 的成功在于三点核心设计，Stage 2 当前缺失全部三点：

1. **身份伪装**：浏览器 Cookie 预热让 requests 看起来像真实浏览器会话
2. **节奏控制**：窗口分批 + 冷却暂停，避免持续高频请求
3. **自适应降级**：失败时自动降速而非硬撞

将这三者移植到 `_crawl_caifuhao_posts()` 即可解决 IP 拦截问题。改进不涉及架构变更，仅需修改 `crawler.py` 中 `_crawl_caifuhao_posts()` 一个方法。
