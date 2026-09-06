$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running deterministic subject-kernel audit...'
& $python -m pytest tests/test_kernel.py -q
if ($LASTEXITCODE -ne 0) { throw 'Kernel tests failed.' }
& $python -m pytest --cov=noyra.core --cov-report=term-missing --cov-fail-under=85
if ($LASTEXITCODE -ne 0) { throw 'Kernel coverage audit failed.' }
& $python -m ruff check src/noyra/core tests/test_kernel.py scripts
if ($LASTEXITCODE -ne 0) { throw 'Kernel lint audit failed.' }
& $python -m ruff format --check src/noyra/core tests/test_kernel.py
if ($LASTEXITCODE -ne 0) { throw 'Kernel format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Kernel type audit failed.' }

$database = Join-Path $env:NOYRA_DATA_DIR 'audit-kernel.sqlite3'
if (Test-Path -LiteralPath $database) {
    Remove-Item -LiteralPath $database -Force
}

try {
    & $python -c "from pathlib import Path; from noyra.core import SubjectKernel; from noyra.core.types import content_hash; p=Path(r'$database'); k=SubjectKernel(p, 'Noyra-audit', content_hash({'seed':'audit'})); k.boot(); k.activate(); k.checkpoint({'audit':'ok'}, reason='kernel audit'); print(k.health())"
    if ($LASTEXITCODE -ne 0) { throw 'Kernel process-reopen smoke test failed.' }
    & $python -c "import sqlite3; c=sqlite3.connect(r'$database'); assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'; assert c.execute('PRAGMA foreign_key_check').fetchall() == []; print('SQLite integrity checks passed')"
    if ($LASTEXITCODE -ne 0) { throw 'SQLite integrity audit failed.' }
}
finally {
    if (Test-Path -LiteralPath $database) {
        Remove-Item -LiteralPath $database -Force
    }
    $wal = "$database-wal"
    $shm = "$database-shm"
    $lock = "$database.lock"
    if (Test-Path -LiteralPath $wal) { Remove-Item -LiteralPath $wal -Force }
    if (Test-Path -LiteralPath $shm) { Remove-Item -LiteralPath $shm -Force }
    if (Test-Path -LiteralPath $lock) { Remove-Item -LiteralPath $lock -Force }
}

Write-Host 'Subject-kernel audit passed.'
