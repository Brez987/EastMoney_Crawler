param(
    [int]$WorkerCount = 3,
    [int]$DetailWorkers = 3,
    [int]$ListWorkers = 6,
    [int]$ListPageLimit = 0,
    [int]$MaxRetries = 2,
    [double]$StaleLockHours = 3,
    [double]$MinFreeGb = 20,
    [int]$Limit = 0,
    [string]$CrawlMode = "incremental",
    [string]$StartDate = "2009-01-01",
    [string]$StockList = "",
    [string[]]$SourceDir = @(),
    [string]$ProgressDir = "",
    [string]$Python = "python",
    [string]$ListSource = "html",
    [switch]$DryRun,
    [switch]$RetryFailed,
    [switch]$Visible,
    [switch]$Watch
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectDir "batch_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

if (-not $ProgressDir) {
    if ($CrawlMode -eq "full") {
        $ProgressDir = Join-Path $ProjectDir "batch_progress_full_20090101"
    } else {
        $ProgressDir = Join-Path $ProjectDir "batch_progress"
    }
}

Write-Host "Project: $ProjectDir"
Write-Host "Crawl mode: $CrawlMode"
Write-Host "Workers: $WorkerCount"
Write-Host "Detail workers per stock: $DetailWorkers"
if ($CrawlMode -eq "full") {
    Write-Host "List workers per stock: $ListWorkers"
    Write-Host "List source: $ListSource"
    if ($ListSource -in @("api", "auto")) {
        Write-Host "List source compatibility mode: $ListSource will run via fast requests html"
    }
    if ($ListPageLimit -gt 0) {
        Write-Host "List page limit per stock: $ListPageLimit"
    }
    Write-Host "Start date: $StartDate"
}
Write-Host "Progress dir: $ProgressDir"
if ($CrawlMode -eq "incremental") {
    if ($SourceDir.Count -eq 0) {
        Write-Host "Source dirs: batch_worker defaults"
    } else {
        Write-Host "Source dirs:"
        $SourceDir | ForEach-Object { Write-Host "  - $_" }
    }
}

for ($i = 1; $i -le $WorkerCount; $i++) {
    $workerId = "worker_$i"
    $workerArgs = @(
        "batch_worker.py",
        "--worker-id", $workerId,
        "--crawl-mode", $CrawlMode,
        "--detail-workers", "$DetailWorkers",
        "--max-retries", "$MaxRetries",
        "--stale-lock-hours", "$StaleLockHours",
        "--min-free-gb", "$MinFreeGb",
        "--progress-dir", $ProgressDir
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
    if ($DryRun) {
        $workerArgs += "--dry-run"
    }
    if ($RetryFailed) {
        $workerArgs += "--retry-failed"
    }
    $startArgs = @{
        FilePath = $Python
        ArgumentList = $workerArgs
        WorkingDirectory = $ProjectDir
        WindowStyle = $(if ($Visible) { "Normal" } else { "Hidden" })
    }

    if (-not $Visible) {
        $startArgs.RedirectStandardOutput = Join-Path $LogDir "$workerId.out.log"
        $startArgs.RedirectStandardError = Join-Path $LogDir "$workerId.err.log"
    }

    $process = Start-Process @startArgs -PassThru
    Write-Host "Started $workerId pid=$($process.Id)"
}

# ── 启动实时进度面板 ──
if ($Watch) {
    $watchScript = Join-Path $ProjectDir "watch_batch_progress.ps1"
    $watchArgs = @(
        "-File", $watchScript,
        "-ProgressDir", $ProgressDir,
        "-RefreshSeconds", "10"
    )
    $watchProcess = Start-Process -FilePath "powershell.exe" -ArgumentList $watchArgs -PassThru -WindowStyle Normal
    Write-Host "Started progress watcher pid=$($watchProcess.Id)"
}

Write-Host ""
Write-Host "=== Monitor ==="
if ($Watch) {
    Write-Host "  Progress panel is running in a separate window (Ctrl+C to exit)"
} else {
    Write-Host "  Start progress panel:"
    if ($CrawlMode -eq "full") {
        Write-Host "    .\watch_batch_progress.ps1"
    } else {
        Write-Host "    .\watch_batch_progress.ps1 -ProgressDir batch_progress"
    }
}
Write-Host ""
Write-Host "  Quick stats:"
if ($CrawlMode -eq "full") {
    Write-Host "    (Get-ChildItem batch_progress_full_20090101\*.done).Count"
    Write-Host "    (Get-ChildItem batch_progress_full_20090101\*.failed).Count"
} else {
    Write-Host "    (Get-ChildItem batch_progress\*.done).Count"
    Write-Host "    (Get-ChildItem batch_progress\*.failed).Count"
}
Write-Host "  View logs:  Get-ChildItem batch_logs\*.log"
