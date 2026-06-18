# 000001 自动化流水线速度优化方案与实施记录

更新时间：2026-06-17

目标：在保持现有 CSV 字段兼容的前提下，显著缩短 `README.md` 第 2.4 节所述 CSV-Native 流水线的爬取耗时，为后续 4000+ 股票代码批量爬取做准备。

---

## 1. 外部参考与可借鉴点

| 参考 | 链接 | 可借鉴点 | 本项目采用方式 |
| --- | --- | --- | --- |
| zcyeee/EastMoney_Crawler | https://github.com/zcyeee/EastMoney_Crawler | 原项目以 Selenium + MongoDB 为主，强调反爬重启和断点处理 | 保留 Selenium 作为兜底，不再作为列表/评论主路径 |
| hogking/eastmoney-guba | https://github.com/hogking/eastmoney-guba | 使用代理池、Redis、MongoDB 做长期运行和去重 | 本项目暂不引入新基础设施，优先压缩单股票流水线耗时 |
| kayzhou/eastmoney-crawler | https://github.com/kayzhou/eastmoney-crawler | 轻量请求封装，适合证明东方财富部分页面可直接 HTTP 获取 | 列表页改为 `requests + BeautifulSoup + article_list JSON` |
| 东方财富评论 JSONP 接口 | https://gbapi.eastmoney.com/reply/JSONP/ArticleNewReplyList | 评论可通过 `postid + md5(postid)` 直接获取 JSONP | 评论主路径改为 API 并发，Selenium 只兜底 |

核心判断：这个项目不需要换成 Scrapy 或重写框架。最大收益来自把 Selenium 从“主路径”降级为“兜底路径”，并减少 CSV 大文件重复重写。

---

## 2. 原瓶颈

| 阶段 | 原问题 | 影响 |
| --- | --- | --- |
| Stage 1 列表页 | 每页 `driver.get()` + DOM 查询 | 浏览器启动、渲染、JS 执行成本高 |
| Stage 1 CSV 状态 | `post_id` 和最新日期分两遍扫描 | 100MB+ CSV 下有多余 I/O |
| Stage 2 正文写回 | 每 10 条正文重写一次基础 CSV 和新帖 CSV | 大文件场景会把耗时放大到 O(重写次数 * 文件大小) |
| Stage 2 评论 | 每个有评论帖子都用 Selenium 打开页面 | 是后续 4000+ 股票批处理的最大风险 |
| 财富号 ID | 新帖 CSV 未正确保存 `post_source_id` | 评论 API 需要股吧 `post_id`，正文 URL 需要财富号文章号 |
| 批量股票 | 只能改源码里的 `STOCK_CODE` | 4000+ 股票无法稳定批处理 |

---

## 3. 已实施优化

### 3.1 列表页改为 HTTP 快速解析

实现位置：`crawler.py`

新增逻辑：
- `PostCrawler._fetch_post_page_fast()`
- `PostCrawler._extract_article_payload()`
- `PostCrawler._article_to_post_info()`

策略：
1. 直接请求 `https://guba.eastmoney.com/list,{code},f_{page}.html`。
2. 从页面内 `var article_list = {...}` 解析结构化帖子数据。
3. 用 SSR 表格里的可见行顺序过滤隐藏/推荐数据。
4. 失败时自动回退原 Selenium 列表解析。

实测：
- 第 1 页快速解析：`0.573s`，得到 79 条（保留旧逻辑：跳过首页第一条跨吧/置顶行）。
- 连续 3 页非写入 benchmark：`0.941s`，239 条，约 `3.19 页/秒`。

### 3.2 评论改为 API 并发优先

实现位置：`crawler.py`

新增逻辑：
- `CommentCrawler._fetch_comments_via_api()`
- `CommentCrawler._parse_jsonp()`
- `CommentCrawler._comment_from_api_reply()`
- `CommentCrawler.crawl_comment_info()` 重写为 API 并发 + Selenium 兜底

