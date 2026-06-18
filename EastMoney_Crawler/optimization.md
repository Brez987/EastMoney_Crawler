# 2000 支股票批量跑数作战手册

## 目标与当前基线

目标：在 30 天内跑完约 **2000 支股票** 的 `Stage 1 → Stage 2 → Stage 3` 流水线。

最低吞吐要求：

- 2000 支 / 30 天 ≈ **67 支/天**
- 推荐目标：**80 支/天以上**，给失败重试、网络波动和人工检查留余量

当前代码事实：

- `auto_pipeline_000001.py` 已支持 `--stock`，无需改源码切股票。
- `auto_pipeline_000001.py` 已支持 `--detail-workers`，默认 `3`。
- Stage 2 已不爬评论，不要求 MongoDB。
- 普通股吧帖正文默认用 `post_title` 填充，不再打开详情页。
- 财富号正文走多线程 `requests`。
- 财富号正文失效/空正文会写入 `{stock}_detail_failed.jsonl`，后续重跑会跳过。
- Stage 3 当前默认 `SKIP_BAIDU_UPLOAD=True`，产物复制到 `data/{stock}_enhanced.csv`。

默认批量运行档位：

| 档位 | batch worker | 每个 worker 的 `--detail-workers` | 适用场景 |
|------|--------------|-----------------------------------|----------|
| 保守档 | 2 | 2 | 8GB 内存或需要先验证稳定性 |
| 推荐档 | 3 | 3 | 默认全量跑数档位 |
| 加速档 | 4 | 3 | 16GB+ 内存且 20 支试运行稳定 |

先跑 **20 支试运行**。如果平均每支超过 **30 分钟**，不要直接全量跑，应先检查失败原因、磁盘 I/O、财富号失效率和网络状态。

---

## 一、数据来源与股票发现

批量脚本不要强依赖 `数据_list.csv` 的文件名或编码。更稳妥的做法是从股票 CSV 文件自动发现股票代码。

推荐股票发现顺序：

1. 扫描历史 CSV 目录中的 `*.csv`，文件名匹配 `^\d{6}\.csv$`。
2. 同时扫描项目根目录中的 `*.csv`，排除 `数据_list.csv`、`方象_list.csv` 等非股票文件。
3. 如果未来保留 list 文件，只作为补充输入，不作为唯一来源。

推荐新增参数：

```powershell
python auto_pipeline_000001.py --stock 000002 --stage 1 --source-dir e:\guba_project\EastMoney_Crawler\数据
```

`--source-dir` 行为建议：

- Stage 1 优先查找 `--source-dir\{stock}.csv`。
- 其次查找项目根目录 `{stock}.csv`。
- 找到后直接复制到 `temp_extract/{stock}_base.csv`。
- 不要让 batch worker 反复复制大 CSV 到项目根目录，减少 I/O 和人为清理负担。

如果暂时不实现 `--source-dir`，batch worker 必须在运行 Stage 1 前确认 `{stock}.csv` 已位于项目根目录，否则直接标记 `.failed`，不要进入无意义重试。

---

## 二、批量执行设计

采用队列式 Master-Worker 模式：

- `batch_launcher.ps1`：唯一入口，一次启动 N 个 worker 窗口。
- `batch_worker.py`：循环抢占股票、串行执行 Stage 1/2/3、写状态文件。
- `batch_progress/`：共享状态目录。

状态文件固定为：

| 文件 | 含义 |
|------|------|
| `{stock}.lock` | 某 worker 正在处理该股票 |
| `{stock}.done` | 该股票三阶段成功完成 |
| `{stock}.failed` | 该股票失败，需人工或后续补跑 |
| `{stock}.retrying` | 当前处于自动重试中 |
| `{stock}.failed_upload` | Stage 3 上传失败，但本地产物保留 |
| `{stock}.summary.json` | 本股票耗时、输出大小、退出码、失败原因摘要 |

