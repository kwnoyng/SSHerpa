"""VM 생성·제거 — 명령 조립, 출력 파싱, create/destroy 흐름.

실제 부팅은 GCP 호스트에서 검증하고, 여기서는 명령이 올바른지와
멱등성·안전장치를 가짜 run 으로 검증한다.
"""

import pytest

from ssherpa import vm
from ssherpa.ssh import CommandResult, Target

TARGET = Target(name="gcp-lab", host="34.22.85.249", user="ssherpa")
PUBKEY = "ssh-ed25519 AAAATESTKEY ssherpa-vm"

# virsh 가 실제로 만들어내는 표 형태
DOMIFLIST = (
    " Interface   Type      Source    Model    MAC\n"
    "-----------------------------------------------------------\n"
    " vnet0       network   default   virtio   52:54:00:AB:CD:EF\n"
)

LEASES = (
    " Expiry Time           MAC address         Protocol   IP address           Hostname\n"
    "----------------------------------------------------------------------------------\n"
    " 2026-08-05 12:00:00   52:54:00:ab:cd:ef   ipv4       192.168.122.10/24    ssherpa-node-1\n"
)


class TestUserData:
    def test_is_cloud_config(self):
        # 첫 줄이 #cloud-config 가 아니면 cloud-init 이 통째로 무시한다
        assert vm.user_data("ssherpa-node-1", PUBKEY).startswith("#cloud-config\n")

    def test_injects_account_key_and_sudo(self):
        text = vm.user_data("ssherpa-node-1", PUBKEY)
        assert "name: ssherpa" in text
        assert PUBKEY in text
        assert "NOPASSWD:ALL" in text

    def test_hostname_matches_vm_name(self):
        assert "hostname: ssherpa-node-1" in vm.user_data("ssherpa-node-1", PUBKEY)


class TestParsing:
    def test_mac_from_domiflist(self):
        assert vm.parse_mac(DOMIFLIST) == "52:54:00:ab:cd:ef"

    def test_mac_missing(self):
        assert vm.parse_mac("no table here") == ""

    def test_ip_from_leases(self):
        assert vm.parse_lease_ip(LEASES, "52:54:00:ab:cd:ef") == "192.168.122.10"

    def test_other_vm_leases_are_ignored(self):
        assert vm.parse_lease_ip(LEASES, "52:54:00:00:00:01") == ""

    def test_empty_mac_never_matches(self):
        # mac 을 못 읽었을 때 "" 로 검색하면 아무 행이나 걸린다 — 막아야 한다
        assert vm.parse_lease_ip(LEASES, "") == ""


class TestSteps:
    def test_download_is_rerun_safe(self):
        command = vm.download_step().command
        assert f"test -f {vm.BASE_IMAGE} || " in command

    def test_download_validates_before_moving_into_place(self):
        # 받다 만 파일이 base 가 되면 이후 모든 VM 이 조용히 깨진다
        command = vm.download_step().command
        assert ".tmp" in command
        assert command.index("qemu-img info") < command.index("mv ")

    def test_disk_is_thin_backed_by_base(self):
        command = vm.disk_step(vm.VmSpec()).command
        assert f"-b {vm.BASE_IMAGE}" in command
        assert "-F qcow2" in command
        assert "10G" in command

    def test_seed_volume_label_is_cidata_on_fallback(self):
        # cloud-init 은 'cidata' 라벨로 seed 를 알아본다 — xorriso 경로에서
        # 라벨이 빠지면 계정이 안 생기고 로그인 불가 VM 이 된다
        command = vm.seed_step(vm.VmSpec(), PUBKEY).command
        assert "cloud-localds" in command
        assert "-volid cidata" in command

    def test_boot_uses_spec_and_never_opens_a_console(self):
        spec = vm.VmSpec(name="ssherpa-node-1", memory_mb=4096, vcpus=4)
        command = vm.boot_step(spec).command
        assert "--memory 4096" in command
        assert "--vcpus 4" in command
        assert "--noautoconsole" in command  # 콘솔이 열리면 자동화가 멈춘다
        assert "--import" in command  # 설치 마법사 없이 완제품 디스크로 부팅
        assert "network=default" in command

    def test_vm_survives_host_reboot(self):
        assert "--autostart" in vm.boot_step(vm.VmSpec()).command


