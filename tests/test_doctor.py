"""doctor 진단 — 파싱과 판정 매트릭스.

판정은 오직 능력(vmx, /dev/kvm, 메모리, 디스크)으로 하고, 가상화 종류
감지는 실패 처방의 문구에만 쓰인다. 같은 'vmx 없음'이라도 물리 서버는
BIOS, GCP 는 머신 계열, vSphere 는 관리자 요청으로 처방이 갈려야 한다.
"""

import pytest

from ssherpa import doctor


def probe_output(
    virt="none",
    container="none",
    vendor="ASUSTeK COMPUTER INC.",
    product="PRIME B650",
    cpuflag="vmx",
    kvm="yes",
    virsh="absent",
    libvirtd="",
    mem_kb=16384000,
    disk_bytes=200 * 1024**3,
):
    """DOCTOR_PROBE 가 만들어내는 형태의 stdout 을 조립한다."""
    return (
        f"SSHERPA_VIRT={virt}\n"
        f"SSHERPA_CONTAINER={container}\n"
        f"SSHERPA_VENDOR={vendor}\n"
        f"SSHERPA_PRODUCT={product}\n"
        f"SSHERPA_CPUFLAG={cpuflag}\n"
        f"SSHERPA_KVM={kvm}\n"
        f"SSHERPA_VIRSH={virsh}\n"
        f"SSHERPA_LIBVIRTD={libvirtd}\n"
        f"SSHERPA_MEM=MemTotal:       {mem_kb} kB\n"
        f"SSHERPA_DISK={disk_bytes}\n"
        f"SSHERPA_DISK2={disk_bytes}\n"
    )


# gcp-lab (e2-medium) 실측 출력 그대로
GCP_E2 = probe_output(
    virt="google",
    vendor="Google",
    product="Google Compute Engine",
    cpuflag="",
    kvm="no",
    mem_kb=4030888,
    disk_bytes=15 * 1024**3,
)


def diagnose(text):
    return doctor.diagnose(doctor.parse_probe(text))


class TestParse:
    def test_parses_all_fields(self):
        facts = doctor.parse_probe(probe_output(virsh="10.0.0", libvirtd="active"))
        assert facts.virt == "none"
        assert facts.cpu_flag == "vmx"
        assert facts.kvm_device is True
        assert facts.virsh_version == "10.0.0"
        assert facts.libvirtd == "active"
        assert facts.memory_mb == 16000
        assert facts.disk_free_gb == pytest.approx(200, abs=1)

    def test_missing_everything_does_not_crash(self):
        facts = doctor.parse_probe("garbage\nno markers here")
        assert facts.memory_mb is None
        assert facts.disk_free_gb is None
        assert facts.cpu_flag == ""

    def test_disk_falls_back_when_libvirt_dir_missing(self):
        text = probe_output().replace(
            f"SSHERPA_DISK={200 * 1024**3}", "SSHERPA_DISK="
        )
        facts = doctor.parse_probe(text)
        assert facts.disk_free_gb == pytest.approx(200, abs=1)


class TestCapableHost:
    """능력이 되면 클라우드든 물리든 통과한다 — 위치는 판정 기준이 아니다."""

    def test_physical_machine_passes(self):
        result = diagnose(probe_output())
        assert result.capable is True
        assert "physical machine" in result.host_type
        assert "ASUSTeK" in result.host_type

    def test_cloud_vm_with_nested_passes(self):
        # GCP N2 + 중첩 활성화 시나리오: 클라우드 VM 이지만 vmx 가 보인다
        result = diagnose(
            probe_output(virt="google", vendor="Google", product="Google Compute Engine")
        )
        assert result.capable is True
        assert "virtual machine (google" in result.host_type

    def test_amd_flag_counts_too(self):
        result = diagnose(probe_output(cpuflag="svm"))
        assert result.capable is True
        assert any("AMD-V" in detail for _, _, detail in result.rows)

    def test_memory_capacity_estimate_is_shown(self):
        result = diagnose(probe_output(mem_kb=16384000))  # 16 GB
        memory_row = next(r for r in result.rows if r[0] == "Memory")
        assert "fits ~7" in memory_row[2]  # (16000-1500)//2048 = 7


