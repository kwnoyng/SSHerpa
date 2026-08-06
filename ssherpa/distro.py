"""쿠버네티스 배포판별 설치 절차.

배포판마다 다른 것은 '단계 목록'과 '경로' 뿐이다. 설치 로직 자체는
cluster.py 가 공통으로 처리한다.

설치를 명령 하나가 아니라 단계 목록으로 모델링한 이유는, 나중에 kubeadm
처럼 준비 작업이 여러 개인 배포판이 들어와도 구조를 바꾸지 않기 위함이다.

tls-san 은 CLI 플래그가 아니라 항상 config.yaml 에 쓴다. 호스트 주소가
바뀌었을 때(클라우드 중지/재시작) 파일을 다시 쓰고 재시작하면 인증서가
재발급되는데, 플래그로 박아두면 그 길이 없다.
"""

from dataclasses import dataclass
from typing import Optional

# k3s 설치는 이미지 내려받기까지 포함해 1분 가까이 걸린다.
INSTALL_TIMEOUT = 600


@dataclass
class Step:
    """원격에서 실행할 단계 하나."""

    label: str
    command: str
    timeout: int = 120


@dataclass
class Distro:
    name: str
    kubeconfig_path: str  # 원격의 kubeconfig 위치
    uninstall_path: str  # 제거 스크립트 위치
    installed_marker: str  # 이 파일이 있으면 이미 설치된 것으로 본다
    kubectl: str  # 노드 상태를 확인할 때 쓸 kubectl 경로
    min_memory_mb: int  # 이보다 적으면 설치해도 노드가 Ready 가 되지 않는다
    service: str  # systemd 유닛 이름. status 로 실행 여부를 본다
    summary: str  # 선택 프롬프트에 보여줄 한 줄 설명
    config_path: str  # tls-san 을 담는 설정 파일 (SSHerpa 가 소유·관리)
    serving_cert_path: str  # API 서버 serving 인증서 — SAN 검사에 쓴다
    token_path: str  # server 가 발급한 합류 토큰. agent 가 이걸로 들어온다
    join_port: int  # agent 가 두드릴 포트. 배포판마다 다르다

    def install_steps(self, api_address: str) -> list[Step]:
        raise NotImplementedError

    def agent_install_steps(self, server_address: str, token: str) -> list[Step]:
        """이 노드를 기존 클러스터에 agent 로 합류시킨다.

        server 는 자기가 만든 토큰을 아는 노드만 받아들인다. 그래서 순서가
        정해져 있다 — server 를 먼저 세우고, 거기서 토큰을 읽어, agent 에게
        건넨다. 병렬로 시작할 수 없는 이유다.
        """
        raise NotImplementedError

    def read_token_command(self) -> str:
        """server 가 발급한 합류 토큰을 읽는다 (root 만 읽을 수 있다)."""
        return f"sudo cat {self.token_path}"

    def tls_config_step(self, api_address: str) -> Step:
        """접속 주소를 인증서 SAN 에 넣도록 설정 파일을 쓴다.

        이 파일은 SSHerpa 가 만들고 소유한다. 통째로 덮어쓰는 이유다.
        """
        directory = self.config_path.rsplit("/", 1)[0]
        return Step(
            label="configure tls-san",
            command=(
                f"sudo mkdir -p {directory} && "
                f"printf 'tls-san:\\n  - %s\\n' '{api_address}' "
                f"| sudo tee {self.config_path} >/dev/null"
            ),
        )

    def refresh_certificate_step(self, api_address: str) -> Step:
        """설정을 새 주소로 다시 쓰고 재시작해 인증서를 재발급시킨다."""
        return Step(
            label="refresh certificate",
            command=(
                f"{self.tls_config_step(api_address).command} && "
                f"sudo systemctl restart {self.service}"
            ),
            timeout=INSTALL_TIMEOUT,
        )

    def read_san_command(self) -> str:
        """serving 인증서의 SAN 목록을 읽는다."""
        return (
            f"sudo openssl x509 -in {self.serving_cert_path} "
            "-noout -ext subjectAltName 2>/dev/null"
        )

    def node_status_command(self) -> str:
        """노드가 Ready 인지 확인한다. kubeconfig 는 root 만 읽을 수 있다."""
        return (
            f"sudo {self.kubectl} --kubeconfig {self.kubeconfig_path} "
            "get nodes --no-headers 2>/dev/null"
        )

    def read_kubeconfig_command(self) -> str:
        return f"sudo cat {self.kubeconfig_path}"

    def uninstall_steps(self) -> list[Step]:
        return [
            Step(
                label=f"uninstall {self.name}",
                command=(
                    f"if [ -x {self.uninstall_path} ]; then sudo {self.uninstall_path}; "
                    f"else echo 'not installed'; fi"
                ),
                timeout=300,
            )
        ]

    def status_command(self) -> str:
        """설치 여부와 서비스 상태를 한 줄로 보고한다."""
        return (
            f'printf "SSHERPA_D {self.name} %s %s\\n" '
            f'"$(test -x {self.installed_marker} && echo yes || echo no)" '
            f'"$(systemctl is-active {self.service} 2>/dev/null || true)"'
        )


