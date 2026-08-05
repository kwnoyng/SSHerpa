"""VM 기반(libvirt) 준비 — 명령 조립과 setup 흐름.

실제 설치는 GCP 호스트에서 검증하고, 여기서는 명령이 올바르게 조립되는지와
setup 이 조기실패·멱등 원칙을 지키는지를 가짜 run 으로 검증한다.
"""

import pytest

from ssherpa import virt
from ssherpa.ssh import CommandResult, Target

TARGET = Target(name="gcp-lab", host="34.50.34.61", user="ssherpa")


def setup_probe_output(
    cpuflag="vmx",
    kvm="yes",
    virsh="absent",
    mem_kb=16384000,
    os_id="ubuntu",
    id_like="debian",
):
    """SETUP_PROBE(doctor 검사 + os-release) 형태의 stdout 을 조립한다."""
    return (
        "SSHERPA_VIRT=none\n"
        "SSHERPA_CONTAINER=none\n"
        "SSHERPA_VENDOR=V\n"
        "SSHERPA_PRODUCT=P\n"
        f"SSHERPA_CPUFLAG={cpuflag}\n"
        f"SSHERPA_KVM={kvm}\n"
        f"SSHERPA_VIRSH={virsh}\n"
        "SSHERPA_LIBVIRTD=\n"
        f"SSHERPA_MEM=MemTotal:       {mem_kb} kB\n"
        f"SSHERPA_DISK={200 * 1024**3}\n"
        f"SSHERPA_DISK2={200 * 1024**3}\n"
        "SSHERPA_OSRELEASE\n"
        f"ID={os_id}\n"
        f'ID_LIKE="{id_like}"\n'
        'VERSION_ID="24.04"\n'
    )


def verify_output(virsh="10.0.0", libvirtd="active", net="active"):
    return (
        f"SSHERPA_VIRSH={virsh}\n"
        f"SSHERPA_LIBVIRTD={libvirtd}\n"
        f"SSHERPA_NET={net}\n"
    )


class FakeHost:
    """setup 이 날리는 원격 명령을 기록하고 준비된 응답을 돌려준다."""

    def __init__(self, probe_out, verify_out=None):
        self.probe_out = probe_out
        self.verify_out = verify_out or verify_output()
        self.commands: list[str] = []

    def run(self, target, command, timeout=30):  # noqa: ARG002
        self.commands.append(command)
        if "SSHERPA_OSRELEASE" in command:
            return CommandResult(0, self.probe_out, "")
        if "SSHERPA_NET=" in command:
            return CommandResult(0, self.verify_out, "")
        return CommandResult(0, "", "")

    def ran(self, fragment):
        return any(fragment in command for command in self.commands)


@pytest.fixture
def host(monkeypatch):
    def build(probe_out, verify_out=None):
        fake = FakeHost(probe_out, verify_out)
        monkeypatch.setattr(virt, "run", fake.run)
        return fake

    return build


class TestPackages:
    def test_debian_installs_without_prompts(self):
        # 프롬프트가 뜨면 SSH 너머에서 영원히 멈춘다
        command = virt.install_step("Debian").command
        assert "apt-get install" in command
        assert "DEBIAN_FRONTEND=noninteractive" in command
        assert "-y" in command

    def test_redhat_uses_dnf(self):
        command = virt.install_step("RedHat").command
        assert "dnf install -y" in command
        assert "apt-get" not in command

    @pytest.mark.parametrize("family", ["Debian", "RedHat"])
    def test_next_phase_tools_are_included(self, family):
        # VM 생성 단계가 쓸 도구까지 지금 깔아둔다: VM 생성기, 디스크 도구,
        # cloud-init seed ISO 도구. 이름은 계열마다 다르다.
        packages = virt.PACKAGES[family]
        assert any(p in ("virtinst", "virt-install") for p in packages)
        assert any(p in ("qemu-utils", "qemu-img") for p in packages)
        assert any(p in ("cloud-image-utils", "xorriso") for p in packages)

    @pytest.mark.parametrize("family", ["Debian", "RedHat"])
    def test_install_allows_more_time_than_default(self, family):
        assert virt.install_step(family).timeout >= 300


class TestServiceAndNetworkSteps:
    def test_libvirtd_starts_now_and_on_boot(self):
        assert "enable --now libvirtd" in virt.enable_step().command

    def test_network_start_is_rerun_safe(self):
        # net-start 는 이미 켜진 네트워크에 오류를 돌려준다 — 활성 목록에
        # 없을 때만 켜야 setup 재실행이 안전하다.
        command = virt.network_step().command
        assert "net-autostart default" in command
        assert "grep -qx default || " in command
        assert "net-start default" in command

    def test_virsh_always_runs_as_root(self):
        # 비 root virsh 는 qemu:///session (자기만의 세계) 을 본다
        for fragment in virt.network_step().command.split("&&"):
            if "virsh" in fragment:
                assert "sudo virsh" in fragment


