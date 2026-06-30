param(
    [int]$CheckSeconds = 300,
    [int]$WorkerCount = 3,
    [int]$DetailWorkers = 3,
    [int]$ListWorkers = 6,
    [string]$CrawlMode = "full",
    [string]$StartDate = "2009-01-01",
    [string]$ListSource = "html",
    [double]$DeferredRetrySeconds = 900,
    [string]$StockList = "",
    [string]$ProgressDir = "",
    [switch]$RetryFailed
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ProjectDir "batch_launcher.ps1"

function Get-BatchWorkerProcess {
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*batch_worker.py*" -and
            $_.CommandLine -like "*$ProjectDir*"
        }
}

while ($true) {
    $workers = @(Get-BatchWorkerProcess)
    if ($workers.Count -eq 0) {
        Write-Host "$(Get-Date -Format s) no batch_worker.py process found; restarting launcher"
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $Launcher,
            "-WorkerCount", "$WorkerCount",
            "-DetailWorkers", "$DetailWorkers",
            "-ListWorkers", "$ListWorkers",
            "-CrawlMode", $CrawlMode,
            "-StartDate", $StartDate,
            "-ListSource", $ListSource,
            "-DeferredRetrySeconds", "$DeferredRetrySeconds",
            "-NoWatch"
        )
        if ($StockList) {
            $args += @("-StockList", $StockList)
        }
        if ($ProgressDir) {
            $args += @("-ProgressDir", $ProgressDir)
        }
        if ($RetryFailed) {
            $args += "-RetryFailed"
        }
        Start-Process -FilePath "powershell.exe" -ArgumentList $args -WorkingDirectory $ProjectDir -WindowStyle Hidden | Out-Null
    } else {
        Write-Host "$(Get-Date -Format s) workers alive: $($workers.Count)"
    }
    Start-Sleep -Seconds $CheckSeconds
}
