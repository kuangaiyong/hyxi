# HYXi 便携包构建 —— 一条命令出 ZIP
#
# 用法：在仓库根目录执行  .\build\build.ps1
# 退出码：0 = 出包成功；1 = 任一步失败（含泄漏自检不过）
#
# 产物是免安装的便携包：解压双击即用，不需要管理员权限、不写注册表、删目录即卸载。
# 交付出去的东西里**不含任何源码**，见下面「泄漏自检」那一步。

$ErrorActionPreference = 'Stop'

$root  = Split-Path -Parent $PSScriptRoot
$out   = Join-Path $root 'build\out'

function Write-Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Write-Ok($t)   { Write-Host "    [OK]   $t" -ForegroundColor Green }
function Write-Fail($t) { Write-Host "    [失败] $t" -ForegroundColor Red }

# PS 5.1 在 $ErrorActionPreference='Stop' 下，会把原生命令写到 stderr 的**任何一行**
# 当成终止性错误 —— npm / vite 的彩色进度就走 stderr，第一步就会莫名其妙地炸。
# 成败一律以退出码为准，这也是这三个工具真正表达结果的地方。
function Invoke-Native($what, [scriptblock]$block) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $block } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "$what 失败（退出码 $LASTEXITCODE）" }
}

$version = (Get-Content (Join-Path $root 'frontend\package.json') -Raw | ConvertFrom-Json).version
$name    = "HYXi-$version-win64"
$stage   = Join-Path $out $name

Write-Host "HYXi 便携包构建  v$version" -ForegroundColor White

# ---------------- 1. 前端 ----------------
Write-Step '构建前端'
Push-Location (Join-Path $root 'frontend')
try {
    Invoke-Native '前端构建' { npm run build }
} finally { Pop-Location }
Write-Ok '前端产物就绪'

# ---------------- 2. 采集脚本 ----------------
Write-Step '打包采集脚本'
# 注意：这里只做 bundle + minify，不是字节码。Playwright 的 page.evaluate 靠
# fn.toString() 把函数当文本送进浏览器，任何丢弃函数源文本的方案（bytenode / pkg /
# SEA）都会让 DOM 提取直接失效 —— 实测报 "Passed function is not well-serializable!"
$collectorScript = Join-Path $root 'build\collectors.build.mjs'
$collectorOut = Join-Path $out 'collectors'
Invoke-Native '采集脚本打包' { node $collectorScript $collectorOut }