Worker 串行执行每只股票：

```powershell
python auto_pipeline_000001.py --stock {stock} --stage 1
python auto_pipeline_000001.py --stock {stock} --stage 2 --detail-workers 3
python auto_pipeline_000001.py --stock {stock} --stage 3
```

每完成一只股票，写入 `{stock}.summary.json`：

```json
{
  "stock": "000002",
  "worker_id": "worker_1",
  "stage1_seconds": 120,
  "stage2_seconds": 420,
  "stage3_seconds": 30,
  "total_seconds": 570,
  "enhanced_csv_mb": 118.4,
  "detail_failed_count": 12,
  "exit_code": 0,
  "finished_at": "2026-06-18T14:00:00"
}
```

文件锁协议：

- 使用 `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` 原子创建 `{stock}.lock`。
- 创建成功才拥有该股票处理权。
- 已存在 `.done`、`.failed` 的股票默认跳过。
- stale lock 默认 **3 小时**。超过 3 小时且 lock 无心跳更新，允许其他 worker 回收。

Worker 每 60 秒刷新当前 `.lock` 文件 mtime，作为心跳。

---

## 三、失败与恢复硬规则

单股票最多自动重试 **2 次**。超过后写 `.failed`，继续下一只，避免单只股票拖垮全局进度。

失败分类：

| 场景 | 处理 |
------|------|
| Stage 1 缺 CSV | 直接 `.failed`，原因 `missing_source_csv` |
| CSV 字段缺失或编码不可读 | 直接 `.failed`，原因 `invalid_csv_schema` |
| 磁盘剩余低于 20GB | 暂停抢新任务，不标记股票失败 |
| Stage 2 财富号失效/空正文 | 不算失败，写 `{stock}_detail_failed.jsonl`，重跑跳过 |
| Stage 2 进程异常退出 | 重试，最多 2 次 |
| Stage 3 本地合并失败 | 重试，最多 2 次；仍失败写 `.failed` |
| Stage 3 上传失败 | 保留本地产物，写 `.failed_upload`，不删除输出 |
| worker 窗口崩溃 | lock 超过 3 小时后由其他 worker 回收 |

断电/重启恢复：

```powershell
cd e:\guba_project\EastMoney_Crawler
.\batch_launcher.ps1 -WorkerCount 3
```

worker 启动后自动跳过 `.done`，回收 stale `.lock`，继续处理 pending 股票。

---

## 四、磁盘与产物策略

默认先使用本地留存：

- Stage 3 输出：`data/{stock}_enhanced.csv`
- 先跑 20 支，记录平均输出大小。

全量 2000 支前必须二选一：

| 方案 | 条件 | 做法 |
|------|------|------|
| 网盘生产模式 | bypy 稳定、上传带宽足够 | 设置 `SKIP_BAIDU_UPLOAD=False`，Stage 3 上传后清理本地临时文件 |
| 本地生产模式 | 无稳定网盘 | 准备至少 `250GB` 可用空间，每 200 支归档/转移一次 |

Worker 启动每只股票前检查磁盘：

- 剩余空间 `< 20GB`：暂停抢新股票，打印告警。
- 剩余空间 `< 10GB`：所有 worker 应停止，等待人工清理。

每只股票完成后建议清理：

- 保留：`data/{stock}_enhanced.csv`
- 保留：`batch_progress/{stock}.done`
- 保留：`batch_progress/{stock}.summary.json`
- 删除：`temp_export/{stock}_*.csv`
- 删除：`.pipeline_flags/{stock}_stage*.done`
- 删除：`batch_progress/{stock}.lock`
- 可保留：`temp_extract/{stock}_detail_failed.jsonl`，用于后续审计财富号失效情况

---

## 五、分阶段运行计划

### Phase 0：单只验证

目标：确认当前三阶段对 `000001` 或任意一只股票可完整跑通。

命令：

