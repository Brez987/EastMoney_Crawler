param(
    [int]$WorkerCount = 3,
    [int]$DetailWorkers = 3,
    [int]$MaxRetries = 2,
    [double]$StaleLockHours = 3,
    [double]$MinFreeGb = 20,
    [int]$Limit = 0,
    [string[]]$SourceDir = @(),
    [string]$ProgressDir = "",
    [string]$Python = "python",
    [switch]$DryRun,
    [switch]$RetryFailed,
    [switch]$Visible
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectDir "batch_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

if (-not $ProgressDir) {
    $ProgressDir = Join-Path $ProjectDir "batch_progress"
}

Write-Host "Project: $ProjectDir"
Write-Host "Workers: $WorkerCount"
Write-Host "Detail workers per stock: $DetailWorkers"
Write-Host "Progress dir: $ProgressDir"
if ($SourceDir.Count -eq 0) {
    Write-Host "Source dirs: batch_worker defaults"
} else {
    Write-Host "Source dirs:"
    $SourceDir | ForEach-Object { Write-Host "  - $_" }
}

for ($i = 1; $i -le $WorkerCount; $i++) {
    $workerId = "worker_$i"
    $workerArgs = @(
        "batch_worker.py",
        "--worker-id", $workerId,
        "--detail-workers", "$DetailWorkers",
        "--max-retries", "$MaxRetries",
        "--stale-lock-hours", "$StaleLockHours",
        "--min-free-gb", "$MinFreeGb",
        "--progress-dir", $ProgressDir
    )

    foreach ($dir in $SourceDir) {
        $workerArgs += @("--source-dir", $dir)
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

Write-Host "Use these commands to monitor progress:"
Write-Host "  (Get-ChildItem batch_progress\*.done).Count"
Write-Host "  (Get-ChildItem batch_progress\*.failed).Count"
Write-Host "  Get-ChildItem batch_logs\*.log"
