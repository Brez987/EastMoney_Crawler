# EastMoney_Crawler 数据缺失问题调研结论

## 一句话结论

问题不在解析器本身，而在 **数据资产没有随 GitHub 仓库正确分发**，叠加 **Stage 3 合并时会静默跳过缺失的 `*_new_posts.csv`**。  
本地 `data/` 的好结果来自“历史基线 CSV + Stage 1 增量新帖 CSV”的完整链路；别人跑出的 `guba_data/` 基本没有合并进 Stage 1 的增量新帖，所以大量 2025-2026 年帖子缺失。

## 证据

### 1. GitHub 仓库没有包含复现所需的历史输入

当前 Git 仓库根目录是 `E:\guba_project`，项目代码在 `EastMoney_Crawler/` 子目录。远端 `main` 当前指向：

```text
394ea02ee0ac23c93f8b916b55989d5bd2774eba refs/heads/main
```

本地 `main` 还有 1 个未推送提交：

```text
## main...origin/main [ahead 1]
```

但这不是核心算法差异，因为本地领先提交主要是数据/文档资产，代码主体仍然是同一套流水线。

真正关键的是 `.gitignore`：

```text
*.csv
*.rar
temp_extract/
temp_export/
.pipeline_flags/
batch_progress/
batch_logs/
```

因此这些本地关键文件不会进入 GitHub：

- `EastMoney_Crawler/数据/000001.csv` 等历史基线 CSV
- `EastMoney_Crawler/guba_data.rar`
- `EastMoney_Crawler/data/*_enhanced.csv`
- `EastMoney_Crawler/temp_extract/*_base.csv`
- `EastMoney_Crawler/temp_extract/*_new_posts.csv`

远端 `origin/main` 里也没有任何可用的股票 CSV / RAR 输入。换句话说，别人 `git clone` 后只有代码，没有本地这次试运行所依赖的历史基线数据。

### 2. 本地好结果来自“历史基线 + 新帖增量”

本地好结果的行数可以拆成：

| 股票 | 历史基线行数 | Stage 1 新帖行数 | `data/` 最终行数 |
|---|---:|---:|---:|
| 000001 | 244,807 | 84,435 | 329,242 |
| 000002 | 227,450 | 115,279 | 342,729 |
| 000003 | 3,221 | 30 | 3,251 |
| 000004 | 59,955 | 10,711 | 70,666 |
| 000005 | 63,078 | 58 | 63,136 |
| 000006 | 88,074 | 25,020 | 113,094 |
| 000007 | 52,298 | 1,862 | 54,160 |
| 000008 | 82,871 | 16,842 | 99,713 |
| 000010 | 69,327 | 8,146 | 77,473 |

这与本地日志吻合。例如 `000002` 的 Stage 3 日志显示：

```text
已读取 基础数据: temp_extract\000002_base.csv
已读取 新帖子: temp_extract\000002_new_posts.csv
整合完成: 342729 条记录
```

说明本地 `data/` 是完整合并产物，不是单纯靠当前网页实时爬出来的。

### 3. `guba_data/` 缺的主要就是 Stage 1 增量新帖

对比 `data/` 与 `guba_data/` 后，`guba_data/` 行数明显偏少：

| 股票 | `data/` 行数 | `guba_data/` 行数 | 行数差 | `guba_data/` 缺少的 Stage 1 新帖唯一 ID |
|---|---:|---:|---:|---:|
| 000001 | 329,242 | 238,517 | 90,725 | 29,221 |
| 000002 | 342,729 | 220,585 | 122,144 | 115,260 |
| 000003 | 3,251 | 3,236 | 15 | 1 |
| 000004 | 70,666 | 59,343 | 11,323 | 10,634 |
| 000005 | 63,136 | 59,850 | 3,286 | 0 |
| 000006 | 113,094 | 87,377 | 25,717 | 24,962 |
| 000007 | 54,160 | 51,716 | 2,444 | 1,783 |
| 000008 | 99,713 | 82,187 | 17,526 | 16,770 |
| 000010 | 77,473 | 68,866 | 8,607 | 8,077 |

缺失年份也集中在 2025-2026 年。例如：

- `000002` 缺失唯一 ID 中，2025 年 74,077 条，2026 年 41,212 条。
- `000001` 缺失唯一 ID 中，2025 年 19,581 条，2026 年 9,643 条。

