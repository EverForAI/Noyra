$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'env.ps1')

$python = Join-Path $env:NOYRA_PROJECT_ROOT '.venv\Scripts\python.exe'

Write-Host 'Running Ubuntu service and deployment audit...'
& $python -m pytest tests/test_service.py tests/test_interaction.py tests/test_capability.py tests/test_m42_p1_01_sleep_deadlock.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py -q
if ($LASTEXITCODE -ne 0) { throw 'Service tests failed.' }
# The service intentionally keeps a broad defensive HTTP surface. The full
# repository gate remains the primary quality signal; this focused deployment
# suite enforces a 70% integration floor while the route matrix is expanded.
# Coverage reports are rounded to whole percentages, so use 69.9 as the
# precise 70% floor.
& $python -m pytest --cov=noyra.service --cov=noyra.interaction.projection --cov=noyra.autonomy --cov-report=term-missing --cov-fail-under=69.9 tests/test_service.py tests/test_interaction.py tests/test_capability.py tests/test_m42_p1_01_sleep_deadlock.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py
if ($LASTEXITCODE -ne 0) { throw 'Service coverage audit failed.' }
& $python -m ruff check src/noyra/service.py src/noyra/__main__.py src/noyra/core/at_rest.py src/noyra/core/integrity.py src/noyra/core/operator_controls.py src/noyra/web src/noyra/autonomy src/noyra/interaction/projection.py tests/test_service.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py
if ($LASTEXITCODE -ne 0) { throw 'Service lint audit failed.' }
& $python -m ruff format --check src/noyra/service.py src/noyra/__main__.py src/noyra/core/at_rest.py src/noyra/core/integrity.py src/noyra/core/operator_controls.py src/noyra/autonomy src/noyra/interaction/projection.py tests/test_service.py tests/test_m42_p1_02_integrity_runtime.py tests/test_m42_p2_14_at_rest.py tests/test_m42_p3_06_operator_controls.py
if ($LASTEXITCODE -ne 0) { throw 'Service format audit failed.' }
& $python -m mypy src tests
if ($LASTEXITCODE -ne 0) { throw 'Service type audit failed.' }

$compose = Get-Content -Raw -Encoding UTF8 (Join-Path $env:NOYRA_PROJECT_ROOT 'docker-compose.yml')
$dockerfile = Get-Content -Raw -Encoding UTF8 (Join-Path $env:NOYRA_PROJECT_ROOT 'Dockerfile')
$installer = Get-Content -Raw -Encoding UTF8 (Join-Path $env:NOYRA_PROJECT_ROOT 'scripts\install-ubuntu.sh')
$unit = Get-Content -Raw -Encoding UTF8 (Join-Path $env:NOYRA_PROJECT_ROOT 'deploy\systemd\noyra.service')
$deployEnv = Get-Content -Raw -Encoding UTF8 (Join-Path $env:NOYRA_PROJECT_ROOT 'deploy\noyra.env.example')
if ($compose -notmatch '127\.0\.0\.1:8765:8765' -or $compose -notmatch 'read_only: true') {
    throw 'Docker Compose must keep the dashboard on loopback with a read-only root filesystem.'
}
if ($unit -notmatch 'NoNewPrivileges=true' -or $unit -notmatch 'ReadWritePaths=/var/lib/noyra') {
    throw 'Systemd hardening or persistent data permissions are missing.'
}
if ($dockerfile -notmatch 'NOYRA_INSTALL_PROFILE=base' -or $dockerfile -notmatch 'requirements-cloud\.lock') {
    throw 'Docker base/cloud installation profiles are not locked.'
}
if ($installer -notmatch 'base\|cloud' -or $installer -notmatch 'requirements-cloud\.lock') {
    throw 'Ubuntu base/cloud installation profiles are not locked.'
}
if ($installer -notmatch 'backup-key init' -or $installer -notmatch 'chmod 0640') {
    throw 'Ubuntu installer does not provision the external backup keyring safely.'
}
if ($unit -notmatch 'UMask=0077' -or $unit -notmatch 'ReadOnlyPaths=/etc/noyra/backup-keyring\.json') {
    throw 'Systemd at-rest file permissions are incomplete.'
}
if ($deployEnv -notmatch 'NOYRA_AT_REST_MODE=required' -or $deployEnv -notmatch 'NOYRA_BACKUP_KEYRING_PATH=') {
    throw 'Production at-rest enforcement is missing from the deployment environment.'
}
if ($compose -notmatch 'NOYRA_VOLUME_ENCRYPTION_BACKEND: attestation' -or $compose -notmatch 'volume-attestation\.json:ro') {
    throw 'Docker Compose must require a read-only host-volume encryption attestation.'
}
if (-not (Test-Path -LiteralPath (Join-Path $env:NOYRA_PROJECT_ROOT 'requirements-cloud.lock'))) {
    throw 'Cloud dependency lock is missing.'
}

$temporaryEnv = $false
try {
    $envFile = Join-Path $env:NOYRA_PROJECT_ROOT '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        Copy-Item -LiteralPath (Join-Path $env:NOYRA_PROJECT_ROOT '.env.example') -Destination $envFile
        $temporaryEnv = $true
    }
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        docker compose --project-name noyra config --quiet
        if ($LASTEXITCODE -ne 0) { throw 'Docker Compose configuration is invalid.' }
    }
}
finally {
    if ($temporaryEnv) {
        Remove-Item -LiteralPath $envFile -Force
    }
}

Write-Host 'Ubuntu service and deployment audit passed.'
