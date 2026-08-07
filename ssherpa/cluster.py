"""클러스터 설치·제거.

핵심 경계가 하나 있다. 이 모듈은 **노드 목록**만 받아서 동작한다.
그 노드가 타겟 호스트 자신인지(host 모드), 호스트 위에 만든 VM 인지
(vm 모드) 알지 못한다. 덕분에 vm 모드가 추가돼도 이 파일은 바뀌지 않는다.
"""

import contextlib
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import kubeconfig as kubeconf
from .distro import CONFIG_FOREIGN, Distro, Step
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

    cli_name/in_vm 은 오류 힌트에 넣을 명령을 짓기 위해서만 쓴다. 설치
    로직은 이 값들을 보지 않는다 — 이 모듈이 노드의 정체를 모른다는 경계는
    그대로다.
    """

    name: str
    target: Target
    role: str = "server"  # server | agent
    cli_name: Optional[str] = None  # 사용자가 등록한 타겟 이름
    in_vm: bool = False  # 이 노드가 호스트 위의 VM 인가
    short_name: Optional[str] = None  # 진행 표시에 쓸 짧은 이름

    def label(self) -> str:
        """진행 표시에 붙일 이름. 어느 노드의 일인지 보이지 않으면 같은
        단계가 여러 번 지나갈 때 전부 다시 하는 것처럼 읽힌다 (실사용 오해)."""
        return self.short_name or self.name

    def _cli(self, verb: str, *flags: str) -> str:
        parts = ["ssherpa", verb]
        if self.cli_name:
            parts.append(self.cli_name)
        parts.extend(flags)
        return " ".join(parts)

    def cli_ssh(self) -> str:
        """이 노드 안으로 들어가는 명령.

        vm 모드에서 target.name 은 'lab-01/ssherpa-node-1' 같은 합성 이름이라
        그대로 안내하면 등록되지 않은 타겟을 시키게 된다. 사용자가 실제로
        타이핑하는 이름과, VM 안을 가리키는 --vm 을 쓴다.
        """
        return self._cli("ssh", "--vm") if self.in_vm else self._cli("ssh")

    def cli_up(self, *flags: str) -> str:
        """이 노드를 다시 세우는 명령."""
        return self._cli("up", *flags, *(("--vm",) if self.in_vm else ()))

    def cli_down(self) -> str:
        """이 노드를 걷어내는 명령.

        down 은 호스트에 무엇이 있든 스스로 찾으므로 모드 플래그가 없다.
        """
        return self._cli("down")


def nodes_for_host_mode(target: Target) -> list[Node]:
    """호스트 자신을 유일한 노드로 삼는다."""
    return [
        Node(
            name=target.name or target.host,
            target=target,
            role="server",
            cli_name=target.name,
        )
    ]


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
    """이미 설치돼 있는가.

    sudo 로 묻는다. 권한이 모자라 못 읽은 것을 '없다' 로 읽으면 up 이
    도는 클러스터 위에 설치를 다시 건다.
    """
    result = run(node.target, f"sudo test -x {distro.installed_marker}")
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
            f"    {node.cli_up(f'--distro {alternatives[0]}')}",
        ]

    raise ClusterError(f"not enough memory for {distro.name}", hints)


def count_ready(status_output: str) -> int:
    """kubectl get nodes 출력에서 Ready 인 노드 수를 센다.

    STATUS 칸은 한 값이 아니라 쉼표로 이어붙인 목록이다. 노드를 cordon 하면
    'Ready,SchedulingDisabled' 가 되는데, 칸을 통째로 비교하면 멀쩡히 Ready 인
    그 노드를 세지 못한다. cordon/drain 은 이 도구를 쓰는 사람이 연습할 바로
    그 동작이라, 연습해둔 클러스터에 up 을 다시 돌리면 300초를 기다렸다가
    'only N of M nodes are Ready' 라는 틀린 진단을 받게 된다.

    그래서 첫 칸만 본다 — 'NotReady' 도 거기 오므로 정확히 비교해야 한다.
    """
    ready = 0
    for line in status_output.splitlines():
        fields = line.split()
        # NAME STATUS ROLES AGE VERSION
        if len(fields) >= 2 and fields[1].split(",")[0] == "Ready":
            ready += 1
    return ready


def wait_for_ready(node: Node, distro: Distro, expected: int = 1) -> None:
    """노드가 expected 개만큼 Ready 가 될 때까지 기다린다.

    상태는 언제나 server 노드에게 묻는다 — 클러스터 전체를 아는 것은
    거기뿐이고, agent 에는 kubeconfig 자체가 없다.
    """
    deadline = time.monotonic() + READY_TIMEOUT
    ready = 0

    while time.monotonic() < deadline:
        result = run(node.target, distro.node_status_command(), timeout=60)
        ready = count_ready(result.stdout)
        if ready >= expected:
            return
        time.sleep(READY_POLL_INTERVAL)

    detail = (
        f"only {ready} of {expected} nodes are Ready"
        if expected > 1
        else "the node never reported Ready"
    )
    raise ClusterError(
        f"cluster did not become ready within {READY_TIMEOUT}s ({detail})",
        [
            "The install finished but the cluster never reached the expected size.",
            f"Inspect the service:  {node.cli_ssh()}",
            # 유닛 이름은 배포판이 정한다 — k3s 는 'k3s', RKE2 는
            # 'rke2-server'. 이름에서 지어내면 k3s 쪽이 틀린다 (실측).
            f"    sudo journalctl -u {distro.service} -n 50",
        ],
    )


def config_is_ours(node: Node, distro: Distro) -> bool:
    """배포판 설정 파일을 우리가 덮어써도 되는가.

    tls-san 은 파일을 통째로 다시 쓰는 방식으로 넣는다. 이미 k3s 가 깔린
    호스트를 타겟으로 잡으면 그 파일에 사용자의 설정(disable, 데이터스토어
    주소, apiserver 인자 …)이 들어 있을 수 있는데, 그대로 덮어쓰면 그것들이
    사라진 채 서비스가 재시작된다 — 실측으로 꺼둔 컴포넌트가 되살아났다.
    """
    result = run(node.target, distro.config_ownership_command(), timeout=60)
    return CONFIG_FOREIGN not in result.stdout


def _foreign_config_error(distro: Distro, api_address: str) -> ClusterError:
    return ClusterError(
        f"{distro.config_path} was not written by SSHerpa",
        [
            "It holds settings SSHerpa did not put there, and the only way it",
            "knows to add an address is to rewrite the whole file — which would",
            "throw those settings away and restart the service.",
            "",
            f"Add the address yourself, so the certificate covers {api_address}:",
            "",
            "    tls-san:",
            f"      - {api_address}",
            "",
            f"then restart {distro.service} and run this again. SSHerpa will",
            "leave the file alone from then on.",
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
    # 이 파일은 클러스터 관리자 자격증명이다 — 주인만 읽을 수 있어야 한다.
    kubeconf.write_private(path, rewritten)
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
    context: Optional[str] = None  # ~/.kube/config 에 병합된 컨텍스트 이름
    context_is_current: bool = False  # current-context 로 잡혔나
    merge_error: Optional[str] = None  # 병합 실패 사유 (클러스터 자체는 정상)
    node_count: int = 1  # 클러스터를 이루는 노드 수


def read_token(node: Node, distro: Distro) -> str:
    """server 가 발급한 합류 토큰. agent 는 이것 없이 들어올 수 없다."""
    result = run(node.target, distro.read_token_command(), timeout=60)
    token = result.stdout.strip()
    if result.rc != 0 or not token:
        raise ClusterError(
            "could not read the cluster join token",
            [
                f"Expected it at {distro.token_path} on the first node.",
                "Without it the other nodes cannot join.",
            ],
        )
    return token


def join_agents(
    server: Node, agents: list[Node], distro: Distro, reporter=None
) -> None:
    """agent 노드들을 server 가 이미 서 있는 클러스터에 합류시킨다.

    server 를 먼저 세우고 토큰을 읽어야 하므로 이 단계는 뒤로 밀 수 없다.
    agent 끼리는 서로를 몰라도 되니 순서는 상관없다.
    """
    reporter = reporter or NullReporter()

    with reporter.step("read join token"):
        token = read_token(server, distro)

    # agent 는 server 의 노드 주소로 붙는다. 밖에서 닿는 주소(api_address)가
    # 아니다 — 같은 네트워크 안에 있으니 굳이 밖으로 돌아 나갈 이유가 없고,
    # NAT 뒤에서는 그 주소로 자기 자신을 찾지도 못한다.
    server_address = server.target.host

    for agent in agents:
        for step in distro.agent_install_steps(server_address, token):
            with reporter.step(f"{agent.label()}: {step.label}"):
                _run_step(agent, step)


def up(
    node: Node,
    distro: Distro,
    reporter=None,
    api_address: Optional[str] = None,
    agents: Optional[list[Node]] = None,
) -> UpResult:
    """클러스터를 세우고 kubeconfig 를 가져온다.

    node 는 server, agents 는 그 뒤에 합류시킬 노드들이다. agents 가 비면
    단일 노드 클러스터이고, 그때의 동작은 예전과 같다.

    api_address 는 '내 PC 의 kubectl 이 접속할 주소'다. host 모드에서는
    노드 주소 그대로지만, vm 모드에서는 노드(VM)의 주소가 NAT 안이라
    밖에서 닿는 주소(호스트)를 따로 받아 인증서와 kubeconfig 에 넣는다.
    """
    reporter = reporter or NullReporter()
    api_address = api_address or node.target.host
    agents = agents or []
    expected = 1 + len(agents)

    with reporter.step("preflight"):
        already = is_installed(node, distro)
        if not already:
            check_memory(node, distro)
            # 설치 단계도 설정 파일을 쓴다. 아직 설치되지 않았어도 파일이
            # 먼저 놓여 있을 수 있다(미리 준비해 둔 설정).
            if not config_is_ours(node, distro):
                raise _foreign_config_error(distro, api_address)

    if not already:
        for step in distro.install_steps(api_address):
            with reporter.step(step.label):
                _run_step(node, step)

    # server 가 Ready 여야 토큰이 발급돼 있고 합류를 받을 수 있다.
    with reporter.step("wait for node"):
        wait_for_ready(node, distro)

    if agents:
        pending = [a for a in agents if not is_installed(a, distro)]
        if pending:
            join_agents(node, pending, distro, reporter)
        with reporter.step(f"wait for {expected} nodes"):
            wait_for_ready(node, distro, expected=expected)

    # 인증서가 지금 접속 주소를 포함하는지 확인한다. 클라우드에서 중지/재시작
    # 으로 IP 가 바뀐 뒤 재실행하면, kubeconfig 만 갱신되고 인증서는 옛 주소로
    # 남아 kubectl 이 전부 거부된다. 설정을 다시 쓰고 재시작하면 재발급된다.
    refreshed = False
    with reporter.step("verify certificate"):
        covered = certificate_covers(node, distro, api_address)

    if covered is False:
        # 치유는 설정 파일을 다시 쓰는 일이다. 남의 파일이면 여기서 멈춘다 —
        # 인증서를 고치자고 사용자의 클러스터 설정을 지울 수는 없다.
        if not config_is_ours(node, distro):
            raise _foreign_config_error(distro, api_address)
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
                        f"    {node.cli_down()}",
                        f"    {node.cli_up()}",
                    ],
                )

    with reporter.step("fetch kubeconfig"):
        path = fetch_kubeconfig(node, distro, api_address)

    # 병합은 편의 기능이다. 사용자의 ~/.kube/config 가 깨져 있어도 클러스터는
    # 정상이므로, 여기서 실패해도 up 전체를 실패로 만들지 않는다 — 대신
    # 사유를 담아 보내 CLI 가 경고와 대안(환경변수)을 안내하게 한다.
    context = None
    is_current = False
    merge_error = None
    with reporter.step("update ~/.kube/config"):
        try:
            merged = kubeconf.merge(
                path.read_text(encoding="utf-8"), node.name
            )
            context = merged.context
            is_current = merged.became_current
        except kubeconf.KubeconfigError as exc:
            merge_error = str(exc)

    with reporter.step("verify api access"):
        reachable = api_reachable(api_address)

    return UpResult(
        kubeconfig=path,
        api_address=api_address,
        api_reachable=reachable,
        already_installed=already,
        certificate_refreshed=refreshed,
        context=context,
        context_is_current=is_current,
        merge_error=merge_error,
        node_count=expected,
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
                    f"Inspect the host:  {node.cli_ssh()}",
                    f"    sudo systemctl status {distro.service}",
                ],
            )

    # 로컬 kubeconfig 도 같이 치운다. 남겨두면 죽은 클러스터를 가리킨다.
    path = kubeconfig_path(node.name)
    if path.exists():
        path.unlink()

    # ~/.kube/config 의 우리 항목도 걷어낸다. 실패해도 제거 자체는 성공이다.
    with contextlib.suppress(kubeconf.KubeconfigError):
        kubeconf.remove(node.name)

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
    node_lines: list[str] = field(default_factory=list)  # kubectl get nodes 출력

    @property
    def ready_count(self) -> int:
        return count_ready("\n".join(self.node_lines))

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
            host.node_lines = [
                line for line in nodes.stdout.strip().splitlines() if line.strip()
            ]
            break

    return host
