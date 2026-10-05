$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Run uv sync in the project folder before creating the shortcut.'
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $projectRoot 'Vibe Launcher.lnk'))
$shortcut.TargetPath = $pythonPath
$shortcut.Arguments = '-m vibe_launcher.ui'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = 'Vibe Launcher - Codex / Claude / SSH'
$shortcut.Save()
Write-Output (Join-Path $projectRoot 'Vibe Launcher.lnk')
