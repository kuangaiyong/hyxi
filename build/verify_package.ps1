# 便携包验收 —— 在「干净环境」里真跑一遍
#
# 用法：.\build\verify_package.ps1 [解压目标目录]
#
# 干净的含义：启动子进程时把 PATH 剥到只剩系统目录，Python / Node / npm 全部不可见。
# 目标机器上就是这个样子 —— 只要还依赖开发机残留的任何东西，这一步就会失败。

$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $PSScriptRoot
$destDir = if ($args[0]) { $args[0] } else { 'C:\hyxi-deploy-test' }

function Write-Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Write-Ok($t)   { Write-Host "    [OK]   $t" -ForegroundColor Green }
function Write-Fail($t) { Write-Host "    [失败] $t" -ForegroundColor Red }

$zip = Get-ChildItem (Join-Path $root 'build\out') -Filter 'HYXi-*-win64.zip' |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) { Write-Fail '没找到 build\out\HYXi-*.zip，先跑 build\build.ps1'; exit 1 }

Write-Host "验收 $($zip.Name)" -ForegroundColor White

# ---------------- 1. 解压到全新目录 ----------------
Write-Step '解压到全新目录'
if (Test-Path $destDir) { Remove-Item $destDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $destDir | Out-Null
Expand-Archive -Path $zip.FullName -DestinationPath $destDir -Force
$pkg = (Get-ChildItem $destDir -Directory | Select-Object -First 1).FullName
Write-Ok "解压到 $pkg"

# 用户拿到的就是这个状态：没有 .env、没有 data
foreach ($f in @('.env', 'data')) {
    if (Test-Path (Join-Path $pkg $f)) { Write-Fail "包里居然带着 $f"; exit 1 }
}
Write-Ok '包内无 .env、无 data —— 首次启动才生成'

# ---------------- 2. 剥掉 PATH 启动 ----------------
Write-Step '在剥掉 Python / Node 的环境里启动'
$cleanPath = "$env:SystemRoot\system32;$env:SystemRoot;$env:SystemRoot\system32\Wbem"
$exe = Join-Path $pkg 'app\hyxi.exe'
if (-not (Test-Path $exe)) { Write-Fail "找不到 $exe"; exit 1 }

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $exe
$psi.WorkingDirectory = $pkg
$psi.UseShellExecute = $false
$psi.EnvironmentVariables['PATH'] = $cleanPath
# 别让它把浏览器弹到操作者脸上
$psi.EnvironmentVariables['HYXI_NO_BROWSER'] = '1'
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
# 程序输出是 UTF-8（真实使用路径里由 .bat 的 chcp 65001 兜住）。这里不显式指定的话
# .NET 会按系统 ANSI 代码页（本机 936）解码，中文全是乱码，日志断言也就跟着失灵
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$proc = [System.Diagnostics.Process]::Start($psi)

# **两个流都必须读**。uvicorn 的日志走 stderr，只读 stdout 的话 stderr 管道缓冲区
# （几 KB）填满后子进程会直接卡死在写日志上，表现为「服务起来了但不响应」
$stdout = New-Object System.Text.StringBuilder
$null = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
    $null = $Event.MessageData.AppendLine($EventArgs.Data)
} -MessageData $stdout
$null = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
    $null = $Event.MessageData.AppendLine($EventArgs.Data)
} -MessageData $stdout
$proc.BeginOutputReadLine()
$proc.BeginErrorReadLine()

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 3
        if ($h.status -eq 'ok') { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 1000 }
    if ($proc.HasExited) { break }
}

if (-not $ready) {
    Write-Fail '服务没起来'
    Write-Host $stdout.ToString() -ForegroundColor DarkGray
    if (-not $proc.HasExited) { $proc.Kill() }
    exit 1
}
Write-Ok '服务已就绪（PATH 里没有 Python、没有 Node）'

try {
    # ---------------- 3. 页面与接口 ----------------
    Write-Step '页面与接口'
    $index = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/' -UseBasicParsing
    if ($index.Content -notmatch '<div id="app">|<div id=app>') { Write-Fail '首页不是前端页面'; exit 1 }
    Write-Ok '首页由后端直接发布（无 nginx、无 Vite）'

    $deep = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/tasks/abc-123/progress' -UseBasicParsing
    if ($deep.StatusCode -ne 200) { Write-Fail '深链刷新 404 了'; exit 1 }
    Write-Ok '深链直接刷新落到 SPA 上'

    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/v1/not-a-route' -UseBasicParsing | Out-Null
        Write-Fail '打错的接口地址居然返回了 200'; exit 1
    } catch {
        if ($_.Exception.Response.StatusCode.value__ -ne 404) { Write-Fail '打错的接口地址没回 404'; exit 1 }
    }
    Write-Ok '打错的接口地址仍是 404，不会被 SPA 回退顶掉'

    $ver = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/version'
    Write-Ok "服务自报版本 $($ver.version)"

    # ---------------- 4. 首启自举 ----------------
    Write-Step '首次启动的自举'
    $env_ = Join-Path $pkg '.env'
    if (-not (Test-Path $env_)) { Write-Fail '.env 没有自动生成'; exit 1 }
    $envText = Get-Content $env_ -Raw
    if ($envText -notmatch 'TWEAKERS_SECRET_KEY=\S+') { Write-Fail '.env 里没有加密密钥'; exit 1 }
    Write-Ok '.env 已生成，含本机专属加密密钥'
    if (-not (Test-Path (Join-Path $pkg 'data\hyxi.db'))) { Write-Fail '数据库没建起来'; exit 1 }
    Write-Ok 'data\hyxi.db 已创建'

    $log = $stdout.ToString()
    if ($log -notmatch [regex]::Escape('node\node.exe')) {
        Write-Fail '自检没报出包内的 node，可能用的是别处的'
        Write-Host $log -ForegroundColor DarkGray
        exit 1
    }
    Write-Ok '采集用的是包内自带的 node\node.exe'

    Write-Host "`n启动日志：" -ForegroundColor DarkGray
    Write-Host $log -ForegroundColor DarkGray
}
finally {
    if (-not $proc.HasExited) {
        Start-Process taskkill -ArgumentList '/PID', $proc.Id, '/T', '/F' -NoNewWindow -Wait
    }
}

Write-Step '结果'
Write-Host "    便携包验收通过：$pkg" -ForegroundColor Green
exit 0