这说明 `guba_data/` 不是“爬不到更老的历史”，而是没有把本地 Stage 1 补爬出来的新帖 CSV 正确合并进去。

## 出问题的具体步骤

### 上游问题：数据准备阶段不完整

`auto_pipeline_000001.py` 的 Stage 1 逻辑是：

1. 优先找 `{stock}.csv`、`数据/{stock}.csv` 或命令行传入的 `--source-dir/{stock}.csv`。
2. 找到后复制为 `temp_extract/{stock}_base.csv`。
3. 扫描历史 CSV 的最大日期，作为增量补爬停止阈值。
4. 将新帖写入 `temp_extract/{stock}_new_posts.csv`。

所以这个项目当前不是“clone 后直接全量复现”的纯代码项目，而是“代码 + 历史基线数据”的增量流水线。GitHub 仓库没有历史 CSV 时，别人无法得到与本地一致的 Stage 1 输入。

### 直接问题：Stage 3 对缺失输入过于宽松

`merge_csv_files()` 当前逻辑是：

```python
for src_path, label in [(base_csv, '基础数据'), (new_csv, '新帖子')]:
    if not os.path.exists(src_path):
        continue
```

也就是说，只要 `temp_extract/{stock}_new_posts.csv` 不存在，Stage 3 会直接跳过它并继续生成最终 CSV，不会报错。这正好能解释 `guba_data/` 的形态：最终文件存在，但少了大量新帖。

另外 Stage 2 / Stage 3 主要依赖 `.pipeline_flags/{stock}_stage*.done` 判断前序阶段是否完成，flag 文件不记录 `base_rows`、`new_rows`、输入路径、hash 或时间戳。如果 flag 或临时目录来自旧运行，或者 Stage 3 在另一个目录里运行，就可能产生“看似成功、实际缺数据”的输出。

## 应该如何修正

### 1. 不要把历史数据依赖藏在本地

必须把历史基线数据作为正式数据资产发布，而不是指望 GitHub 代码仓库隐式包含它。

推荐做法：


README与 E:\guba_project\EastMoney_Crawler\股票爬虫自动化流水线.md的开头，明确写：`git clone` 只包含代码，运行批量流水线前必须准备历史 CSV。

普通 GitHub 仓库不适合直接提交几百 MB 到十几 GB 的 CSV/RAR/ZIP。当前本地还有 `guba_missing_data.zip` 这类大包，但远端 `main` 没有它，且代码也不会自动把该 zip 当作历史源使用。

### 2. Stage 3 不能静默跳过关键产物

建议把 `merge_csv_files()` 改成强校验：

- `base_csv` 不存在时直接失败。
- `new_csv` 缺失时不要静默 `continue`，至少打印高亮警告。
- 如果 Stage 1 manifest 记录 `new_rows > 0`，但 `new_csv` 不存在或行数不匹配，Stage 3 必须失败。
- 输出时打印 `base_rows`、`new_rows`、`out_rows`，并校验 `out_rows == base_rows + new_rows`。

更稳的结构是 Stage 1 生成：

```text
temp_extract/{stock}_stage1_manifest.json
```

内容包含：

```json
{
  "stock": "000002",
  "source_csv": ".../数据/000002.csv",
  "source_rows": 227450,
  "source_latest_date": "2025-02-06",
  "new_posts_csv": ".../temp_extract/000002_new_posts.csv",
  "new_rows": 115279,
  "created_at": "..."
}
```

Stage 2 / Stage 3 每次运行都读取并校验这个 manifest，而不是只相信 `.done` flag。

### 3. Stage 1 不应复用过期 base

当前 Stage 1 如果发现 `temp_extract/{stock}_base.csv` 已存在，会直接跳过复制历史源。这样如果历史源更新了，或者临时目录来自别人旧运行，就会继续使用旧 base。

建议增加：

- `--force-refresh-base` 参数，重跑时强制从 `--source-dir` 复制最新历史 CSV。
- 或者默认比较源文件大小 / mtime / sha256，发现不同就重建 base。
- batch worker 在处理某只股票前可以清理该股票的旧临时产物：
  - `temp_extract/{stock}_base.csv`
  - `temp_extract/{stock}_new_posts.csv`
  - `temp_export/{stock}_enhanced.csv`
  - `.pipeline_flags/{stock}_stage*.done`

