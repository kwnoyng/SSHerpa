"""호스트의 VM 능력 진단.

check 가 "SSHerpa 와 일할 수 있나"의 관문이라면, doctor 는 "이 호스트가
VM 을 만들 수 있나"의 진단서다. 실패가 곧 탈락이 아니다 — VM 모드가 안
되는 호스트도 host 모드는 되므로, 판정과 함께 갈 길을 안내한다.

판정 기준은 오직 '능력'(vmx/svm, /dev/kvm, 메모리, 디스크)이다.
가상화 종류·클라우드 브랜드 감지는 판정에 관여하지 않고, 실패했을 때
처방을 그 환경에 맞는 말로 쓰는 데만 쓴다 — 같은 "vmx 없음"이라도
물리 서버는 BIOS 를 켜면 되고, GCP E2 는 머신 계열을 바꿔야 하고,
vSphere VM 은 관리자에게 요청해야 한다.
"""

from dataclasses import dataclass, field
from typing import Optional

# VM 1대(2GB) + 호스트 몫(~1.5GB). 이보다 적으면 VM 이 떠도 OOM 으로 죽는다.
MIN_MEMORY_MB = 3500
HOST_RESERVE_MB = 1500
PER_VM_MB = 2048

# base 이미지(~600MB) + VM 디스크 + 스냅샷 성장분. 디스크 가득참은 스냅샷이
# 반쯤 쓰이다 깨지는 최악의 실패 양상이라 보수적으로 잡는다.
MIN_DISK_GB = 15

# 한 번의 접속으로 전부 가져온다. systemd-detect-virt 는 물리 서버에서
# "none" 을 출력하며 rc 1 로 끝나므로 || true 로 감싼다. 명령 자체가 없으면
# 빈 값 → unknown 으로 처리된다.
DOCTOR_PROBE = (
    'echo "SSHERPA_VIRT=$(systemd-detect-virt 2>/dev/null || true)"; '
    'echo "SSHERPA_CONTAINER=$(systemd-detect-virt --container 2>/dev/null || true)"; '
    'echo "SSHERPA_VENDOR=$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null)"; '
    'echo "SSHERPA_PRODUCT=$(cat /sys/class/dmi/id/product_name 2>/dev/null)"; '
    'echo "SSHERPA_CPUFLAG=$(grep -o -m1 -E \'vmx|svm\' /proc/cpuinfo)"; '
    'echo "SSHERPA_KVM=$(test -e /dev/kvm && echo yes || echo no)"; '
    'echo "SSHERPA_VIRSH=$(virsh --version 2>/dev/null || echo absent)"; '
    'echo "SSHERPA_LIBVIRTD=$(systemctl is-active libvirtd 2>/dev/null || true)"; '
    'echo "SSHERPA_MEM=$(grep MemTotal /proc/meminfo)"; '
    'echo "SSHERPA_DISK=$('
    "df --output=avail -B1 /var/lib/libvirt 2>/dev/null | tail -1 || true"
    ')"; '
    'echo "SSHERPA_DISK2=$(df --output=avail -B1 /var/lib 2>/dev/null | tail -1)"'
)


@dataclass
class HostFacts:
    """DOCTOR_PROBE 출력을 해석한 원자료."""

    virt: str = ""  # none(물리) / google / amazon / microsoft / vmware / kvm ...
    container: str = ""  # none / docker / lxc ...
    vendor: str = ""
    product: str = ""
    cpu_flag: str = ""  # vmx / svm / ""
    kvm_device: bool = False
    virsh_version: str = ""  # "" 이면 미설치
    libvirtd: str = ""  # active / inactive / ""
    memory_mb: Optional[int] = None
    disk_free_gb: Optional[float] = None


def parse_probe(stdout: str) -> HostFacts:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if line.startswith("SSHERPA_") and "=" in line:
            key, _, value = line.partition("=")
            fields[key.removeprefix("SSHERPA_")] = value.strip()

    facts = HostFacts(
        virt=fields.get("VIRT", ""),
        container=fields.get("CONTAINER", ""),
        vendor=fields.get("VENDOR", ""),
        product=fields.get("PRODUCT", ""),
        cpu_flag=fields.get("CPUFLAG", ""),
        kvm_device=fields.get("KVM", "") == "yes",
    )

    virsh = fields.get("VIRSH", "")
    facts.virsh_version = "" if virsh in ("", "absent") else virsh
    facts.libvirtd = fields.get("LIBVIRTD", "")

    # "MemTotal:  4030888 kB"
    mem_parts = fields.get("MEM", "").split()
    if len(mem_parts) >= 2 and mem_parts[1].isdigit():
        facts.memory_mb = int(mem_parts[1]) // 1024

    disk = fields.get("DISK") or fields.get("DISK2") or ""
    if disk.isdigit():
        facts.disk_free_gb = int(disk) / (1024**3)

    return facts


