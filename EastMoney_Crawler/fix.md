# 批量爬取性能优化方案

## 已完成的优化

以下改进已在当前代码中实现，不需重复处理：

- **进度面板** (`watch_batch_progress.ps1`)：显示实时进度、完成耗时、总量限制、失败/阻塞警告
- **超时自动跳过** (`batch_worker.py`)：`--stock-timeout-minutes` + `--max-consecutive-failures`，连续超时自动退出
- **deferred 重试**：反爬触发后延迟重试，避免硬冲
- **UTF-8 编码修复**：面板 JSON 解析已统一使用 `-Encoding UTF8`

---

## 方案 1：Stage 2 财富号补爬优化

**优先级：最高。预估节省 10%-25% 总耗时。**

改动文件：`crawler.py`、`auto_pipeline_000001.py`

### 1.1 浏览器 Cookie 预热改为 lazy

**当前问题**（`crawler.py:652`）：每只股票进入财富号补爬时，无条件调用 `_bootstrap_caifuhao_session_via_browser()` 启动 Chrome，固定 sleep(3)+sleep(2)，即使代码已注释"浏览器预热失败不致命，WAP API 仍可工作"。

**改进方案**：

不提前预热，仅在以下条件触发时才启动浏览器获取 Cookie：
- WAP API 连续出现 `http_403` / `http_429` / `blocked_validation`
- 窗口内 retryable 失败率 > 10%

```python
master_cookies = []
_lazy_warmed = False

def _warm_cookie_if_needed(reason: str):
    nonlocal _lazy_warmed, master_cookies
    if _lazy_warmed:
        return
    _lazy_warmed = True
    try:
        self._bootstrap_caifuhao_session_via_browser()
        master_cookies = list(self._caifuhao_session.cookies) if self._caifuhao_session else []
    except Exception as e:
        print(f'{self.symbol}: [CAIFUHAO] lazy warmup failed ({reason}): {e}')
```

**收益**：每只股票减少 ~5s 的 Chrome 启动开销，5500 只 ≈ 7.6 小时。

### 1.2 请求节流改为自适应（简化版）

**当前问题**（`crawler.py:642-647`）：节流参数固定写死：

```python
WINDOW_PAUSE = (2.0, 5.0)
PER_REQ_DELAY = (0.1, 0.4)
```

无论服务器是否正常响应，每 50 条都暂停 2-5 秒。

**改进方案**：单档自适应，不需要 fast/balanced/conservative 三档。核心逻辑：

```python
# 初始激进值
PER_REQ_DELAY = (0.05, 0.15)
WINDOW_PAUSE = (0.3, 1.0)

# 在窗口结束时根据失败率调整
fail_rate = window_failed / max(len(window), 1)
if window_blocked:
    pause = random.uniform(30, 60)          # 硬封禁，长冷却
    PER_REQ_DELAY = (1.0, 3.0)              # 大幅降速
elif fail_rate >= 0.05:
    pause = random.uniform(3.0, 8.0)        # 高失败率，切保守
elif fail_rate > 0:
    pause = random.uniform(1.0, 3.0)        # 偶发失败，温和降速
else:
    pause = random.uniform(0.2, 0.8)        # 零失败，极速
    PER_REQ_DELAY = (0.03, 0.10)            # 进一步加速
```

新增 `--caifuhao-conservative` 开关，在夜间或已被封时锁定保守参数，日常默认自适应。

**收益**：财富号多的股票 Stage 2 耗时降低 15%-35%。

### 1.3 delta 和 checkpoint 改成批量写

**当前问题**：

- `_append_content_delta`（`auto_pipeline_000001.py:1072`）每条正文都 open/write/close，Windows 下开销大
- `_save_checkpoint`（`auto_pipeline_000001.py:1151`）每 50 条重新 `_load_checkpoint` → 合并 → 全量重写 JSON

**改进方案**：

delta 文件持有句柄，批量 flush：

```python
delta_f = open(delta_path, 'a', encoding='utf-8')
_pending = [0]

def _append_buffered(post_id, content):
    delta_f.write(json.dumps({'post_id': str(post_id), 'post_content': content}, ensure_ascii=False) + '\n')
    _pending[0] += 1
    if _pending[0] >= 50:
        delta_f.flush(); os.fsync(delta_f.fileno()); _pending[0] = 0
```

