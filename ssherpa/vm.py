"""VM 생성·제거 (호스트 위에서 virsh/qemu 를 부린다).

virt.py 가 기반(QEMU/libvirt/NAT 네트워크)을 준비했다는 전제 위에서,
클라우드 이미지 + cloud-init 으로 VM 을 만들고 IP 를 알아낸다.

옵션 철학: VM 의 사양은 VmSpec 의 기본값으로 고정한다. 호스트 OS 는
고객이 정하지만 VM 안의 OS 는 SSHerpa 가 만드는 세계라 우리가 정한다 —
Ubuntu LTS 하나로 고정하면 "VM 위 k3s" 조합을 하나만 검증하면 된다.
사양 옵션은 필요가 증명되면 VmSpec 필드를 CLI 플래그로 여는 것으로
대응한다 (이 층은 그때도 바뀌지 않는다).
"""

import re
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# API_PORT 는 cluster 와 같은 값을 공유해야 인증서(tls-san)·kubeconfig·
# 포워딩이 한 포트를 가리킨다.
from .cluster import API_PORT
from .distro import Step
from .doctor import PER_VM_MB
from .ssh import Target, run

# VM 안에 넣을 OS. 설치 마법사가 이미 끝나 있는 완제품 디스크(qcow2)라
# 부팅 + cloud-init 만으로 쓸 수 있는 상태가 된다.
BASE_IMAGE_URL = (
    "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
)

IMAGES_DIR = "/var/lib/ssherpa/images"
BASE_IMAGE = f"{IMAGES_DIR}/ubuntu-24.04.qcow2"
VM_ROOT = "/var/lib/ssherpa/vms"

# 이 접두사가 붙은 VM 만 SSHerpa 소유다. teardown 은 절대 이 밖을
# 건드리지 않는다 — 사용자가 손으로 만든 VM 은 남의 물건이다.
VM_PREFIX = "ssherpa-"

DOWNLOAD_TIMEOUT = 900  # 이미지 ~600MB — 회선에 따라 수 분
IP_TIMEOUT = 180  # 첫 부팅 + DHCP 까지 기다리는 최대 시간(초)
IP_POLL_INTERVAL = 3

_MAC_RE = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", re.IGNORECASE)