def _in_container(facts: HostFacts) -> bool:
    return facts.container not in ("", "none")


def host_type(facts: HostFacts) -> str:
    """사람이 읽을 호스트 정체. 판정이 아니라 표시·처방용."""
    if _in_container(facts):
        return f"container ({facts.container})"
    if facts.virt == "none":
        hardware = " ".join(p for p in (facts.vendor, facts.product) if p)
        return f"physical machine ({hardware})" if hardware else "physical machine"
    if facts.virt:
        brand = facts.product or facts.vendor
        return f"virtual machine ({facts.virt}" + (f" — {brand})" if brand else ")")
    return "unknown"


def _virtualization_hints(facts: HostFacts) -> list[str]:
    """'vmx/svm 없음'의 처방. 같은 증상이라도 환경마다 고치는 곳이 다르다."""
    if facts.virt == "none":
        # 물리 서버에서 가상화 명령어가 없는 CPU 는 요즘 사실상 없다 — BIOS 다.
        return [
            "This is a physical machine with virtualization disabled.",
            "Enable VT-x / AMD-V in the BIOS/UEFI (often under 'CPU features'",
            "or 'Advanced'), then run doctor again.",
        ]
    if facts.virt == "google":
        # E2 는 아예 불가, N1/N2 는 가능하지만 기본은 꺼져 있다 —
        # '이미 N2 인데 플래그만 꺼진' 경우가 실제로 흔하다 (실화).
        # update 명령에는 이 플래그가 없다(실측: unrecognized arguments).
        # export → YAML 수정 → update-from-file 만이 기존 인스턴스에 먹힌다.
        return [
            "This GCP instance does not expose nested virtualization.",
            "E2 series never can. On N1/N2 it exists but is OFF by default —",
            "with the instance stopped, enable it from Cloud Shell:",
            "    gcloud compute instances export <instance> \\",
            "        --zone <zone> --destination=vm.yaml",
            "    printf 'advancedMachineFeatures:\\n  enableNestedVirtualization: true\\n' \\",
            "        >> vm.yaml",
            "    gcloud compute instances update-from-file <instance> \\",
            "        --zone <zone> --source=vm.yaml",
            "Or use host mode as is:",
            "    ssherpa up <target>",
        ]
    if facts.virt == "amazon" or "EC2" in facts.product:
        return [
            "Regular AWS EC2 instances never expose virtualization —",
            "only *.metal instance types do. Host mode works as is:",
            "    ssherpa up <target>",
        ]
    if facts.virt == "microsoft":
        return [
            "On Hyper-V, expose virtualization to this VM from the Windows host:",
            '    Set-VMProcessor -VMName "<vm>" -ExposeVirtualizationExtensions $true',
            "On Azure, nested virtualization needs a v3-or-later size.",
        ]
    if facts.virt == "vmware":
        return [
            "This VM runs on VMware. Ask the vSphere administrator to enable",
            "'Expose hardware assisted virtualization to the guest OS'",
            "in this VM's CPU settings.",
        ]
    if facts.virt:
        return [
            f"This is a virtual machine ({facts.virt}) whose hypervisor does not",
            "expose nested virtualization. Enable it on the hypervisor side,",
            "or use host mode as is.",
        ]
    return [
        "Could not tell whether this host is physical or virtual.",
        "If physical: enable VT-x / AMD-V in the BIOS.",
        "If a VM: the hypervisor must expose nested virtualization.",
    ]


@dataclass
class Diagnosis:
    """진단 결과. rows 는 (라벨, 상태, 설명), 상태는 ok/fail/info."""

    rows: list[tuple[str, str, str]] = field(default_factory=list)
    host_type: str = ""
    capable: bool = False
    failure: str = ""  # capable=False 일 때의 한 줄 요약
    hints: list[str] = field(default_factory=list)


