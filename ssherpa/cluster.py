"""클러스터 설치·제거.

핵심 경계가 하나 있다. 이 모듈은 **노드 목록**만 받아서 동작한다.
그 노드가 타겟 호스트 자신인지(host 모드), 호스트 위에 만든 VM 인지
(vm 모드) 알지 못한다. 덕분에 vm 모드가 추가돼도 이 파일은 바뀌지 않는다.
"""

import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .distro import Distro, Step
from .ssh import CommandResult, Target, run

API_PORT = 6443
READY_TIMEOUT = 300  # 노드가 Ready 가 될 때까지 기다리는 최대 시간(초)
READY_POLL_INTERVAL = 5


class ClusterError(Exception):
    """설치·제거 중 발생한 오류. hints 는 해결 방법."""

    def __init__(self, message: str, hints: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.hints = hints or []


@dataclass
class Node:
    """클러스터를 구성하는 노드 하나.

    host 모드에서는 타겟 호스트 자신이고, vm 모드에서는 그 위에 만든 VM 이다.
    """

    name: str
    target: Target
    role: str = "server"  # server | agent


def nodes_for_host_mode(target: Target) -> list[Node]:
    """호스트 자신을 유일한 노드로 삼는다."""
    return [Node(name=target.name or target.host, target=target, role="server")]


class NullReporter:
    """진행 표시가 필요 없을 때(테스트 등) 쓰는 기본 구현."""

    def step(self, label: str):  # noqa: ARG002
        from contextlib import nullcontext

        return nullcontext()


def _run_step(node: Node, step: Step) -> CommandResult:
    result = run(node.target, step.command, timeout=step.timeout)
    if result.rc != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ClusterError(
            f"step failed: {step.label}",
            detail[-3:] if detail else ["No output from the remote command."],
        )
    return result


def is_installed(node: Node, distro: Distro) -> bool:
    result = run(node.target, f"test -x {distro.installed_marker}")
    return result.rc == 0


def total_memory_mb(node: Node) -> Optional[int]:
    """호스트의 전체 메모리(MB). 읽지 못하면 None."""
    result = run(node.target, "grep MemTotal /proc/meminfo")
    if result.rc != 0:
        return None
    parts = result.stdout.split()
    # MemTotal:  2035224 kB
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1]) // 1024
    return None


def check_memory(node: Node, distro: Distro) -> None:
    """메모리가 모자라면 설치 전에 막는다.

    부족한 채로 설치하면 컨트롤 플레인은 뜨지만 CNI 설치 파드가 스케줄되지
    못해 노드가 영영 NotReady 로 남는다. 그 상태를 몇 분 기다렸다가 알려주는
    것보다, 시작 전에 진짜 원인을 말해주는 편이 낫다.
    """
    total = total_memory_mb(node)
    if total is None or total >= distro.min_memory_mb:
        return

    alternatives = [
        other
        for other in ("k3s",)
        if other != distro.name and total >= 900
    ]
    hints = [
        f"This host has {total / 1024:.1f} GB; "
        f"{distro.name} needs about {distro.min_memory_mb / 1024:.1f} GB.",
        "Resize the host to continue.",
    ]
    if alternatives:
        hints += [
            "",
            "Or use a lighter distribution that fits:",
            f"    ssherpa up {node.target.name or ''} --distro {alternatives[0]}".replace(
                "  ", " "
            ),
        ]

    raise ClusterError(f"not enough memory for {distro.name}", hints)


def wait_for_ready(node: Node, distro: Distro) -> None:
    """노드가 Ready 상태가 될 때까지 기다린다."""
    deadline = time.monotonic() + READY_TIMEOUT
    last = ""

    while time.monotonic() < deadline:
        result = run(node.target, distro.node_status_command(), timeout=60)
        for line in result.stdout.splitlines():
            fields = line.split()
            # NAME STATUS ROLES AGE VERSION  —  'NotReady' 와 구분해야 한다
            if len(fields) >= 2:
                last = fields[1]
                if last == "Ready":
                    return
        time.sleep(READY_POLL_INTERVAL)

    raise ClusterError(
        f"node did not become ready within {READY_TIMEOUT}s"
        + (f" (last status: {last})" if last else ""),
        [
            "The install finished but the node never reported Ready.",
            f"Inspect the service on the host:  ssherpa ssh {node.target.name or ''}".rstrip(),
            f"    sudo journalctl -u {distro.name}-server -n 50",
        ],
    )