class VmError(Exception):
    """VM 생성·제거 중의 오류. hints 는 해결 방법."""

    def __init__(self, message: str, hints: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.hints = hints or []


@dataclass
class VmSpec:
    """VM 한 대의 사양. 기본값이 곧 Phase A 의 결정이다."""

    name: str = f"{VM_PREFIX}node-1"
    memory_mb: int = PER_VM_MB  # doctor 의 용량 추정("~N × 2 GB VMs")과 같은 상수
    vcpus: int = 2
    disk_gb: int = 10  # 얇은 파일 — 실제로는 쓴 만큼만 차지한다


@dataclass
class VmInfo:
    name: str
    ip: str
    mac: str
    already_existed: bool


def local_key_paths() -> tuple[Path, Path]:
    base = Path.home() / ".ssherpa" / "vm_ed25519"
    return base, base.with_suffix(".pub")


def ensure_local_key() -> str:
    """VM 접속 전용 키가 없으면 만들고, 공개키 한 줄을 돌려준다.

    사용자의 기존 키 배치에 의존하지 않기 위한 전용 키다. 우리가 만드는
    파일이므로 권한도 처음부터 바르다 (ssh-keygen 이 600 으로 만든다).
    """
    private, public = local_key_paths()
    if public.exists():
        return public.read_text(encoding="utf-8").strip()

    private.parent.mkdir(parents=True, exist_ok=True)
    try:
        if private.exists():
            # 개인키만 남은 경우 — 공개키는 개인키에서 다시 뽑을 수 있다
            derived = subprocess.run(
                ["ssh-keygen", "-y", "-f", str(private)],
                capture_output=True, text=True, check=True,
            )
            public.write_text(derived.stdout, encoding="utf-8")
        else:
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                 "-C", "ssherpa-vm", "-f", str(private)],
                capture_output=True, text=True, check=True,
            )
    except FileNotFoundError as exc:
        raise VmError(
            "ssh-keygen not found",
            ["Install the OpenSSH client — it provides both ssh and ssh-keygen."],
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise VmError(
            "could not create the VM access key",
            (exc.stderr or "").strip().splitlines()[-2:],
        ) from exc

    return public.read_text(encoding="utf-8").strip()


def user_data(name: str, pubkey: str) -> str:
    """cloud-init 설정. 클라우드 이미지에는 계정이 하나도 없다 —
    첫 부팅 때 이 파일이 로그인 가능한 상태를 만든다."""
    return (
        "#cloud-config\n"
        f"hostname: {name}\n"
        "users:\n"
        "  - name: ssherpa\n"
        "    shell: /bin/bash\n"
        '    sudo: "ALL=(ALL) NOPASSWD:ALL"\n'
        "    ssh_authorized_keys:\n"
        f"      - {pubkey}\n"
    )


def vm_dir(name: str) -> str:
    return f"{VM_ROOT}/{name}"


def download_step() -> Step:
    """base 이미지를 내려받는다. 있으면 그대로 쓴다(타겟당 1회).

    받다 만 파일이 base 로 남으면 이후 모든 VM 이 조용히 깨진다 —
    임시 이름으로 받고, qemu-img 가 읽을 수 있는지 확인한 뒤에야
    제자리로 옮긴다.
    """
    return Step(
        label="fetch base image",
        command=(
            f"sudo mkdir -p {IMAGES_DIR} && "
            f"test -f {BASE_IMAGE} || "
            f"(sudo curl -fsSL -o {BASE_IMAGE}.tmp {BASE_IMAGE_URL} && "
            f"sudo qemu-img info {BASE_IMAGE}.tmp >/dev/null && "
            f"sudo mv {BASE_IMAGE}.tmp {BASE_IMAGE})"
        ),
        timeout=DOWNLOAD_TIMEOUT,
    )


def disk_step(spec: VmSpec) -> Step:
    """base 를 원본으로 삼는 얇은 디스크를 만든다 — 복사가 아니라
    '달라진 부분만 기록'이라 몇 초면 끝나고 공간도 아낀다."""
    directory = vm_dir(spec.name)
    return Step(
        label="create disk",
        command=(
            f"sudo mkdir -p {directory} && "
            f"sudo qemu-img create -f qcow2 -b {BASE_IMAGE} -F qcow2 "
            f"{directory}/disk.qcow2 {spec.disk_gb}G >/dev/null"
        ),
    )


def seed_step(spec: VmSpec, pubkey: str) -> Step:
    """cloud-init seed ISO 를 만든다.

    도구는 실행 시점에 고른다: Debian 계열엔 cloud-localds, RHEL 9 계열엔
    xorriso 가 있다 (virt.PACKAGES 가 각각 깔아둔다). 볼륨 라벨 'cidata' 가
    cloud-init 이 seed 를 알아보는 표식이라 xorriso 경로에서도 지켜야 한다.
    """
    directory = vm_dir(spec.name)
    content = user_data(spec.name, pubkey)
    return Step(
        label="write cloud-init seed",
        command=(
            "set -e\n"
            f"sudo tee {directory}/user-data >/dev/null <<'SSHERPA_EOF'\n"
            f"{content}"
            "SSHERPA_EOF\n"
            f"printf 'instance-id: %s\\nlocal-hostname: %s\\n' "
            f"'{spec.name}' '{spec.name}' "
            f"| sudo tee {directory}/meta-data >/dev/null\n"
            f"cd {directory}\n"
            "if command -v cloud-localds >/dev/null 2>&1; then "
            "sudo cloud-localds seed.iso user-data meta-data; "
            "else "
            "sudo xorriso -as mkisofs -output seed.iso -volid cidata "
            "-joliet -rock user-data meta-data; fi"
        ),
    )


def boot_step(spec: VmSpec) -> Step:
    directory = vm_dir(spec.name)
    return Step(
        label="boot vm",
        command=(
            "sudo virt-install "
            f"--name {spec.name} "
            f"--memory {spec.memory_mb} "
            f"--vcpus {spec.vcpus} "
            f"--disk path={directory}/disk.qcow2,format=qcow2,bus=virtio "
            f"--disk path={directory}/seed.iso,device=cdrom "
            "--osinfo ubuntu24.04 "
            "--network network=default,model=virtio "
            "--graphics none --noautoconsole --autostart --import"
        ),
        timeout=300,
    )


def vm_state(target: Target, name: str) -> str:
    """running / shut off / ... 없으면 ""."""
    result = run(target, f"sudo virsh domstate {name} 2>/dev/null")
    return result.stdout.strip() if result.rc == 0 else ""


def parse_mac(domiflist_output: str) -> str:
    match = _MAC_RE.search(domiflist_output)
    return match.group(0).lower() if match else ""


def parse_lease_ip(leases_output: str, mac: str) -> str:
    """net-dhcp-leases 표에서 이 MAC 의 IPv4 주소를 찾는다."""
    for line in leases_output.splitlines():
        if mac and mac in line.lower():
            for field in line.split():
                if "/" in field and field.count(".") == 3:
                    return field.split("/")[0]
    return ""


class _NullReporter:
    def step(self, label: str):  # noqa: ARG002
        return nullcontext()


def _run_step(target: Target, step: Step) -> None:
    result = run(target, step.command, timeout=step.timeout)
    if result.rc != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise VmError(
            f"step failed: {step.label}",
            detail[-3:] if detail else ["No output from the remote command."],
        )


def wait_for_ip(target: Target, name: str) -> tuple[str, str]:
    """(mac, ip) 를 돌려준다. 가상 공유기의 임대 장부에 IP 가 올라올
    때까지 기다린다 — 첫 부팅은 cloud-init 까지 1분 안팎이 걸린다."""
    iflist = run(target, f"sudo virsh domiflist {name}", timeout=30)
    mac = parse_mac(iflist.stdout)
    if not mac:
        raise VmError(
            f"could not read the network interface of {name}",
            ["The VM exists but has no NIC attached — this is unexpected.",
             f"Inspect it on the host:  sudo virsh dumpxml {name}"],
        )

    deadline = time.monotonic() + IP_TIMEOUT
    while time.monotonic() < deadline:
        leases = run(target, "sudo virsh net-dhcp-leases default", timeout=30)
        ip = parse_lease_ip(leases.stdout, mac)
        if ip:
            return mac, ip
        time.sleep(IP_POLL_INTERVAL)

    raise VmError(
        f"{name} did not obtain an IP within {IP_TIMEOUT}s",
        [
            "The VM is defined but never reached the DHCP handshake.",
            "Watch it boot from the host console:",
            f"    sudo virsh console {name}   (exit with Ctrl+])",
        ],
    )


def create(
    target: Target, *, spec: Optional[VmSpec] = None, reporter=None
) -> VmInfo:
    """VM 하나를 만들고 IP 까지 알아낸다. 재실행해도 안전하다(멱등).

    같은 이름의 VM 이 이미 있으면 재사용하고(꺼져 있으면 켠다),
    없을 때만 이미지·디스크·seed·부팅 절차를 밟는다.
    """
    spec = spec or VmSpec()
    reporter = reporter or _NullReporter()

    with reporter.step("preflight"):
        state = vm_state(target, spec.name)
        already = bool(state)

    if not already:
        pubkey = ensure_local_key()
        for step in (download_step(), disk_step(spec), seed_step(spec, pubkey)):
            with reporter.step(step.label):
                _run_step(target, step)
        boot = boot_step(spec)
        with reporter.step(boot.label):
            _run_step(target, boot)
    elif state != "running":
        with reporter.step("start vm"):
            _run_step(
                target, Step(label="start vm", command=f"sudo virsh start {spec.name}")
            )

    with reporter.step("wait for ip"):
        mac, ip = wait_for_ip(target, spec.name)

    return VmInfo(name=spec.name, ip=ip, mac=mac, already_existed=already)



# 우리가 만든 iptables 규칙의 서명. 청소할 때 이 표식만 걷어낸다 —
# 호스트에 원래 있던 규칙은 남의 물건이다 (VM 접두사와 같은 원칙).
_RULE_TAG = "ssherpa-api"

# iptables-save 출력에서 우리 표식이 붙은 규칙만 골라 지운다.
# 규칙 삭제(-D)는 추가할 때와 똑같은 인자를 요구하는데, VM 을 다시 만들면
# IP 가 바뀌어 옛 규칙의 인자를 모른다 — 그래서 표식으로 찾는다.
_CLEAN_RULES = (
    "clean() { "
    "sudo iptables-save -t \"$1\" 2>/dev/null "
    f"| grep -F -- '{_RULE_TAG}' "
    "| sed -e 's/^-A //' -e 's/\"//g' "
    "| while IFS= read -r rule; do sudo iptables -t \"$1\" -D $rule; done; }; "
    "clean nat; clean filter"
)


def expose_api_step(vm_ip: str) -> Step:
    """호스트의 6443 을 VM 의 6443 으로 넘겨주는 통행 규칙(포트포워딩).

    내 PC 의 kubectl 은 NAT 안의 VM 주소에 직접 닿을 수 없다. 호스트
    주소로 접속하게 하고, 호스트가 VM 으로 전달한다 — 집 공유기의
    포트포워딩과 같은 원리다. 옛 규칙을 걷어낸 뒤 추가하므로 VM 이
    새 IP 로 재생성돼도 재실행이 곧 갱신이다.
    """
    return Step(
        label="expose api port",
        command=(
            "set -e\n"
            f"{_CLEAN_RULES}\n"
            f"sudo iptables -t nat -A PREROUTING -p tcp --dport {API_PORT} "
            f"-m comment --comment {_RULE_TAG} "
            f"-j DNAT --to-destination {vm_ip}:{API_PORT}\n"
            # libvirt 가 FORWARD 에 거부 규칙을 깔아두므로 그보다 앞(-I 1)이어야 한다
            f"sudo iptables -I FORWARD 1 -p tcp -d {vm_ip} --dport {API_PORT} "
            f"-m comment --comment {_RULE_TAG} -j ACCEPT"
        ),
    )


def unexpose_api_step() -> Step:
    return Step(label="close api port", command=_CLEAN_RULES)


def expose_api(target: Target, vm_ip: str, reporter=None) -> None:
    step = expose_api_step(vm_ip)
    with (reporter or _NullReporter()).step(step.label):
        _run_step(target, step)


def unexpose_api(target: Target, reporter=None) -> None:
    step = unexpose_api_step()
    with (reporter or _NullReporter()).step(step.label):
        _run_step(target, step)


def list_vms(target: Target) -> list[str]:
    """호스트에 있는 SSHerpa 소유 VM 이름 목록.

    virsh 가 없는 호스트(host 모드만 쓰는)는 빈 목록이다 — 오류가 아니다.
    """
    result = run(target, "sudo virsh list --all --name 2>/dev/null || true")
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(VM_PREFIX)
    ]


