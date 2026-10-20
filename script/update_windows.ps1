# Safe main-branch update. ASCII source works with Windows PowerShell 5.1.
param([switch]$NoStart, [switch]$SkipAssets, [switch]$SkipOllama)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot "windows_common.ps1")
try {
    Set-Location -LiteralPath $repo
    Assert-WorkbenchStopped $repo
    $branch = & git branch --show-current
    if ($LASTEXITCODE -ne 0 -or $branch -ne "main") {
        throw "Update requires the main branch. Switch to main first; no branch is created automatically."
    }
    $dirty = & git status --porcelain --untracked-files=no --ignore-submodules=dirty
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect repository status." }
    if ($dirty) { throw "Tracked files have local changes. Preserve or commit them before updating; nothing will be discarded." }
    & git fetch origin main
    if ($LASTEXITCODE -ne 0) { throw "Cannot fetch origin/main. Check your connection and retry." }
    $ahead = & git rev-list --count origin/main..HEAD
    if ($LASTEXITCODE -ne 0 -or [int]$ahead -ne 0) {
        throw "Local main contains commits absent from origin/main. Resolve them before updating."
    }
    & git merge --ff-only origin/main
    if ($LASTEXITCODE -ne 0) { throw "Main could not fast-forward. Existing files were not reset or cleaned." }
    $setupArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "setup_windows.ps1"))
    foreach ($name in @("NoStart", "SkipAssets", "SkipOllama")) {
        if (Get-Variable -Name $name -ValueOnly) { $setupArgs += "-$name" }
    }
    & powershell @setupArgs
    exit $LASTEXITCODE
} catch {
    Write-Host "Update incomplete: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
