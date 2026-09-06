$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Noyra virtual environment was not found at $Python"
}
Set-Location $Root
& $Python -m noyra.desktop
exit $LASTEXITCODE