def find(target: Target) -> Optional[VmInfo]:
    """호스트의 SSHerpa VM 을 찾아 접속 정보(IP)까지 채운다. 없으면 None.

    켜져 있지 않으면 오류다 — IP 는 살아 있는 VM 만 갖는다.
    """
    names = list_vms(target)
    if not names:
        return None

    name = names[0]
    state = vm_state(target, name)
    if state != "running":
        label = target.name or "<target>"
        raise VmError(
            f"{name} is not running ({state or 'unknown'})",
            [
                "Bring it (and the cluster inside) back up:",
                f"    ssherpa up {label} --vm",
            ],
        )

    mac, ip = wait_for_ip(target, name)
    return VmInfo(name=name, ip=ip, mac=mac, already_existed=True)


def vm_target(host: Target, info: VmInfo) -> Target:
    """VM 을 여느 target 처럼 다루게 하는 Target 을 만든다.

    VM 의 주소(192.168.122.x)는 호스트 안에서만 통하므로 호스트를
    경유지(-J)로 쓴다. 이 Target 하나로 기존 부품 전부(probe, 클러스터
    설치, kubeconfig 가져오기)가 수정 없이 VM 위에서 동작한다 —
    cluster.py 가 노드의 정체를 모르게 설계한 이유가 여기서 회수된다.
    """
    private, _ = local_key_paths()
    return Target(
        name=f"{host.name}/{info.name}" if host.name else info.name,
        host=info.ip,
        user="ssherpa",  # cloud-init 이 만든 계정 (user_data 참고)
        key=str(private),
        jump=host.jump_spec(),
    )


