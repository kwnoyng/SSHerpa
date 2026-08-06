"""VM 생성·제거 (호스트 위에서 virsh/qemu 를 부린다).

virt.py 가 기반(QEMU/libvirt/NAT 네트워크)을 준비했다는 전제 위에서,
클라우드 이미지 + cloud-init 으로 VM 을 만들고 IP 를 알아낸다.

옵션 철학: VM 의 사양은 VmSpec 의 기본값으로 고정한다. 호스트 OS 는
고객이 정하지만 VM 안의 OS 는 SSHerpa 가 만드는 세계라 우리가 정한다 —
Ubuntu LTS 하나로 고정하면 "VM 위 k3s" 조합을 하나만 검증하면 된다.
사양 옵션은 필요가 증명되면 VmSpec 필드를 CLI 플래그로 여는 것으로
대응한다 (이 층은 그때도 바뀌지 않는다).
"""

import contextlib
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
    """VM 한 대의 사양. 기본값이 곧 지금의 결정이다."""

    name: str = f"{VM_PREFIX}node-1"
    memory_mb: int = PER_VM_MB  # doctor 의 용량 추정("~N × 2 GB VMs")과 같은 상수
    vcpus: int = 2
    disk_gb: int = 10  # 얇은 파일 — 실제로는 쓴 만큼만 차지한다


def node_label(target_name: str, vm_name: str) -> str:
    """CLI 가 이 VM 을 부르는 이름. kubeconfig 파일명과 컨텍스트가 여기서 나온다.

    up 과 down 이 각자 이름을 지으면 한쪽 규칙만 바뀌었을 때 죽은 클러스터를
    가리키는 항목이 로컬에 남는다 — 실측으로 겪었다. 그래서 한 곳에서만 짓는다.
    """
    return f"{target_name}-{vm_name.removeprefix(VM_PREFIX)}"


def node_name(index: int) -> str:
    """1 -> ssherpa-node-1. 이름이 결정적이라 재실행이 재사용이 되고,
    teardown 은 접두사만 보고 우리 것만 골라낼 수 있다."""
    return f"{VM_PREFIX}node-{index}"


def specs_for(count: int, memory_mb: int = PER_VM_MB) -> list[VmSpec]:
    """노드 수만큼의 사양 목록. 첫 번째가 server 가 된다."""
    return [
        VmSpec(name=node_name(i), memory_mb=memory_mb) for i in range(1, count + 1)
    ]


@dataclass
class VmInfo:
    name: str
    ip: str
    mac: str
    already_existed: bool


def local_key_paths() -> tuple[Path, Path]:
    base = Path.home() / ".ssherpa" / "vm_ed25519"
    return base, base.with_suffix(".pub")


def known_hosts_path() -> Path:
    """VM 접속에만 쓰는 known_hosts.

    사용자의 ~/.ssh/known_hosts 에 VM 을 기록하면 안 되는 이유가 있다.
    VM 주소는 NAT 풀(192.168.122.x)에서 재활용되는데 VM 은 새로 만들 때마다
    새 호스트 키를 발급받는다. 그래서 같은 주소에 다른 키가 오는 일이
    반드시 반복되고, ssh 는 그때마다 중간자 공격으로 의심해 접속을 끊는다
    (실사용에서 발생). 우리 파일로 분리해 두면 사용자의 진짜 호스트 기록을
    더럽히지 않고, 우리 쪽 항목만 정리할 수 있다.
    """
    return Path.home() / ".ssherpa" / "known_hosts"


def forget_host_key(address: str) -> None:
    """이 주소로 기억해둔 옛 VM 의 키를 지운다.

    새로 만든 VM 은 반드시 새 키를 갖는다 — 그러니 남아 있던 기록은
    맞을 수가 없다. 지우면 다음 접속이 accept-new 로 정상 등록된다.
    """
    path = known_hosts_path()
    if not path.exists():
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["ssh-keygen", "-R", address, "-f", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )


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