checkpoint 改内存维护，每 200 条原子写一次（不再每次读旧文件）。

**收益**：Windows 下减少大量小文件 open/close，Stage 2 更平滑。

### 1.4 普通帖标题填充与财富号正文合并一次 CSV 重写

**当前问题**（`auto_pipeline_000001.py:1188-1263`）：先 `_flush_updates_to_csv` 写 title_fills，财富号补爬结束再写 content_updates，对大 CSV 是两次全量读写。

**改进方案**：

1. title_fills 不立即 flush，合并进 content_updates
2. 财富号补爬结束后统一 flush 一次
3. 中途如果累积更新 > 5000 条或 Stage 2 已运行 > 10min，做一次中间 flush

**收益**：大多数股票只重写一次 full CSV，大股票（20MB+）明显减 I/O。

---

## 方案 2：Stage 1 列表页全量抓取优化

**优先级：最高。预估节省 10%-20% 总耗时。**

改动文件：`auto_pipeline_000001.py`、`crawler.py`

### 2.1 窗口暂停改为失败率驱动

**当前问题**（`auto_pipeline_000001.py:897`）：`window_pause_range=(3.0, 8.0)` 固定值，即使窗口内 0 失败也暂停 3-8 秒。大股票 300-800 页时这个累计开销可达几十到上百秒。

**改进方案**（同 1.2 的自适应逻辑，复用到 Stage 1）：

```python
if window_blocked:
    pause = random.uniform(30, 60)
elif fail_rate >= 0.05:
    pause = random.uniform(3.0, 8.0)
elif fail_rate > 0:
    pause = random.uniform(1.0, 3.0)
else:
    pause = random.uniform(0.2, 0.8)
```

### 2.2 暴露列表页参数到命令行

当前 `LIST_WINDOW_SIZE` 已暴露到 `batch_worker.py`。补充暴露暂停范围：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--list-window-pause-min` | 0.3 | 无失败时最小暂停 (秒) |
| `--list-window-pause-max` | 1.2 | 无失败时最大暂停 (秒) |
| `--list-window-size` | 120 | 每个窗口的页数 |

### 2.3 小股票自动减少并发

boundary_page 很小时不需要高并发：

```python
if boundary_page <= 20:
    effective_workers = min(list_workers, 2)
elif boundary_page <= 80:
    effective_workers = min(list_workers, 4)
else:
    effective_workers = list_workers
```

---

## 方案 3：三 Stage 合并为单进程

**优先级：高。预估节省 5-15s/只。**

改动文件：`auto_pipeline_000001.py`、`batch_worker.py`

**当前问题**：每只股票启动 3 个子进程（stage 1 → stage 2 → stage 3），每次重新 import pandas/selenium/requests。5500 只 ≈ 16500 次进程启动。

**改进方案**：

**第一步**：`auto_pipeline_000001.py` 新增 `--stage all`，单次进程内串行执行三个阶段。保留 `.pipeline_flags` 机制，每个 stage 成功后仍写 flag。

```python
if args.stage == 'all':
    ok = run_stage1(...)
    if ok: ok = run_stage2(...)
    if ok: ok = run_stage3(...)
    sys.exit(0 if ok else 1)
```

**第二步**（更优，后续迭代）：`batch_worker.run_stock_pipeline` 直接 import `auto_pipeline_000001` 的函数并调用，完全消除子进程开销。当前先以 `--stage all` 子进程方式验证稳定性。

---

## 方案 4：Stage 3 流式校验直写 data 目录

**优先级：中。预估节省 0%-2% 总耗时，主要减少磁盘压力。**

改动文件：`auto_pipeline_000001.py`

**当前问题**：Stage 3 先全量读入 `all_rows` → 排序 → 写 `temp_export/` → copy 到 `data/`。Stage 1 已按 `post_publish_time` 降序写出，Stage 2 不改变行顺序，所以排序通常是冗余的。

**改进方案**：

1. 只检查是否单调递减；乱序时回退到当前排序逻辑
2. 输出直接写 `data/{stock}_full_20090101.csv.tmp`，校验通过后 `os.replace`
3. 取消 `temp_export` 中间目录

---

## 方案 5：Worker 任务领取游标化

**优先级：中。减少 5000+ 大列表的文件扫描开销。**

改动文件：`batch_worker.py`

**当前问题**：每个 worker 领取下一只股票都从 `stocks.items()` 开头扫描，后期 `.done` 越多越浪费。

**改进方案**：

```python
stock_items = list(stocks.items())
cursor = abs(hash(worker_id)) % len(stock_items)  # 各 worker 错开起点

