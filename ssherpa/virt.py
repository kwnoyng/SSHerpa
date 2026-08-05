"""VM 기반(QEMU + libvirt) 준비.

doctor 가 "이 호스트가 VM 을 만들 수 있나"를 진단한다면, 이 모듈은 실제로
그 능력을 켠다 — QEMU 와 libvirt 를 설치하고, 데몬을 올리고, VM 이 쓸
기본 NAT 네트워크(virbr0)를 살린다.

vm 모드 up 의 첫 단계로 쓰일 내부 모듈이며 단독 명령으로 노출하지 않는다.
사용자가 libvirt 의 존재를 몰라도 되게 하는 것이 목표다.
"""

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Optional

from . import doctor
from . import facts as facts_mod
from .distro import Step
from .ssh import Target, run

# apt/dnf 는 미러 상태에 따라 수 분이 걸릴 수 있다.
INSTALL_TIMEOUT = 600

# family -> 설치할 패키지. 이름만 다르고 내용물은 같다.
#   qemu-kvm             VM 프로세스를 만드는 QEMU 본체
#   libvirt(-daemon-*)   관리 데몬 libvirtd
#   *-client(s)          virsh CLI
#   virt-install(=virtinst)  이미지에서 VM 을 만드는 표준 도구 (VM 생성 단계)
#   qemu-img(=qemu-utils)    qcow2 디스크 생성·복제 (VM 생성 단계)
#   cloud-image-utils/xorriso  cloud-init 초기설정 ISO 제작 (VM 생성 단계)
#
# RedHat 계열은 아직 실기 검증 전이다(검증 캠페인은 별도 과제). RHEL 9 는
# genisoimage 를 걷어냈으므로 seed ISO 도구는 xorriso 를 쓴다.
PACKAGES: dict[str, list[str]] = {
    "Debian": [
        "qemu-kvm",
        "libvirt-daemon-system",
        "libvirt-clients",
        "virtinst",
        "qemu-utils",
        "cloud-image-utils",
    ],
    "RedHat": [
        "qemu-kvm",
        "libvirt",
        "libvirt-client",
        "virt-install",
        "qemu-img",
        "xorriso",
    ],
}

# doctor 의 능력 검사에 os-release 를 덧붙여 접속 한 번으로 끝낸다.
SETUP_PROBE = (
    doctor.DOCTOR_PROBE
    + '; echo "SSHERPA_OSRELEASE"; cat /etc/os-release 2>/dev/null'
)

# 설치 후 상태를 마커로 받아온다. virsh 는 qemu:///system 을 봐야 하므로
# 네트워크 확인에는 sudo 가 필요하다 (비 root 는 자기만의 세션을 본다).
VERIFY_PROBE = (
    'echo "SSHERPA_VIRSH=$(virsh --version 2>/dev/null || echo absent)"; '
    'echo "SSHERPA_LIBVIRTD=$(systemctl is-active libvirtd 2>/dev/null || true)"; '
    'echo "SSHERPA_NET=$(sudo virsh net-list --name 2>/dev/null '
    '| grep -qx default && echo active || echo inactive)"'
)