class TestParseVerify:
    def test_healthy_state(self):
        state = virt.parse_verify(verify_output())
        assert state.virsh_version == "10.0.0"
        assert state.libvirtd == "active"
        assert state.network == "active"

    def test_absent_virsh_reads_as_not_installed(self):
        assert virt.parse_verify(verify_output(virsh="absent")).virsh_version == ""

    def test_garbage_does_not_crash(self):
        state = virt.parse_verify("no markers at all")
        assert state.virsh_version == ""


class TestSetupFlow:
    def test_incapable_host_fails_before_touching_anything(self, host):
        # 능력 미달이면 doctor 의 진단·처방을 들고 즉시 실패한다 —
        # 반쯤 설치된 호스트를 남기지 않는다.
        fake = host(setup_probe_output(cpuflag="", kvm="no"))
        with pytest.raises(virt.VirtError) as excinfo:
            virt.setup(TARGET)
        assert "cannot run VMs" in excinfo.value.message
        assert excinfo.value.hints  # 환경별 처방이 그대로 전달된다
        assert not fake.ran("apt-get")

    def test_low_memory_also_fails_early(self, host):
        fake = host(setup_probe_output(mem_kb=2035224))
        with pytest.raises(virt.VirtError):
            virt.setup(TARGET)
        assert not fake.ran("apt-get")

    def test_fresh_ubuntu_install(self, host):
        fake = host(setup_probe_output())
        result = virt.setup(TARGET)
        assert result.already_installed is False
        assert fake.ran("apt-get install")
        assert fake.ran("enable --now libvirtd")
        assert fake.ran("net-autostart default")

    def test_rocky_goes_through_dnf(self, host):
        fake = host(setup_probe_output(os_id="rocky", id_like="rhel centos fedora"))
        virt.setup(TARGET)
        assert fake.ran("dnf install")
        assert not fake.ran("apt-get")

    def test_rerun_skips_package_install(self, host):
        # 멱등성: virsh 가 이미 있으면 패키지 설치를 건너뛰고
        # 데몬·네트워크·검증만 다시 밟는다.
        fake = host(setup_probe_output(virsh="10.0.0"))
        result = virt.setup(TARGET)
        assert result.already_installed is True
        assert not fake.ran("apt-get")
        assert fake.ran("enable --now libvirtd")

    def test_unknown_distro_without_virsh_fails(self, host):
        fake = host(setup_probe_output(os_id="alpine", id_like=""))
        with pytest.raises(virt.VirtError) as excinfo:
            virt.setup(TARGET)
        assert "cannot install libvirt automatically" in excinfo.value.message
        assert not fake.ran("apt-get") and not fake.ran("dnf")

    def test_unknown_distro_with_manual_install_proceeds(self, host):
        # 손으로 깔아둔 libvirt 는 그대로 존중한다 — 힌트에서 한 약속이다
        fake = host(setup_probe_output(os_id="alpine", id_like="", virsh="10.0.0"))
        result = virt.setup(TARGET)
        assert result.already_installed is True
        assert fake.ran("enable --now libvirtd")

    def test_verify_distrusts_exit_codes(self, host):
        # 설치 명령이 전부 0 을 줘도 데몬이 안 살아 있으면 실패 선언
        host(setup_probe_output(), verify_output(libvirtd="inactive"))
        with pytest.raises(virt.VirtError) as excinfo:
            virt.setup(TARGET)
        assert any("libvirtd" in hint for hint in excinfo.value.hints)

    def test_verify_requires_the_network(self, host):
        host(setup_probe_output(), verify_output(net="inactive"))
        with pytest.raises(virt.VirtError) as excinfo:
            virt.setup(TARGET)
        assert any("network" in hint for hint in excinfo.value.hints)

    def test_step_failure_surfaces_remote_stderr(self, host, monkeypatch):
        fake = host(setup_probe_output())

        def failing_run(target, command, timeout=30):  # noqa: ARG001
            if "apt-get" in command:
                return CommandResult(100, "", "E: Unable to locate package qemu-kvm")
            return fake.run(target, command, timeout)

        monkeypatch.setattr(virt, "run", failing_run)
        with pytest.raises(virt.VirtError) as excinfo:
            virt.setup(TARGET)
        assert "step failed" in excinfo.value.message
        assert any("Unable to locate" in hint for hint in excinfo.value.hints)
