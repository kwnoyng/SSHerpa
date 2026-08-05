"""원격 호스트 검사(probe) 로직.

check 명령이 SSH 접속 한 번으로 uid / sudo / os-release 를 모두 가져오고,
그 출력을 해석하는 부분이다. 화면 출력과 무관한 순수 판정 로직이라
cli.py 가 아니라 여기 산다.
"""

# 한 번의 접속으로 uid / sudo / os-release 를 모두 가져온다.
# 검사마다 접속하면 핸드셰이크가 반복돼 느려진다.
PROBE = (
    'echo "SSHERPA_UID=$(id -u)"; '
    'echo "SSHERPA_SUDO"; '
    "sudo -n true 2>&1; "
    'echo "SSHERPA_SUDO_RC=$?"; '
    'echo "SSHERPA_OSRELEASE"; '
    "cat /etc/os-release 2>/dev/null"
)


def split_probe(stdout: str) -> tuple[str, str, str]:
    """PROBE 출력을 (uid, sudo구간, os-release) 로 나눈다."""
    uid = ""
    sudo_block: list[str] = []
    osrelease: list[str] = []
    section = "head"

    for line in stdout.splitlines():
        if line.startswith("SSHERPA_UID="):
            uid = line.split("=", 1)[1].strip()
        elif line == "SSHERPA_SUDO":
            section = "sudo"
        elif line == "SSHERPA_OSRELEASE":
            section = "os"
        elif section == "sudo":
            sudo_block.append(line)
        elif section == "os":
            osrelease.append(line)

    return uid, "\n".join(sudo_block), "\n".join(osrelease)


def judge_sudo(uid: str, sudo_block: str) -> tuple[bool, str, list[str]]:
    """(통과여부, 표시문구, 힌트) 를 돌려준다."""
    if uid == "0":
        return True, "root account", []

    rc = None
    message_lines = []
    for line in sudo_block.splitlines():
        if line.startswith("SSHERPA_SUDO_RC="):
            rc = line.split("=", 1)[1].strip()
        else:
            message_lines.append(line)
    message = "\n".join(message_lines).lower()

    if rc == "0":
        return True, "NOPASSWD", []

    if "command not found" in message or rc == "127":
        return False, "sudo is not installed", [
            "Install sudo on the target host, or use the root account",
        ]

    if "password is required" in message:
        return False, "password required", []

    if "not allowed" in message or "not in the sudoers" in message:
        return False, "not in sudoers", []

    return False, "sudo is unavailable", (
        [line for line in message_lines if line.strip()][:2]
    )


def sudo_fix_hint(user: str) -> list[str]:
    return [
        f"SSHerpa needs passwordless sudo for '{user}'.",
        "Run this on the target host:",
        "",
        f"    echo '{user} ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ssherpa",
    ]