```powershell
python auto_pipeline_000001.py --stock 000001 --stage 1
python auto_pipeline_000001.py --stock 000001 --stage 2 --detail-workers 3
python auto_pipeline_000001.py --stock 000001 --stage 3
```

通过门槛：

- 生成 `data/000001_enhanced.csv`。
- Stage 2 不爬评论、不要求 MongoDB。
- 财富号失效不会导致长时间卡死。

### Phase 1：20 支试运行

目标：验证真实吞吐、磁盘增长、失败率。

建议：

- `WorkerCount=2` 或 `3`
- `--detail-workers 3`
- 记录每只股票 summary

进入下一阶段门槛：

- 成功率 `>= 90%`
- 平均每支耗时 `<= 30 分钟`
- 无重复处理同一股票
- 磁盘增长符合预估

### Phase 2：200 支小批量

目标：验证 overnight 稳定性。

进入下一阶段门槛：

- 连续运行 8 小时以上无系统性卡死。
- stale lock 能自动恢复。
- `.failed` 可解释，不出现大量同类错误。
- 输出 CSV 可正常打开、字段完整。

### Phase 3：全量 2000 支

目标：按 `batch_progress/*.done` 推进。

每日检查：

```powershell
(Get-ChildItem batch_progress\*.done).Count
(Get-ChildItem batch_progress\*.failed).Count
(Get-ChildItem batch_progress\*.failed_upload).Count
Get-ChildItem batch_progress\*.lock | Select-Object Name,LastWriteTime
```

全量完成标准：

- `.done >= 2000`
- `.failed` 有明确原因，可单独补跑
- `data/` 或网盘中存在对应 enhanced CSV

---

## 六、实现清单

### 必做

1. 新增 `batch_worker.py`
   - 股票发现
   - 原子锁抢占
   - Stage 1/2/3 subprocess 调用
   - 重试与失败分类
   - summary JSON 输出
   - stale lock 回收
   - 磁盘空间检查

2. 新增 `batch_launcher.ps1`
   - 参数：`-WorkerCount`
   - 默认启动 3 个 PowerShell worker
   - 每个 worker 带唯一 `--worker-id`

3. 建议增强 `auto_pipeline_000001.py`
   - 增加 `--source-dir`
   - Stage 1 支持从历史 CSV 目录读取 `{stock}.csv`

### 暂不做

- 不恢复评论爬取。
- 不把普通股吧帖正文改回详情页爬取。
- 不追求过高 worker 数，先以稳定跑完为目标。

---

## 七、测试与验收

实现后先跑：

```powershell
python -m py_compile auto_pipeline_000001.py crawler.py parser.py batch_worker.py
python -m unittest discover -s tests
```

批量脚本 dry-run：

```powershell
python batch_worker.py --dry-run --limit 5
```

验收点：

- dry-run 只打印将处理股票，不创建 `.done`。
- 3 支股票小批量时，多个 worker 不会重复处理同一股票。
- 手动杀掉一个 worker 后，超过 stale lock 时间可恢复。
- 人为移除一个 CSV，会写 `.failed` 并继续下一只。
- Stage 3 没有评论 CSV 时仍正常产出 enhanced CSV。

全量前验收门槛：

- 20 支试运行成功率 `>= 90%`。
- 平均每支耗时 `<= 30 分钟`。
- 没有重复处理同一股票。
- 磁盘增长与预估一致。

---

## 八、默认运行命令

试运行：

```powershell
cd e:\guba_project\EastMoney_Crawler
.\batch_launcher.ps1 -WorkerCount 2
```

推荐生产运行：

```powershell
cd e:\guba_project\EastMoney_Crawler
.\batch_launcher.ps1 -WorkerCount 3
```

如果 20 支试运行稳定且内存充足，再考虑：

```powershell
.\batch_launcher.ps1 -WorkerCount 4
```

不要一开始就开高并发。当前优先级是 **稳妥完成 2000 支**，不是追求单日最高速度。
