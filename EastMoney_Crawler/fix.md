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
