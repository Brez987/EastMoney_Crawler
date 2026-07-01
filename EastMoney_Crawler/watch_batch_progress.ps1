# watch_batch_progress.ps1 - Batch crawl real-time progress dashboard
param(
    [string]$ProgressDir = "batch_progress_full_20090101",
    [int]$RefreshSeconds = 5,
    [string]$StockListFile = "",
    [int]$Limit = 0
)

$ErrorActionPreference = "SilentlyContinue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not [System.IO.Path]::IsPathRooted($ProgressDir)) {
    $ProgressDir = Join-Path $ProjectDir $ProgressDir
}
if (-not $StockListFile) {
    $defaultList = Get-ChildItem -LiteralPath $ProjectDir -Filter "*_list.csv" -File |
        Sort-Object Name |
        Select-Object -First 1
    if ($defaultList) {
        $StockListFile = $defaultList.FullName
    }
}
if ($StockListFile -and -not [System.IO.Path]::IsPathRooted($StockListFile)) {
    $StockListFile = Join-Path $ProjectDir $StockListFile
}

function Get-StockCodeFromFileName {
    param([string]$FileName)
    if ($FileName -match '^(\d{6})\.') {
        return $Matches[1]
    }
    return ($FileName -split '\.')[0]
}

function Format-Duration {
    param([double]$TotalSeconds)
    if ($TotalSeconds -le 0) { return "0h0min" }
    $h = [math]::Floor($TotalSeconds / 3600)
    $m = [math]::Floor(($TotalSeconds % 3600) / 60)
    $s = [math]::Round($TotalSeconds % 60, 0)
    if ($h -gt 0) {
        return "${h}h${m}min"
    } elseif ($m -gt 0) {
        return "0h${m}min"
    } else {
        return "0h0min${s}s"
    }
}