class VirtError(Exception):
    """VM 기반 준비 중의 오류. hints 는 해결 방법."""

    def __init__(self, message: str, hints: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.hints = hints or []


def install_step(family: str) -> Step:
    packages = " ".join(PACKAGES[family])
    if family == "Debian":
        # 프롬프트가 뜨면 SSH 너머에서 영원히 멈춘다 — 비대화형을 강제한다.
        command = (
            "sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive "
            f"apt-get install -y -qq {packages}"
        )
    else:
        command = f"sudo dnf install -y -q {packages}"
    return Step(label="install qemu + libvirt", command=command, timeout=INSTALL_TIMEOUT)


def enable_step() -> Step:
    return Step(label="enable libvirtd", command="sudo systemctl enable --now libvirtd")


def network_step() -> Step:
    """기본 NAT 네트워크를 켜고 부팅 시 자동 시작으로 만든다.

    net-start 는 이미 켜진 네트워크에 오류를 돌려주므로, 활성 목록에 없을
    때만 켠다 — 그래야 재실행이 안전하다. autostart 는 반복해도 무해하다.
    """
    return Step(
        label="start default network",
        command=(
            "sudo virsh net-autostart default >/dev/null && "
            "(sudo virsh net-list --name | grep -qx default || "
            "sudo virsh net-start default)"
        ),
    )


@dataclass
class VirtState:
    """VERIFY_PROBE 출력을 해석한 상태."""

    virsh_version: str = ""  # "" 이면 미설치
    libvirtd: str = ""  # active / inactive / ""
    network: str = ""  # active / inactive / ""


def parse_verify(stdout: str) -> VirtState:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        if line.startswith("SSHERPA_") and "=" in line:
            key, _, value = line.partition("=")
            fields[key.removeprefix("SSHERPA_")] = value.strip()

    virsh = fields.get("VIRSH", "")
    return VirtState(
        virsh_version="" if virsh in ("", "absent") else virsh,
        libvirtd=fields.get("LIBVIRTD", ""),
        network=fields.get("NET", ""),
    )


class _NullReporter:
    def step(self, label: str):  # noqa: ARG002
        return nullcontext()


def _run_step(target: Target, step: Step) -> None:
    result = run(target, step.command, timeout=step.timeout)
    if result.rc != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise VirtError(
            f"step failed: {step.label}",
            detail[-3:] if detail else ["No output from the remote command."],
        )


@dataclass
class SetupResult:
    already_installed: bool  # virsh 가 이미 있었다 (패키지 설치를 건너뜀)
    virsh_version: str


def setup(target: Target, reporter=None) -> SetupResult:
    """호스트에 VM 기반을 준비한다. 재실행해도 안전하다(멱등).

    능력이 안 되는 호스트(중첩 가상화 꺼짐, 메모리 부족 등)는 설치를
    시작하기 전에 doctor 의 진단과 처방을 그대로 들고 실패한다 —
    반쯤 설치된 호스트를 남기지 않는다.
    """
    reporter = reporter or _NullReporter()

    with reporter.step("preflight"):
        probe = run(target, SETUP_PROBE, timeout=60)
        head, _, os_text = probe.stdout.partition("SSHERPA_OSRELEASE")
        facts = doctor.parse_probe(head)

        diagnosis = doctor.diagnose(facts)
        if not diagnosis.capable:
            raise VirtError(
                f"this host cannot run VMs — {diagnosis.failure}", diagnosis.hints
            )

        already = bool(facts.virsh_version)
        os_info = facts_mod.detect(os_text)
        if not already and os_info.family not in PACKAGES:
            described = os_info.pretty_name or os_info.id or "unknown"
            raise VirtError(
                f"cannot install libvirt automatically on this OS ({described})",
                [
                    "Automatic install covers Debian/Ubuntu and RHEL-family hosts.",
                    "Install QEMU + libvirt yourself, then run again — an existing",
                    "install is detected and used as is.",
                ],
            )

    if not already:
        step = install_step(os_info.family)
        with reporter.step(step.label):
            _run_step(target, step)

    for step in (enable_step(), network_step()):
        with reporter.step(step.label):
            _run_step(target, step)

    # 설치 명령들의 종료 코드 0 을 믿지 않는다 (rke2 제거 사건의 교훈).
    # virsh 응답·데몬·네트워크가 실제로 살아 있어야 성공을 선언한다.
    with reporter.step("verify"):
        state = parse_verify(run(target, VERIFY_PROBE, timeout=60).stdout)
        problems = []
        if not state.virsh_version:
            problems.append("virsh does not answer")
        if state.libvirtd != "active":
            problems.append(f"libvirtd is {state.libvirtd or 'in an unknown state'}")
        if state.network != "active":
            problems.append("the default NAT network is not active")
        if problems:
            raise VirtError(
                "the VM foundation did not come up",
                problems
                + [f"Inspect the host:  ssherpa ssh {target.name or ''}".rstrip()],
            )

    return SetupResult(already_installed=already, virsh_version=state.virsh_version)
