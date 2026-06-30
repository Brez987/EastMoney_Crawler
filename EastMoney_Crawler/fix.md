# 东方财富股吧全量爬虫 — 问题修复与优化方案

> **分析时间**：2026-06-30  
> **对比基线**：[股吧爬虫问题分析文档.md](./股吧爬虫问题分析文档.md)（另一电脑运行环境）  
> **当前代码库**：`e:\guba_project\EastMoney_Crawler\`

---

## 问题逐项对照

| # | 问题 | 文档状态 | 当前代码状态 | 说明 |
|---|------|:------:|:----------:|------|
| 1 | Windows 文件锁定冲突 | 已修复 | **未修复** | `write_json()` 无重试逻辑，`_safe_replace()` 不存在 |
| 2 | UnicodeDecodeError | 未修复 | **未修复** | `resp.text` 无多编码容错链 |
| 3 | Worker 静默退出 | 未修复 | **部分修复** | 已添加超时跳过 + 连续失败退出，但 `find_next_claim()` 仍无重试等待 |
| 4 | Python stdout 缓冲 | 未修复 | **未修复** | `PYTHONUNBUFFERED` 未设置，`batch_worker.py` 无行缓冲 |
| 5 | 路径空格解析错误 | 已修复 | **已修复** | `ProgressDir` 已加转义引号 |
| 6 | 多 Worker 竞争 | 部分缓解 | **部分缓解** | 原子锁 + 3h 过期检查，但过期时间过长 |
| 7 | Stage 1 失败 | 未定位根因 | **仍存在** | 4 只失败股票无具体错误信息 |
| 8 | 零磁盘预警 | 低危 | **未修复** | 无趋势预测，仅瞬时检查 |

---

## 问题 1：Windows 文件锁定冲突（PermissionError）⚠️ 高危

### 当前代码状态

`batch_worker.py` 第 190-196 行：

```python
def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)
```

**问题**：`os.replace` 在 Windows 上遇到文件被其他进程（反病毒软件、Windows 索引、另一个 Worker）持有时会直接抛出 `PermissionError`，无重试。

`auto_pipeline_000001.py` 中 `_safe_replace()` 函数在当前代码中**不存在**。

### 触发场景
- 多 Worker 同时写入同一股票进度文件（`.failed`、`.retrying`）
- Stage 3 合并 CSV 时 `shutil.move` 与反病毒软件冲突
- Worker 崩溃后残留文件句柄未释放

### 优化方案

**1.1 `write_json()` 加重试（P0）**

```python
def write_json(path: Path, payload: dict, max_retries: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    for attempt in range(1, max_retries + 1):
        try:
            os.replace(tmp_path, path)
            return
        except (PermissionError, FileNotFoundError) as e:
            if attempt == max_retries:
                raise
            delay = 0.5 * attempt
            print(f"[write_json] retry {attempt}/{max_retries} after {delay}s: {e}")
            time.sleep(delay)
```

**1.2 临时文件名加 PID 后缀（P1）**

```python
tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
```

避免多 Worker 写入同名临时文件互相覆盖。

**1.3 `auto_pipeline_000001.py` 新增 `_safe_replace()`（P1）**

```python
def _safe_replace(src: str, dst: str, max_retries: int = 5) -> None:
    """原子替换文件，Windows 下处理 PermissionError 重试。"""
    for attempt in range(1, max_retries + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == max_retries:
                raise
            time.sleep(0.5 * attempt)
```

并将所有 `os.replace()` / `shutil.move()` 调用替换为 `_safe_replace()`。

---

## 问题 2：UnicodeDecodeError — 财富号返回非 UTF-8 数据 ⚠️ 中危

### 当前代码状态

`parser.py` 第 452 行（`_try_requests_caifuhao`）：

```python
text = resp.text or ''
```

`parser.py` 第 312、349 行（`_try_wap_caifuhao`）：

```python
brief_data = self._parse_json_or_jsonp(brief_resp.text)
data = self._parse_json_or_jsonp(resp.text)
```

**问题**：`resp.text` 在 requests 库中通常不会抛 `UnicodeDecodeError`（requests 内部用 chardet 检测编码并 fallback 到 ISO-8859-1）。但 `curl_cffi` 库的 `resp.text` 行为可能不同，且 `resp.content` 直接 decode 时无容错。

### 触发场景
- 财富号返回 gzip 压缩异常的二进制数据
- 反爬页面返回非 UTF-8 编码内容（如 GBK）
- `curl_cffi` 的 response 对象与标准 requests 行为不一致

### 优化方案

**2.1 `_try_requests_caifuhao` 增加编码容错链（P1）**

```python
# 替换 line 452: text = resp.text or ''
try:
    text = resp.text or ''
except (UnicodeDecodeError, LookupError):
    try:
        text = resp.content.decode('utf-8', errors='replace')
    except Exception:
        try:
            text = resp.content.decode('gbk', errors='replace')
        except Exception:
            text = resp.content.decode('latin-1', errors='replace')
```

**2.2 `_try_wap_caifuhao` JSON 解析容错（P1）**

```python
# 替换 line 312: brief_data = self._parse_json_or_jsonp(brief_resp.text)
try:
    raw_text = brief_resp.text
except (UnicodeDecodeError, LookupError):
    raw_text = brief_resp.content.decode('utf-8', errors='replace')
brief_data = self._parse_json_or_jsonp(raw_text)
```

同样处理 line 349 的 `resp.text`。

---

## 问题 3：Worker 静默退出（无自动恢复）⚠️ 高危

### 当前代码状态

`batch_worker.py` 第 843-859 行：

```python
def find_next_claim(stocks, args) -> ClaimedStock | None:
    stale_seconds = args.stale_lock_hours * 3600
    for stock, source_csv in stocks.items():
        claim = claim_stock(stock, source_csv, ...)
        if claim is not None:
            return claim
    return None  # 无等待，立即返回 None
```

**问题**：当只剩 1 个 Worker 且所有「下一个」股票都有锁文件（来自已死 Worker 的残留锁）时，`find_next_claim` 立即返回 None，Worker 退出。3 小时 stale 过期时间内，系统完全停滞。

**本次会话已添加的修复**：
- 单股超时自动跳过（`--stock-timeout-minutes`）
- 连续失败自动退出（`--max-consecutive-failures`）

**仍缺失**：
- `find_next_claim` 无重试等待机制
- 无外部进程守护/监控

### 优化方案

**3.1 `find_next_claim` 增加等待重试（P0）**

```python
def find_next_claim(
    stocks: dict[str, Path | None],
    args: argparse.Namespace,
    max_wait_seconds: float = 120.0,
) -> ClaimedStock | None:
    stale_seconds = args.stale_lock_hours * 3600
    start = time.time()
    while True:
        for stock, source_csv in stocks.items():
            claim = claim_stock(
                stock, source_csv, args.progress_dir,
                args.worker_id, stale_seconds=stale_seconds,
                retry_failed=args.retry_failed,
            )
            if claim is not None:
                return claim
        elapsed = time.time() - start
        if elapsed >= max_wait_seconds:
            print(f"[{args.worker_id}] no pending stock after {elapsed:.0f}s wait")
            return None
        print(f"[{args.worker_id}] all stocks locked/busy, retrying in 30s...")
        time.sleep(30)
```

**3.2 缩短 stale lock 默认值（P0）**

`batch_worker.py` 第 937 行：`stale_lock_hours` 默认值从 `3.0` 改为 `1.0`。

```python
parser.add_argument("--stale-lock-hours", type=float, default=1.0,
                    help="Hours before a lock file is considered stale")
```

`batch_launcher.ps1` 第 7 行同步修改：

```powershell
[double]$StaleLockHours = 1,
```

**3.3 外部守护脚本（P1）**

创建一个简单的 PowerShell 守护脚本，每 5 分钟检查 Python 进程数，为 0 则自动重启 launcher：

```powershell
# daemon.ps1
param([int]$CheckSeconds = 300)
while ($true) {
    $pyCount = (Get-Process python -ErrorAction SilentlyContinue).Count
    if ($pyCount -eq 0) {
        Write-Host "$(Get-Date) No Python processes, restarting launcher..."
        .\batch_launcher.ps1 -WorkerCount 3 -CrawlMode full -StartDate 2009-01-01 -ListSource html
    }
    Start-Sleep -Seconds $CheckSeconds
}
```

---

## 问题 4：Python stdout 缓冲导致日志不可见 ⚠️ 中危

### 当前代码状态

`batch_launcher.ps1` 第 30-31 行：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

**缺失**：`$env:PYTHONUNBUFFERED = "1"` 未设置。

`batch_worker.py`：无 `sys.stdout.reconfigure(line_buffering=True)`。

`auto_pipeline_000001.py` 第 36-39 行：

```python
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
```

**缺失**：未设置 `line_buffering=True`。

### 影响
- `batch_logs/worker_*.out.log` 长期为空或 8KB 块写入
- 无法判断 Worker 是卡死还是正常跑
- 本次会话中多次误判 Worker 已退出

### 优化方案（5 分钟修复）

**4.1 `batch_launcher.ps1` 设置环境变量（P0）**

```powershell
$env:PYTHONUNBUFFERED = "1"    # 新增
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

**4.2 `batch_worker.py` 开头添加行缓冲（P0）**

```python
# 在 imports 之后、main 之前
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
```

**4.3 `auto_pipeline_000001.py` 已有 reconfigure 处增加 `line_buffering`（P0）**

```python
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
```

---

## 问题 5：路径空格解析错误

**已修复**，无需处理。

---

## 问题 6：多 Worker 竞争同一股票 ⚠️ 中危

### 当前代码状态

`claim_stock()` 使用 `os.O_CREAT | os.O_EXCL` 实现原子文件锁，设计正确。但：

- **stale lock 过期时间**：默认 3 小时（`batch_worker.py` line 937），过长
- **无 worker_id 交叉校验**：写入 `.done`/`.failed` 时不检查是否由当前 Worker 持有锁

### 优化方案

**6.1 缩短 stale lock 默认值（P0）** — 同问题 3.2

**6.2 `write_json` 写入前校验 worker_id（P1）**

```python
def mark_state(progress_dir, stock, suffix, payload):
    lock_path = state_path(progress_dir, stock, ".lock")
    # 如果锁不存在或 worker_id 不匹配，说明被其他 Worker 抢占
    if lock_path.exists():
        try:
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            if lock_data.get("worker_id") != payload.get("worker_id"):
                print(f"[{payload.get('worker_id')}] lock stolen by {lock_data.get('worker_id')}, skipping write")
                return
        except Exception:
            pass
    write_json(state_path(progress_dir, stock, suffix), payload)
```

---

## 问题 7：Stage 1 失败 — 4 只股票全部 3 次重试失败 ⚠️ 中危

### 当前代码状态

失败股票的错误信息仅记录为 `stage1_exit_1`，stderr 被 `Start-Process` 吞掉，无法定位根因。

### 优化方案

**7.1 stderr 合并到 stdout（P0）**

`batch_worker.py` 第 508-516 行已设置 `stderr=subprocess.STDOUT`，但还需确保 `auto_pipeline_000001.py` 内部异常被打印到 stdout。

**7.2 失败时输出完整错误信息（P0）**

在 `classify_stage_failure()` 中增加更多错误分类：

```python
def classify_stage_failure(stage: int, result: StageResult) -> str:
    tail = result.output_tail
    if result.returncode == -1 and "TIMEOUT" in tail:
        return f"stage{stage}_timeout"
    if "验证" in tail or "validate" in tail.lower() or "blocked" in tail.lower():
        return f"stage{stage}_blocked"
    if "ConnectionError" in tail or "Timeout" in tail or "timeout" in tail.lower():
        return f"stage{stage}_network"
    if "MemoryError" in tail or "disk" in tail.lower():
        return f"stage{stage}_resource"
    if stage == 3 and ("上传失败" in tail or "upload" in tail.lower()):
        return "upload_failed"
    return f"stage{stage}_exit_{result.returncode}"
```

**7.3 失败股票自动回退 Selenium 路线（P2）**

对连续失败 2 次的股票，自动尝试 `--list-source selenium`。

---

## 问题 8：零磁盘预警机制 ⚠️ 低危

### 当前代码状态

`batch_worker.py` 第 881-888 行：

```python
free_gb = disk_free_gb(PROJECT_DIR)
if free_gb < args.min_free_gb:
    print(f"disk free {free_gb:.2f}GB < {args.min_free_gb:.2f}GB; waiting...")
    time.sleep(args.disk_wait_seconds)
    continue
```

**缺失**：无趋势预测，无 Stage 1 启动前空间估算。

### 优化方案

**8.1 趋势预测（P2）**

```python
def estimate_stock_space(stock: str, crawl_mode: str) -> float:
    """根据已完成股票的平均大小估算新股票所需空间（GB）"""
    data_dir = DEFAULT_DATA_DIR
    if not data_dir.exists():
        return 0.5  # 默认估算 0.5 GB
    csv_files = list(data_dir.glob("*_full_*.csv"))
    if not csv_files:
        return 0.5
    avg_size = sum(f.stat().st_size for f in csv_files) / len(csv_files)
    return avg_size / (1024 ** 3) * 2  # 2x 安全系数
```

在 `process_claim` 中调用：

```python
estimated = estimate_stock_space(claim.stock, args.crawl_mode)
free_gb = disk_free_gb(PROJECT_DIR)
if free_gb < estimated * 2:
    print(f"[{args.worker_id}] insufficient disk: {free_gb:.1f}GB free, need ~{estimated:.1f}GB, waiting...")
    time.sleep(args.disk_wait_seconds)
    return False
```

---

## 优先级排序

| 优先级 | 问题 | 修复项 | 工作量 |
|:------:|------|--------|:------:|
| **P0** | 4 | stdout 行缓冲（`PYTHONUNBUFFERED` + `line_buffering`） | 5 min |
| **P0** | 1 | `write_json()` 加重试逻辑 | 15 min |
| **P0** | 3 | `find_next_claim()` 等待重试 + stale lock 缩短至 1h | 20 min |
| **P0** | 7 | `classify_stage_failure` 增加错误分类 | 10 min |
| **P1** | 2 | 财富号编码容错链 | 15 min |
| **P1** | 1 | `auto_pipeline_000001.py` 新增 `_safe_replace()` | 20 min |
| **P1** | 6 | `mark_state` 写入前校验 worker_id | 15 min |
| **P1** | 3 | 外部守护脚本 `daemon.ps1` | 15 min |
| **P2** | 8 | 磁盘趋势预测 | 30 min |
| **P2** | 7 | 失败自动回退 Selenium 路线 | 2 h |

---

## 建议执行顺序

```
第一轮（30 min，P0 全部修复）：
  1. stdout 行缓冲（问题 4）
  2. write_json 重试（问题 1）
  3. find_next_claim 等待重试 + stale lock 1h（问题 3）
  4. classify_stage_failure 增强（问题 7）

第二轮（1 h，P1 修复）：
  5. 财富号编码容错（问题 2）
  6. _safe_replace 封装（问题 1）
  7. mark_state worker_id 校验（问题 6）
  8. 守护脚本 daemon.ps1（问题 3）

第三轮（按需，P2 修复）：
  9. 磁盘趋势预测（问题 8）
  10. 失败自动回退 Selenium（问题 7）
```