def diagnose(facts: HostFacts) -> Diagnosis:
    result = Diagnosis(host_type=host_type(facts))

    # 컨테이너는 다른 모든 판정에 앞선다 — VM 도 host 모드도 정상이 아니다.
    if _in_container(facts):
        result.rows.append(("Host type", "fail", result.host_type))
        result.failure = "this is a container, not a host"
        result.hints = [
            "SSHerpa manages hosts, and a container cannot run VMs or systemd",
            "services properly. Point it at the real machine instead.",
        ]
        return result

    ok = True

    # ① CPU 가상화 — 유일하게 협상 불가능한 검사
    if facts.cpu_flag == "vmx":
        result.rows.append(("CPU virtualization", "ok", "vmx (Intel VT-x)"))
    elif facts.cpu_flag == "svm":
        result.rows.append(("CPU virtualization", "ok", "svm (AMD-V)"))
    else:
        result.rows.append(
            ("CPU virtualization", "fail", "no vmx/svm in /proc/cpuinfo")
        )
        ok = False
        result.failure = "this host cannot create VMs"
        result.hints = _virtualization_hints(facts)

    # ② /dev/kvm — CPU 가 '할 수 있다'와 커널이 '준비했다'는 다르다
    if facts.kvm_device:
        result.rows.append(("/dev/kvm", "ok", "present"))
    elif facts.cpu_flag:
        module = "kvm_intel" if facts.cpu_flag == "vmx" else "kvm_amd"
        result.rows.append(("/dev/kvm", "fail", "missing — kvm module not loaded"))
        if ok:
            ok = False
            result.failure = "KVM is not available"
            result.hints = [
                "The CPU supports virtualization but the kernel module is not loaded:",
                f"    sudo modprobe {module}",
            ]
    else:
        # vmx 가 없으면 /dev/kvm 이 없는 건 당연한 결과 — 원인 쪽만 처방한다
        result.rows.append(("/dev/kvm", "fail", "missing"))

    # ③ libvirt — 없어도 실패가 아니다. VM 셋업이 설치할 것이다.
    if facts.virsh_version:
        state = facts.libvirtd or "state unknown"
        result.rows.append(("libvirt", "ok", f"v{facts.virsh_version} ({state})"))
    else:
        result.rows.append(("libvirt", "info", "not installed (vm setup will install it)"))

    # ④ 메모리 — 모자라면 VM 이 떠도 OOM 으로 이상하게 죽는다
    if facts.memory_mb is None:
        result.rows.append(("Memory", "info", "could not read /proc/meminfo"))
    elif facts.memory_mb >= MIN_MEMORY_MB:
        capacity = (facts.memory_mb - HOST_RESERVE_MB) // PER_VM_MB
        result.rows.append(
            (
                "Memory",
                "ok",
                f"{facts.memory_mb / 1024:.1f} GB — fits ~{capacity} × 2 GB VMs",
            )
        )
    else:
        result.rows.append(
            (
                "Memory",
                "fail",
                f"{facts.memory_mb / 1024:.1f} GB — needs at least "
                f"{MIN_MEMORY_MB / 1024:.1f} GB for one 2 GB VM",
            )
        )
        if ok:
            ok = False
            result.failure = "not enough memory for VMs"
            result.hints = ["Resize the host, or use host mode which needs less."]

    # ⑤ 디스크 — 가득참은 스냅샷이 반쯤 쓰이다 깨지는 최악의 실패 양상
    if facts.disk_free_gb is None:
        result.rows.append(("Disk", "info", "could not read free space"))
    elif facts.disk_free_gb >= MIN_DISK_GB:
        result.rows.append(("Disk", "ok", f"{facts.disk_free_gb:.0f} GB free"))
    else:
        # 반올림하면 "15 GB free — needs 15 GB" 같은 모순 문구가 된다 (실측)
        result.rows.append(
            (
                "Disk",
                "fail",
                f"{facts.disk_free_gb:.1f} GB free — needs at least {MIN_DISK_GB} GB "
                "for images and snapshots",
            )
        )
        if ok:
            ok = False
            result.failure = "not enough disk space for VM images"
            result.hints = ["Free up space under /var/lib, or attach a larger disk."]

    result.capable = ok
    return result
