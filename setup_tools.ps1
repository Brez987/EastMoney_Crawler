# ============================================================
# One-click deploy: Chrome + ChromeDriver + MongoDB (portable)
# All files under e:\guba_project\tools\
# Usage: .\setup_tools.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolsDir = Join-Path $ScriptDir "tools"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Deploy Chrome + ChromeDriver + MongoDB" -ForegroundColor Cyan
Write-Host "  Target: $ToolsDir" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Create tools directory
if (-not (Test-Path $ToolsDir)) {
    New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
}

# ============================================================
# Stage 1: Download Chrome for Testing (131.0.6778.204)
# ============================================================
$ChromeZip = Join-Path $env:TEMP "chrome-win64.zip"
$ChromeDir = Join-Path $ToolsDir "chrome-win64"
$ChromeExe = Join-Path $ChromeDir "chrome.exe"

if (Test-Path $ChromeExe) {
    Write-Host "[SKIP] Chrome exists: $ChromeDir" -ForegroundColor Yellow
}
else {
    Write-Host "[1/3] Downloading Chrome for Testing (~120MB)..." -ForegroundColor Green
    $chromeUrl = "https://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/win64/chrome-win64.zip"

    $hasBits = Get-Command "Start-BitsTransfer" -ErrorAction SilentlyContinue
    if ($hasBits) {
        Start-BitsTransfer -Source $chromeUrl -Destination $ChromeZip
    }
    else {
        Invoke-WebRequest -Uri $chromeUrl -OutFile $ChromeZip
    }

    Write-Host "  Extracting Chrome..." -ForegroundColor Gray
    Expand-Archive -Path $ChromeZip -DestinationPath $ToolsDir -Force
    Remove-Item $ChromeZip -Force
    Write-Host "  [OK] Chrome ready: $ChromeDir" -ForegroundColor Green
}

# ============================================================
# Stage 2: Download ChromeDriver (131.0.6778.204)
# ============================================================
$DriverZip = Join-Path $env:TEMP "chromedriver-win64.zip"
$DriverDir = Join-Path $ToolsDir "chromedriver-win64"
$DriverExe = Join-Path $DriverDir "chromedriver.exe"

if (Test-Path $DriverExe) {
    Write-Host "[SKIP] ChromeDriver exists: $DriverDir" -ForegroundColor Yellow
}
else {
    Write-Host "[2/3] Downloading ChromeDriver (~8MB)..." -ForegroundColor Green
    $driverUrl = "https://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/win64/chromedriver-win64.zip"

    $hasBits = Get-Command "Start-BitsTransfer" -ErrorAction SilentlyContinue
    if ($hasBits) {
        Start-BitsTransfer -Source $driverUrl -Destination $DriverZip
    }
    else {
        Invoke-WebRequest -Uri $driverUrl -OutFile $DriverZip
    }

    Write-Host "  Extracting ChromeDriver..." -ForegroundColor Gray
    Expand-Archive -Path $DriverZip -DestinationPath $ToolsDir -Force
    Remove-Item $DriverZip -Force
    Write-Host "  [OK] ChromeDriver ready: $DriverDir" -ForegroundColor Green
}

# ============================================================
# Stage 3: Download MongoDB 7.0 portable (ZIP)
# ============================================================
$MongoZip = Join-Path $env:TEMP "mongodb-windows-x86_64-7.0.zip"
$MongoDir = Join-Path $ToolsDir "mongodb"
$MongoBinDir = Join-Path $MongoDir "mongodb-win32-x86_64-windows-7.0.20\bin"
$MongoExe = Join-Path $MongoBinDir "mongod.exe"

if (Test-Path $MongoExe) {
    Write-Host "[SKIP] MongoDB exists: $MongoBinDir" -ForegroundColor Yellow
}
else {
    Write-Host "[3/3] Downloading MongoDB 7.0 portable (~120MB)..." -ForegroundColor Green
    $mongoUrl = "https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-7.0.20.zip"

    $hasBits = Get-Command "Start-BitsTransfer" -ErrorAction SilentlyContinue
    if ($hasBits) {
        Start-BitsTransfer -Source $mongoUrl -Destination $MongoZip
    }
    else {
        Invoke-WebRequest -Uri $mongoUrl -OutFile $MongoZip
    }

    Write-Host "  Extracting MongoDB (may take a while)..." -ForegroundColor Gray
    New-Item -ItemType Directory -Path $MongoDir -Force | Out-Null
    Expand-Archive -Path $MongoZip -DestinationPath $MongoDir -Force
    Remove-Item $MongoZip -Force
    Write-Host "  [OK] MongoDB ready: $MongoBinDir" -ForegroundColor Green
}

# ============================================================
# Verification
# ============================================================
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Verification" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Chrome
if (Test-Path $ChromeExe) {
    $ver = & $ChromeExe --version 2>&1
    Write-Host "[OK] Chrome: $ver" -ForegroundColor Green
}
else {
    Write-Host "[FAIL] Chrome not found" -ForegroundColor Red
}

# ChromeDriver
if (Test-Path $DriverExe) {
    $ver = & $DriverExe --version 2>&1 | Select-Object -First 1
    Write-Host "[OK] ChromeDriver: $ver" -ForegroundColor Green
}
else {
    Write-Host "[FAIL] ChromeDriver not found" -ForegroundColor Red
}

# MongoDB
if (Test-Path $MongoExe) {
    $ver = & $MongoExe --version 2>&1 | Select-Object -First 1
    Write-Host "[OK] MongoDB: $ver" -ForegroundColor Green
}
else {
    Write-Host "[FAIL] MongoDB not found" -ForegroundColor Red
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Deploy complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Directory structure:" -ForegroundColor White
Write-Host "    $ToolsDir\chrome-win64\          <-- Chrome browser" -ForegroundColor Gray
Write-Host "    $ToolsDir\chromedriver-win64\    <-- ChromeDriver" -ForegroundColor Gray
Write-Host "    $ToolsDir\mongodb\               <-- MongoDB portable" -ForegroundColor Gray
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor White
Write-Host "    1. Start MongoDB: .\start_mongodb.ps1" -ForegroundColor Yellow
Write-Host "    2. Stop  MongoDB: .\stop_mongodb.ps1" -ForegroundColor Yellow
Write-Host "    3. The crawler will auto-detect tools\ Chrome and ChromeDriver"
Write-Host ""