def wait_for_ip(
    target: Target, name: str, timeout: int = IP_TIMEOUT
) -> tuple[str, str]:
    """(mac, ip) 를 돌려준다. 가상 공유기의 임대 장부에 IP 가 올라올
    때까지 기다린다 — 첫 부팅은 cloud-init 까지 1분 안팎이 걸린다.

    timeout=0 이면 한 번만 보고 만다. 상태를 '조회' 하는 쪽은 기다리면
    안 된다 — status 가 주소 없는 VM 앞에서 3분간 멈춰 있었다 (실측).
    """
    iflist = run(target, f"sudo virsh domiflist {name}", timeout=30)
    mac = parse_mac(iflist.stdout)
    if not mac:
        raise VmError(
            f"could not read the network interface of {name}",
            ["The VM exists but has no NIC attached — this is unexpected.",
             f"Inspect it on the host:  sudo virsh dumpxml {name}"],
        )

    deadline = time.monotonic() + timeout
    while True:
        leases = run(target, "sudo virsh net-dhcp-leases default", timeout=30)
        ip = parse_lease_ip(leases.stdout, mac)
        if ip:
            return mac, ip
        if time.monotonic() >= deadline:
            break
        time.sleep(IP_POLL_INTERVAL)

    # 기다리지 않기로 한 호출(조회 명령)에는 '시간이 모자랐다' 가 아니라
    # '지금은 주소가 없다' 가 사실이다.
    message = (
        f"{name} has no address yet"
        if timeout <= 0
        else f"{name} did not obtain an IP within {timeout}s"
    )
    raise VmError(
        message,
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

    # 방금 만든 VM 은 새 호스트 키를 갖는다. 이 주소로 기억해둔 옛 키가
    # 남아 있으면 ssh 가 키 변경으로 보고 접속을 끊는다 — 주소가 풀에서
    # 재활용되므로 반드시 일어나는 일이다 (실사용에서 발생).
    if not already:
        forget_host_key(ip)

    # 지금 받은 주소를 이 VM 의 것으로 못박는다. 예약은 이미 있는 VM 에도
    # 걸 수 있어서, 옛 버전이 만든 VM 도 재실행 한 번으로 주소가 고정된다.
    reserve = reserve_ip_step(spec.name, mac, ip)
    with reporter.step(reserve.label):
        _run_step(target, reserve)

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


# 규칙을 세우는 스크립트와, 부팅 때 그것을 부르는 유닛. 둘 다 우리가
# 만들고 소유하므로 통째로 덮어쓰고 통째로 지운다.
FORWARD_SCRIPT = "/usr/local/bin/ssherpa-api-forward"
FORWARD_UNIT = "ssherpa-api-forward.service"
FORWARD_UNIT_PATH = f"/etc/systemd/system/{FORWARD_UNIT}"


def forward_script(vm_ip: str) -> str:
    """호스트의 6443 을 VM 으로 넘기는 규칙을 세우는 스크립트.

    명령을 그때그때 실행하지 않고 스크립트로 남기는 이유: 지금 세우는
    규칙과 재부팅 뒤에 다시 세울 규칙이 같은 코드여야 한다. 둘로 나뉘면
    한쪽만 고쳐지는 날이 온다.

    root 로 실행되므로 sudo 를 쓰지 않는다.

    내용은 영어로 쓴다 — 이 파일은 고객사 호스트에 남아 그곳 관리자가
    읽는다. 우리 저장소 안의 주석과 달리 여기는 사용자의 자리다.
    """
    return f"""#!/bin/sh
# Managed by SSHerpa - do not edit.
# Recreated by `ssherpa up <target> --vm`, removed by `ssherpa down <target>`.
#
# iptables rules live in kernel memory, so a reboot would otherwise leave the
# VM running with no way in from outside. systemd runs this again at boot.
set -e

# Remove only rules carrying our tag. Deleting a rule (-D) needs the same
# arguments it was added with, and the old address may no longer be known,
# so they are found by the tag instead.
clean() {{
    iptables-save -t "$1" 2>/dev/null \\
        | grep -F -- '{_RULE_TAG}' \\
        | sed -e 's/^-A //' -e 's/"//g' \\
        | while IFS= read -r rule; do iptables -t "$1" -D $rule; done
}}
clean nat
clean filter

iptables -t nat -A PREROUTING -p tcp --dport {API_PORT} \\
    -m comment --comment {_RULE_TAG} \\
    -j DNAT --to-destination {vm_ip}:{API_PORT}

# libvirt puts a reject rule in FORWARD, so ours has to come first (-I 1).
iptables -I FORWARD 1 -p tcp -d {vm_ip} --dport {API_PORT} \\
    -m comment --comment {_RULE_TAG} -j ACCEPT
"""


def forward_unit() -> str:
    """부팅 때 규칙을 다시 세우는 systemd 유닛.

    배포판의 iptables-persistent 를 쓰지 않는 이유: 그것은 호스트의 모든
    규칙을 저장하고 복원한다. 관리자가 일부러 지운 남의 규칙까지 되살리게
    되므로, 우리 규칙만 다시 세우는 유닛을 따로 둔다.

    libvirt 뒤에 실행해야 한다. libvirt 는 네트워크를 켤 때 FORWARD 에
    자기 규칙을 넣는데, 우리가 먼저 들어가면 그 뒤로 밀려 거부당한다.
    """
    return f"""[Unit]
Description=SSHerpa: forward the Kubernetes API port into the VM
After=libvirtd.service virtnetworkd.service network-online.target
Wants=libvirtd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={FORWARD_SCRIPT}

[Install]
WantedBy=multi-user.target
"""


def expose_api_step(vm_ip: str) -> Step:
    """호스트의 6443 을 VM 의 6443 으로 넘겨주는 통행 규칙(포트포워딩).

    내 PC 의 kubectl 은 NAT 안의 VM 주소에 직접 닿을 수 없다. 호스트
    주소로 접속하게 하고, 호스트가 VM 으로 전달한다 — 집 공유기의
    포트포워딩과 같은 원리다.

    규칙은 커널 메모리에만 살아서 재부팅이면 사라진다. 그래서 세우는
    것으로 끝내지 않고, 부팅 때 다시 세우도록 등록까지 한다.
    """
    return Step(
        label="expose api port",
        command=(
            "set -e\n"
            f"sudo tee {FORWARD_SCRIPT} >/dev/null <<'SSHERPA_EOF'\n"
            f"{forward_script(vm_ip)}"
            "SSHERPA_EOF\n"
            f"sudo chmod 755 {FORWARD_SCRIPT}\n"
            f"sudo tee {FORWARD_UNIT_PATH} >/dev/null <<'SSHERPA_EOF'\n"
            f"{forward_unit()}"
            "SSHERPA_EOF\n"
            "sudo systemctl daemon-reload\n"
            f"sudo systemctl enable {FORWARD_UNIT} >/dev/null 2>&1\n"
            # start 가 아니라 restart 다. 이미 적용된 유닛은 start 로 다시
            # 실행되지 않아, 주소가 바뀐 재실행이 조용히 무시된다.
            f"sudo systemctl restart {FORWARD_UNIT}"
        ),
    )


def reserve_ip_step(name: str, mac: str, ip: str) -> Step:
    """이 VM 이 앞으로도 같은 주소를 받도록 예약한다.

    예약이 없으면 재부팅마다 주소가 달라질 수 있고(실측: .250 → .160 →
    .231), 그러면 부팅 때 다시 세우는 규칙이 없는 VM 을 가리키게 된다.
    주소를 고정하는 편이 규칙을 매번 다시 알아내는 것보다 단순하다.

    이미 있는 예약은 걷어내고 다시 넣는다 — MAC, IP, 이름 셋 모두로 한 번씩.
    libvirt 는 이 셋 중 어느 하나만 겹쳐도 add 를 거절하는데, 옛 예약이
    어느 쪽으로 남아 있을지는 알 수 없다. VM 을 `ssherpa down` 이 아니라
    손으로(virsh) 지우면 이름만 같고 MAC 은 다른 예약이 남는다 (실측).
    """
    delete = (
        "sudo virsh net-update default delete ip-dhcp-host "
        "\"<host {}/>\" --live --config >/dev/null 2>&1 || true"
    )
    return Step(
        label="reserve vm address",
        command=(
            delete.format(f"mac='{mac}'") + "\n"
            + delete.format(f"ip='{ip}'") + "\n"
            + delete.format(f"name='{name}'") + "\n"
            + "sudo virsh net-update default add ip-dhcp-host "
            f"\"<host mac='{mac}' name='{name}' ip='{ip}'/>\" --live --config"
        ),
    )


def release_ip_step(mac: str) -> Step:
    """VM 이 사라지면 그 예약도 걷는다. 남겨두면 없는 MAC 이 주소를 붙든다."""
    return Step(
        label="release vm address",
        command=(
            "sudo virsh net-update default delete ip-dhcp-host "
            f"\"<host mac='{mac}'/>\" --live --config >/dev/null 2>&1 || true"
        ),
    )


def unexpose_api_step() -> Step:
    """규칙과, 규칙을 다시 세우던 장치까지 함께 걷는다.

    유닛만 남기면 다음 부팅에서 없는 VM 으로 가는 길을 다시 뚫는다.
    """
    return Step(
        label="close api port",
        command=(
            f"sudo systemctl disable --now {FORWARD_UNIT} >/dev/null 2>&1 || true\n"
            f"sudo rm -f {FORWARD_UNIT_PATH} {FORWARD_SCRIPT}\n"
            "sudo systemctl daemon-reload\n"
            # 유닛이 사라진 뒤에도 지금 서 있는 규칙은 남으므로 직접 걷는다
            f"{_CLEAN_RULES}"
        ),
    )


def expose_api(target: Target, vm_ip: str, reporter=None) -> None:
    step = expose_api_step(vm_ip)
    with (reporter or _NullReporter()).step(step.label):
        _run_step(target, step)

        # 규칙이 지금 서 있는 것과 재부팅 뒤에도 서는 것은 다른 문제다.
        # 등록까지 확인해야 "살아남는다"고 말할 수 있다.
        state = run(target, f"systemctl is-enabled {FORWARD_UNIT} 2>/dev/null")
        if state.stdout.strip() != "enabled":
            raise VmError(
                "the API forwarding rule would not survive a reboot",
                [
                    f"{FORWARD_UNIT} is "
                    f"{state.stdout.strip() or 'not registered'} — the rule is "
                    "in place now, but a restart of the host would drop it.",
                    f"Inspect it:  ssherpa ssh {target.name or ''}".rstrip(),
                    f"    systemctl status {FORWARD_UNIT}",
                ],
            )


def unexpose_api(target: Target, reporter=None) -> None:
    step = unexpose_api_step()
    with (reporter or _NullReporter()).step(step.label):
        _run_step(target, step)


_NO_VIRSH = "SSHERPA_NO_VIRSH"


def list_vms(target: Target) -> list[str]:
    """호스트에 있는 SSHerpa 소유 VM 이름 목록.

    virsh 는 켜진 것을 먼저, 꺼진 것을 나중에 늘어놓는다. 그대로 쓰면
    꺼진 노드 하나 때문에 목록 순서가 뒤집혀 읽기 어려우므로 이름순으로
    돌려준다 — 첫 번째가 언제나 server(node-1) 라는 약속도 여기 걸려 있다.

    virsh 가 없는 호스트(host 모드만 쓰는)는 빈 목록이다 — 오류가 아니다.
    하지만 virsh 가 있는데 대답하지 못하는 것은 다르다. 둘을 뭉뚱그리면
    libvirtd 가 죽은 호스트에서 'VM 없음' 이라 답하게 되고, down 은
    멀쩡한 클러스터를 두고 "지울 것이 없다" 며 끝난다.
    """
    result = run(
        target,
        f"command -v virsh >/dev/null 2>&1 || {{ echo {_NO_VIRSH}; exit 0; }}\n"
        "sudo virsh list --all --name",
    )
    if _NO_VIRSH in result.stdout:
        return []
    if result.rc != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise VmError(
            "could not ask the host which VMs exist",
            (detail[-2:] if detail else [])
            + [
                "virsh is installed but did not answer, so SSHerpa cannot tell",
                "whether a VM cluster is running here.",
                "    systemctl status libvirtd",
            ],
        )
    return sorted(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(VM_PREFIX)
    )


def forwarding_installed(target: Target) -> bool:
    """포워딩 장치가 호스트에 남아 있는가.

    VM 을 손으로 지운 뒤에도 유닛과 스크립트는 남는다. 그 상태를 모르면
    down 이 그것들을 지나쳐, 부팅마다 없는 주소로 6443 을 넘기는 규칙이
    되살아난다.
    """
    result = run(target, f"test -f {FORWARD_UNIT_PATH} || test -f {FORWARD_SCRIPT}")
    return result.rc == 0


def find(
    target: Target,
    name: Optional[str] = None,
    timeout: int = IP_TIMEOUT,
) -> Optional[VmInfo]:
    """호스트의 SSHerpa VM 을 찾아 접속 정보(IP)까지 채운다. 없으면 None.

    이름을 주지 않으면 첫 번째(server) 노드를 고른다 — `ssherpa ssh --vm`
    처럼 "그 VM"이라고만 말했을 때 들어갈 곳이다.

    timeout=0 은 '기다리지 말고 지금 상태만' 이라는 뜻이다. 조회 명령은
    답을 만들어내려 기다리면 안 된다.

    켜져 있지 않으면 오류다 — IP 는 살아 있는 VM 만 갖는다.
    """
    names = list_vms(target)
    if not names:
        return None
    if name is not None and name not in names:
        return None

    name = name or sorted(names)[0]
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

    mac, ip = wait_for_ip(target, name, timeout=timeout)
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
        known_hosts=str(known_hosts_path()),
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
        # 주소 예약은 MAC 으로 걸려 있다. VM 을 지우고 나면 알아낼 수 없으니
        # 먼저 읽어둔다.
        mac = parse_mac(run(target, f"sudo virsh domiflist {name}", timeout=30).stdout)

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

    if mac:
        release = release_ip_step(mac)
        with reporter.step(release.label):
            _run_step(target, release)

    # 제거 스크립트의 종료 코드를 믿지 않는다 — 실제로 사라졌는지 본다.
    with reporter.step("verify removal"):
        if vm_state(target, name):
            raise VmError(
                f"{name} is still defined after removal",
                [f"Inspect it on the host:  sudo virsh dominfo {name}"],
            )

    return True
