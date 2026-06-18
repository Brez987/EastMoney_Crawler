# ============================================================
# Stop MongoDB
# ============================================================

$proc = Get-Process -Name "mongod" -ErrorAction SilentlyContinue
if ($proc) {
    Stop-Process -Name "mongod" -Force
    Write-Host "[OK] MongoDB stopped" -ForegroundColor Green
}
else {
    Write-Host "[INFO] MongoDB is not running" -ForegroundColor Yellow
}
