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

# **端口必须先是空的**。被别的实例（比如开发后端）占着时，便携包的自检会因为
# 「端口 8000 已被占用」直接退出，而下面那个健康检查会打到**那个实例**上并判定
# 「服务已就绪」—— 后面每一条断言都在验错对象。实测踩过：开发后端没有 web/，
# 于是报「首页不是前端页面」，看起来像打包坏了，其实包根本没起来
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    Write-Fail '端口 8000 已被占用，先把它停掉再验收 —— 否则会验到别的实例上去'
    Write-Host '    Get-NetTCPConnection -LocalPort 8000,5173 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }' -ForegroundColor DarkGray
    exit 1
}

# **两个流都要接住**。uvicorn 的日志走 stderr，只接 stdout 的话 stderr 管道缓冲区
# （几 KB）填满后子进程会卡死在写日志上，表现成「服务起来了但不响应」。
# 直接落文件而不是用 Register-ObjectEvent：那条路要两个处理器往同一个非线程安全的
# StringBuilder 写，订阅又不会自己注销，实测 PowerShell 收尾时就不肯退出了
$outFile = Join-Path $destDir 'stdout.log'
$errFile = Join-Path $destDir 'stderr.log'

$savedPath = $env:PATH
$env:PATH = $cleanPath
$env:HYXI_NO_BROWSER = '1'   # 别让它把浏览器弹到操作者脸上
try {
    $proc = Start-Process -FilePath $exe -WorkingDirectory $pkg -PassThru -NoNewWindow `
                          -RedirectStandardOutput $outFile -RedirectStandardError $errFile
} finally {
    $env:PATH = $savedPath
}

function Get-Log {
    # 程序写的是 UTF-8。不显式指定的话 PS 5.1 按系统 ANSI 代码页（本机 936）读，
    # 中文全是乱码，下面那条 node 路径断言也就跟着失灵
    $a = if (Test-Path $outFile) { Get-Content $outFile -Raw -Encoding UTF8 } else { '' }
    $b = if (Test-Path $errFile) { Get-Content $errFile -Raw -Encoding UTF8 } else { '' }
    return "$a`n$b"
}

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 3
        if ($h.status -eq 'ok') { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 1000 }
    if ($proc.HasExited) { break }
}

if (-not $ready) {
    # PS 里 if 不是表达式，( ) 里只允许 pipeline —— 写成 Write-Fail (if ...) 会把 if
    # 当成命令名，配上开头那句 $ErrorActionPreference = 'Stop' 就是整个脚本当场中止：
    # 日志不打、$proc.Kill() 不执行，hyxi.exe 变成孤儿进程继续占着 8000 端口。
    # 赋值右侧的 if 是合法的，所以先接一把
    $failMsg = if ($proc.HasExited) { "进程已退出（退出码 $($proc.ExitCode)）" } else { '服务没起来' }
    Write-Fail $failMsg
    Write-Host (Get-Log) -ForegroundColor DarkGray
    if (-not $proc.HasExited) { $proc.Kill() }
    exit 1
}
# 进程活着才算数：它退出了而端口仍有应答，说明应答的是别人
if ($proc.HasExited) {
    Write-Fail "便携包进程已退出（退出码 $($proc.ExitCode)），刚才应答的是别的实例"
    Write-Host (Get-Log) -ForegroundColor DarkGray
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
    # 数据目录在包的**同级**，不在包里 —— 包目录带版本号，装里面的话
    # 每升一次级就是一个全新的空目录，配置和历史数据全丢
    Write-Step '首次启动的自举'
    $dataDir = Join-Path (Split-Path -Parent $pkg) 'HYXi-数据'
    if (Test-Path (Join-Path $pkg 'data')) { Write-Fail '数据建到包里去了，升级会丢'; exit 1 }
    $env_ = Join-Path $dataDir '.env'
    if (-not (Test-Path $env_)) { Write-Fail ".env 没有生成在 $dataDir"; exit 1 }
    $envText = Get-Content $env_ -Raw
    if ($envText -notmatch 'TWEAKERS_SECRET_KEY=\S+') { Write-Fail '.env 里没有加密密钥'; exit 1 }
    Write-Ok '.env 已生成在数据目录里，含本机专属加密密钥'
    if (-not (Test-Path (Join-Path $dataDir 'hyxi.db'))) { Write-Fail '数据库没建起来'; exit 1 }
    Write-Ok "数据建在包外：$dataDir"

}
finally {
    # **先停子进程再读日志**：它把 stdout 重定向到文件后 Python 会块缓冲，
    # 而且文件一直被独占打开着 —— 进程还活着时去读只能读到零星几行。
    # 进程一退出，文件就写全并关闭了
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    $proc.WaitForExit(10000) | Out-Null
}

$log = Get-Log
if ($log -notmatch [regex]::Escape('node\node.exe')) {
    Write-Fail '自检没报出包内的 node，可能用的是别处的'
    Write-Host $log -ForegroundColor DarkGray
    exit 1
}
Write-Ok '采集用的是包内自带的 node\node.exe'

Write-Host "`n启动日志：" -ForegroundColor DarkGray
Write-Host $log -ForegroundColor DarkGray

# ---------------- 5. 升级后数据还在 ----------------
# 用户实测报过：每升一次级，LLM 配置、数据源、跑过的任务和舆情结论全部要重来。
#
# 探针用真实接口存一份 LLM 配置，而不是比 hyxi.db 的哈希 —— 库是 WAL 模式的，
# 上一个实例被 Stop-Process 强杀后 -wal 可能非空，下一个实例关连接时会 checkpoint
# 把主库重写一遍：逻辑内容没变、字节变了，拿哈希当不变量会把发布门禁卡成红的。
# 而「LLM 配置还在不在」正是用户抱怨的那件事本身。
Write-Step '升级一次，看数据在不在'