def destroy(target: Target, name: str, reporter=None) -> bool:
    """VM 과 그 저장소를 지운다. 없었으면 False.

    안전장치: SSHerpa 접두사가 붙은 VM 만 지운다. 사용자가 손으로 만든
    VM 은 이름이 무엇이든 우리 소관이 아니다.
    """
    if not name.startswith(VM_PREFIX):
        raise VmError(
            f"refusing to destroy '{name}' — not created by SSHerpa",
            [f"SSHerpa only manages VMs named {VM_PREFIX}*."],
        )

    reporter = reporter or _NullReporter()

    with reporter.step("preflight"):
        if not vm_state(target, name):
            return False

    with reporter.step("destroy vm"):
        _run_step(
            target,
            Step(
                label="destroy vm",
                command=(
                    f"sudo virsh destroy {name} >/dev/null 2>&1; "
                    f"sudo virsh undefine {name} && "
                    f"sudo rm -rf {vm_dir(name)}"
                ),
                timeout=120,
            ),
        )

    # 제거 스크립트의 종료 코드를 믿지 않는다 — 실제로 사라졌는지 본다.
    with reporter.step("verify removal"):
        if vm_state(target, name):
            raise VmError(
                f"{name} is still defined after removal",
                [f"Inspect it on the host:  sudo virsh dominfo {name}"],
            )

    return True