function Show-LiveDashboard {
    Clear-Host
    if (-not (Test-Path $ProgressDir)) {
        Write-Host "== Progress dir not found: $ProgressDir ==" -ForegroundColor Red
        Write-Host "   Waiting for batch to start..." -ForegroundColor DarkYellow
        return
    }

    $allFiles = Get-ChildItem $ProgressDir -File

    $doneFiles     = $allFiles | Where-Object { $_.Name -match "\.done$" }
    $failedFiles   = $allFiles | Where-Object { $_.Name -match "\.failed$" -and $_.Name -notmatch "upload" }
    $upfailFiles   = $allFiles | Where-Object { $_.Name -match "\.failed_upload$" }
    $lockFiles     = $allFiles | Where-Object { $_.Name -match "\.lock$" }
    $retryFiles    = $allFiles | Where-Object { $_.Name -match "\.retrying$" }
    $deferFiles    = $allFiles | Where-Object { $_.Name -match "\.deferred$" }
    $progressFiles = $allFiles | Where-Object { $_.Name -match "\.progress\.json$" }
    $summaryFiles  = $allFiles | Where-Object { $_.Name -match "\.summary\.json$" }

    $done    = $doneFiles.Count
    $failed  = $failedFiles.Count
    $upfail  = $upfailFiles.Count
    $locks   = $lockFiles.Count
    $retry   = $retryFiles.Count
    $defer   = $deferFiles.Count
    $running = $progressFiles.Count

    # Calculate total tasks
    $terminal = $done + $failed + $upfail
    $totalTasks = 0
    $listCount = 0
    if (Test-Path $StockListFile) {
        $listCount = (Get-Content $StockListFile -Encoding UTF8 | Where-Object { $_ -match '\d{6}' }).Count
    }
    if ($Limit -gt 0) {
        # Limit 模式下，总量 = 已完成数 + 本次限额，但不超过列表总数
        $totalTasks = [math]::Min($terminal + $Limit, $listCount)
        if ($totalTasks -eq 0) { $totalTasks = $Limit }
    } elseif ($listCount -gt 0) {
        $totalTasks = $listCount
    } else {
        $totalTasks = $terminal + $locks + $retry + $defer + $running
    }

    $pending = [math]::Max(0, $totalTasks - $terminal)
    $pct = if ($totalTasks -gt 0) { [math]::Round($terminal / $totalTasks * 100, 1) } else { 0 }

    # ===== HEADER =====
    $width = 78
    Write-Host ("=" * $width) -ForegroundColor Cyan
    Write-Host ("  Stock Crawler Progress Dashboard  [{0}]" -f (Split-Path $ProgressDir -Leaf)) -ForegroundColor Cyan
    Write-Host ("-" * $width) -ForegroundColor Cyan

    # Progress bar
    $barWidth = 40
    $filled = [math]::Round($barWidth * $pct / 100)
    $bar = ("#" * $filled) + ("-" * ($barWidth - $filled))
    Write-Host ("  [{0}] {1}%" -f $bar, $pct) -ForegroundColor $(if ($pct -ge 90) { "Green" } elseif ($pct -ge 50) { "Yellow" } else { "White" })

    # Task summary with limit info
    $limitInfo = ""
    if ($Limit -gt 0) {
        $limitInfo = "  (Limit=$Limit)"
    }
    Write-Host ("  总共有 {0} 支股票待爬{1}" -f $totalTasks, $limitInfo) -ForegroundColor Cyan
    Write-Host ("  Done: {0,-5}Failed: {1,-5}UploadFail: {2,-5}Pending: {3,-5}" -f $done, $failed, $upfail, $pending) -ForegroundColor Cyan
    Write-Host ("  Running: {0,-5}Retrying: {1,-5}Deferred: {2,-5}Locks: {3,-5}" -f $running, $retry, $defer, $locks) -ForegroundColor Cyan
    Write-Host ("=" * $width) -ForegroundColor Cyan
    Write-Host ""

    # ===== BUILD MAPS (use -Encoding UTF8 to match Python's output) =====
    $lockMap = @{}
    $lockFiles | ForEach-Object {
        $s = Get-StockCodeFromFileName $_.Name
        try { $l = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json; $lockMap[$s] = $l } catch {}
    }
    $retryMap = @{}
    $retryFiles | ForEach-Object {
        $s = Get-StockCodeFromFileName $_.Name
        try { $r = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json; $retryMap[$s] = $r } catch {}
    }
    $deferMap = @{}
    $deferFiles | ForEach-Object {
        $s = Get-StockCodeFromFileName $_.Name
        try { $d = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json; $deferMap[$s] = $d } catch {}
    }

    # ===== SECTION 1: RUNNING / IN-PROGRESS TASKS =====
    if ($running -gt 0 -or $locks -gt 0 -or $retry -gt 0 -or $defer -gt 0) {
        Write-Host "── 进行中的任务 ──" -ForegroundColor Yellow
        Write-Host ("{0,-8} {1,-10} {2,-24} {3,-12} {4,-8}" -f "Stock", "Stage", "Progress", "Speed", "ETA") -ForegroundColor White
        Write-Host ("-" * 68) -ForegroundColor Gray

        $shownStocks = @{}

        # ── 1) Tasks with live progress (.progress.json) ──
        $progressFiles | Sort-Object LastWriteTime -Descending | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            $shownStocks[$stock] = $true
            $p = $null
            # Try UTF8 parse; retry once after 300ms to handle atomic-replace race
            try {
                $p = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            } catch {
                Start-Sleep -Milliseconds 300
                try { $p = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
            }
            if (-not $p) {
                # JSON parsing failed after retry — fallback to lock info
                $lockInfo = $lockMap[$stock]
                $age = [math]::Round(((Get-Date) - $_.LastWriteTime).TotalSeconds, 0)
                $ageStr = if ($age -lt 60) { "${age}s" } else { "$([math]::Round($age/60,1))m" }
                if ($lockInfo) {
                    $workDesc = $lockInfo.worker_id
                } else {
                    $workDesc = "parsing($ageStr)"
                }
                Write-Host ("{0,-8} {1,-10} {2,-24} {3,-12} {4,-8}" -f $stock, "?", $workDesc, "-", "-") -ForegroundColor DarkYellow
                return
            }

            $stage = if ($p.stage_label) { "$($p.stage_label)" } else { "?" }
            $color = "White"
            $extra = ""

            if ($p.status -eq "done") {
                $prog = "Stage DONE"
                $color = "Green"
            } elseif ($p.status -eq "running") {
                $prog = if ($p.progress) { $p.progress } else { "running..." }
                if ($p.pct) { 
                    $pctVal = [int]$p.pct
                    $prog += " ($pctVal%)"
                    $color = if ($pctVal -ge 90) { "Green" } elseif ($pctVal -ge 50) { "Yellow" } else { "White" }
                }
            } elseif ($p.status -eq "blocked" -or $p.status -eq "paused_blocked") {
                $prog = "BLOCKED"
                $color = "Red"
                if ($p.reason) { $extra = " [$($p.reason)]" }
            } elseif ($p.status -eq "error") {
                $prog = "ERROR"
                $color = "Red"
                if ($p.error) { $extra = " [$($p.error)]" }
            } else {
                $prog = "$($p.status)"
                $color = "DarkYellow"
            }
            $speed = if ($p.speed) { $p.speed } else { "-" }
            $eta = if ($p.eta) { $p.eta } else { "-" }

            # Detect stale progress (no update in > 5 min)
            $ageSec = ((Get-Date) - $_.LastWriteTime).TotalSeconds
            if ($p.status -eq "running" -and $ageSec -gt 300) {
                $ageMin = [math]::Round($ageSec / 60, 1)
                $extra += " [STALE ${ageMin}m]"
                if ($color -ne "Red") { $color = "Magenta" }
            }

            if ($p.ok -ne $null) { $extra += " ok=$($p.ok)" }
            if ($p.fail_perm -ne $null -and $p.fail_perm -gt 0) { $extra += " fail=$($p.fail_perm)" }
            if ($p.empty -ne $null) { $extra += " empty=$($p.empty)" }

            Write-Host ("{0,-8} {1,-10} {2,-24} {3,-12} {4,-8}{5}" -f $stock, $stage, $prog, $speed, $eta, $extra) -ForegroundColor $color
        }

        # ── 2) Tasks with lock but no progress yet ──
        $lockFiles | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            if ($shownStocks.ContainsKey($stock)) { return }
            $shownStocks[$stock] = $true
            $lockInfo = $lockMap[$stock]
            $worker = if ($lockInfo) { $lockInfo.worker_id } else { "?" }
            $age = [math]::Round(((Get-Date) - $_.LastWriteTime).TotalSeconds, 0)
            $ageStr = if ($age -lt 60) { "${age}s" } else { "$([math]::Round($age/60,1))m" }
            $ageColor = if ($age -gt 300) { "Red" } else { "DarkYellow" }
            $lockDesc = "lock held " + $ageStr + " (" + $worker + ")"
            Write-Host ("{0,-8} {1,-10} {2,-24} {3,-12} {4,-8}" -f $stock, "init", $lockDesc, "-", "-") -ForegroundColor $ageColor
        }

        # ── 3) Retrying tasks ──
        $retryFiles | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            if ($shownStocks.ContainsKey($stock)) { return }
            $shownStocks[$stock] = $true
            $attempt = "?"
            $prevReason = ""
            if ($retryMap.ContainsKey($stock)) {
                $attempt = "attempt $($retryMap[$stock].attempt)/$($retryMap[$stock].max_retries+1)"
                $prevReason = $retryMap[$stock].previous_reason
            }
            $retryDesc = $attempt + " " + $prevReason
            Write-Host ("{0,-8} {1,-10} {2,-24} {3,-12} {4,-8}" -f $stock, "retrying", $retryDesc, "-", "-") -ForegroundColor Magenta
        }

        # ── 4) Deferred tasks ──
        $deferFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 10 | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            if ($shownStocks.ContainsKey($stock)) { return }
            $shownStocks[$stock] = $true
            $reason = ""
            $retryAfter = ""
            if ($deferMap.ContainsKey($stock)) {
                $reason = $deferMap[$stock].reason
                $retryAfter = $deferMap[$stock].retry_after
            }
            $deferDesc = $reason + " (retry @ " + $retryAfter + ")"
            Write-Host ("{0,-8} {1,-10} {2,-24} {3,-12} {4,-8}" -f $stock, "deferred", $deferDesc, "-", "-") -ForegroundColor DarkYellow
        }
        Write-Host ""
    }

    # ===== SECTION 2: RECENTLY COMPLETED =====
    if ($done -gt 0) {
        $recentDone = $doneFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 8
        $completedStocks = @()
        $recentDone | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            $duration = ""
            $csvSize = ""
            $matchedSummary = $summaryFiles | Where-Object { $_.Name -match "^${stock}\." }
            if ($matchedSummary) {
                try {
                    $s = Get-Content $matchedSummary.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                    $totalSec = [double]$s.total_seconds
                    if ($totalSec -gt 0) {
                        $duration = "耗时 " + (Format-Duration $totalSec)
                    }
                    $csvMb = [double]$s.output_csv_mb
                    if ($csvMb -gt 0) {
                        $csvSize = "$([math]::Round($csvMb, 1))MB"
                    }
                } catch {}
            }
            $completedStocks += @{ Stock = $stock; Duration = $duration; CsvSize = $csvSize }
        }
        if ($completedStocks.Count -gt 0) {
            Write-Host "── 最近完成 (${done} 只) ──" -ForegroundColor Green
            $completedStocks | ForEach-Object {
                $line = "  $($_.Stock) 已完成"
                if ($_.Duration) { $line += "，$($_.Duration)" }
                if ($_.CsvSize) { $line += "，输出 $($_.CsvSize)" }
                Write-Host $line -ForegroundColor Green
            }
            if ($done -gt 8) {
                Write-Host ("  ... 及另外 {0} 只已完成" -f ($done - 8)) -ForegroundColor DarkGray
            }
            Write-Host ""
        }
    }

    # ===== SECTION 3: NETWORK / BLOCKED WARNINGS =====
    $staleTasks = @()
    $blockedTasks = @()
    $lockedStaleTasks = @()
    $progressFiles | ForEach-Object {
        try { $p = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $p = $null }
        if ($p) {
            $ageSec = ((Get-Date) - $_.LastWriteTime).TotalSeconds
            $isBlocked = ($p.status -eq "blocked" -or $p.status -eq "paused_blocked")
            $isStale = ($p.status -eq "running" -and $ageSec -gt 300)
            $stock = Get-StockCodeFromFileName $_.Name
            if ($isBlocked) { $blockedTasks += $stock }
            if ($isStale) { $staleTasks += $stock }
        }
    }
    # 检测长时间持有 lock 但无 progress 的卡死任务
    $lockFiles | ForEach-Object {
        $stock = Get-StockCodeFromFileName $_.Name
        $ageSec = ((Get-Date) - $_.LastWriteTime).TotalSeconds
        # 如果该 stock 没有 progress 文件且 lock 超过 10 分钟
        $hasProgress = $progressFiles | Where-Object { (Get-StockCodeFromFileName $_.Name) -eq $stock }
        if (-not $hasProgress -and $ageSec -gt 600) {
            $lockedStaleTasks += $stock
        }
    }
    if ($staleTasks.Count -gt 0 -or $blockedTasks.Count -gt 0 -or $lockedStaleTasks.Count -gt 0) {
        Write-Host "╔══════════════════════════════════════════════════════════════════════╗" -ForegroundColor Red
        Write-Host "║  *** 异常警告：检测到卡死/阻塞/超时任务！ ***                       ║" -ForegroundColor Red
        Write-Host "╚══════════════════════════════════════════════════════════════════════╝" -ForegroundColor Red
        if ($blockedTasks.Count -gt 0) {
            Write-Host "  [BLOCKED] 被东方财富反爬拦截: $($blockedTasks -join ', ')" -ForegroundColor Red
        }
        if ($staleTasks.Count -gt 0) {
            Write-Host "  [STALE]   超过5分钟无进度更新: $($staleTasks -join ', ')" -ForegroundColor Red
        }
        if ($lockedStaleTasks.Count -gt 0) {
            Write-Host "  [STUCK]   lock超过10分钟无进度: $($lockedStaleTasks -join ', ')" -ForegroundColor Red
        }
        Write-Host "  可能原因: 东方财富反爬 / IP被限速 / 网络不稳定 / 进程卡死" -ForegroundColor Yellow
        Write-Host "  建议操作:" -ForegroundColor Yellow
        Write-Host "    1. 按 Ctrl+C 停止所有 worker 窗口" -ForegroundColor White
        Write-Host "    2. 更换IP（VPN / 代理 / 重启路由器）" -ForegroundColor White
        Write-Host "    3. 等待 5-10 分钟后再重启" -ForegroundColor White
        Write-Host "    4. 降低并发后重启:" -ForegroundColor White
        Write-Host "       .\batch_launcher.ps1 -WorkerCount 1 -ListWorkers 3 -Limit 2" -ForegroundColor White
        Write-Host "    5. 或缩短超时自动跳过:" -ForegroundColor White
        Write-Host "       .\batch_launcher.ps1 -WorkerCount 1 -StockTimeoutMinutes 20 -Limit 2" -ForegroundColor White
        Write-Host ""
    }

    # ===== SECTION 4: ALL DONE / NO WORKERS =====
    if ($locks -eq 0 -and $running -eq 0 -and $done -gt 0) {
        if ($pending -eq 0) {
            if ($failed -eq 0 -and $upfail -eq 0) {
                Write-Host "  *** ALL TASKS COMPLETED! ***" -ForegroundColor Green
            } else {
                Write-Host "  *** ALL TASKS COMPLETED (with failures) ***" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  *** NO ACTIVE WORKERS — $pending stocks pending! ***" -ForegroundColor Red
            Write-Host "  Restart launcher to continue:" -ForegroundColor Yellow
            Write-Host "    .\batch_launcher.ps1 -WorkerCount 1 -CrawlMode full -StartDate 2009-01-01 -ListSource html -Limit 2" -ForegroundColor White
            Write-Host "  Or resume all:" -ForegroundColor Yellow
            Write-Host "    .\batch_launcher.ps1 -WorkerCount 4 -CrawlMode full -StartDate 2009-01-01 -ListSource html" -ForegroundColor White
        }
        Write-Host ""
    }

    # ===== SECTION 5: FAILED TASKS =====
    if ($failed -gt 0) {
        Write-Host "── 失败任务 (${failed}) ──" -ForegroundColor Red
        $failedFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 10 | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            try {
                $f = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                $reason = $f.failed_reason
                if (-not $reason) { $reason = $f.reason }
                $attempts = $f.attempts
                $line = "  $stock  attempts=$attempts"
                if ($reason) { $line += "  reason=$reason" }
                # 尝试从 summary 获取更多信息
                $matchedSummary = $summaryFiles | Where-Object { $_.Name -match "^${stock}\." }
                if ($matchedSummary) {
                    try {
                        $s = Get-Content $matchedSummary.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                        $totalSec = [double]$s.total_seconds
                        if ($totalSec -gt 0) {
                            $line += "  耗时=" + (Format-Duration $totalSec)
                        }
                    } catch {}
                }
                Write-Host $line -ForegroundColor Red
            } catch {
                Write-Host "  $stock" -ForegroundColor Red
            }
        }
        if ($failed -gt 10) {
            Write-Host "  ... 及另外 $($failed - 10) 个失败任务" -ForegroundColor DarkGray
        }
        Write-Host ""
    }
    if ($upfail -gt 0) {
        Write-Host "── 上传失败 (${upfail}) ──" -ForegroundColor DarkRed
        $upfailFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 5 | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            Write-Host "  $stock" -ForegroundColor DarkRed
        }
        Write-Host ""
    }

    # ===== SECTION 6: OUTPUT OVERVIEW =====
    $dataDir = Join-Path $ProjectDir "data"
    if (Test-Path $dataDir) {
        $csvs = Get-ChildItem $dataDir -Filter "*_full_*.csv" -File
        if ($csvs.Count -gt 0) {
            $totalSize = [math]::Round(($csvs | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
            Write-Host "== Output: data/ ==" -ForegroundColor Gray
            Write-Host "  CSV files: $($csvs.Count)  Total size: ${totalSize}MB"
            Write-Host ""
        }
    }

    Write-Host ("Refresh: ${RefreshSeconds}s  |  Updated: {0:HH:mm:ss}  |  Ctrl+C to exit" -f (Get-Date)) -ForegroundColor Gray
}

# Main loop
Write-Host "Progress dashboard starting... (Ctrl+C to exit)" -ForegroundColor Cyan
Write-Host "Watching: $ProgressDir" -ForegroundColor Cyan
if ($Limit -gt 0) {
    Write-Host "Task limit: $Limit" -ForegroundColor Cyan
}
Write-Host ""
while ($true) {
    Show-LiveDashboard
    Start-Sleep -Seconds $RefreshSeconds
}