class K3s(Distro):
    def __init__(self) -> None:
        super().__init__(
            name="k3s",
            kubeconfig_path="/etc/rancher/k3s/k3s.yaml",
            uninstall_path="/usr/local/bin/k3s-uninstall.sh",
            installed_marker="/usr/local/bin/k3s",
            kubectl="k3s kubectl",
            # k3s 는 512MB 에서도 뜨지만 여유가 없으면 파드를 못 띄운다.
            min_memory_mb=900,
            service="k3s",
            summary="lightweight, installs in about 20s, runs in 1 GB",
            config_path="/etc/rancher/k3s/config.yaml",
            serving_cert_path=(
                "/var/lib/rancher/k3s/server/tls/serving-kube-apiserver.crt"
            ),
            token_path="/var/lib/rancher/k3s/server/node-token",
            join_port=6443,  # k3s 는 API 포트로 합류를 받는다
        )

    def install_steps(self, api_address: str) -> list[Step]:
        # 설정 파일을 설치보다 먼저 쓴다. k3s 는 첫 기동 때 config.yaml 을
        # 읽어 tls-san 을 인증서에 넣는다.
        return [
            self.tls_config_step(api_address),
            Step(
                label="install k3s",
                command="curl -sfL https://get.k3s.io | sudo sh -",
                timeout=INSTALL_TIMEOUT,
            ),
        ]

    def agent_install_steps(self, server_address: str, token: str) -> list[Step]:
        # 같은 설치 스크립트가 K3S_URL 유무로 server/agent 를 가른다.
        return [
            Step(
                label="join cluster",
                command=(
                    "curl -sfL https://get.k3s.io | sudo "
                    f"K3S_URL=https://{server_address}:{self.join_port} "
                    f"K3S_TOKEN='{token}' sh -"
                ),
                timeout=INSTALL_TIMEOUT,
            )
        ]


class RKE2(Distro):
    def __init__(self) -> None:
        super().__init__(
            name="rke2",
            kubeconfig_path="/etc/rancher/rke2/rke2.yaml",
            uninstall_path="/usr/local/bin/rke2-uninstall.sh",
            installed_marker="/usr/local/bin/rke2",
            kubectl="/var/lib/rancher/rke2/bin/kubectl",
            # 2GB 호스트에서 실측: 컨트롤 플레인은 뜨지만 CNI 설치 파드가
            # Pending 에 걸려 노드가 영영 NotReady 로 남는다.
            min_memory_mb=3500,
            service="rke2-server",
            summary="security-hardened, closer to production, needs 4 GB",
            config_path="/etc/rancher/rke2/config.yaml",
            serving_cert_path=(
                "/var/lib/rancher/rke2/server/tls/serving-kube-apiserver.crt"
            ),
            token_path="/var/lib/rancher/rke2/server/node-token",
            # RKE2 는 API(6443) 와 별도로 supervisor 포트로 합류를 받는다.
            # 6443 으로 보내면 붙는 듯하다 조용히 실패한다.
            join_port=9345,
        )

    def uninstall_steps(self) -> list[Step]:
        # rke2-uninstall.sh 만 돌리면 컨테이너와 프로세스가 남은 채로
        # 종료 코드 0 을 돌려준다. 실측: 재부팅 후 rke2-server 가 되살아나
        # 6443 을 선점하는 바람에 k3s 가 뜨지 못했다.
        return [
            Step(
                label="stop rke2",
                command=(
                    "if [ -x /usr/local/bin/rke2-killall.sh ]; "
                    "then sudo /usr/local/bin/rke2-killall.sh >/dev/null 2>&1; fi"
                ),
                timeout=300,
            ),
            *super().uninstall_steps(),
        ]

    def install_steps(self, api_address: str) -> list[Step]:
        # RKE2 는 k3s 와 달리 설치와 기동이 분리돼 있다.
        return [
            Step(
                label="install rke2",
                command="curl -sfL https://get.rke2.io | sudo sh -",
                timeout=INSTALL_TIMEOUT,
            ),
            self.tls_config_step(api_address),
            Step(
                label="start rke2",
                command="sudo systemctl enable --now rke2-server.service",
                timeout=INSTALL_TIMEOUT,
            ),
        ]

    def agent_install_steps(self, server_address: str, token: str) -> list[Step]:
        # agent 도 설치·설정·기동이 분리돼 있고, 설정은 server 와 같은
        # config.yaml 자리를 쓴다 (tls-san 대신 server/token 을 담는다).
        directory = self.config_path.rsplit("/", 1)[0]
        return [
            Step(
                label="install rke2 agent",
                command=(
                    "curl -sfL https://get.rke2.io | "
                    "sudo INSTALL_RKE2_TYPE=agent sh -"
                ),
                timeout=INSTALL_TIMEOUT,
            ),
            Step(
                label="configure join",
                command=(
                    f"sudo mkdir -p {directory} && "
                    f"printf 'server: https://%s:%s\\ntoken: %s\\n' "
                    f"'{server_address}' '{self.join_port}' '{token}' "
                    f"| sudo tee {self.config_path} >/dev/null"
                ),
            ),
            Step(
                label="join cluster",
                command="sudo systemctl enable --now rke2-agent.service",
                timeout=INSTALL_TIMEOUT,
            ),
        ]


DISTROS: dict[str, Distro] = {
    "k3s": K3s(),
    "rke2": RKE2(),
}


def get(name: str) -> Optional[Distro]:
    return DISTROS.get(name)


def names() -> str:
    return ", ".join(DISTROS)