class TestVirtualizationHints:
    """같은 실패, 환경별로 다른 처방 — doctor 의 존재 이유."""

    def test_gcp_e2_real_output(self):
        # 실측 재현: 판정 실패 + GCP 맞춤 처방 + host 모드 안내.
        # 'N2 인데 플래그만 꺼진' 경우도 같은 처방으로 해결돼야 한다 (실화)
        result = diagnose(GCP_E2)
        assert result.capable is False
        assert "cannot create VMs" in result.failure
        joined = "\n".join(result.hints)
        assert "GCP" in joined
        # update --enable-nested-virtualization 은 존재하지 않는 플래그다(실측).
        # 실제로 성공한 export → update-from-file 흐름을 그대로 제공해야 한다.
        assert "enableNestedVirtualization" in joined
        assert "update-from-file" in joined
        assert "host mode" in joined

    def test_physical_with_bios_off(self):
        result = diagnose(probe_output(cpuflag="", kvm="no"))
        joined = "\n".join(diagnose(probe_output(cpuflag="", kvm="no")).hints)
        assert result.capable is False
        assert "BIOS" in joined

    def test_aws_ec2(self):
        result = diagnose(
            probe_output(
                virt="amazon", vendor="Amazon EC2", product="t3.medium", cpuflag="", kvm="no"
            )
        )
        joined = "\n".join(result.hints)
        assert "*.metal" in joined

    def test_hyperv(self):
        result = diagnose(
            probe_output(virt="microsoft", vendor="Microsoft Corporation",
                         product="Virtual Machine", cpuflag="", kvm="no")
        )
        joined = "\n".join(result.hints)
        assert "Set-VMProcessor" in joined

    def test_vmware(self):
        result = diagnose(
            probe_output(virt="vmware", vendor="VMware, Inc.",
                         product="VMware Virtual Platform", cpuflag="", kvm="no")
        )
        joined = "\n".join(result.hints)
        assert "vSphere" in joined

    def test_unknown_hypervisor_gets_generic_hint(self):
        result = diagnose(probe_output(virt="xen", cpuflag="", kvm="no"))
        joined = "\n".join(result.hints)
        assert "hypervisor" in joined

    def test_detection_failure_still_judges_by_capability(self):
        # systemd-detect-virt 가 없어도 판정은 능력 기준으로 이뤄진다
        result = diagnose(probe_output(virt="", cpuflag="", kvm="no"))
        assert result.capable is False
        joined = "\n".join(result.hints)
        assert "BIOS" in joined and "hypervisor" in joined  # 양쪽 다 안내


class TestOtherFailures:
    def test_container_is_refused_outright(self):
        result = diagnose(probe_output(container="docker"))
        assert result.capable is False
        assert "container" in result.failure
        assert "container (docker)" in result.host_type

    def test_kvm_module_not_loaded(self):
        # CPU 는 되는데 커널 모듈이 안 올라온 경우 — modprobe 처방
        result = diagnose(probe_output(cpuflag="vmx", kvm="no"))
        assert result.capable is False
        joined = "\n".join(result.hints)
        assert "modprobe kvm_intel" in joined

    def test_kvm_module_hint_matches_amd(self):
        result = diagnose(probe_output(cpuflag="svm", kvm="no"))
        assert "modprobe kvm_amd" in "\n".join(result.hints)

    def test_low_memory(self):
        result = diagnose(probe_output(mem_kb=2035224))  # 2 GB
        assert result.capable is False
        assert "not enough memory" in result.failure

    def test_low_disk(self):
        result = diagnose(probe_output(disk_bytes=5 * 1024**3))
        assert result.capable is False
        assert "disk" in result.failure

    def test_first_failure_wins_the_summary(self):
        # vmx 없음 + 메모리 부족이 겹치면 근본 원인(vmx) 쪽 처방이 나와야 한다
        result = diagnose(probe_output(cpuflag="", kvm="no", mem_kb=2035224))
        assert "cannot create VMs" in result.failure


class TestLibvirtIsInformational:
    def test_absent_is_not_a_failure(self):
        result = diagnose(probe_output(virsh="absent"))
        assert result.capable is True
        libvirt_row = next(r for r in result.rows if r[0] == "libvirt")
        assert libvirt_row[1] == "info"

    def test_installed_and_running(self):
        result = diagnose(probe_output(virsh="10.0.0", libvirtd="active"))
        libvirt_row = next(r for r in result.rows if r[0] == "libvirt")
        assert libvirt_row[1] == "ok"
        assert "10.0.0" in libvirt_row[2]
