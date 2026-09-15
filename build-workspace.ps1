$ErrorActionPreference = 'Stop'
$workspacePreviousPath = $env:PATH
try {
    # Avoid collecting unrelated ICU and CRT DLLs from document-tool runtimes.
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR"
    & (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -m PyInstaller --clean --noconfirm (Join-Path $PSScriptRoot 'MAX-Workspace.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Workspace build failed.' }
} finally {
    $env:PATH = $workspacePreviousPath
}