def parse_san_list(text: str) -> list[str]:
    """openssl 의 subjectAltName 출력에서 주소만 뽑는다.

    입력 형태:
        X509v3 Subject Alternative Name:
            DNS:kubernetes, DNS:localhost, IP Address:34.50.34.61, ...
    """
    values: list[str] = []
    for line in text.splitlines():
        for entry in line.split(","):
            entry = entry.strip()
            for prefix in ("DNS:", "IP Address:", "IP:"):
                if entry.startswith(prefix):
                    values.append(entry[len(prefix):].strip())
                    break
    return values


def certificate_covers(node: Node, distro: Distro, address: str) -> Optional[bool]:
    """serving 인증서가 이 주소를 포함하는지. 확인할 수 없으면 None.

    kubectl 은 접속한 주소가 인증서 SAN 에 없으면 거부한다. 클라우드에서
    중지/재시작으로 IP 가 바뀌면 인증서는 옛 주소로 남으므로, 여기서
    걸러내지 않으면 '성공했는데 못 쓰는' kubeconfig 를 내주게 된다.
    """
    result = run(node.target, distro.read_san_command(), timeout=60)
    if result.rc != 0 or "DNS:" not in result.stdout:
        return None  # openssl 이 없거나 인증서를 못 읽음 — 판단 불가
    return address in parse_san_list(result.stdout)


def kubeconfig_dir() -> Path:
    return Path.home() / ".ssherpa" / "kubeconfig"


def kubeconfig_path(name: str) -> Path:
    return kubeconfig_dir() / f"{name}.yaml"


def rewrite_kubeconfig(text: str, api_address: str) -> str:
    """kubeconfig 의 접속 주소를 실제 주소로 바꾼다.

    원격 kubeconfig 는 127.0.0.1 을 가리킨다. 서버 자신에게는 맞는 주소지만
    내 PC 에서 쓰려면 밖에서 닿는 주소로 바꿔야 한다.
    """
    return re.sub(
        r"server:\s*https://\S+",
        f"server: https://{api_address}:{API_PORT}",
        text,
    )


def fetch_kubeconfig(node: Node, distro: Distro, api_address: str) -> Path:
    """원격 kubeconfig 를 가져와 접속 주소를 바꿔 로컬에 저장한다."""
    result = run(node.target, distro.read_kubeconfig_command(), timeout=60)
    if result.rc != 0 or not result.stdout.strip():
        raise ClusterError(
            "could not read the kubeconfig from the host",
            [f"Expected it at {distro.kubeconfig_path}"],
        )

    rewritten = rewrite_kubeconfig(result.stdout, api_address)
    path = kubeconfig_path(node.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rewritten, encoding="utf-8")
    return path


