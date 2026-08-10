# HYXi 舆情分析平台 —— 一键启动
#
# 用法：在仓库根目录执行  .\start.ps1
# 退出码：0 = 全部就绪；1 = 有服务没起来（日志已打在屏幕上）

$ErrorActionPreference = 'Stop'

$root        = $PSScriptRoot
$python      = Join-Path $root 'backend\.venv\Scripts\python.exe'
$logDir      = Join-Path $root 'backend\data\logs'
$backendUrl  = 'http://127.0.0.1:8000'
$frontendUrl = 'http://localhost:5173'
$timeout     = 90

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    [OK]   $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    [警告] $text" -ForegroundColor Yellow }
function Write-Fail($text) { Write-Host "    [失败] $text" -ForegroundColor Red }

function Test-Listening($port) {
    $null -ne (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Get-ListenerPid($port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($conn) { return $conn.OwningProcess }
    return $null
}

# 轮询到能取到 200 为止。服务刚拉起时端口会先监听、再能应答，只看端口会误判成功
function Wait-Until($url, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

function Show-Tail($file) {
    if (-not (Test-Path $file)) { return }
    $name = [System.IO.Path]::GetFileName($file)
    Write-Host "    ---- $name 末尾 20 行 ----" -ForegroundColor DarkGray
    Get-Content $file -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}

Write-Host 'HYXi 舆情分析平台 —— 一键启动' -ForegroundColor White

# ---------------- 1. 前置检查 ----------------
Write-Step '检查依赖'

$missing = @()
if (-not (Test-Path $python)) {
    $missing += 'Python 虚拟环境未创建。执行：py -3.12 -m venv backend\.venv；再 .\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt pytest'
}
if (-not (Test-Path (Join-Path $root 'node_modules\playwright'))) {
    $missing += '采集依赖缺失，且必须装在项目根目录（frontend\node_modules 顶不上）。执行：npm ci'
}
if (-not (Test-Path (Join-Path $root 'frontend\node_modules'))) {
    $missing += '前端依赖缺失。执行：cd frontend; npm install'
}

if ($missing.Count -gt 0) {
    foreach ($item in $missing) { Write-Fail $item }
    exit 1
}
Write-Ok '三处依赖齐全'

if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Warn '未找到 .env：接口将不做鉴权，且无法保存数据源凭据'
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# ---------------- 2. 启动后端 ----------------
Write-Step '启动后端 (8000)'

$startedBackend = $false
if (Test-Listening 8000) {
    Write-Warn "端口 8000 已在监听（PID $(Get-ListenerPid 8000)），跳过启动，直接验证"
} else {
    # main:app 的 import 依赖 cwd，必须在 backend 目录下启动
    Start-Process -FilePath $python `
        -ArgumentList '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8000' `
        -WorkingDirectory (Join-Path $root 'backend') `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'start_backend.out.log') `
        -RedirectStandardError (Join-Path $logDir 'start_backend.err.log')
    $startedBackend = $true
    Write-Ok '进程已拉起'
}

# ---------------- 3. 启动前端 ----------------
Write-Step '启动前端 (5173)'

$startedFrontend = $false
if (Test-Listening 5173) {
    Write-Warn "端口 5173 已在监听（PID $(Get-ListenerPid 5173)），跳过启动，直接验证"
} else {
    Start-Process -FilePath 'npm.cmd' `
        -ArgumentList 'run', 'dev' `
        -WorkingDirectory (Join-Path $root 'frontend') `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'start_frontend.out.log') `
        -RedirectStandardError (Join-Path $logDir 'start_frontend.err.log')
    $startedFrontend = $true
    Write-Ok '进程已拉起'
}

# ---------------- 4. 验证 ----------------
Write-Step '验证服务是否真的起来了'

$ok = $true

if (Wait-Until "$backendUrl/api/health" $timeout) {
    Write-Ok "后端就绪  $backendUrl/api/health  (PID $(Get-ListenerPid 8000))"
} else {
    Write-Fail "后端在 $timeout 秒内没有应答"
    # 日志只在本轮亲自拉起进程时才有解释力；端口被别人占着时那份日志是上一轮留下的，
    # 会出现「没有应答」下面紧跟着「Application startup complete」这种误导
    if ($startedBackend) {
        Show-Tail (Join-Path $logDir 'start_backend.err.log')
    } else {
        Write-Fail '端口 8000 被其他程序占用，它不是本项目的后端'
    }
    $ok = $false
}

if (Wait-Until "$frontendUrl/" $timeout) {
    Write-Ok "前端就绪  $frontendUrl  (PID $(Get-ListenerPid 5173))"
} else {
    Write-Fail "前端在 $timeout 秒内没有应答"
    if ($startedFrontend) {
        Show-Tail (Join-Path $logDir 'start_frontend.err.log')
    } else {
        Write-Fail '端口 5173 被其他程序占用，它不是本项目的前端'
    }
    $ok = $false
}

# 两个都活着还不够：页面靠 Vite 把 /api 代理到 8000，这一跳断了页面照样是空的
if ($ok) {
    if (Wait-Until "$frontendUrl/api/health" 30) {
        Write-Ok '前端 → 后端 代理链路通'
    } else {
        Write-Fail '前端起来了，但 /api 代理打不通后端'
        $ok = $false
    }
}

# ---------------- 5. 结果 ----------------
Write-Step '结果'

if ($ok) {
    Write-Host "    全部服务已就绪，打开 $frontendUrl" -ForegroundColor Green
    Write-Host '    停止全部服务：' -ForegroundColor DarkGray
    Write-Host '    Get-NetTCPConnection -LocalPort 8000,5173 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }' -ForegroundColor DarkGray
    exit 0
} else {
    Write-Host "    启动失败。完整日志在 $logDir" -ForegroundColor Red
    exit 1
}
