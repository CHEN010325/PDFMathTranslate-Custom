# PDFMathTranslate-Custom 一键部署 (Windows 10/11)
# 由 script\setup_windows.bat 调用; 也可直接运行:
#   powershell -NoProfile -ExecutionPolicy Bypass -File script\setup_windows.ps1
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$repo   = Split-Path $PSScriptRoot -Parent
$kernel = Join-Path $repo "pdf2zh\kernel\PDFMathTranslate-next.git"
$venv   = Join-Path $kernel ".venv"
$port   = if ($env:PDF2ZH_PORT) { $env:PDF2ZH_PORT } else { "7860" }
$skipShortcut = [bool]$env:PDF2ZH_SKIP_SHORTCUT
$skipModel    = [bool]$env:PDF2ZH_SKIP_MODEL

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " PDFMathTranslate-Custom 一键部署"
Write-Host " 仓库: $repo"
Write-Host "============================================================" -ForegroundColor Cyan

function Fail($msg) {
    Write-Host ""
    Write-Host "[部署未完成] $msg" -ForegroundColor Red
    Write-Host "修复后重新运行本脚本, 已完成的步骤会自动跳过。"
    exit 1
}

# ---- 1. 子模块 ----
Write-Host "[1/6] 初始化翻译引擎子模块 ..."
git submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { Fail "子模块初始化失败, 请检查网络后重试。" }

# ---- 2. 查找 Python 3.10+ ----
Write-Host "[2/6] 查找 Python (需要 3.10-3.13) ..."
$pycmd = $null
foreach ($c in @("py -3.12", "py -3.11", "py -3.13", "python")) {
    try {
        $v = & $c.Split(" ")[0] --version 2>$null
        if ($LASTEXITCODE -eq 0) { $pycmd = $c; break }
    } catch {}
}
# py launcher 带版本号: "py -3.12" 需整体执行
if (-not $pycmd) { Fail "未找到 Python。请安装 3.11/3.12: https://www.python.org/downloads/ (勾选 Add to PATH) 或 winget install Python.Python.3.12" }
$pyExe, $pyArgs = if ($pycmd -like "py *") { $pycmd.Split(" ")[0], $pycmd.Split(" ")[1] } else { $pycmd, $null }
$ver = & $pyExe $pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.10" -or [version]$ver -ge [version]"3.14") {
    Fail "Python $ver 不在支持范围 (3.10-3.13), 请安装 3.11 或 3.12。"
}
Write-Host "      找到 Python $ver"

# ---- 3. venv + 引擎 ----
Write-Host "[3/6] 创建内核虚拟环境并安装 pdf2zh-next 2.9.0 (约 5 分钟) ..."
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $pyExe $pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "venv 创建失败。" }
}
$vp = Join-Path $venv "Scripts\python.exe"
& $vp -m pip install --upgrade pip -q
# 分两步装: pdf2zh-next 2.9.0 的依赖声明与 babeldoc>=0.6.4 冲突, 无法一次性解析;
# 与本机验证过的环境一致——先装引擎, 再独立升级 babeldoc(pip 会警告依赖冲突, 属预期)。
& $vp -m pip install -q "pdf2zh-next==2.9.0"
if ($LASTEXITCODE -ne 0) { Fail "pdf2zh-next 安装失败, 请检查网络后重新运行。" }
& $vp -m pip install -q "babeldoc==0.6.4"
if ($LASTEXITCODE -ne 0) { Fail "babeldoc 升级失败, 请检查网络后重新运行。" }
$pkgver = (& $vp -m pip show pdf2zh-next | Select-String "^Version").ToString()
Write-Host "      已安装 $pkgver"

# ---- 4. 补丁 ----
Write-Host "[4/6] 应用定制补丁 (Ollama 修复 / 历史页签) ..."
& $vp (Join-Path $repo "script\apply_all_patches.py")
if ($LASTEXITCODE -ne 0) { Fail "补丁应用失败, 请将上方输出发给作者。" }