def api_reachable(host: str, timeout: float = 5.0) -> bool:
    """내 PC 에서 쿠버네티스 API 포트에 닿는지 확인한다."""
    try:
        with socket.create_connection((host, API_PORT), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class UpResult:
    kubeconfig: Path
    api_address: str
    api_reachable: bool
    already_installed: bool
    certificate_refreshed: bool = False


def up(node: Node, distro: Distro, reporter=None) -> UpResult:
    """노드 하나에 쿠버네티스를 설치하고 kubeconfig 를 가져온다."""
    reporter = reporter or NullReporter()
    api_address = node.target.host

    with reporter.step("preflight"):
        already = is_installed(node, distro)
        if not already:
            check_memory(node, distro)

    if not already:
        for step in distro.install_steps(api_address):
            with reporter.step(step.label):
                _run_step(node, step)

    with reporter.step("wait for node"):
        wait_for_ready(node, distro)

    # 인증서가 지금 접속 주소를 포함하는지 확인한다. 클라우드에서 중지/재시작
    # 으로 IP 가 바뀐 뒤 재실행하면, kubeconfig 만 갱신되고 인증서는 옛 주소로
    # 남아 kubectl 이 전부 거부된다. 설정을 다시 쓰고 재시작하면 재발급된다.
    refreshed = False
    with reporter.step("verify certificate"):
        covered = certificate_covers(node, distro, api_address)

    if covered is False:
        with reporter.step("refresh certificate"):
            _run_step(node, distro.refresh_certificate_step(api_address))
            refreshed = True
        with reporter.step("wait for node"):
            wait_for_ready(node, distro)
        with reporter.step("verify certificate"):
            if certificate_covers(node, distro, api_address) is False:
                raise ClusterError(
                    f"the certificate still does not cover {api_address}",
                    [
                        "The refresh did not take. Reinstalling will issue a "
                        "fresh certificate:",
                        f"    ssherpa down {node.target.name or ''}".rstrip(),
                        f"    ssherpa up {node.target.name or ''}".rstrip(),
                    ],
                )

    with reporter.step("fetch kubeconfig"):
        path = fetch_kubeconfig(node, distro, api_address)

    with reporter.step("verify api access"):
        reachable = api_reachable(api_address)

    return UpResult(
        kubeconfig=path,
        api_address=api_address,
        api_reachable=reachable,
        already_installed=already,
        certificate_refreshed=refreshed,
    )


def down(node: Node, distro: Distro, reporter=None) -> bool:
    """쿠버네티스를 제거한다. 설치돼 있지 않았으면 False 를 돌려준다."""
    reporter = reporter or NullReporter()

    with reporter.step("preflight"):
        installed = is_installed(node, distro)

    if not installed:
        return False

    for step in distro.uninstall_steps():
        with reporter.step(step.label):
            _run_step(node, step)

    # 제거 스크립트가 종료 코드 0 을 주고도 실제로는 안 지우는 경우가 있다.
    # 실측: rke2 가 남아 6443 을 계속 점유하는 바람에 다음 설치가 실패했다.
    with reporter.step("verify removal"):
        if is_installed(node, distro):
            raise ClusterError(
                f"{distro.name} is still installed after uninstall",
                [
                    "The uninstall script reported success but left the binary behind.",
                    f"Inspect the host:  ssherpa ssh {node.target.name or ''}".rstrip(),
                    f"    sudo systemctl status {distro.service}",
                ],
            )

    # 로컬 kubeconfig 도 같이 치운다. 남겨두면 죽은 클러스터를 가리킨다.
    path = kubeconfig_path(node.name)
    if path.exists():
        path.unlink()

    return True


@dataclass
class DistroStatus:
    name: str
    installed: bool
    service_state: str  # active / inactive / activating / failed / ""

    @property
    def running(self) -> bool:
        return self.service_state == "active"


@dataclass
class HostStatus:
    distros: list[DistroStatus]
    node_line: Optional[str] = None  # kubectl get nodes 한 줄

    @property
    def installed(self) -> list[DistroStatus]:
        return [d for d in self.distros if d.installed]

    @property
    def running(self) -> list[DistroStatus]:
        return [d for d in self.distros if d.running]

    @property
    def conflicted(self) -> bool:
        """두 배포판이 함께 설치돼 있으면 6443 을 두고 충돌한다."""
        return len(self.installed) > 1


def status(node: Node, distros: list[Distro]) -> HostStatus:
    """호스트에 무엇이 설치돼 있고 무엇이 돌고 있는지 조사한다."""
    probe = "; ".join(d.status_command() for d in distros)
    result = run(node.target, probe, timeout=60)

    states: dict[str, DistroStatus] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        # SSHERPA_D <name> <yes|no> <service-state>
        if len(fields) >= 3 and fields[0] == "SSHERPA_D":
            states[fields[1]] = DistroStatus(
                name=fields[1],
                installed=fields[2] == "yes",
                service_state=fields[3] if len(fields) >= 4 else "",
            )

    ordered = [
        states.get(d.name, DistroStatus(d.name, installed=False, service_state=""))
        for d in distros
    ]
    host = HostStatus(distros=ordered)

    # 돌고 있는 배포판이 있으면 노드 상태까지 확인한다.
    for distro in distros:
        state = states.get(distro.name)
        if state and state.running:
            nodes = run(node.target, distro.node_status_command(), timeout=60)
            line = nodes.stdout.strip().splitlines()
            if line:
                host.node_line = line[0]
            break

    return host
