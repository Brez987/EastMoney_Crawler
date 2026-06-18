# ============================================================
# MongoDB portable start script
# Usage: .\start_mongodb.ps1
# Stop:  .\stop_mongodb.ps1
# ============================================================

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MongoDir = Join-Path $ScriptDir "tools\mongodb"
$MongoBin = Join-Path $MongoDir "mongodb-win32-x86_64-windows-7.0.20\bin"
$MongoExe = Join-Path $MongoBin "mongod.exe"
$DataDir = Join-Path $MongoDir "data"
$LogDir  = Join-Path $MongoDir "log"
$LogFile = Join-Path $LogDir "mongod.log"

# Check mongod.exe
if (-not (Test-Path $MongoExe)) {
    Write-Host "[ERROR] MongoDB not found: $MongoExe" -ForegroundColor Red
    Write-Host "Please run .\setup_tools.ps1 first" -ForegroundColor Yellow
    exit 1
}

# Create data and log directories
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
if (-not (Test-Path $LogDir))  { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

# Check if already running
$existing = Get-Process -Name "mongod" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[INFO] MongoDB already running (PID: $($existing.Id))" -ForegroundColor Green
    exit 0
}

Write-Host "[INFO] Starting MongoDB..." -ForegroundColor Cyan
Write-Host "  bin  : $MongoExe"
Write-Host "  data : $DataDir"
Write-Host "  log  : $LogFile"

# Start mongod in background
$proc = Start-Process -FilePath $MongoExe `
    -ArgumentList "--dbpath `"$DataDir`" --logpath `"$LogFile`" --logappend --bind_ip 127.0.0.1 --port 27017" `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 2

if (-not $proc.HasExited) {
    Write-Host "[OK] MongoDB started (PID: $($proc.Id), port: 27017)" -ForegroundColor Green
}
else {
    Write-Host "[ERROR] MongoDB failed to start. Check log: $LogFile" -ForegroundColor Red
    Get-Content $LogFile -Tail 20
}
