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

# 全程日志: 屏幕输出同步写入仓库根目录 setup_log_*.log (*.log 已被
# .gitignore 忽略); 部署遇到问题把该文件整个发回来即可定位
$logFile = Join-Path $repo ("setup_log_{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
try { Start-Transcript -Path $logFile | Out-Null } catch {}

function Log($msg) {
    Write-Host ("      [{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg)
}

function Fail($msg) {
    Write-Host ""
    Write-Host "[部署未完成] $msg" -ForegroundColor Red
    Write-Host "修复后重新运行本脚本, 已完成的步骤会自动跳过。"
    Write-Host "本次部署完整日志: $logFile"
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

Log "部署开始"

# ---- 1. 子模块 ----
Write-Host "[1/6] 初始化翻译引擎子模块 ..."
Log "执行: git submodule update --init --recursive"
git submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { Fail "子模块初始化失败, 请检查网络后重试。" }
Log "子模块就绪"

# ---- 2. 查找 Python 3.10-3.13 ----
# 逐候选探测真实版本:不能只看 py/python 能否启动——新版 Python 安装管理器
# (py) 的默认版本可能是 3.14+,而 "py -3.12" 指定的版本又未必安装,必须对
# 每个候选连版本参数一起执行并校验版本号,命中范围内即用。
Write-Host "[2/6] 查找 Python (需要 3.10-3.13) ..."
$snip = "import sys; print('%d.%d' % sys.version_info[:2])"
$pyExe, $pyArgs, $ver = $null, $null, $null
foreach ($cand in @(
    @("py", "-3.12"), @("py", "-3.11"), @("py", "-3.13"), @("py", "-3.10"),
    @("python", $null), @("python3", $null)
)) {
    try {
        if ($cand[1]) { $ver = & $cand[0] $cand[1] -c $snip 2>$null }
        else { $ver = & $cand[0] -c $snip 2>$null }
        $ok = ($LASTEXITCODE -eq 0 -and "$ver" -match '^\d+\.\d+$' -and
            [version]"$ver" -ge [version]"3.10" -and [version]"$ver" -lt [version]"3.14")
        if ($ok) {
            $pyExe, $pyArgs = $cand[0], $cand[1]
            Log ("候选 {0} {1}: Python {2} -> 选用" -f $cand[0], $cand[1], $ver)
            break
        }
        Log ("候选 {0} {1}: 不可用 (探测版本: '{2}')" -f $cand[0], $cand[1], "$ver".Trim())
    } catch {
        Log ("候选 {0} {1}: 未安装" -f $cand[0], $cand[1])
    }
}
if (-not $pyExe) {
    Fail "未找到 Python 3.10-3.13。请安装 3.11/3.12: https://www.python.org/downloads/ (勾选 Add to PATH) 或 winget install Python.Python.3.12"
}

# ---- 3. venv + 引擎 ----
Write-Host "[3/6] 创建内核虚拟环境并安装 pdf2zh-next 2.9.0 (约 5 分钟) ..."
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $pyExe $pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "venv 创建失败。" }
}
$vp = Join-Path $venv "Scripts\python.exe"
# pip 源: 默认清华 TUNA —— 客户机多在国内, pypi.org 直连只有 1-2MB/s;
# 失败自动回退 阿里云 -> 官方源。海外部署设环境变量 PDF2ZH_PIP_INDEX 覆盖,
# 例如恢复官方源: set PDF2ZH_PIP_INDEX=https://pypi.org/simple
$mirrors = @(
    $env:PDF2ZH_PIP_INDEX,
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.org/simple"
) | Where-Object { $_ }

function Install-PipPackage($spec, $desc) {
    foreach ($m in $mirrors) {
        Log "安装 $desc (源: $m) ..."
        & $vp -m pip install $spec -i $m
        if ($LASTEXITCODE -eq 0) { Log "$desc 安装完成"; return }
        Log "源 $m 失败, 自动尝试下一个 ..."
    }
    Fail "$desc 安装失败(所有 pip 源均失败), 请检查网络后重新运行。"
}

# 幂等保护: 引擎已装好就整段跳过。重跑时若带着"pdf2zh-next 要 pymupdf<1.25.3
# 而 babeldoc 已把 pymupdf 升级"的已知冲突再执行 pip install, 解析器会试图
# 降级 pymupdf 修复冲突, 白白下载还可能因镜像 403 而中断
$haveEngine = (& $vp -m pip show pdf2zh-next 2>$null | Select-String "^Version: 2.9.0")
$haveBabel = (& $vp -m pip show babeldoc 2>$null | Select-String "^Version: 0.6.4")
if ($haveEngine -and $haveBabel) {
    Log "pdf2zh-next 2.9.0 与 babeldoc 0.6.4 均已安装, 跳过引擎安装"
} else {
    Log "升级 pip (源: $($mirrors[0])) ..."
    & $vp -m pip install --upgrade pip -i $mirrors[0]
    if ($LASTEXITCODE -ne 0) { Log "[提示] pip 升级失败, 不阻塞, 用 venv 自带 pip 继续" }
    # 分两步装: pdf2zh-next 2.9.0 的依赖声明与 babeldoc>=0.6.4 冲突, 无法一次性解析;
    # 与本机验证过的环境一致——先装引擎, 再独立升级 babeldoc(pip 会警告依赖冲突, 属预期)。
    # 不加 -q: 保留 pip 进度条, 客户机网慢时窗口长期无输出会被误认为卡死。
    Install-PipPackage "pdf2zh-next==2.9.0" "pdf2zh-next==2.9.0 及全部依赖 (下载约 0.5GB, 国内镜像一般 1-3 分钟)"
    Install-PipPackage "babeldoc==0.6.4" "babeldoc==0.6.4 (排版内核)"
    $pkgver = (& $vp -m pip show pdf2zh-next | Select-String "^Version").ToString()
    Log "引擎安装完成: $pkgver"
}

# ---- 4. 补丁 ----
Write-Host "[4/6] 应用定制补丁 (Ollama 修复 / 历史页签) ..."
Log "执行: apply_all_patches.py"
& $vp (Join-Path $repo "script\apply_all_patches.py")
if ($LASTEXITCODE -ne 0) { Fail "补丁应用失败, 请将上方输出发给作者。" }
Log "补丁应用完成"

# ---- 5. Ollama 与模型 ----
Write-Host "[5/6] 检查 Ollama 与翻译模型 ..."
$ollama = "ollama"
if (Get-Command $ollama -ErrorAction SilentlyContinue) {
    Log "检测到 Ollama: $((Get-Command $ollama).Source)"
} else {
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $local) { $ollama = $local; Log "检测到 Ollama (本地安装): $local" }
}
if (-not (Get-Command $ollama -ErrorAction SilentlyContinue)) {
    Log "未检测到 Ollama, 通过 winget 安装 (下载约 1GB) ..."
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $local) { $ollama = $local; Log "Ollama 安装完成: $local" }
    else { Log "[警告] winget 安装后仍未找到 ollama 命令, 可能需要重开终端" }
}
if (Get-Command $ollama -ErrorAction SilentlyContinue) {
    if ($skipModel) {
        Write-Host "      按要求跳过模型下载。"
    } else {
        Log "下载翻译模型 s2021008840/hy-mt2:7b-q4_k_m (约 4.6GB, 已装则秒过) ..."
        & $ollama pull s2021008840/hy-mt2:7b-q4_k_m
        if ($LASTEXITCODE -ne 0) {
            Write-Host "      [警告] 模型下载失败, 稍后手动运行: ollama pull s2021008840/hy-mt2:7b-q4_k_m" -ForegroundColor Yellow
        } else {
            Log "翻译模型就绪"
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
    Log "预下载 BabelDOC 模型资产 (版面模型/字体, 实测约 340MB) ..."
    & (Join-Path $venv "Scripts\python.exe") -c "from babeldoc.assets import assets; assets.warmup()"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      [警告] 资产预下载失败, 首次翻译时会自动重试。" -ForegroundColor Yellow
    } else {
        Log "BabelDOC 资产就绪"
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
Log "启动器已生成: $launcher"

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
Log "静默启动器已生成: $hiddenLauncher"

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
        Log "桌面快捷方式已创建: $([IO.Path]::Combine($desktop, 'PDF翻译工作台.lnk'))"
    } catch {
        Write-Host "      [警告] 快捷方式创建失败, 可直接运行 start_workbench.bat" -ForegroundColor Yellow
    }
}

Log "部署完成"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " 部署完成! 正在启动工作台 (浏览器将自动打开, 端口 $port) ..."
Write-Host " 日常使用: 双击桌面「PDF翻译工作台」图标(无黑窗, 自动打开网页)"
Write-Host " 停止服务: 网页右上角「关闭服务」按钮"
Write-Host "============================================================" -ForegroundColor Green
Log "完整日志: $logFile"
try { Stop-Transcript | Out-Null } catch {}
Set-Location $kernel
$env:PYTHONPATH = $repo
$env:PDF2ZH_PORT = $port
& $vp -m custom_pdf2zh.webapp.server $port
