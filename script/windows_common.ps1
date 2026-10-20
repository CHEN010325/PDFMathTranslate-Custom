# Shared by deployment and update. Keep this file ASCII for Windows PowerShell 5.1.
function Assert-WorkbenchStopped([string]$Repository) {
    $lockPath = Join-Path $Repository "pdf2zh\kernel\PDFMathTranslate-next.git\pdf2zh_files\_server.lock"
    if (Test-Path -LiteralPath $lockPath) {
        try {
            $handle = [IO.File]::Open($lockPath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $handle.Dispose()
        } catch {
            throw "Close the workbench using its shutdown button before updating or redeploying. No files have been changed."
        }
    }
}
