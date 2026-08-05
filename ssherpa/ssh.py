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
    """접속 대상. user/port/key 는 '지정 안 함'(None) 이 유효한 상태다.

    지정하지 않은 값은 ssh 에 아예 넘기지 않는다 — 그래야 ssh 가
    ~/.ssh/config 의 User/Port/IdentityFile/ProxyJump 를 존중한다.
    우리가 기본값 22 를 강제로 넘기면 config 의 Port 2222 를 덮어써서
    'ssh 로는 되는데 SSHerpa 로는 안 되는' 미스터리를 만든다.
    """

    name: Optional[str]
    host: str
    user: Optional[str] = None
    port: Optional[int] = None
    key: Optional[str] = None

    def destination(self) -> str:
        """ssh 에 넘길 목적지. user 미지정이면 host 만 (config 가 정함)."""
        return f"{self.user}@{self.host}" if self.user else self.host

    def endpoint(self) -> str:
        """표시용 주소."""
        end = self.destination()
        return f"{end}:{self.port}" if self.port else end


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
    ]
    # 명시된 것만 넘긴다. 안 넘긴 항목은 ssh 가 ~/.ssh/config 에서 찾는다.
    if target.port:
        argv += ["-p", str(target.port)]
    if target.key:
        argv += ["-i", os.path.expanduser(target.key)]
    argv += [target.destination(), remote_command]
    return argv


def _find_default_keys() -> list[str]:
    """--key 없이 실행됐을 때 ssh 가 실제로 시도했을 키 목록."""
    ssh_dir = Path.home() / ".ssh"
    return [str(ssh_dir / name) for name in DEFAULT_KEY_NAMES if (ssh_dir / name).exists()]


def _auth_hints(target: Target) -> list[str]:
    """인증 실패는 원인이 여러 가지라, 상황에 맞는 안내만 골라서 준다."""
    user_label = target.user or "the login user"
    install = (
        "Install the matching public key on the target "
        f"(append it to ~/.ssh/authorized_keys for '{user_label}')."
    )

    if target.key:
        hints = [
            f"The key you specified was rejected:  {target.key}",
            install,
        ]
        if target.user:
            hints.append(f"Also verify the username '{target.user}' is correct.")
        return hints

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


def _permission_fix_hints(key_path: str, windows: Optional[bool] = None) -> list[str]:
    """개인키 권한 오류의 처방.

    같은 규칙('개인키는 나만')이지만 권한 체계가 달라 명령이 다르다.
    POSIX 는 모드 비트(chmod 600), Windows 는 ACL(icacls). Windows 사용자에게
    chmod 를 안내하면 Git Bash 에선 성공한 것처럼 보이면서 ACL 은 그대로라
    같은 오류가 반복된다 — 처방은 ssh 를 실행하는 이쪽 OS 를 따라야 한다.
    """
    if windows is None:
        windows = os.name == "nt"

    if windows:
        # %USERNAME% 은 cmd 전용이고 PowerShell 에선 확장되지 않는다.
        # 셸에 상관없이 붙여넣어 동작하도록 실제 사용자 이름을 채워 넣는다.
        #
        # /reset 이 먼저다. 흔히 도는 한 줄짜리(/inheritance:r + /grant:r)는
        # '상속된' 넓은 권한만 지운다 — Everyone 이 명시적으로 부여된 파일에선
        # 실측 결과 그대로 남았다. /reset 은 명시 항목까지 걷어낸다.
        user = os.environ.get("USERNAME") or "<your-username>"
        return [
            "Windows refuses private keys that other accounts can access.",
            "Restrict the file to yourself and try again:",
            "",
            f'    icacls "{key_path}" /reset',
            f'    icacls "{key_path}" /inheritance:r /grant:r "{user}:F"',
        ]

    return [
        "macOS/Linux refuse private keys that others can read.",
        "Restrict the file and try again:",
        "",
        f"    chmod 600 {key_path}",
    ]


def _classify(stderr: str, target: Target) -> SSHError:
    """ssh 의 stderr 를 보고 사람이 읽을 수 있는 오류로 변환한다."""
    lower = stderr.lower()
    ep = f"{target.host}:{target.port}" if target.port else target.host
    port_note = (
        f"currently {target.port}"
        if target.port
        else "ssh default 22, or Port in your ~/.ssh/config"
    )

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
                f"Check the port number ({port_note})",
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
    # 'bad permissions' 는 Windows OpenSSH 전용 문구다:
    #   "Bad permissions. Try removing permissions for user: ..."
    if (
        "are too open" in lower
        or "unprotected private key" in lower
        or "bad permissions" in lower
    ):
        key = target.key or "<your private key>"
        return SSHError(
            "private key ignored: file permissions are too open",
            _permission_fix_hints(key),
        )

    if "permission denied" in lower:
        return SSHError(
            f"authentication failed ({target.destination()})",
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
                f"If it keeps happening, check the host:  ssh {target.destination()}",
            ],
        ) from exc

    if proc.returncode == SSH_FAILURE_RC:
        raise _classify(proc.stderr, target)

    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