### 4. README / 运行命令需要改成强制显式数据源

不要让别人默认运行：

```powershell
.\batch_launcher.ps1
```

应该要求先准备历史源，然后显式传入：

```powershell
python batch_worker.py --dry-run --limit 5 --source-dir .\数据
.\batch_launcher.ps1 -WorkerCount 2 -DetailWorkers 3 -Limit 20 -SourceDir .\数据
```

验收时先看 dry-run 是否发现预期股票数量，再跑正式任务。

### 5. 对已经生成的错误 `guba_data/`，不要继续增量叠加

错误产物已经缺了大量新帖，不建议拿它继续当基线。应当：

1. 删除对应股票的旧临时状态：

```powershell
Remove-Item .\temp_extract\000002_* -Force
Remove-Item .\temp_export\000002_enhanced.csv -Force
Remove-Item .\.pipeline_flags\000002_* -Force
Remove-Item .\batch_progress\000002.* -Force
```

2. 确认 `.\数据\000002.csv` 存在且行数正确。
3. 重新跑完整 Stage 1/2/3 或 batch worker。
4. 对照本地好结果的行数验收，例如 `000002` 应该接近 `342,729` 行，而不是 `220,585` 行。

## 最小修复优先级

1. 在 README 里写清楚数据校验、运行命令。
2. 修改 Stage 3：缺少 `*_new_posts.csv` 时不再静默成功；至少输出明确警告，最好结合 Stage 1 manifest 直接失败。
3. 增加行数校验：每次 Stage 3 输出 `base_rows + new_rows = out_rows`。
4. 重跑别人那份 `guba_data/`，不要在错误输出上继续补。

---

# 2026-06-23 新需求：按 `数据_list.csv` 从 2009-01-01 起全量爬取

## 目标变化

原来的流水线是“历史粗糙 CSV + 增量补爬新帖”，现在应改为：

- 不再依赖原始 `数据/*.csv`。
- 以 `数据_list.csv` 为唯一股票任务来源；当前文件是单列股票代码，共 4383 行。
- 对清单中的每只股票，从 `2009-01-01` 起包含式全量爬取帖子列表，即保留 `post_publish_time >= 2009-01-01` 的帖子。
- 继续保持 CSV-Native、不走 MongoDB 的路线。
- 速度至少不低于旧流水线，优先使用当前已有的 requests 快速列表页解析能力，Selenium 只作为极少数异常页兜底。

## 总体改造策略

不要在旧的“增量补爬”逻辑上硬塞 `stop_date=2009-01-01`。旧逻辑的含义是“遇到 `<= stop_date` 的旧帖就停止且不写入该页”，用于补新帖是对的，但用于“从 2009-01-01 起全量爬取”会漏掉边界页，尤其是包含 `2009-01-01` 与更早日期混合的页面。

建议保留旧模式，并新增一个明确的全量模式：

```text
--crawl-mode incremental  # 旧逻辑，继续支持历史 CSV + 增量补爬
--crawl-mode full         # 新逻辑，按 start-date 从网页全量爬取
```

全量模式的三阶段可以调整为：

1. Stage 1 full：从列表页抓取 `start_date` 之后的所有帖子，输出 `temp_extract/{stock}_full_posts.csv`。
2. Stage 2 detail：沿用当前正文补爬优化，普通股吧帖用标题填充 `content`，财富号帖用 requests 多线程补正文，失败写入 JSONL，不阻塞整只股票。
3. Stage 3 export：不再合并 `base + new`，而是校验 `full_posts.csv` 后导出到 `data/{stock}_full_20090101.csv`，同时写 manifest。

这样旧增量逻辑不会被破坏，新全量逻辑也不会继续背负“历史基线 CSV”的复杂性。

## 代码修改点

### 1. `batch_worker.py` 改为支持从 `数据_list.csv` 领任务

新增参数：

```text
--stock-list 数据_list.csv
--crawl-mode full
--start-date 2009-01-01
--list-workers 6
```

新增函数：

```python
def load_stock_list(path: Path) -> dict[str, Path | None]:
    ...
```

要求：

- 每行读取一个代码，`strip()` 后 `zfill(6)`。
- 跳过空行、注释行。
- 去重但保留原始顺序。
- full 模式下 `source_csv` 可以是 `None`，不要再调用 `validate_source_csv()`。
- incremental 模式继续使用原来的 `discover_stock_csvs(source_dirs)`。