class TestSpecDefaults:
    def test_memory_matches_doctor_estimate(self):
        # doctor 가 "~N × 2 GB VMs" 라고 추정한 그 상수와 같아야 한다
        from ssherpa.doctor import PER_VM_MB

        assert vm.VmSpec().memory_mb == PER_VM_MB

    def test_name_carries_the_ownership_prefix(self):
        assert vm.VmSpec().name.startswith(vm.VM_PREFIX)


class FakeHost:
    """virsh 명령에 준비된 응답을 주고 실행 내역을 기록한다."""

    def __init__(self, state="", leases=LEASES):
        self.state = state  # "" 이면 VM 없음
        self.leases = leases
        self.commands: list[str] = []

    def run(self, target, command, timeout=30):  # noqa: ARG002
        self.commands.append(command)
        if "virsh list" in command:
            return CommandResult(0, "ssherpa-node-1\n" if self.state else "", "")
        if "virsh domstate" in command:
            if self.state:
                return CommandResult(0, f"{self.state}\n", "")
            return CommandResult(1, "", "error: failed to get domain")
        if "virsh domiflist" in command:
            return CommandResult(0, DOMIFLIST, "")
        if "net-dhcp-leases" in command:
            return CommandResult(0, self.leases, "")
        return CommandResult(0, "", "")

    def ran(self, fragment):
        return any(fragment in command for command in self.commands)


@pytest.fixture
def host(monkeypatch):
    def build(state="", leases=LEASES):
        fake = FakeHost(state, leases)
        monkeypatch.setattr(vm, "run", fake.run)
        monkeypatch.setattr(vm, "ensure_local_key", lambda: PUBKEY)
        return fake

    return build


class TestCreateFlow:
    def test_fresh_create_walks_every_step(self, host):
        fake = host(state="")
        info = vm.create(TARGET)
        assert info.already_existed is False
        assert info.ip == "192.168.122.10"
        assert info.mac == "52:54:00:ab:cd:ef"
        assert fake.ran("curl")
        assert fake.ran("qemu-img create")
        assert fake.ran("virt-install")

    def test_running_vm_is_reused_untouched(self, host):
        fake = host(state="running")
        info = vm.create(TARGET)
        assert info.already_existed is True
        assert not fake.ran("virt-install")
        assert not fake.ran("curl")
        assert not fake.ran("virsh start")

    def test_stopped_vm_is_started_not_recreated(self, host):
        fake = host(state="shut off")
        info = vm.create(TARGET)
        assert info.already_existed is True
        assert fake.ran("virsh start ssherpa-node-1")
        assert not fake.ran("virt-install")

    def test_missing_nic_fails_with_inspection_hint(self, host, monkeypatch):
        fake = host(state="")

        def no_nic_run(target, command, timeout=30):
            if "domiflist" in command:
                return CommandResult(0, "no interfaces", "")
            return fake.run(target, command, timeout)

        monkeypatch.setattr(vm, "run", no_nic_run)
        with pytest.raises(vm.VmError) as excinfo:
            vm.create(TARGET)
        assert "network interface" in excinfo.value.message

    def test_step_failure_surfaces_remote_stderr(self, host, monkeypatch):
        fake = host(state="")

        def failing_run(target, command, timeout=30):
            if "virt-install" in command:
                return CommandResult(1, "", "ERROR unknown OS name 'ubuntu24.04'")
            return fake.run(target, command, timeout)

        monkeypatch.setattr(vm, "run", failing_run)
        with pytest.raises(vm.VmError) as excinfo:
            vm.create(TARGET)
        assert any("unknown OS name" in hint for hint in excinfo.value.hints)


class TestApiExposure:
    """내 PC 의 kubectl 은 NAT 안의 VM 에 못 간다 — 호스트가 6443 을 넘겨준다."""

    def test_forwards_host_port_to_the_vm(self):
        command = vm.expose_api_step("192.168.122.160").command
        assert "--dport 6443" in command
        assert "--to-destination 192.168.122.160:6443" in command

    def test_rules_are_tagged_as_ours(self):
        # 표식 없는 규칙은 지울 방법이 없고, 남의 규칙을 지워서도 안 된다
        assert "ssherpa-api" in vm.expose_api_step("h").command

    def test_stale_rules_are_cleaned_before_adding(self):
        # VM 을 다시 만들면 IP 가 바뀐다 (실측 .250 → .160). 옛 규칙이
        # 남아 있으면 그쪽이 먼저 걸려 kubectl 이 죽은 VM 으로 간다.
        command = vm.expose_api_step("192.168.122.160").command
        assert command.index("iptables-save") < command.index("--to-destination")

    def test_forward_rule_is_inserted_before_libvirt_reject(self):
        assert "-I FORWARD 1" in vm.expose_api_step("h").command

    def test_unexpose_removes_but_never_adds(self):
        command = vm.unexpose_api_step().command
        assert "ssherpa-api" in command
        assert "-A PREROUTING" not in command

    def test_port_matches_cluster_constant(self):
        from ssherpa.cluster import API_PORT

        assert str(API_PORT) in vm.expose_api_step("h").command


