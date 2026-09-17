$ErrorActionPreference = 'Stop'
$workspacePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workspacePython)) {
    & py -3.13 -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.13 for Windows, including the py launcher.' }
}
& $workspacePython -m pip install -r (Join-Path $PSScriptRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $workspacePython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
Write-Host 'Ready. Restore transferred data before the first application launch; see TRANSFER.md.'
Write-Host 'Run: .venv\Scripts\python.exe workspace_ui.py'
