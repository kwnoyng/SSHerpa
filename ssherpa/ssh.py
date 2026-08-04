"""SSH 연결 및 원격 명령 실행.

paramiko 대신 시스템의 OpenSSH 클라이언트를 subprocess 로 호출한다.
  - 사용자의 ~/.ssh/config, ssh-agent 를 그대로 존중한다
  - Windows 11 은 OpenSSH 클라이언트를 기본 포함한다
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --key 를 주지 않았을 때 ssh 가 자동으로 시도하는 키들
DEFAULT_KEY_NAMES = ("id_ed25519", "id_ecdsa", "id_rsa")

CONNECT_TIMEOUT = 10  # ssh 자체 연결 타임아웃(초)
COMMAND_TIMEOUT = 30  # 원격 명령 전체 타임아웃(초)

# ssh 바이너리 자체가 실패했을 때의 종료 코드.
# 이 값이면 원격 명령이 아니라 연결 단계에서 실패한 것이다.
SSH_FAILURE_RC = 255


@dataclass
class Target:
    name: Optional[str]
    host: str
    user: str
    port: int = 22
    key: Optional[str] = None

    def endpoint(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


@dataclass
class CommandResult:
    rc: int
    stdout: str
    stderr: str


class SSHError(Exception):
    """연결 자체가 실패했을 때. message 는 요약, hints 는 해결 방법."""

    def __init__(self, message: str, hints: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.hints = hints or []


def _build_command(target: Target, remote_command: str) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",              # 비밀번호 프롬프트 금지 (자동화용)
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-p", str(target.port),
    ]
    if target.key:
        argv += ["-i", os.path.expanduser(target.key)]
    argv += [f"{target.user}@{target.host}", remote_command]
    return argv


def _find_default_keys() -> list[str]:
    """--key 없이 실행됐을 때 ssh 가 실제로 시도했을 키 목록."""
    ssh_dir = Path.home() / ".ssh"
    return [str(ssh_dir / name) for name in DEFAULT_KEY_NAMES if (ssh_dir / name).exists()]


def _auth_hints(target: Target) -> list[str]:
    """인증 실패는 원인이 여러 가지라, 상황에 맞는 안내만 골라서 준다."""
    install = (
        "Install the matching public key on the target "
        f"(append it to ~/.ssh/authorized_keys for '{target.user}')."
    )

    if target.key:
        return [
            f"The key you specified was rejected:  {target.key}",
            install,
            f"Also verify the username '{target.user}' is correct.",
        ]

    defaults = _find_default_keys()
    if defaults:
        return [
            "No --key was given, so these default keys were tried and rejected:",
            *[f"    {path}" for path in defaults],
            "Pass --key to use a different key, or:",
            f"    {install}",
        ]

    return [
        "No --key was given, and no default key exists in ~/.ssh",
        "",
        "Either point SSHerpa at an existing key:",
        "    ssherpa check ... --key <path-to-private-key>",
        "",
        "or create one and install it on the target:",
        "    ssh-keygen -t ed25519",
    ]


def _classify(stderr: str, target: Target) -> SSHError:
    """ssh 의 stderr 를 보고 사람이 읽을 수 있는 오류로 변환한다."""
    lower = stderr.lower()
    ep = f"{target.host}:{target.port}"

    if "could not resolve hostname" in lower:
        return SSHError(
            f"could not resolve hostname ({target.host})",
            ["Check the address for typos", "Try specifying the IP address directly"],
        )

    if "connection timed out" in lower or "operation timed out" in lower:
        return SSHError(
            f"connection timed out ({CONNECT_TIMEOUT}s)",
            [
                f"Is {ep} powered on with the port open?",
                "Is a firewall (firewalld / ufw) blocking it?",
            ],
        )

    if "connection refused" in lower:
        return SSHError(
            f"connection refused ({ep})",
            [
                "Check that sshd is running on the target host",
                f"Check the port number (currently {target.port})",
            ],
        )

    if "no route to host" in lower or "network is unreachable" in lower:
        return SSHError(
            f"network unreachable ({target.host})",
            ["Check that you are on the same network and routing works"],
        )

    if "host key verification failed" in lower or "identification has changed" in lower:
        return SSHError(
            "host key verification failed",
            [
                "If you reinstalled the target, remove the old key:",
                f"    ssh-keygen -R {target.host}",
                "If this change was unexpected, it could be a man-in-the-middle attack",
            ],
        )

    # 권한 문제는 permission denied 보다 먼저 본다. ssh 는 키를 무시한 뒤
    # 결국 'Permission denied' 도 같이 뱉기 때문에, 순서가 바뀌면
    # 원인(파일 권한)이 아니라 증상(인증 실패)만 안내하게 된다.
    if "are too open" in lower or "unprotected private key" in lower:
        key = target.key or "<your private key>"
        return SSHError(
            "private key ignored: file permissions are too open",
            [
                "macOS/Linux refuse private keys that others can read.",
                "Restrict the file and try again:",
                "",
                f"    chmod 600 {key}",
            ],
        )

    if "permission denied" in lower:
        return SSHError(
            f"authentication failed ({target.user}@{target.host})",
            _auth_hints(target),
        )

    detail = stderr.strip().splitlines()
    return SSHError(
        "SSH connection failed",
        detail[:3] if detail else ["Could not determine the cause"],
    )


def run(
    target: Target,
    remote_command: str,
    timeout: int = COMMAND_TIMEOUT,
) -> CommandResult:
    """원격 명령을 실행한다.

    연결 자체가 실패하면 SSHError 를 던지고,
    연결은 됐지만 명령이 실패한 경우는 CommandResult 로 돌려준다.

    timeout 은 명령마다 다르다. 상태 조회는 몇 초면 끝나지만 쿠버네티스
    설치는 이미지 내려받기까지 포함해 수 분이 걸린다.
    """
    if shutil.which("ssh") is None:
        raise SSHError(
            "ssh client not found",
            [
                "Windows: install 'OpenSSH Client' from Settings > System > Optional features",
                "Linux/macOS: install the openssh-client package",
            ],
        )

    try:
        proc = subprocess.run(
            _build_command(target, remote_command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SSHError(
            f"no response (timed out after {timeout}s)",
            [
                # 무거운 작업(설치·제거) 직후에는 호스트가 잠시 느려질 수 있다.
                "The host accepted the connection but did not finish in time.",
                "This is often temporary — a busy host right after an install or",
                "uninstall. Try the command again.",
                "",
                f"If it keeps happening, check the host:  ssh {target.user}@{target.host}",
            ],
        ) from exc

    if proc.returncode == SSH_FAILURE_RC:
        raise _classify(proc.stderr, target)

    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
