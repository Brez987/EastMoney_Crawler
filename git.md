```powershell
# ============================================================
# 将当前项目最新进度上传到 GitHub
# 仓库: https://github.com/Brez987/EastMoney_Crawler
# 用法: 在 PowerShell 中运行此脚本
# ============================================================

$ErrorActionPreference = "Stop"
Set-Location "e:\guba_project"

# 1. 显示当前状态
Write-Host "=== 当前 Git 状态 ===" -ForegroundColor Cyan
git status --short

# 2. 添加所有变更（排除 git.md 自身）
Write-Host "`n=== 暂存变更 ===" -ForegroundColor Cyan
git add EastMoney_Crawler\crawler.py
git add EastMoney_Crawler\parser.py
git add EastMoney_Crawler\auto_pipeline_000001.py
git add EastMoney_Crawler\guba_api_client.py
git add EastMoney_Crawler\rate_limiter.py
git add EastMoney_Crawler\browser_utils.py
git add EastMoney_Crawler\batch_worker.py
git add EastMoney_Crawler\batch_launcher.ps1
git add EastMoney_Crawler\compare.md
git add EastMoney_Crawler\solution.md
git add EastMoney_Crawler\股票爬虫自动化流水线.md

# 3. 显示将要提交的内容
Write-Host "`n=== 待提交变更 ===" -ForegroundColor Cyan
git diff --cached --stat

# 4. 提交
$commitMsg = @"
fix: Stage 2财富号爬取优化 — cookie预热+窗口分批+自适应降级

- 复用Stage 1的浏览器cookie预热机制，共享Session伪装身份
- 窗口分批爬取(80条/批)，批间暂停8-12s，请求间延迟0.3-0.8s
- 自适应降级：连续失败5次自动增延迟/降并发，恢复后逐步提速
- 并发数调整为worker_count，失败阈值降至10
- _try_requests_caifuhao支持session参数
"@

Write-Host "`n=== 提交 ===" -ForegroundColor Cyan
git commit -m $commitMsg

# 5. 推送
Write-Host "`n=== 推送到 GitHub ===" -ForegroundColor Cyan
git push origin main

Write-Host "`n=== 完成 ===" -ForegroundColor Green
git log --oneline -3