# ---------------- 3. 后端 ----------------
Write-Step '编译后端（Nuitka，需要几分钟）'
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
Push-Location (Join-Path $root 'backend')
try {
    # 注意：--output-dir=(Join-Path ...) 会被 PowerShell 拆成两个参数
    # （实测得到 "--output-dir=" 和路径各一个），必须先拼成字符串再传
    $nuitkaOut = Join-Path $out 'nuitka'
    Invoke-Native 'Nuitka 编译' {
        & $python -m nuitka `
            --standalone --assume-yes-for-downloads `
            "--output-dir=$nuitkaOut" --output-filename=hyxi.exe `
            --include-package=app --include-package=uvicorn --include-package=apscheduler `
            --include-package=sqlalchemy.dialects.sqlite --include-package=PIL `
            --include-distribution-metadata=APScheduler `
            --nofollow-import-to=pytest --nofollow-import-to=nuitka `
            --company-name=HYXi --product-name='HYXi 舆情分析平台' `
            --file-version=$version --product-version=$version `
            run_server.py
    }
} finally { Pop-Location }
Write-Ok '后端已编译成原生机器码'

# ---------------- 4. 组装 ----------------
Write-Step '组装便携包'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Copy-Item (Join-Path $out 'nuitka\run_server.dist') (Join-Path $stage 'app') -Recurse
Copy-Item (Join-Path $root 'frontend\dist') (Join-Path $stage 'web') -Recurse
Copy-Item (Join-Path $out 'collectors') (Join-Path $stage 'collectors') -Recurse

# node_modules 必须放在包根：Node 的 require 是从**请求文件所在目录**逐级向上找的，
# collectors/ 的上一级正好是包根。挪进 app\ 就解析不到 playwright 了
$nm = Join-Path $stage 'node_modules'
New-Item -ItemType Directory -Force -Path $nm | Out-Null
foreach ($dep in @('playwright', 'playwright-core')) {
    Copy-Item (Join-Path $root "node_modules\$dep") (Join-Path $nm $dep) -Recurse
}

# 便携 Node 运行时。用本机这一份，与 collectors.build.mjs 的 target 对得上
$nodeSrc = (Get-Command node -ErrorAction Stop).Source
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'node') | Out-Null
Copy-Item $nodeSrc (Join-Path $stage 'node\node.exe')

Copy-Item (Join-Path $root 'build\启动 HYXi.bat') $stage
Copy-Item (Join-Path $root 'build\使用说明.txt') $stage
Write-Ok "已组装到 $stage"

# ---------------- 5. 泄漏自检 ----------------
# 这一步是硬门槛：交付包里混进源码或密钥就等于把 IP 和账号一起送出去
Write-Step '泄漏自检'
$problems = @()

$py = Get-ChildItem $stage -Recurse -Include *.py, *.pyc, *.pyi -File -ErrorAction SilentlyContinue
if ($py) { $problems += "混进了 $($py.Count) 个 Python 源文件，第一个：$($py[0].FullName)" }

# .env / 数据库 / 会话 / 明文 LLM 密钥 —— 分别是加密密钥、业务数据、Facebook 会话 cookie
foreach ($secret in @('.env', 'config.json', '.scraper_state.json', 'hyxi.db')) {
    $hit = Get-ChildItem $stage -Recurse -Force -Filter $secret -ErrorAction SilentlyContinue
    if ($hit) { $problems += "混进了机密文件 $secret：$($hit[0].FullName)" }
}
foreach ($dir in @('data', 'sessions', 'media')) {
    $hit = Get-ChildItem $stage -Recurse -Force -Directory -Filter $dir -ErrorAction SilentlyContinue
    if ($hit) { $problems += "混进了运行期数据目录 $dir：$($hit[0].FullName)" }
}

# 采集脚本必须是打包压缩过的，不能是源码原样拷过去
foreach ($f in Get-ChildItem (Join-Path $stage 'collectors') -Filter *.js -File) {
    $src = Join-Path $root "collectors\$($f.Name)"
    $text = Get-Content $f.FullName -Raw
    if ((Test-Path $src) -and ((Get-FileHash $f.FullName).Hash -eq (Get-FileHash $src).Hash)) {
        $problems += "$($f.Name) 与源码逐字节相同，压缩没生效"
    }
    if ($text -match "require\('\./lib/") { $problems += "$($f.Name) 没有 bundle，还在 require ./lib/" }
    if (($text -split "`n").Count -gt 200) { $problems += "$($f.Name) 有 $(($text -split "`n").Count) 行，不像压缩过的" }
}
if (Test-Path (Join-Path $stage 'collectors\lib')) { $problems += 'collectors\lib 源码目录被拷进来了' }

# 构建期依赖不该跟着分发
foreach ($dev in @('esbuild', '@esbuild')) {
    if (Test-Path (Join-Path $stage "node_modules\$dev")) { $problems += "构建期依赖 $dev 混进了包里" }
}

if ($problems.Count -gt 0) {
    foreach ($p in $problems) { Write-Fail $p }
    Write-Host "`n出包中止：交付包里不能有上面这些东西。" -ForegroundColor Red
    exit 1
}
Write-Ok '无源码、无密钥、无运行期数据、无构建期依赖'

# ---------------- 6. 打包 ----------------
Write-Step '压缩'
$zip = Join-Path $out "$name.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip -CompressionLevel Optimal

$sizeStage = '{0:N0} MB' -f ((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
$sizeZip   = '{0:N0} MB' -f ((Get-Item $zip).Length / 1MB)

Write-Step '结果'
Write-Host "    $zip" -ForegroundColor Green
Write-Host "    解压后 $sizeStage / 压缩后 $sizeZip" -ForegroundColor Green
Write-Host "    交付方式：把这个 ZIP 发过去，对方解压后双击「启动 HYXi.bat」" -ForegroundColor DarkGray
exit 0
