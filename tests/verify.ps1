# Verify the SSHerpa v0.1 acceptance criteria.
#
#   .\tests\verify.ps1          bring up containers and run checks
#   .\tests\verify.ps1 -Down    tear down containers
#
# Requires Docker Desktop to be running.

param([switch]$Down)

# docker writes progress to stderr. Treating that as 'Stop' in PowerShell 5.1
# kills the script during perfectly normal operation, so use Continue and
# decide success/failure from $LASTEXITCODE only.
$ErrorActionPreference = "Continue"

$Root       = Split-Path -Parent $PSScriptRoot
$DockerDir  = Join-Path $PSScriptRoot "docker"
$Compose    = Join-Path $DockerDir "docker-compose.yml"
$KeyPath    = Join-Path $DockerDir "id_test"
$Ssherpa    = Join-Path $Root ".venv\Scripts\ssherpa.exe"

if ($Down) {
    docker compose -f $Compose down -v
    exit 0
}

if (-not (Test-Path $Ssherpa)) {
    Write-Host "ssherpa is not installed. Run this first:" -ForegroundColor Red
    Write-Host "  py -3 -m venv .venv"
    Write-Host "  .venv\Scripts\python.exe -m pip install -e ."
    exit 1
}

# --- 1. Test-only SSH key (generated on first run) -------------------------
if (-not (Test-Path $KeyPath)) {
    Write-Host "Generating test SSH key..." -ForegroundColor Cyan
    ssh-keygen -t ed25519 -f $KeyPath -N '""' -C "ssherpa-test" | Out-Null
}

# --- 2. Start containers ---------------------------------------------------
Write-Host "Building and starting test containers... (first run takes a few minutes)" -ForegroundColor Cyan
docker compose --progress quiet -f $Compose up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to start containers. Is Docker Desktop running?" -ForegroundColor Red
    exit 1
}

# --- 3. Cases --------------------------------------------------------------
# ExpectExit: expected exit code from 'ssherpa check' (0 = pass, 1 = must fail)
$Cases = @(
    @{ Name = "Ubuntu 22.04 (healthy)";  Port = 2205; User = "admin";  ExpectExit = 0; Expect = "family: Debian, passes" }
    @{ Name = "Ubuntu 24.04 (healthy)";  Port = 2201; User = "admin";  ExpectExit = 0; Expect = "family: Debian, passes" }
    @{ Name = "Rocky 9 (healthy)";       Port = 2202; User = "admin";  ExpectExit = 0; Expect = "family: RedHat, passes" }
    @{ Name = "AlmaLinux 9 (healthy)";   Port = 2206; User = "admin";  ExpectExit = 0; Expect = "family: RedHat, passes" }
    @{ Name = "Ubuntu 24.04 (no sudo)";  Port = 2203; User = "nosudo"; ExpectExit = 1; Expect = "fails at the sudo check" }
    @{ Name = "Ubuntu 20.04 (too old)";  Port = 2204; User = "admin";  ExpectExit = 1; Expect = "fails at the support check" }
)

Write-Host "Waiting for sshd..." -ForegroundColor Cyan
foreach ($case in $Cases) {
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $client = New-Object Net.Sockets.TcpClient
            $client.Connect("127.0.0.1", $case.Port)
            $client.Close()
            $ready = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) {
        Write-Host "Port $($case.Port) never opened." -ForegroundColor Red
        exit 1
    }
    # Rebuilding a container changes its host key, so drop any stale entry.
    ssh-keygen -R "[127.0.0.1]:$($case.Port)" 2>$null | Out-Null
}

# --- 4. Run and judge ------------------------------------------------------
$failed = 0
foreach ($case in $Cases) {
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor DarkGray
    Write-Host "$($case.Name)  --  expected: $($case.Expect)" -ForegroundColor Yellow
    Write-Host ("=" * 70) -ForegroundColor DarkGray

    & $Ssherpa check --host 127.0.0.1 --port $case.Port --user $case.User --key $KeyPath
    $actual = $LASTEXITCODE

    if ($actual -eq $case.ExpectExit) {
        Write-Host "  [PASS] exit code $actual" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] exit code $actual (expected $($case.ExpectExit))" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host ("=" * 70) -ForegroundColor DarkGray
if ($failed -eq 0) {
    Write-Host "All passed ($($Cases.Count)/$($Cases.Count))" -ForegroundColor Green
} else {
    Write-Host "Failed $failed of $($Cases.Count)" -ForegroundColor Red
}
Write-Host ""
Write-Host "To tear down:  .\tests\verify.ps1 -Down" -ForegroundColor DarkGray

exit $failed
