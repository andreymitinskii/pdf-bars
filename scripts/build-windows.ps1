param([string]$Python = "python", [string]$ISCC = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe")
$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & $Python -m PyInstaller packaging/windows/pdf-bars.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    New-Item -ItemType Directory -Path release/1.7 -Force | Out-Null
    Compress-Archive -Path dist/pdf-bars/* -DestinationPath release/1.7/pdf-bars-1.7-windows-x64-portable.zip -Force
    & $ISCC packaging/windows/pdf-bars.iss
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
} finally { Pop-Location }