# ---- 5. Ollama 与模型 ----
Write-Host "[5/6] 检查 Ollama 与翻译模型 ..."
$ollama = "ollama"
if (-not (Get-Command $ollama -ErrorAction SilentlyContinue)) {
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $local) { $ollama = $local }
}
if (-not (Get-Command $ollama -ErrorAction SilentlyContinue)) {
    Write-Host "      未检测到 Ollama, 尝试通过 winget 安装 ..."
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $local) { $ollama = $local }
}
if (Get-Command $ollama -ErrorAction SilentlyContinue) {
    if ($skipModel) {
        Write-Host "      按要求跳过模型下载。"
    } else {
        Write-Host "      下载翻译模型 s2021008840/hy-mt2:7b-q4_k_m (约 4.6GB, 已装则秒过) ..."
        & $ollama pull s2021008840/hy-mt2:7b-q4_k_m
        if ($LASTEXITCODE -ne 0) {
            Write-Host "      [警告] 模型下载失败, 稍后手动运行: ollama pull s2021008840/hy-mt2:7b-q4_k_m" -ForegroundColor Yellow
        } else {
            Write-Host "      模型就绪。"
        }
    }
} else {
    Write-Host "      [警告] Ollama 未就绪, 请手动安装 https://ollama.com/download 后运行:" -ForegroundColor Yellow
    Write-Host "             ollama pull s2021008840/hy-mt2:7b-q4_k_m" -ForegroundColor Yellow
}

    # 预下载 BabelDOC 排版/公式模型资产(~1-2GB): 没有这一步, 用户首次翻译时
    # 才会从 funstory CDN 下载, 国内网络可能很慢甚至失败
    # 注: pdf2zh_next --warmup 在 2.9.0 会先下载资产、再因缺 input 文件断言退出
    #     (误报失败), 这里直接调 babeldoc 的 warmup, 干净退出
    Write-Host "      预下载 BabelDOC 模型资产 (版面模型/字体, 约 1-2GB) ..."
    & (Join-Path $venv "Scripts\python.exe") -c "from babeldoc.assets import assets; assets.warmup()"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      [警告] 资产预下载失败, 首次翻译时会自动重试。" -ForegroundColor Yellow
    } else {
        Write-Host "      资产就绪。"
    }

# ---- 6. 启动器与桌面快捷方式 ----
Write-Host "[6/6] 生成启动器 ..."
$launcher = Join-Path $repo "start_workbench.bat"
@"
@echo off
title PDF Translation Workbench
cd /d "$kernel"
set "PYTHONPATH=$repo"
set "PDF2ZH_PORT=$port"
".venv\Scripts\python.exe" -m custom_pdf2zh.webapp.server $port
pause
"@ | Out-File -FilePath $launcher -Encoding ascii

# 静默启动器(双击桌面图标的目标): 服务在跑 → 直接打开网页;
# 没在跑 → 无黑窗隐藏启动(bat 里 server 就绪后会自己打开网页)
# 内容保持纯 ASCII: wscript 对非 ASCII 的 .vbs 依赖系统代码页, 易乱码
$hiddenLauncher = Join-Path $repo "start_workbench_hidden.vbs"
@"
' PDF Translation Workbench silent launcher (no console window).
' If the service is already running, just open the web page;
' otherwise start it hidden (the server opens the page when ready).
Dim sh, http, url, launcher, running
url = "http://127.0.0.1:$port/"
launcher = "$launcher"
Set sh = CreateObject("WScript.Shell")
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", url, False
http.Send
running = (Err.Number = 0)
On Error GoTo 0
If running Then
    sh.Run url
Else
    sh.Run Chr(34) & launcher & Chr(34), 0, False
End If
"@ | Out-File -FilePath $hiddenLauncher -Encoding ascii

if ($skipShortcut) {
    Write-Host "      按要求跳过桌面快捷方式。"
} else {
    try {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $shell = New-Object -ComObject WScript.Shell
        $lnk = $shell.CreateShortcut((Join-Path $desktop "PDF翻译工作台.lnk"))
        $lnk.TargetPath = $hiddenLauncher
        $lnk.WorkingDirectory = $repo
        $lnk.IconLocation = Join-Path $kernel ".venv\Scripts\python.exe,0"
        $lnk.Save()
        Write-Host "      桌面快捷方式已创建(双击=静默启动+自动打开网页)。"
    } catch {
        Write-Host "      [警告] 快捷方式创建失败, 可直接运行 start_workbench.bat" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " 部署完成! 正在启动工作台 (浏览器将自动打开, 端口 $port) ..."
Write-Host " 日常使用: 双击桌面「PDF翻译工作台」图标(无黑窗, 自动打开网页)"
Write-Host " 停止服务: 网页右上角「关闭服务」按钮"
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Set-Location $kernel
$env:PYTHONPATH = $repo
$env:PDF2ZH_PORT = $port
& $vp -m custom_pdf2zh.webapp.server $port
