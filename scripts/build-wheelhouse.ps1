$ErrorActionPreference = "Stop"

$RootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$Wheelhouse = Join-Path $RootDir "packaging\wheelhouse"
New-Item -ItemType Directory -Force -Path $Wheelhouse | Out-Null

python -m pip download `
  --only-binary=:all: `
  --platform manylinux2014_x86_64 `
  --python-version 38 `
  --implementation cp `
  --abi cp38 `
  --dest $Wheelhouse `
  -r (Join-Path $RootDir "requirements.txt")

Write-Output $Wheelhouse
