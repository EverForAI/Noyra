$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

& $python (Join-Path $PSScriptRoot 'audit-m41.py') @args
exit $LASTEXITCODE