`build_stage_cmd()` 在 full 模式下传入：

```text
python auto_pipeline_000001.py --stock 000001 --stage 1 --crawl-mode full --start-date 2009-01-01 --list-workers 6
python auto_pipeline_000001.py --stock 000001 --stage 2 --crawl-mode full --detail-workers 3
python auto_pipeline_000001.py --stock 000001 --stage 3 --crawl-mode full
```

进度目录建议单独使用：

```text
batch_progress_full_20090101/
```

避免与旧增量任务的 `.done/.failed/.lock` 混在一起。

### 2. `batch_launcher.ps1` 加 full 模式参数

新增参数：

```powershell
[string]$StockList = ".\数据_list.csv"
[string]$CrawlMode = "full"
[string]$StartDate = "2009-01-01"
[int]$ListWorkers = 6
```

推荐运行命令：

```powershell
.\batch_launcher.ps1 `
  -WorkerCount 3 `
  -DetailWorkers 3 `
  -ListWorkers 6 `
  -CrawlMode full `
  -StartDate 2009-01-01 `
  -StockList .\数据_list.csv `
  -ProgressDir .\batch_progress_full_20090101
```

生产环境不要盲目把 `WorkerCount * ListWorkers` 拉太高。建议总列表页并发先控制在 12-24 之间，例如 `WorkerCount=3, ListWorkers=6`，观察验证页、超时和失败页比例后再调。

### 3. `auto_pipeline_000001.py` 增加 full 专用路径与 manifest

新增路径函数：

```python
def full_posts_csv_path(stock_code: str) -> str:
    return os.path.join(TEMP_DIR, f'{stock_code}_full_posts.csv')

def full_manifest_path(stock_code: str) -> str:
    return os.path.join(TEMP_DIR, f'{stock_code}_full_manifest.json')
```

full manifest 至少记录：

```json
{
  "stock": "000001",
  "crawl_mode": "full",
  "start_date": "2009-01-01",
  "max_page": 4120,
  "boundary_page": 3908,
  "completed_pages": 3908,
  "failed_pages": [],
  "rows": 312345,
  "unique_post_ids": 312345,
  "min_time": "2009-01-01 09:31",
  "max_time": "2026-06-23 10:00",
  "created_at": "..."
}
```

Stage 2 / Stage 3 full 模式只相信这个 manifest 和 `full_posts.csv`，不再读取 `base_csv`、`new_posts_csv` 或历史源 CSV。

### 4. `crawler.py` 增加适合全量日期边界的列表抓取方法

当前 `PostCrawler._fetch_post_page_fast()` 已经能用 requests 从 `article_list` 解析列表页，这是全量爬取速度的基础，应继续作为主路径。

新增一个全量方法，例如：

```python
def crawl_post_info_since(
    self,
    start_date: str,
    storage_callback,
    list_workers: int = 6,
    checkpoint_callback=None,
) -> dict:
    ...
```

日期规则必须是：

- 写入：`post_date >= start_date`
- 停止：当某页所有有效帖子 `post_date < start_date` 时停止
- 边界页：如果同一页同时有 `2009-01-01` 与 `2008-12-31`，只写入 `>= 2009-01-01` 的行，然后结束或继续探测下一页

不要复用旧增量条件：

```python
all(post_date <= stop_date)
```

全量模式应使用：

```python
kept = [row for row in dic_list if row["post_date"] >= start_date]
all_before_start = dic_list and all(row["post_date"] < start_date for row in dic_list if row["post_date"])
```

### 5. 为速度增加“边界定位 + 并发分页”

为了比旧流水线更快，建议不要只按页串行抓到 2009。列表页是按时间倒序的，可以分两步：

1. 获取 `max_page`。
2. 用页面日期范围做二分探测，找到最后一个可能包含 `post_date >= 2009-01-01` 的 `boundary_page`。
3. 对 `1..boundary_page` 用小线程池并发抓取。

建议新增：

```python
def get_page_date_range(page_num: int) -> tuple[str | None, str | None, int]:
    # 返回该页 max_date, min_date, row_count

def find_boundary_page_since(start_date: str) -> int:
    # 二分查找日期边界；若发现日期不单调或页面异常，退回顺序探测
