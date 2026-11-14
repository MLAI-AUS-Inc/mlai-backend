# Only the approved disposable-local inventory; never use an existing database.
$ErrorActionPreference='Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
$phaseNetworkCreated=$false
$phaseContainerCreated=$false
$phaseTestExit=1
try {
 if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw 'Local Python 3.11 environment is required.' }
 docker network inspect cybertest-phase-local-20261112 *> $null
 if ($LASTEXITCODE -eq 0) { throw 'Test network already exists; inspect before proceeding.' }
 docker container inspect cybertest-phase-postgres-20261112 *> $null
 if ($LASTEXITCODE -eq 0) { throw 'Test container already exists; inspect before proceeding.' }
 docker network create --driver bridge --opt com.docker.network.bridge.enable_ip_masquerade=false cybertest-phase-local-20261112
 if ($LASTEXITCODE -ne 0) { throw 'Cannot create dedicated local test network.' }
 $phaseNetworkCreated=$true
 docker run --detach --name cybertest-phase-postgres-20261112 --network cybertest-phase-local-20261112 --publish 127.0.0.1:55473:5432 --env POSTGRES_USER=cybertest_phase_test --env POSTGRES_DB=cybertest_phase_seed --env POSTGRES_HOST_AUTH_METHOD=trust --tmpfs /var/lib/postgresql/data:rw,size=768m --label cybertest-purpose=phase-concurrency pgvector/pgvector:pg16
 if ($LASTEXITCODE -ne 0) { throw 'Cannot create disposable PostgreSQL.' }
 $phaseContainerCreated=$true
 $phaseReady=$false
 for ($phaseAttempt=0; $phaseAttempt -lt 30; $phaseAttempt++) {
  docker exec cybertest-phase-postgres-20261112 pg_isready -U cybertest_phase_test -d cybertest_phase_seed *> $null
  if ($LASTEXITCODE -eq 0) { $phaseReady=$true; break }
  Start-Sleep -Seconds 1
 }
 if (-not $phaseReady) { throw 'Disposable PostgreSQL did not become ready.' }
 $phasePort=docker port cybertest-phase-postgres-20261112 5432/tcp
 if ($phasePort -ne '127.0.0.1:55473') { throw 'Expected loopback-only PostgreSQL port is absent.' }
 .venv/Scripts/python.exe scripts/test_cybertest_phase_postgres.py > two-phase-postgres-concurrency.log 2>&1
 $phaseTestExit=$LASTEXITCODE
 Get-Content two-phase-postgres-concurrency.log -Tail 35
} finally {
 if ($phaseContainerCreated) {
  docker rm --force cybertest-phase-postgres-20261112
  if ($LASTEXITCODE -ne 0) { $phaseTestExit=1 }
 }
 if ($phaseNetworkCreated) {
  docker network rm cybertest-phase-local-20261112
  if ($LASTEXITCODE -ne 0) { $phaseTestExit=1 }
 }
 Pop-Location
}
exit $phaseTestExit