class TestListVms:
    def test_filters_to_ssherpa_prefix(self, monkeypatch):
        # 고객사 호스트에는 남의 VM 이 있을 수 있다 — 우리 것만 보인다
        out = " ssherpa-node-1\ncustomer-db\n\n"
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, out, "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET) == ["ssherpa-node-1"]

    def test_host_without_virsh_reads_as_no_vms(self, monkeypatch):
        # host 모드만 쓰는 호스트에는 virsh 자체가 없다 — 오류가 아니다
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, "", "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET) == []


class TestVmTarget:
    """VM 을 여느 target 처럼 — 기존 부품 전부가 이 Target 으로 동작해야 한다."""

    INFO = vm.VmInfo(
        name="ssherpa-node-1", ip="192.168.122.160",
        mac="52:54:00:b3:ab:70", already_existed=False,
    )

    def test_reaches_the_vm_through_the_host(self):
        built = vm.vm_target(TARGET, self.INFO)
        assert built.host == "192.168.122.160"
        assert built.jump == "ssherpa@34.22.85.249"

    def test_jump_keeps_the_host_port(self):
        host = Target(name="lab", host="10.0.0.1", user="admin", port=2222)
        assert vm.vm_target(host, self.INFO).jump == "admin@10.0.0.1:2222"

    def test_uses_the_dedicated_key_and_cloud_init_account(self):
        built = vm.vm_target(TARGET, self.INFO)
        assert built.user == "ssherpa"  # user_data 가 만드는 계정과 같아야 한다
        assert built.key == str(vm.local_key_paths()[0])

    def test_name_says_where_it_lives(self):
        assert vm.vm_target(TARGET, self.INFO).name == "gcp-lab/ssherpa-node-1"


class TestFind:
    """ssherpa ssh --vm 이 쓰는 조회 — IP·이름을 사용자 대신 알아낸다."""

    def test_no_vm_returns_none(self, host):
        host(state="")
        assert vm.find(TARGET) is None

    def test_running_vm_comes_back_with_its_ip(self, host):
        host(state="running")
        info = vm.find(TARGET)
        assert info.name == "ssherpa-node-1"
        assert info.ip == "192.168.122.10"

    def test_stopped_vm_points_at_up(self, host):
        # 꺼진 VM 은 IP 가 없다 — 켜는 법(up --vm)을 알려주고 실패한다
        host(state="shut off")
        with pytest.raises(vm.VmError) as excinfo:
            vm.find(TARGET)
        assert any("up gcp-lab --vm" in hint for hint in excinfo.value.hints)


class TestDestroy:
    def test_refuses_vms_it_does_not_own(self, host):
        # 사용자가 손으로 만든 VM 은 이름이 뭐든 우리 소관이 아니다
        fake = host(state="running")
        with pytest.raises(vm.VmError) as excinfo:
            vm.destroy(TARGET, "customer-database")
        assert "refusing" in excinfo.value.message
        assert not fake.ran("undefine")

    def test_absent_vm_returns_false(self, host):
        host(state="")
        assert vm.destroy(TARGET, "ssherpa-node-1") is False

    def test_removes_definition_and_storage(self, host, monkeypatch):
        fake = host(state="running")
        # 제거 후의 domstate 는 '없음' 이어야 검증을 통과한다
        original = fake.run

        def run_then_gone(target, command, timeout=30):
            result = original(target, command, timeout)
            if "undefine" in command:
                fake.state = ""
            return result

        monkeypatch.setattr(vm, "run", run_then_gone)
        assert vm.destroy(TARGET, "ssherpa-node-1") is True
        assert fake.ran("undefine")
        assert fake.ran(f"rm -rf {vm.VM_ROOT}/ssherpa-node-1")

    def test_distrusts_exit_codes(self, host):
        # undefine 이 0 을 돌려줘도 VM 이 남아 있으면 실패 선언
        host(state="running")
        with pytest.raises(vm.VmError) as excinfo:
            vm.destroy(TARGET, "ssherpa-node-1")
        assert "still defined" in excinfo.value.message