策略：
1. 使用 `gbapi.eastmoney.com/reply/JSONP/ArticleNewReplyList`。
2. 参数 `h = md5(post_id)`，`postid = 股吧 post_id`。
3. 默认 `8` worker 并发。
4. 单帖最多抓 `5` 页评论，每页 `50` 条。
5. API 失败，或 CSV 显示有评论但 API 返回空时，进入 Selenium 兜底。

实测：
- 帖子 `1728375394`：API `0.414s` 获取 3 条评论。

### 3.3 正文 CSV 写回改为 delta 模式

实现位置：`auto_pipeline_000001.py`

新增逻辑：
- `_content_delta_path()`
- `_load_content_delta()`
- `_append_content_delta()`
- `_delete_content_delta()`

策略：
1. 爬正文过程中只追加小型 JSONL delta 文件。
2. 每 50 条保存一次 checkpoint。
3. 全部完成后统一重写 CSV 一次。
4. 恢复任务时先读取尚未合并的 delta，防止 checkpoint 存在但正文未写回。

收益：
- 原逻辑：每 10 条重写一次 100MB 级 CSV。
- 新逻辑：每个 Stage 2 正文任务通常只重写一次 CSV。

### 3.4 Stage 1 CSV 状态单次扫描

实现位置：`auto_pipeline_000001.py`

新增 `scan_csv_state()`，一次扫描同时得到：
- 已有 `post_id` 集合
- 最新日期
- 最早日期
- 行数

### 3.5 修复财富号 ID 映射

实现位置：
- `crawler.py`
- `parser.py`
- `auto_pipeline_000001.py`

新约定：
- `post_id`：股吧帖子 ID，用于评论 API。
- `post_source_id`：财富号文章 ID，用于正文 URL。
- `url`：财富号正文 URL 仍使用 `post_source_id`。

这与历史基础 CSV 的字段含义保持一致。

### 3.6 CLI 支持任意股票代码

实现位置：`auto_pipeline_000001.py`

现在可以直接运行：

```powershell
python auto_pipeline_000001.py --stock 000001 --stage 1
python auto_pipeline_000001.py --stock 000002 --stage 1
python auto_pipeline_000001.py --stock 600000 --stage 1
```

无需再手工修改源码里的 `STOCK_CODE`。

---

## 4. 验证结果

已执行：

```powershell
python -m py_compile auto_pipeline_000001.py crawler.py parser.py
python -m unittest discover -s tests
```

结果：
- 编译通过。
- 单元测试 `27` 个全部通过。
- 新增离线测试覆盖列表页快速解析和评论 JSONP 扁平化。
- 真实网络 smoke test 通过：
  - 列表页第 1 页：`0.573s`
  - 列表页 3 页：`0.941s`
  - 评论 API 单帖：`0.414s`

---

## 5. 风险与控制

| 风险 | 控制 |
| --- | --- |
| 东方财富调整 `article_list` 变量名或结构 | 自动回退 Selenium；新增离线测试便于定位 |
| 评论 API 对少量帖子返回空 | CSV 显示有评论但 API 空时进入 Selenium 兜底 |
| 评论页数过多导致耗时增加 | 默认最多 5 页/帖，可按任务需要调小或调大 |
| 并发过高触发限流 | 当前仅评论 API 8 worker；列表页仍按页顺序抓取 |
| 任务中断导致正文写回未完成 | delta JSONL + checkpoint 恢复后会先合并未写回内容 |

---

## 6. 后续建议

1. 增加批量调度脚本：读取股票代码列表，按 `--stock` 自动跑 Stage 1/2/3。
2. 给评论 API worker 增加命令行参数，例如 `--comment-workers 4/8/16`，根据服务器和限流情况调节。
3. 对 Stage 3 合并导出增加跳过评论选项，便于先完成帖子主数据。
4. 对 4000+ 股票建议按交易所或代码段分批运行，每批记录耗时、失败代码、API 失败率。