for offset in range(len(stock_items)):
    idx = (cursor + offset) % len(stock_items)
    stock, source_csv = stock_items[idx]
    claimed = claim_stock(stock, ...)
    if claimed:
        cursor = (idx + 1) % len(stock_items)  # 下次从下一个继续
        return claimed
```

---

## 方案 6：轻量吞吐监控

**优先级：低。帮助判断是否需要调参。**

改动：`watch_batch_progress.ps1` 或新增一个小脚本。

不搞复杂的推荐引擎。只需读取最近 N 个 `.summary.json` 输出：

```
已完成: 150  剩余: 5400
最近10只: 平均 5.2min/只  (Stage1:2.3min  Stage2:2.7min  Stage3:0.2min)
吞吐: ~11.5只/小时  (当前 2 worker)
估算: 约 470小时 → 需加速
建议: WorkerCount 从 2 → 4 (~23只/小时, 约235小时)
```

核心规则只有两条：
- 若最近 20 只成功率 ≥ 95% 且无 blocked → 可加 worker
- 若出现 blocked/deferred 增加 → 降 worker 或等冷却

---

## 落地顺序

### 第 1 步：Stage 2 固定开销优化（预计影响最大）

- 1.1 lazy browser warmup
- 1.3 delta/checkpoint 批量写
- 1.4 CSV 重写合并

验证：`.\batch_launcher.ps1 -WorkerCount 1 -Limit 10 -NoWatch`，对比 summary 中 Stage 2 耗时。

### 第 2 步：Stage 1 + Stage 2 自适应节流

- 1.2 财富号自适应节流
- 2.1 列表页失败率驱动暂停
- 2.3 小股票减少并发

验证：`.\batch_launcher.ps1 -WorkerCount 2 -Limit 20 -NoWatch`，确认 blocked 不增加。

### 第 3 步：单进程模式

先实现 `--stage all`，`batch_worker` 新增 `--single-process-stock` 开关（默认关）。

验证：`python batch_worker.py --worker-id test --crawl-mode full --stock-list .\数据_list.csv --limit 5 --single-process-stock`

### 第 4 步：参数暴露 + 游标 + 监控

- 2.2 暴露列表页暂停参数
- 方案 5 worker 游标
- 方案 6 吞吐监控

### 第 5 步：逐步提并发

| 档位 | WorkerCount | 说明 |
|------|-------------|------|
| 稳定 | 2-3 | 夜间/无人值守 |
| 标准 | 4-5 | 日常白天 |
| 加速 | 6 | 白天有人监控、无 blocked |
| 冲刺 | 7-8 | 仅在确认稳定时短期使用 |

原则：`WorkerCount × ListWorkers` ≤ 30，`WorkerCount × DetailWorkers` ≤ 30。

---

## 预计效果

组合优化后：
- 当前基线：~6-7 min/只
- 优化后预期：4-5 min/只
- 4 worker：~48-60 只/小时，5500 只约 92-115 小时
- 6 worker：~72-90 只/小时，5500 只约 61-76 小时

按每天 10 小时运行，6 worker 约 6-8 天可完成全量。

---

## 不建议的优化方向

- **WorkerCount > 10**：容易触发反爬，Chrome/磁盘/网络一起抖动
- **引入代理池**：增加失败面，先确认 IP 确实被限流再考虑
- **Selenium 回退为主路径**：稳定但太慢，仅保留作 fallback
- **引入新数据库**：当前 CSV-native 工作正常，瓶颈不在存储层
