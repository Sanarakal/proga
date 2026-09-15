$ErrorActionPreference = 'Stop'
$workspaceSource = Join-Path $PSScriptRoot 'dist\MAX-Workspace'
if (-not (Test-Path -LiteralPath (Join-Path $workspaceSource 'MAX-Workspace.exe'))) {
    throw 'MAX-Workspace build not found next to this script.'
}
$workspaceDestination = Join-Path $env:LOCALAPPDATA 'Programs\MAX-Workspace'
if (Get-Process -Name MAX-Workspace -ErrorAction SilentlyContinue) {
    throw 'Close MAX Workspace before installation.'
}
if (Test-Path -LiteralPath $workspaceDestination) {
    $workspaceBackup = Join-Path $env:LOCALAPPDATA ('Programs\MAX-Workspace.previous-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    Move-Item -LiteralPath $workspaceDestination -Destination $workspaceBackup
}
New-Item -ItemType Directory -Path $workspaceDestination -Force | Out-Null
Copy-Item -Path (Join-Path $workspaceSource '*') -Destination $workspaceDestination -Recurse -Force
$workspaceShell = New-Object -ComObject WScript.Shell
$workspaceShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'MAX Workspace.lnk'
$workspaceShortcut = $workspaceShell.CreateShortcut($workspaceShortcutPath)
$workspaceShortcut.TargetPath = Join-Path $workspaceDestination 'MAX-Workspace.exe'
$workspaceShortcut.WorkingDirectory = $workspaceDestination
$workspaceShortcut.Save()
Write-Host 'Installed MAX Workspace. A desktop shortcut is ready.'