$dataDir = Join-Path $destDir 'HYXi-数据'
$probe   = "probe-model-$(Get-Random)"

function Get-SecretKey($envPath) {
    # 不用 (... | Where-Object ...)[0]：只有一行匹配时管道回的是**单个字符串**
    # 而不是数组，[0] 于是取到第一个字符，.Trim() 当场报
    # 「System.Char does not contain a method named 'Trim'」。实测踩过
    $m = [regex]::Match((Get-Content $envPath -Raw), 'TWEAKERS_SECRET_KEY=(\S+)')
    if (-not $m.Success) { return '' }
    return $m.Groups[1].Value
}

function Start-Pkg($dir, $tag) {
    $o = Join-Path $destDir "stdout-$tag.log"
    $e = Join-Path $destDir "stderr-$tag.log"
    $env:PATH = $cleanPath
    try {
        $p = Start-Process -FilePath (Join-Path $dir 'app\hyxi.exe') -WorkingDirectory $dir `
                           -PassThru -NoNewWindow -RedirectStandardOutput $o -RedirectStandardError $e
    } finally { $env:PATH = $savedPath }
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 3
            if ($h.status -eq 'ok') { return @{ proc = $p; log = $o } }
        } catch { Start-Sleep -Milliseconds 1000 }
        if ($p.HasExited) { break }
    }
    Write-Fail "$tag 没起来"
    if (Test-Path $o) { Write-Host (Get-Content $o -Raw -Encoding UTF8) -ForegroundColor DarkGray }
    exit 1
}

function Stop-Pkg($h) {
    Stop-Process -Id $h.proc.Id -Force -ErrorAction SilentlyContinue
    $h.proc.WaitForExit(10000) | Out-Null
    Start-Sleep -Milliseconds 500
}

# 先用刚验收完的那个实例存一份配置当探针
$h1 = Start-Pkg $pkg 'probe'
try {
    Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/config' -Method Post `
        -ContentType 'application/json' `
        -Body (@{ api_key = 'sk-verify'; base_url = 'http://127.0.0.1:1'; model_name = $probe } | ConvertTo-Json) | Out-Null
} finally { Stop-Pkg $h1 }
$keyBefore = Get-SecretKey (Join-Path $dataDir '.env')
Write-Ok "已存入一份 LLM 配置当探针（model_name=$probe）"

# ---- 5a. 常规升级：新版本解到旁边，直接接上外部数据 ----
$next = Join-Path $destDir 'HYXi-9.9.9-win64'
$stage = Join-Path $destDir '_unzip'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive -Path $zip.FullName -DestinationPath $stage -Force
Move-Item (Get-ChildItem $stage -Directory | Select-Object -First 1).FullName $next
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

$h2 = Start-Pkg $next 'upgrade'
try {
    if (Test-Path (Join-Path $next 'data')) { Write-Fail '新版本又在包里建了 data'; exit 1 }
    Write-Ok '新版本没有另建一份包内数据'
    $cfg = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/config'
    if ($cfg.model_name -ne $probe) {
        Write-Fail "升级后 LLM 配置丢了（读到 '$($cfg.model_name)'，应为 '$probe'）"; exit 1
    }
    Write-Ok '升级后 LLM 配置原样还在'
    $keyAfter = Get-SecretKey (Join-Path $dataDir '.env')
    if ($keyAfter -ne $keyBefore) {
        Write-Fail '加密密钥变了 —— 库里的数据源密码会全部解不开'; exit 1
    }
    Write-Ok '加密密钥没变，已录入的凭据仍能解开'
} finally { Stop-Pkg $h2 }

# ---- 5b. 从旧布局接管：1.8.1 及以前的数据还在旧包肊子里 ----
# _previous_install() 只认「同级目录里有 data\hyxi.db」，不必搬一个完整的包过来
Write-Step '从旧版本包里接数据'
$legacy = Join-Path $destDir 'HYXi-1.8.1-win64'
New-Item -ItemType Directory -Force -Path $legacy | Out-Null
Move-Item $dataDir (Join-Path $legacy 'data')
Move-Item (Join-Path $legacy 'data\.env') (Join-Path $legacy '.env')
if (Test-Path $dataDir) { Write-Fail '外部数据目录没腾干净，接管那条路测不到'; exit 1 }

$h3 = Start-Pkg $next 'adopt'
try {
    $cfg = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/config'
    if ($cfg.model_name -ne $probe) {
        Write-Fail "没有从旧包接到数据（读到 '$($cfg.model_name)'）"; exit 1
    }
    Write-Ok '旧包里的配置已被接管到包外数据目录'
    $keyAfter = Get-SecretKey (Join-Path $dataDir '.env')
    if ($keyAfter -ne $keyBefore) { Write-Fail '接管时密钥没跟过来'; exit 1 }
    Write-Ok '密钥也跟着搬过来了'
    if (-not (Test-Path (Join-Path $legacy 'data\hyxi.db'))) {
        Write-Fail '旧包被搬空了 —— 应该是复制，用户还要能回退'; exit 1
    }
    Write-Ok '旧包原封不动，想回退就回退'
} finally { Stop-Pkg $h3 }

Write-Step '结果'
Write-Host "    便携包验收通过：$pkg" -ForegroundColor Green
Write-Host "    升级验证通过：常规升级 + 从旧包接管，两条路的数据与密钥都在" -ForegroundColor Green
exit 0
