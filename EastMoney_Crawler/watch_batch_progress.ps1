# watch_batch_progress.ps1 — 批量爬取实时进度面板
param(
    [string]$ProgressDir = "batch_progress_full_20090101",
    [int]$RefreshSeconds = 10
)

$ErrorActionPreference = "SilentlyContinue"

function Show-LiveDashboard {
    Clear-Host
    if (-not (Test-Path $ProgressDir)) {
        Write-Host "== 进度目录不存在: $ProgressDir ==" -ForegroundColor Red
        return
    }

    $allFiles = Get-ChildItem $ProgressDir -File

    # ── 按状态分组 ──
    $doneFiles    = $allFiles | Where-Object { $_.Name -match "\.done$" }
    $failedFiles  = $allFiles | Where-Object { $_.Name -match "\.failed$" -and $_.Name -notmatch "upload" }
    $upfailFiles  = $allFiles | Where-Object { $_.Name -match "\.failed_upload$" }
    $lockFiles    = $allFiles | Where-Object { $_.Name -match "\.lock$" }
    $retryFiles   = $allFiles | Where-Object { $_.Name -match "\.retrying$" }
    $progressFiles = $allFiles | Where-Object { $_.Name -match "\.progress\.json$" }

    $done   = $doneFiles.Count
    $failed = $failedFiles.Count
    $upfail = $upfailFiles.Count
    $locks  = $lockFiles.Count
    $retry  = $retryFiles.Count
    $running = $progressFiles.Count

    # 总任务数
    $totalTasks = $done + $failed + $upfail + $locks + $retry + $running
    if (Test-Path "数据_list.csv") {
        $totalTasks = (Get-Content "数据_list.csv" | Where-Object { $_ -match '\d{6}' }).Count
    }

    # ── 已完成的股票代码 ──
    $doneStocks = @{}
    $doneFiles | ForEach-Object { $doneStocks[$_.BaseName] = $true }
    $failedStocks = @{}
    $failedFiles | ForEach-Object { $failedStocks[$_.BaseName] = $true }
    $upfailStocks = @{}
    $upfailFiles | ForEach-Object { $upfailStocks[$_.BaseName] = $true }

    # ── 标题栏 ──
    Write-Host "╔══════════════════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host ("║  batch crawl progress    [{0}]  ║" -f $ProgressDir) -ForegroundColor Cyan
    Write-Host "╠══════════════════════════════════════════════════════════════════════════════╣" -ForegroundColor Cyan
    Write-Host ("║  done: {0,-4}  failed: {1,-4}  upload_fail: {2,-4}  pending: {3,-4}           ║" -f $done, $failed, $upfail, ($totalTasks - $done - $failed - $upfail)) -ForegroundColor Cyan
    $pct = if ($totalTasks -gt 0) { [math]::Round(($done + $upfail) / $totalTasks * 100, 1) } else { 0 }
    Write-Host ("║  progress: {0}%  ({1}/{2})                                                  ║" -f $pct, ($done + $upfail), $totalTasks) -ForegroundColor Cyan
    Write-Host "╚══════════════════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    # ── 运行中任务（从 .progress.json 读取实时进度）──
    if ($running -gt 0 -or $locks -gt 0) {
        Write-Host ("{0,-10} {1,-10} {2,-8} {3,-18} {4,-10} {5,-8}" -f "stock", "worker", "stage", "progress", "speed", "ETA") -ForegroundColor White
        Write-Host ("{0}" -f ("-" * 70)) -ForegroundColor Gray

        $shownStocks = @{}

        # 先显示有实时进度的
        $progressFiles | Sort-Object LastWriteTime -Descending | ForEach-Object {
            $stock = $_.BaseName
            $shownStocks[$stock] = $true
            $p = $null
            try { $p = Get-Content $_.FullName -Raw | ConvertFrom-Json } catch {}
            if (-not $p) { return }

            $worker = ""
            $lockPath = Join-Path $ProgressDir "$stock.lock"
            if (Test-Path $lockPath) {
                try { $l = Get-Content $lockPath -Raw | ConvertFrom-Json; $worker = $l.worker_id } catch {}
            }

            $stage = "$($p.stage_label)"
            $status = if ($p.status -eq "done") { "done" } elseif ($p.status -eq "running") { ">" } else { $p.status }
            $prog = if ($p.progress) { "$($p.progress) ($($p.pct)%)" } else { $status }
            $speed = if ($p.speed) { $p.speed } else { "" }
            $eta = if ($p.eta) { $p.eta } else { "" }

            $color = if ($p.pct -ge 90) { "Green" } else { "Yellow" }
            Write-Host ("{0,-10} {1,-10} {2,-8} {3,-18} {4,-10} {5,-8}" -f $stock, $worker, $stage, $prog, $speed, $eta) -ForegroundColor $color
        }

        # 显示有锁但还没有进度的
        $lockFiles | ForEach-Object {
            $stock = $_.BaseName
            if ($shownStocks.ContainsKey($stock)) { return }
            $worker = ""
            try { $l = Get-Content $_.FullName -Raw | ConvertFrom-Json; $worker = $l.worker_id } catch {}
            $age = [math]::Round(((Get-Date) - $_.LastWriteTime).TotalMinutes, 1)
            Write-Host ("{0,-10} {1,-10} {2,-8} {3,-18} {4,-10} {5,-8}" -f $stock, $worker, "starting", "lock_age_${age}m", "", "") -ForegroundColor DarkYellow
        }
        Write-Host ""
    }
    elseif ($locks -eq 0 -and $running -eq 0 -and $done -gt 0) {
        Write-Host "  All tasks completed!" -ForegroundColor Green
        Write-Host ""
    }

    # ── 失败任务 ──
    if ($failed -gt 0) {
        Write-Host "== Failed ==" -ForegroundColor Red
        $failedFiles | Sort-Object LastWriteTime -Descending | ForEach-Object {
            $stock = $_.BaseName
            try {
                $f = Get-Content $_.FullName -Raw | ConvertFrom-Json
                Write-Host "  $stock  reason: $($f.reason)"
            } catch {
                Write-Host "  $stock"
            }
        }
        Write-Host ""
    }

    # ── 输出文件概览 ──
    $dataDir = "data"
    if (Test-Path $dataDir) {
        $csvs = Get-ChildItem $dataDir -Filter "*_full_*.csv" -File
        if ($csvs.Count -gt 0) {
            $totalSize = [math]::Round(($csvs | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
            Write-Host "== output: data/ ==" -ForegroundColor Gray
            Write-Host "  CSVs: $($csvs.Count)  total: ${totalSize}MB"
            Write-Host ""
        }
    }

    Write-Host ("refresh: ${RefreshSeconds}s  |  last: {0:HH:mm:ss}  |  Ctrl+C to exit" -f (Get-Date)) -ForegroundColor Gray
}

# ── 主循环 ──
Write-Host "Progress panel starting... (Ctrl+C to exit)" -ForegroundColor Cyan
while ($true) {
    Show-LiveDashboard
    Start-Sleep -Seconds $RefreshSeconds
}