```

并发抓取时不要把所有页面一次性塞进内存。推荐按 chunk 执行：

```text
每批 100-300 页 -> ThreadPoolExecutor(list_workers) -> 写 page chunk -> 合并
```

或者每页先写：

```text
temp_extract/full_pages/{stock}/{page}.csv
```

最后按页号合并、去重、排序。这样即使中途断电，也可以只补缺失页，不必重爬已完成页面。

### 6. 全量 CSV 写入要页级断点化

旧模式一只股票只有 `base/new` 两个大文件；full 模式可能一只股票几千页，必须页级断点。

推荐目录：

```text
temp_extract/full_pages/{stock}/000001.csv
temp_extract/full_pages/{stock}/000002.csv
...
temp_extract/{stock}_full_posts.csv
temp_extract/{stock}_full_manifest.json
```

每页文件写入要求：

- 先写 `.tmp`，成功后 `os.replace()`，保证原子落盘。
- 记录页号、行数、重试次数、页面日期范围。
- 合并时按 `post_id` 去重。
- 合并完成后校验：
  - `min(post_publish_time) >= 2009-01-01`
  - `unique_post_ids == output_rows`
  - `failed_pages == []`

### 7. 稳定性策略

列表页失败处理：

- requests 失败重试 3 次，指数退避 + 少量随机抖动。
- 如果出现 `fd_guba_validate` 或“身份核实”，该 worker 暂停 1-3 分钟，并降低本股票 `list_workers`。
- Selenium 只用于少量连续失败页兜底；如果兜底也失败，将页号写入 `failed_pages`，该股票标记为 failed，不产出 done。

全局并发控制：

- 初始建议 `WorkerCount=3, ListWorkers=6`。
- 若验证页变多，降为 `WorkerCount=2, ListWorkers=4`。
- 不建议 4383 支股票同时高并发冲列表页，失败重试成本会抵消速度收益。

状态隔离：

- full 模式使用独立 `batch_progress_full_20090101`。
- full 模式输出文件名使用 `data/{stock}_full_20090101.csv`，不要覆盖旧 `data/{stock}_enhanced.csv`。
- full 模式 manifest 中写清楚 `crawl_mode=full`，防止后续与 incremental 结果混用。

### 8. Stage 2 正文补爬继续复用现有快路径

为了保持速度，不建议全量模式恢复 Selenium 逐帖正文爬取。

沿用当前策略：

- 普通股吧帖：`content` 为空时直接用 `post_title` 填充。
- 财富号帖：只用 requests 多线程补正文。
- 财富号失败：写入 `{stock}_detail_failed.jsonl`，不要反复卡住整只股票。
- `detail_workers` 初始设为 3，稳定后可小幅提高。

如果第一阶段目标只是“帖子列表全量完整”，可以增加：

```text
--skip-detail
```

先快速完成 4383 支股票的列表全量，再单独对财富号正文做补爬队列。这样总体稳定性会更好。

## 建议的落地顺序

1. 先实现 `load_stock_list()` 与 full 模式参数，让 dry-run 能从 `数据_list.csv` 发现 4383 支股票。
2. 实现 sequential 版本的 `crawl_post_info_since(start_date)`，先保证日期边界正确。
3. 加 full manifest、页级失败记录、Stage 3 强校验。
4. 再加入 `find_boundary_page_since()` 和并发分页，把速度提上来。
5. 用 `--limit 5` 跑 5 支股票，对比页面日期范围和输出行数。
6. 用 `--limit 50` 做稳定性试跑，观察验证页、失败页、平均耗时。
7. 最后跑全量 4383 支。

## 验收标准

每只股票 done 前必须满足：

- 输出文件存在：`data/{stock}_full_20090101.csv`
- `post_publish_time` 最小日期不早于 `2009-01-01`
- `post_id` 无重复
- manifest 中 `failed_pages` 为空
- manifest 中 `rows == CSV 实际行数`
- Stage 2 失败的财富号只计入 `detail_failed_count`，不影响帖子列表完整性

批量验收建议额外生成一个总表：

```text
data/full_20090101_summary.csv
```

字段包括：

```text
stock, rows, unique_post_ids, min_time, max_time, page_count, failed_pages, detail_failed_count, seconds, output_mb
```

这样可以快速发现异常股票，例如 `rows=0`、`failed_pages>0`、`min_time<2009-01-01` 或输出大小明显异常。
