# 东方财富股吧全量爬虫

从东方财富股吧批量爬取股票帖子数据，支持全量模式（full），从指定日期起抓取全部帖子。

## 环境准备

### 1. 安装 Python 依赖

```powershell
cd EastMoney_Crawler
pip install -r requirements.txt
```

### 2. 准备股票列表

项目根目录下有 `数据_list.csv`，每行一个 6 位股票代码：

```
000001
000002
000003
...
```

可按需增删股票。

## 快速开始

### 第一步：dry-run 确认股票发现正常

```powershell
cd EastMoney_Crawler
python batch_worker.py --crawl-mode full --stock-list .\数据_list.csv --dry-run --limit 10
```

### 第二步：小规模试跑（1-2 只股票验证链路）

```powershell
.\batch_launcher.ps1 -WorkerCount 1 -DetailWorkers 3 -ListWorkers 6 -CrawlMode full -StartDate 2009-01-01 -ListSource html -Limit 2
```

启动后会自动打开实时进度面板窗口，显示每只股票的爬取进度。

### 第三步：批量生产运行

```powershell
.\batch_launcher.ps1 -WorkerCount 4 -DetailWorkers 3 -ListWorkers 6 -CrawlMode full -StartDate 2009-01-01 -ListSource html
```

## 运行原理

全量模式分三个阶段自动执行：

| 阶段 | 功能 |
|------|------|
| Stage 1 | 多线程抓取列表页 HTML，解析帖子列表，按日期过滤 |
| Stage 2 | 普通股吧帖用标题填充正文；财富号帖子用 curl_cffi（Chrome 120 指纹）+ 浏览器 Cookie 预热多线程爬取正文 |
| Stage 3 | 校验数据完整性（行数、去重、日期范围），导出最终 CSV |

输出文件：`data\{股票代码}_full_20090101.csv`

## 监控进度

### 实时面板（自动启动）

启动 launcher 后自动打开进度面板，也可手动启动：

```powershell
.\watch_batch_progress.ps1 -ProgressDir "batch_progress_full_20090101" -RefreshSeconds 5
```

### 命令行查看

```powershell
# 已完成 / 失败
(Get-ChildItem batch_progress_full_20090101\*.done).Count
(Get-ChildItem batch_progress_full_20090101\*.failed).Count
```

### 面板异常提示

- `STALE(Nm)`：进度文件 N 分钟未更新，爬虫可能卡死，worker 会在超时后自动跳过
- `BLOCKED`：被东方财富反爬拦截，面板会显示红色警告和操作建议
- 连续失败达到阈值后 worker 自动退出，防止 IP 被封

## 参数说明

### batch_launcher.ps1

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-WorkerCount` | 3 | 并行 worker 数量 |
| `-DetailWorkers` | 3 | 每只股票 Stage 2 并发数 |
| `-ListWorkers` | 6 | Stage 1 列表页并发数 |
| `-CrawlMode` | `full` | 爬取模式 |
| `-StartDate` | `2009-01-01` | 起始日期 |
| `-ListSource` | `html` | 列表抓取方式 |
| `-Limit` | 0 | 限制处理股票数量（试跑用，0 为不限制） |
| `-NoWatch` | — | 禁用自动进度面板 |
| `-RetryFailed` | — | 重试已失败的股票 |
| `-Visible` | — | 显示 worker 窗口 |
| `-StockTimeoutMinutes` | 60 | 单股单阶段最大空闲分钟数，超时自动跳过 |
| `-MaxConsecutiveFailures` | 5 | 连续失败上限，达到后自动退出 |

### auto_pipeline_000001.py（单只股票）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--stock` | — | 股票代码 |
| `--stage` | — | 阶段（1/2/3） |
| `--crawl-mode` | `incremental` | `full` 或 `incremental` |
| `--start-date` | `2009-01-01` | 起始日期 |
| `--list-workers` | 6 | 列表页并发数 |
| `--list-page-limit` | 0 | 试跑页数限制 |
| `--detail-workers` | 4 | Stage 2 并发数 |
| `--force-full-refresh` | — | 清空临时产物后重爬 |

## 常见问题

### 股票卡死不动

面板会显示 `STALE` 或 `BLOCKED` 警告。处理方式：

1. 降低并发：`-WorkerCount 1 -ListWorkers 3`
2. 缩短超时：`-StockTimeoutMinutes 20`
3. 更换 IP 后重启

### 连续失败退出

worker 连续失败 5 次后自动退出。建议：

1. 检查网络连接和 IP 是否被封
2. 降低并发参数
3. 等待几分钟后用 `-RetryFailed` 重启

### 磁盘空间不足

默认低于 20GB 时暂停。全量 5000+ 只股票建议准备 250GB+ 可用空间。

### 断电/重启后恢复

直接重新运行 launcher 即可，worker 会自动跳过已完成的股票。

### 排查失败股票

```powershell
# 查看失败原因
Get-Content batch_progress_full_20090101\{股票代码}.failed

# 清理后重跑单只股票
Remove-Item .\temp_extract\{股票代码}_full_posts.csv -Force
Remove-Item .\temp_extract\{股票代码}_full_manifest.json -Force
Remove-Item .\.pipeline_flags\{股票代码}_*.done -Force
Remove-Item .\batch_progress_full_20090101\{股票代码}.* -Force
python auto_pipeline_000001.py --stock {股票代码} --stage 1 --crawl-mode full --start-date 2009-01-01 --list-workers 6
python auto_pipeline_000001.py --stock {股票代码} --stage 2 --crawl-mode full
python auto_pipeline_000001.py --stock {股票代码} --stage 3 --crawl-mode full
```
