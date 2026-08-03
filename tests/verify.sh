#!/usr/bin/env bash
# Verify the SSHerpa v0.1 acceptance criteria (macOS / Linux).
#
#   ./tests/verify.sh          bring up containers and run checks
#   ./tests/verify.sh down     tear down containers
#
# Requires Docker and a virtualenv at .venv (python -m venv .venv && .venv/bin/pip install -e .)
#
# This is the POSIX counterpart of verify.ps1 — same containers, same cases.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
COMPOSE="$HERE/docker/docker-compose.yml"
KEY="$HERE/docker/id_test"
# POSIX venv puts scripts in bin/; on Windows (Git Bash, WSL) it is Scripts/.
SSHERPA="$ROOT/.venv/bin/ssherpa"
[ -x "$SSHERPA" ] || SSHERPA="$ROOT/.venv/Scripts/ssherpa.exe"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'

if [ "${1:-}" = "down" ]; then
    docker compose -f "$COMPOSE" down -v
    exit 0
fi

if [ ! -x "$SSHERPA" ]; then
    echo "${RED}ssherpa is not installed. Run this first:${OFF}"
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/pip install -e ."
    exit 1
fi

# --- 1. Test-only SSH key (generated on first run) -------------------------
if [ ! -f "$KEY" ]; then
    echo "${YELLOW}Generating test SSH key...${OFF}"
    ssh-keygen -t ed25519 -f "$KEY" -N "" -C "ssherpa-test" >/dev/null
fi
# ssh refuses private keys that others can read.
chmod 600 "$KEY"

# --- 2. Start containers ---------------------------------------------------
echo "${YELLOW}Building and starting test containers... (first run takes a few minutes)${OFF}"
if ! docker compose --progress quiet -f "$COMPOSE" up -d --build; then
    echo "${RED}Failed to start containers. Is Docker running?${OFF}"
    exit 1
fi

# --- 3. Cases --------------------------------------------------------------
# name | port | user | expected exit code | what should happen
CASES=(
    "Ubuntu 22.04 (healthy)|2205|admin|0|family: Debian, passes"
    "Ubuntu 24.04 (healthy)|2201|admin|0|family: Debian, passes"
    "Rocky 9 (healthy)|2202|admin|0|family: RedHat, passes"
    "AlmaLinux 9 (healthy)|2206|admin|0|family: RedHat, passes"
    "Ubuntu 24.04 (no sudo)|2203|nosudo|1|fails at the sudo check"
    "Ubuntu 20.04 (too old)|2204|admin|1|fails at the support check"
)

echo "${YELLOW}Waiting for sshd...${OFF}"
for case in "${CASES[@]}"; do
    IFS='|' read -r name port user expect_exit expect <<< "$case"
    ready=""
    for _ in $(seq 1 30); do
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            ready=1
            break
        fi
        sleep 0.5
    done
    if [ -z "$ready" ]; then
        echo "${RED}Port $port never opened.${OFF}"
        exit 1
    fi
    # Rebuilding a container changes its host key, so drop any stale entry.
    ssh-keygen -R "[127.0.0.1]:$port" >/dev/null 2>&1
done

# --- 4. Run and judge ------------------------------------------------------
failed=0
for case in "${CASES[@]}"; do
    IFS='|' read -r name port user expect_exit expect <<< "$case"

    echo
    printf '%s%s%s\n' "$DIM" "======================================================================" "$OFF"
    echo "${YELLOW}${name}  --  expected: ${expect}${OFF}"
    printf '%s%s%s\n' "$DIM" "======================================================================" "$OFF"

    "$SSHERPA" check --host 127.0.0.1 --port "$port" --user "$user" --key "$KEY"
    actual=$?

    if [ "$actual" -eq "$expect_exit" ]; then
        echo "  ${GREEN}[PASS] exit code $actual${OFF}"
    else
        echo "  ${RED}[FAIL] exit code $actual (expected $expect_exit)${OFF}"
        failed=$((failed + 1))
    fi
done

echo
printf '%s%s%s\n' "$DIM" "======================================================================" "$OFF"
if [ "$failed" -eq 0 ]; then
    echo "${GREEN}All passed (${#CASES[@]}/${#CASES[@]})${OFF}"
else
    echo "${RED}Failed $failed of ${#CASES[@]}${OFF}"
fi
echo
echo "${DIM}To tear down:  ./tests/verify.sh down${OFF}"

exit "$failed"
