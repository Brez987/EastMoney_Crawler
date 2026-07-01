param(
    [int]$WorkerCount = 3,
    [int]$DetailWorkers = 3,
    [int]$ListWorkers = 6,
    [double]$ListWindowPauseMin = 0.3,
    [double]$ListWindowPauseMax = 1.2,
    [int]$ListPageLimit = 0,
    [int]$MaxRetries = 2,
    [double]$StaleLockHours = 1,
    [double]$MinFreeGb = 20,
    [int]$Limit = 0,
    [string]$CrawlMode = "full",
    [string]$StartDate = "2009-01-01",
    [string]$StockList = "",
    [string[]]$SourceDir = @(),
    [string]$ProgressDir = "",
    [string]$Python = "",
    [string]$ListSource = "html",
    [double]$StockTimeoutMinutes = 60,
    [double]$DeferredRetrySeconds = 900,
    [int]$MaxConsecutiveFailures = 5,
    [switch]$DryRun,
    [switch]$RetryFailed,
    [switch]$SingleProcessStock,
    [switch]$Visible,
    [switch]$NoWatch
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectDir "batch_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# -- Auto-detect Python --
if (-not $Python) {
    foreach ($cmd in @("python", "python3", "py")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            try {
                $testOutput = & $cmd -c "import sys; print(sys.executable)" 2>&1
                if ($LASTEXITCODE -eq 0 -and $testOutput) {
                    $Python = $found.Source
                    Write-Host "Auto-detected Python: $Python"
                    break
                }
            } catch {
                # ignore
            }
        }
    }
    if (-not $Python) {
        $commonPaths = @(
            "C:\Python312\python.exe",
            "C:\Python311\python.exe",
            "C:\Python310\python.exe",
            "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python312\python.exe",
            "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python311\python.exe",
            "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python310\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
        )
        foreach ($p in $commonPaths) {
            if (Test-Path $p) {
                $Python = $p
                Write-Host "Found Python at: $Python"
                break
            }
        }
    }
    if (-not $Python) {
        Write-Host "ERROR: Cannot find Python." -ForegroundColor Red
        Write-Host "Example: .\batch_launcher.ps1 -Python 'C:\Python312\python.exe'" -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "Checking Python environment..."
$pyCheck = & $Python -c "import sys; print(f'Python {sys.version}')" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Python check failed: $pyCheck" -ForegroundColor Red
    exit 1
}
Write-Host "  $pyCheck"

$workerScript = Join-Path $ProjectDir "batch_worker.py"
if (-not (Test-Path $workerScript)) {
    Write-Host "ERROR: batch_worker.py not found at $workerScript" -ForegroundColor Red
    exit 1
}

if (-not $ProgressDir) {
    if ($CrawlMode -eq "full") {
        $ProgressDir = Join-Path $ProjectDir "batch_progress_full_20090101"
    } else {
        $ProgressDir = Join-Path $ProjectDir "batch_progress"
    }
}

New-Item -ItemType Directory -Force -Path $ProgressDir | Out-Null

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Stock Crawler Batch Launcher" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Project:       $ProjectDir"
Write-Host "Crawl mode:    $CrawlMode"
Write-Host "Workers:       $WorkerCount"
Write-Host "Detail workers per stock: $DetailWorkers"
if ($CrawlMode -eq "full") {
    Write-Host "List workers per stock:   $ListWorkers"
    Write-Host "List window pause: $ListWindowPauseMin-$ListWindowPauseMax sec"
    Write-Host "List source:    $ListSource"
    if ($ListSource -in @("api", "auto")) {
        Write-Host "  (api/auto uses fast requests HTML path)" -ForegroundColor DarkGray
    }
    if ($ListPageLimit -gt 0) {
        Write-Host "List page limit per stock: $ListPageLimit"
    }
    Write-Host "Start date:     $StartDate"
}
Write-Host "Progress dir:  $ProgressDir"
Write-Host "Max retries:   $MaxRetries"
Write-Host "Stock timeout: $StockTimeoutMinutes min"
Write-Host "Deferred retry: $DeferredRetrySeconds sec"
Write-Host "Max consecutive failures: $MaxConsecutiveFailures"
Write-Host "Python:        $Python"
if ($CrawlMode -eq "incremental") {
    if ($SourceDir.Count -eq 0) {
        Write-Host "Source dirs:   batch_worker defaults"
    } else {
        Write-Host "Source dirs:"
        $SourceDir | ForEach-Object { Write-Host "  - $_" }
    }
}
if ($StockList) {
    Write-Host "Stock list:    $StockList"
}
$Watch = -not $NoWatch
if ($Watch) {
    Write-Host "Progress window: AUTO (will open a new window)" -ForegroundColor Green
}
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# -- Start Workers --
$processes = @()
for ($i = 1; $i -le $WorkerCount; $i++) {
    $workerId = "worker_$i"
    $workerArgs = @(
        $workerScript,
        "--worker-id", $workerId,
        "--crawl-mode", $CrawlMode,
        "--detail-workers", "$DetailWorkers",
        "--list-window-pause-min", "$ListWindowPauseMin",
        "--list-window-pause-max", "$ListWindowPauseMax",
        "--max-retries", "$MaxRetries",
        "--stale-lock-hours", "$StaleLockHours",
        "--min-free-gb", "$MinFreeGb",
        "--deferred-retry-seconds", "$DeferredRetrySeconds",
        "--progress-dir", $ProgressDir,
        "--python", $Python
    )

    if ($CrawlMode -eq "full") {
        $workerArgs += @("--start-date", $StartDate)
        $workerArgs += @("--list-workers", "$ListWorkers")
        $workerArgs += @("--list-source", $ListSource)
        if ($ListPageLimit -gt 0) {
            $workerArgs += @("--list-page-limit", "$ListPageLimit")
        }
        if ($StockList) {
            $workerArgs += @("--stock-list", $StockList)
        }
    } else {
        foreach ($dir in $SourceDir) {
            $workerArgs += @("--source-dir", $dir)
        }
    }

    if ($Limit -gt 0) {
        $workerArgs += @("--limit", "$Limit")
    }
    if ($StockTimeoutMinutes -gt 0) {
        $workerArgs += @("--stock-timeout-minutes", "$StockTimeoutMinutes")
    }
    if ($MaxConsecutiveFailures -gt 0) {
        $workerArgs += @("--max-consecutive-failures", "$MaxConsecutiveFailures")
    }
    if ($DryRun) {
        $workerArgs += "--dry-run"
    }
    if ($RetryFailed) {
        $workerArgs += "--retry-failed"
    }
    if ($SingleProcessStock) {
        $workerArgs += "--single-process-stock"
    }

    $startArgs = @{
        FilePath = $Python
        ArgumentList = $workerArgs
        WorkingDirectory = $ProjectDir
        WindowStyle = $(if ($Visible) { "Normal" } else { "Hidden" })
        PassThru = $true
    }

    if (-not $Visible) {
        $startArgs.RedirectStandardOutput = Join-Path $LogDir "$workerId.out.log"
        $startArgs.RedirectStandardError = Join-Path $LogDir "$workerId.err.log"
    }

    $process = Start-Process @startArgs
    $processes += $process
    Write-Host "Started $workerId pid=$($process.Id)" -ForegroundColor Green
}

Start-Sleep -Milliseconds 500

# -- Progress Watch Window --
if ($Watch) {
    Write-Host ""
    Write-Host "Starting progress watch window..." -ForegroundColor Yellow
    $watchScript = Join-Path $ProjectDir "watch_batch_progress.ps1"
    if (Test-Path $watchScript) {
        $stockListArg = ""
        if ($StockList) {
            $stockListArg = $StockList
        } else {
            $defaultList = Get-ChildItem -LiteralPath $ProjectDir -Filter "*_list.csv" -File |
                Sort-Object Name |
                Select-Object -First 1
            if ($defaultList) {
                $stockListArg = $defaultList.FullName
            }
        }
        $watchArgs = @(
            "-NoExit",
            "-ExecutionPolicy", "Bypass",
            "-File", "`"$watchScript`"",
            "-ProgressDir", "`"$ProgressDir`"",
            "-RefreshSeconds", "3"
        )
        if ($stockListArg) {
            $watchArgs += @("-StockListFile", "`"$stockListArg`"")
        }
        if ($Limit -gt 0) {
            $watchArgs += @("-Limit", "$Limit")
        }
        $watchProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $watchArgs -PassThru -WindowStyle Normal
        Write-Host "Progress window started pid=$($watchProcess.Id)" -ForegroundColor Green
    } else {
        Write-Host "WARNING: watch_batch_progress.ps1 not found, skipping progress window" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== Batch Crawl Started ===" -ForegroundColor Green
Write-Host ""
if ($Watch) {
    Write-Host "  Progress panel opened in new window" -ForegroundColor Cyan
} else {
    Write-Host "  Manual progress panel:" -ForegroundColor Cyan
    Write-Host "    .\watch_batch_progress.ps1 -ProgressDir `"$ProgressDir`"" -ForegroundColor White
}
Write-Host ""
Write-Host "  Quick stats (paste in terminal):" -ForegroundColor Cyan
if ($CrawlMode -eq "full") {
    Write-Host "    (Get-ChildItem batch_progress_full_20090101\*.done).Count" -ForegroundColor White
    Write-Host "    (Get-ChildItem batch_progress_full_20090101\*.failed).Count" -ForegroundColor White
} else {
    Write-Host "    (Get-ChildItem batch_progress\*.done).Count" -ForegroundColor White
    Write-Host "    (Get-ChildItem batch_progress\*.failed).Count" -ForegroundColor White
}
Write-Host "  Tail logs: Get-Content batch_logs\worker_1.out.log -Wait" -ForegroundColor White
Write-Host ""
Write-Host "  Worker PIDs: $($processes.Id -join ', ')" -ForegroundColor Gray
