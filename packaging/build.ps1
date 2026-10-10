# Build the packaged program, check it, and (when Inno Setup is installed) the installer + checksums.
#   powershell -File packaging\build.ps1            # everything available
#   powershell -File packaging\build.ps1 -SkipInstaller
# Output: dist-app\Vibe Launcher\ (the program), dist-app\installer\Vibe-Launcher-Setup-<version>.exe, SHA256SUMS.txt
# Needs the build tools: uv sync --group build   (CI does this; locally `uv pip install pyinstaller` into .venv also works).
param([switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Run "uv sync --group build" first.' }
$env:PYTHONUTF8 = '1'

& $python packaging\make_icon.py
if ($LASTEXITCODE) { throw 'Icon build failed.' }
& $python -m PyInstaller --clean --noconfirm --distpath dist-app --workpath build-app packaging\vibe-launcher.spec
if ($LASTEXITCODE) { throw 'PyInstaller failed.' }

# What a packaged build can silently lose (data files, trimmed Qt modules, helper command lines) shows up here.
$folder = Join-Path $root 'dist-app\Vibe Launcher'
$env:QT_QPA_PLATFORM = 'offscreen'
$result = & (Join-Path $folder 'vibe-launcher-cli.exe') --selftest
Write-Output $result
if ($LASTEXITCODE) { throw 'The packaged program failed its self-test.' }
Remove-Item Env:QT_QPA_PLATFORM

if ($SkipInstaller) { return }
$version = (& $python -c "import vibe_launcher; print(vibe_launcher.__version__)").Trim()
$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $candidate) { $iscc = $candidate; break }
    }
}
if (-not $iscc) { Write-Warning 'Inno Setup 6 not found: the program folder is built, the installer is not.'; return }
& $iscc "/DAppVersion=$version" "/DSourceDir=$folder" packaging\installer.iss
if ($LASTEXITCODE) { throw 'Inno Setup failed.' }
$installer = Join-Path $root "dist-app\installer\Vibe-Launcher-Setup-$version.exe"
$hash = (Get-FileHash -Algorithm SHA256 $installer).Hash.ToLower()
# "<hash>  <file name>": the format the launcher's updater reads (updates._checksum)
Set-Content -Path (Join-Path $root 'dist-app\installer\SHA256SUMS.txt') -Value "$hash  $(Split-Path -Leaf $installer)" -Encoding ascii
Write-Output "Installer: $installer ($hash)"
