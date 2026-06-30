# watch_batch_progress.ps1 - Batch crawl real-time progress dashboard
param(
    [string]$ProgressDir = "batch_progress_full_20090101",
    [int]$RefreshSeconds = 5,
    [string]$StockListFile = ""
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

    $done    = $doneFiles.Count
    $failed  = $failedFiles.Count
    $upfail  = $upfailFiles.Count
    $locks   = $lockFiles.Count
    $retry   = $retryFiles.Count
    $defer   = $deferFiles.Count
    $running = $progressFiles.Count

    $totalTasks = 0
    if (Test-Path $StockListFile) {
        $totalTasks = (Get-Content $StockListFile | Where-Object { $_ -match '\d{6}' }).Count
    }
    if ($totalTasks -eq 0) {
        $totalTasks = $done + $failed + $upfail + $locks + $retry + $defer + $running
    }

    $pending = [math]::Max(0, $totalTasks - $done - $failed - $upfail)
    $pct = if ($totalTasks -gt 0) { [math]::Round(($done + $upfail) / $totalTasks * 100, 1) } else { 0 }

    # Header
    $width = 78
    Write-Host ("=" * $width) -ForegroundColor Cyan
    Write-Host ("  Stock Crawler Progress Dashboard  [{0}]" -f (Split-Path $ProgressDir -Leaf)) -ForegroundColor Cyan
    Write-Host ("-" * $width) -ForegroundColor Cyan

    # Progress bar
    $barWidth = 40
    $filled = [math]::Round($barWidth * $pct / 100)
    $bar = ("#" * $filled) + ("-" * ($barWidth - $filled))
    Write-Host ("  [{0}] {1}%" -f $bar, $pct) -ForegroundColor $(if ($pct -ge 90) { "Green" } elseif ($pct -ge 50) { "Yellow" } else { "White" })

    Write-Host ("  Done: {0,-5} Failed: {1,-5} UploadFail: {2,-5} Pending: {3,-5}" -f $done, $failed, $upfail, $pending) -ForegroundColor Cyan
    Write-Host ("  Running: {0,-5} Retrying: {1,-5} Deferred: {2,-5} Locks: {3,-5} Total: {4,-5}" -f $running, $retry, $defer, $locks, $totalTasks) -ForegroundColor Cyan
    Write-Host ("=" * $width) -ForegroundColor Cyan
    Write-Host ""

    # Build lock/retry maps
    $lockMap = @{}
    $lockFiles | ForEach-Object {
        $s = Get-StockCodeFromFileName $_.Name
        try { $l = Get-Content $_.FullName -Raw | ConvertFrom-Json; $lockMap[$s] = $l } catch {}
    }
    $retryMap = @{}
    $retryFiles | ForEach-Object {
        $s = Get-StockCodeFromFileName $_.Name
        try { $r = Get-Content $_.FullName -Raw | ConvertFrom-Json; $retryMap[$s] = $r } catch {}
    }
    $deferMap = @{}
    $deferFiles | ForEach-Object {
        $s = Get-StockCodeFromFileName $_.Name
        try { $d = Get-Content $_.FullName -Raw | ConvertFrom-Json; $deferMap[$s] = $d } catch {}
    }

    # Running tasks
    if ($running -gt 0 -or $locks -gt 0 -or $defer -gt 0) {
        Write-Host ("{0,-8} {1,-10} {2,-10} {3,-20} {4,-12} {5,-8}" -f "Stock", "Worker", "Stage", "Progress", "Speed", "ETA") -ForegroundColor White
        Write-Host ("-" * 72) -ForegroundColor Gray

        $shownStocks = @{}

        # Tasks with live progress
        $progressFiles | Sort-Object LastWriteTime -Descending | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            $shownStocks[$stock] = $true
            $p = $null
            try { $p = Get-Content $_.FullName -Raw | ConvertFrom-Json } catch {}
            if (-not $p) { return }

            $worker = ""
            if ($lockMap.ContainsKey($stock)) {
                $worker = $lockMap[$stock].worker_id
            }

            $stage = "$($p.stage_label)"
            $color = "White"
            $extra = ""

            if ($p.status -eq "done") {
                $prog = "DONE"
                $color = "Green"
            } elseif ($p.status -eq "running") {
                $prog = if ($p.progress) { "$($p.progress) ($($p.pct)%)" } else { "running" }
                $color = if ($p.pct -ge 90) { "Green" } elseif ($p.pct -ge 50) { "Yellow" } else { "White" }
            } elseif ($p.status -eq "blocked" -or $p.status -eq "paused_blocked") {
                $prog = $p.status
                $color = "Red"
                $extra = " BLOCKED"
                if ($p.reason) { $extra += " reason=$($p.reason)" }
            } elseif ($p.status -eq "error") {
                $prog = $p.status
                $color = "Red"
                if ($p.error) { $extra = " err=$($p.error)" }
            } else {
                $prog = $p.status
                $color = "DarkYellow"
            }
            $speed = if ($p.speed) { $p.speed } else { "-" }
            $eta = if ($p.eta) { $p.eta } else { "-" }

            # Detect stale progress (no update in > 5 min)
            $ageSec = ((Get-Date) - $_.LastWriteTime).TotalSeconds
            if ($p.status -eq "running" -and $ageSec -gt 300) {
                $ageMin = [math]::Round($ageSec / 60, 1)
                $extra += " STALE(${ageMin}m)"
                if ($color -ne "Red") { $color = "Magenta" }
            }

            if ($p.empty -ne $null) {
                $extra += " empty=$($p.empty)"
            }

            Write-Host ("{0,-8} {1,-10} {2,-10} {3,-20} {4,-12} {5,-8}{6}" -f $stock, $worker, $stage, $prog, $speed, $eta, $extra) -ForegroundColor $color
        }

        # Tasks with lock but no progress yet (just started)
        $lockFiles | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            if ($shownStocks.ContainsKey($stock)) { return }
            $worker = ""
            if ($lockMap.ContainsKey($stock)) {
                $worker = $lockMap[$stock].worker_id
            }
            $age = [math]::Round(((Get-Date) - $_.LastWriteTime).TotalSeconds, 0)
            $ageStr = if ($age -lt 60) { "${age}s" } else { "$([math]::Round($age/60,1))m" }
            Write-Host ("{0,-8} {1,-10} {2,-10} {3,-20} {4,-12} {5,-8}" -f $stock, $worker, "starting", "waiting (${ageStr})", "-", "-") -ForegroundColor DarkYellow
        }

        # Retrying tasks
        $retryFiles | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            if ($shownStocks.ContainsKey($stock)) { return }
            $worker = ""
            $attempt = "?"
            $prevReason = ""
            if ($lockMap.ContainsKey($stock)) {
                $worker = $lockMap[$stock].worker_id
            }
            if ($retryMap.ContainsKey($stock)) {
                $attempt = "$($retryMap[$stock].attempt)/$($retryMap[$stock].max_retries+1)"
                $prevReason = $retryMap[$stock].previous_reason
            }
            Write-Host ("{0,-8} {1,-10} {2,-10} {3,-20} {4,-12} {5,-8}" -f $stock, $worker, "retry", "attempt $attempt $prevReason", "-", "-") -ForegroundColor Magenta
        }
        $deferFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 20 | ForEach-Object {
            $stock = Get-StockCodeFromFileName $_.Name
            if ($shownStocks.ContainsKey($stock)) { return }
            $reason = ""
            $retryAfter = ""
            if ($deferMap.ContainsKey($stock)) {
                $reason = $deferMap[$stock].reason
                $retryAfter = $deferMap[$stock].retry_after
            }
            Write-Host ("{0,-8} {1,-10} {2,-10} {3,-20} {4,-12} {5,-8}" -f $stock, "-", "deferred", "$reason", "-", $retryAfter) -ForegroundColor DarkYellow
        }
        Write-Host ""
    }

    # Network tips: when blocked or stale tasks detected
    $staleTasks = @()
    $progressFiles | ForEach-Object {
        try { $p = Get-Content $_.FullName -Raw | ConvertFrom-Json } catch { $p = $null }
        if ($p) {
            $ageSec = ((Get-Date) - $_.LastWriteTime).TotalSeconds
            $isBlocked = ($p.status -eq "blocked" -or $p.status -eq "paused_blocked")
            $isStale = ($p.status -eq "running" -and $ageSec -gt 300)
            if ($isBlocked -or $isStale) {
                $stock = Get-StockCodeFromFileName $_.Name
                $staleTasks += $stock
            }
        }
    }
    if ($staleTasks.Count -gt 0) {
        Write-Host "  *** NETWORK WARNING: $($staleTasks.Count) stock(s) stuck/blocked! ***" -ForegroundColor Red
        Write-Host "  Stuck stocks: $($staleTasks -join ', ')" -ForegroundColor Red
        Write-Host "  Possible causes: EastMoney anti-crawl / IP blocked / network slow" -ForegroundColor Yellow
        Write-Host "  Actions:" -ForegroundColor Yellow
        Write-Host "    1. Stop workers: Ctrl+C in worker windows" -ForegroundColor White
        Write-Host "    2. Change IP (VPN / proxy / restart router)" -ForegroundColor White
        Write-Host "    3. Wait 5-10 min before restarting" -ForegroundColor White
        Write-Host "    4. Restart with lower concurrency:" -ForegroundColor White
        Write-Host "       .\batch_launcher.ps1 -WorkerCount 1 -ListWorkers 3 -Limit 2" -ForegroundColor White
        Write-Host "    5. Or use shorter timeout to auto-skip:" -ForegroundColor White
        Write-Host "       .\batch_launcher.ps1 -WorkerCount 1 -StockTimeoutMinutes 20 -Limit 2" -ForegroundColor White
        Write-Host ""
    }

    elseif ($locks -eq 0 -and $running -eq 0 -and $done -gt 0) {
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

    # Failed tasks
    if ($failed -gt 0 -or $upfail -gt 0) {
        if ($failed -gt 0) {
            Write-Host "== Failed Tasks ($failed) ==" -ForegroundColor Red
            $failedFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 10 | ForEach-Object {
                $stock = Get-StockCodeFromFileName $_.Name
                try {
                    $f = Get-Content $_.FullName -Raw | ConvertFrom-Json
                    $reason = $f.reason
                    $attempts = $f.attempts
                    Write-Host "  $stock  attempts: $attempts  reason: $reason"
                } catch {
                    Write-Host "  $stock"
                }
            }
            if ($failed -gt 10) {
                Write-Host "  ... and $($failed - 10) more failed tasks" -ForegroundColor DarkGray
            }
        }
        if ($upfail -gt 0) {
            Write-Host "== Upload Failed ($upfail) ==" -ForegroundColor DarkRed
            $upfailFiles | Sort-Object LastWriteTime -Descending | Select-Object -First 5 | ForEach-Object {
                $stock = Get-StockCodeFromFileName $_.Name
                Write-Host "  $stock"
            }
        }
        Write-Host ""
    }

    # Output file overview
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
Write-Host ""
while ($true) {
    Show-LiveDashboard
    Start-Sleep -Seconds $RefreshSeconds
}
