"""VM 생성·제거 — 명령 조립, 출력 파싱, create/destroy 흐름.

실제 부팅은 GCP 호스트에서 검증하고, 여기서는 명령이 올바른지와
멱등성·안전장치를 가짜 run 으로 검증한다.
"""

import pytest

from ssherpa import vm
from ssherpa.ssh import CommandResult, SSHError, Target

TARGET = Target(name="gcp-lab", host="192.0.2.10", user="ssherpa")
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


class TestNodeNaming:
    """이름이 결정적이라 재실행이 재사용이 되고, teardown 이 우리 것만 고른다."""

    def test_names_are_numbered_from_one(self):
        assert vm.node_name(1) == "ssherpa-node-1"
        assert vm.node_name(3) == "ssherpa-node-3"

    def test_specs_for_three_nodes(self):
        specs = vm.specs_for(3)
        assert [s.name for s in specs] == [
            "ssherpa-node-1",
            "ssherpa-node-2",
            "ssherpa-node-3",
        ]

    def test_single_node_matches_the_default_spec(self):
        # 노드 1개는 --nodes 가 없던 시절과 같은 VM 이어야 한다 —
        # 이름이 달라지면 기존 사용자의 VM 이 재사용되지 않고 새로 생긴다
        assert vm.specs_for(1)[0].name == vm.VmSpec().name

    def test_every_node_carries_the_prefix(self):
        assert all(s.name.startswith(vm.VM_PREFIX) for s in vm.specs_for(5))

    def test_find_defaults_to_the_first_node(self, host):
        # `ssherpa ssh --vm` 은 server 로 들어가야 한다
        fake = host(state="running")
        fake.state = "running"
        assert vm.find(TARGET).name == "ssherpa-node-1"


class TestRecycledAddresses:
    """NAT 풀의 주소는 재활용되고, VM 은 매번 새 호스트 키를 갖는다.

    실사용에서 발생: 지웠다 다시 만든 VM 이 앞서 쓰인 주소를 받자
    'host key verification failed' 로 설치가 통째로 멈췄다.
    """

    def test_fresh_vm_forgets_the_old_key_for_its_address(self, host, monkeypatch):
        host(state="")
        forgotten = []
        monkeypatch.setattr(vm, "forget_host_key", forgotten.append)
        vm.create(TARGET)
        assert forgotten == ["192.168.122.10"]

    def test_reused_vm_keeps_its_key(self, host, monkeypatch):
        # 돌고 있던 VM 은 키가 그대로다 — 지우면 검증을 스스로 포기하는 셈
        host(state="running")
        forgotten = []
        monkeypatch.setattr(vm, "forget_host_key", forgotten.append)
        vm.create(TARGET)
        assert forgotten == []

    def test_forgetting_targets_our_file_only(self, monkeypatch, tmp_path):
        # 사용자의 ~/.ssh/known_hosts 는 우리가 손댈 파일이 아니다
        ours = tmp_path / "known_hosts"
        ours.write_text("192.168.122.10 ssh-ed25519 AAAA\n")
        monkeypatch.setattr(vm, "known_hosts_path", lambda: ours)

        calls = []
        monkeypatch.setattr(
            vm.subprocess, "run", lambda argv, **_k: calls.append(argv)
        )
        vm.forget_host_key("192.168.122.10")
        assert calls == [["ssh-keygen", "-R", "192.168.122.10", "-f", str(ours)]]

    def test_forgetting_is_harmless_without_a_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vm, "known_hosts_path", lambda: tmp_path / "nope")
        vm.forget_host_key("192.168.122.10")  # 예외 없이 지나가야 한다


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


class TestWaitForSsh:
    """주소를 받은 것과 접속을 받을 수 있는 것은 다른 사건이다.

    DHCP 임대는 cloud-init 의 network 단계에서 올라오고, ssherpa 계정과
    그 키는 그 다음 단계에서 만들어진다.

    여기서 기다리는 실패는 전부 run() 이 *예외로* 내는 종류다 — 연결
    단계에서 죽으면 ssh 는 255 를 주고 run() 은 그걸 값이 아니라
    SSHError 로 바꾼다. 가짜가 CommandResult(255, ...) 를 돌려주면 진짜
    보다 관대해져서, 한 번도 안 기다리는 루프가 초록불로 통과한다
    (실제로 그렇게 통과했다).
    """

    REFUSED = SSHError("connection refused (192.168.122.10:22)", ["..."])
    DENIED = SSHError("authentication failed", ["Check the key"])

    def raising(self, monkeypatch, *outcomes):
        """진짜 run() 처럼 — 연결 실패는 던지고, 붙었으면 값을 준다."""
        remaining = list(outcomes)
        sent = []

        def fake_run(target, command, timeout=30):  # noqa: ARG001
            sent.append(command)
            outcome = remaining.pop(0) if remaining else CommandResult(0, "", "")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(vm, "run", fake_run)
        monkeypatch.setattr(vm.time, "sleep", lambda _s: None)
        return sent, remaining

    def test_it_keeps_trying_while_the_vm_refuses(self, monkeypatch):
        # sshd 가 아직 안 떴다 -> connection refused
        _sent, remaining = self.raising(
            monkeypatch, self.REFUSED, self.REFUSED, CommandResult(0, "", "")
        )
        vm.wait_for_ssh(TARGET, "ssherpa-node-1")
        assert remaining == []  # 세 번째에서야 멈췄다

    def test_it_keeps_trying_while_the_account_is_missing(self, monkeypatch):
        # 계정이 아직 없다 -> permission denied. 둘 다 255 다.
        _sent, remaining = self.raising(
            monkeypatch, self.DENIED, CommandResult(0, "", "")
        )
        vm.wait_for_ssh(TARGET, "ssherpa-node-1")
        assert remaining == []

    def test_the_probe_changes_nothing_on_the_vm(self, monkeypatch):
        sent, _ = self.raising(monkeypatch, CommandResult(0, "", ""))
        vm.wait_for_ssh(TARGET, "ssherpa-node-1")
        assert sent == ["true"]

    def test_giving_up_blames_cloud_init_not_the_key(self, monkeypatch):
        # 여기서 인증 실패라고 안내하면 사용자는 있지도 않은 키 문제를 쫓는다
        self.raising(monkeypatch, self.DENIED)
        with pytest.raises(vm.VmError) as excinfo:
            vm.wait_for_ssh(TARGET, "ssherpa-node-1", timeout=0)
        assert "never accepted a connection" in excinfo.value.message
        assert any("cloud-init" in hint for hint in excinfo.value.hints)
        # 원격이 한 말도 버리지 않는다
        assert any("authentication failed" in hint for hint in excinfo.value.hints)

    def test_a_real_unreachable_port_is_waited_out(self):
        """가짜를 하나도 쓰지 않는 확인.

        이 결함은 가짜가 진짜와 다른 계약을 흉내내서 생겼다. 진짜 run()
        을 그대로 통과시키는 검사가 하나는 있어야 한다. 포트 1 은 아무도
        듣지 않으므로 즉시 거절되고, 어디로도 나가지 않는다.
        """
        dead = Target(name="x", host="127.0.0.1", user="ssherpa", port=1)
        with pytest.raises(vm.VmError) as excinfo:
            vm.wait_for_ssh(dead, "ssherpa-node-1", timeout=0)
        # SSHError 가 아니라 VmError 여야 한다 — SSHError 면 안 기다린 것이다
        assert "never accepted a connection" in excinfo.value.message


class TestCreateFlow:
    def test_the_vm_answers_before_create_returns(self, host):
        # create 가 '쓸 수 있는 VM' 을 돌려준다고 말하는 이상, 마지막으로
        # 확인할 것은 그 VM 이 명령을 받는다는 사실이다.
        fake = host(state="")
        vm.create(TARGET)
        assert fake.commands[-1] == "true"

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


class TestForwardingSurvivesReboot:
    """규칙은 커널 메모리에만 산다 — 재부팅이면 VM 은 살아 있는데 길만 사라진다.

    실측(0.4.x): 호스트 재부팅 후 VM 은 autostart 로 돌아오고 안의 k3s 도
    active 인데, DNAT 규칙만 없어져 kubectl 이 조용히 끊겼다.
    """

    def test_rule_is_registered_to_run_at_boot(self):
        command = vm.expose_api_step("192.168.122.65").command
        assert f"systemctl enable {vm.FORWARD_UNIT}" in command

    def test_boot_unit_runs_the_same_script_as_now(self):
        # 지금 세우는 규칙과 부팅 때 세울 규칙이 다른 코드면 언젠가 갈라진다
        assert f"ExecStart={vm.FORWARD_SCRIPT}" in vm.forward_unit()
        assert vm.FORWARD_SCRIPT in vm.expose_api_step("h").command

    def test_script_carries_the_actual_rules(self):
        script = vm.forward_script("192.168.122.65")
        assert script.startswith("#!/bin/sh")
        assert "--to-destination 192.168.122.65:6443" in script
        assert "-I FORWARD 1" in script

    def test_script_runs_as_root_without_sudo(self):
        # systemd 가 root 로 실행한다. sudo 가 섞이면 부팅 시점에 깨진다.
        assert "sudo" not in vm.forward_script("h")

    def test_script_left_on_the_host_is_english(self):
        # 이 파일은 고객사 호스트에 남아 그곳 관리자가 읽는다 —
        # 저장소 안의 주석과 달리 여기는 사용자의 자리다.
        assert vm.forward_script("h").isascii()
        assert vm.forward_unit().isascii()

    def test_unit_waits_for_libvirt(self):
        # libvirt 는 네트워크를 켤 때 FORWARD 에 거부 규칙을 넣는다.
        # 우리가 먼저 들어가면 그 뒤로 밀려 통과하지 못한다.
        unit = vm.forward_unit()
        assert "After=" in unit and "libvirtd.service" in unit

    def test_rerun_restarts_rather_than_starts(self):
        # 이미 적용된 oneshot 유닛은 start 로 다시 실행되지 않는다 —
        # 주소가 바뀐 재실행이 조용히 무시된다.
        command = vm.expose_api_step("h").command
        assert f"systemctl restart {vm.FORWARD_UNIT}" in command

    def test_removal_takes_the_boot_hook_too(self):
        # 유닛만 남기면 다음 부팅에 없는 VM 으로 가는 길을 다시 뚫는다
        command = vm.unexpose_api_step().command
        assert f"disable --now {vm.FORWARD_UNIT}" in command
        assert vm.FORWARD_UNIT_PATH in command
        assert vm.FORWARD_SCRIPT in command

    def test_expose_refuses_to_claim_survival_it_did_not_verify(self, host):
        # systemctl 이 enabled 라고 답하지 않으면 성공을 선언하지 않는다
        fake = host(state="running")
        with pytest.raises(vm.VmError) as excinfo:
            vm.expose_api(TARGET, "192.168.122.65")
        assert "survive a reboot" in excinfo.value.message
        assert fake.ran("systemctl is-enabled")

    def test_expose_passes_when_registered(self, host, monkeypatch):
        fake = host(state="running")

        def enabled_run(target, command, timeout=30):
            if "is-enabled" in command:
                return CommandResult(0, "enabled\n", "")
            return fake.run(target, command, timeout)

        monkeypatch.setattr(vm, "run", enabled_run)
        vm.expose_api(TARGET, "192.168.122.65")  # 예외 없이 통과


class TestAddressReservation:
    """주소가 바뀌면 부팅 때 다시 세운 규칙이 없는 VM 을 가리킨다.

    실측으로 VM 재생성마다 .250 → .160 → .231 로 옮겨 다녔다. 규칙이
    쫓아다니게 하는 것보다 주소를 못박는 편이 단순하다.
    """

    def test_reserves_the_address_for_this_vm(self):
        command = vm.reserve_ip_step("ssherpa-node-1", "52:54:00:ab:cd:ef", "192.168.122.65")
        assert "add ip-dhcp-host" in command.command
        assert "mac='52:54:00:ab:cd:ef'" in command.command
        assert "ip='192.168.122.65'" in command.command

    def test_reservation_persists_past_reboot(self):
        # --config 가 없으면 예약도 메모리에만 남아 같은 문제를 반복한다
        assert "--config" in vm.reserve_ip_step("n", "m", "i").command

    def test_stale_reservations_are_cleared_first(self):
        # libvirt 는 MAC·IP·이름 중 어느 하나만 겹쳐도 add 를 거절한다.
        # 옛 예약이 어느 쪽으로 남아 있을지 모르니 셋 다 걷어낸다 —
        # 손으로(virsh) 지운 VM 은 이름만 같은 예약을 남긴다 (실측).
        command = vm.reserve_ip_step(
            "ssherpa-node-3", "52:54:00:ab:cd:ef", "192.168.122.65"
        ).command
        assert command.index("delete ip-dhcp-host") < command.index("add ip-dhcp-host")
        assert command.count("delete ip-dhcp-host") == 3
        for key in ("mac='52:54:00:ab:cd:ef'", "ip='192.168.122.65'", "name='ssherpa-node-3'"):
            assert f"<host {key}/>" in command

    def test_release_only_deletes(self):
        command = vm.release_ip_step("52:54:00:ab:cd:ef").command
        assert "delete ip-dhcp-host" in command
        assert "add ip-dhcp-host" not in command

    def test_create_pins_the_address(self, host):
        fake = host(state="")
        vm.create(TARGET)
        assert fake.ran("add ip-dhcp-host")

    def test_existing_vm_gets_pinned_too(self, host):
        # 옛 버전이 만든 VM 도 재실행 한 번으로 주소가 고정돼야 한다
        fake = host(state="running")
        vm.create(TARGET)
        assert fake.ran("add ip-dhcp-host")

    def test_destroy_releases_the_reservation(self, host, monkeypatch):
        fake = host(state="running")
        original = fake.run

        def run_then_gone(target, command, timeout=30):
            result = original(target, command, timeout)
            if "undefine" in command:
                fake.state = ""
            return result

        monkeypatch.setattr(vm, "run", run_then_gone)
        vm.destroy(TARGET, "ssherpa-node-1")
        assert fake.ran("delete ip-dhcp-host")

    def test_destroy_releases_by_name_as_well_as_mac(self, host, monkeypatch):
        # 손으로 지운 자리에는 이름만 같고 MAC 은 다른 예약이 남는다 (실측).
        # MAC 만 보고 걷으면 그것이 그대로 남는다.
        fake = host(state="running")
        original = fake.run

        def run_then_gone(target, command, timeout=30):
            result = original(target, command, timeout)
            if "undefine" in command:
                fake.state = ""
            return result

        monkeypatch.setattr(vm, "run", run_then_gone)
        vm.destroy(TARGET, "ssherpa-node-1")
        assert fake.ran("name='ssherpa-node-1'")
        assert fake.ran("mac='52:54:00:ab:cd:ef'")

    def test_an_unreadable_mac_does_not_skip_the_release(self, host, monkeypatch):
        # domiflist 를 못 읽어도 이름은 안다 — 예약은 걷어야 한다
        fake = host(state="running")
        original = fake.run

        def no_mac(target, command, timeout=30):
            if "domiflist" in command:
                return CommandResult(0, "no table here", "")
            result = original(target, command, timeout)
            if "undefine" in command:
                fake.state = ""
            return result

        monkeypatch.setattr(vm, "run", no_mac)
        vm.destroy(TARGET, "ssherpa-node-1")
        assert fake.ran("name='ssherpa-node-1'")


class TestListVms:
    def test_filters_to_ssherpa_prefix(self, monkeypatch):
        # 고객사 호스트에는 남의 VM 이 있을 수 있다 — 우리 것만 보인다
        out = " ssherpa-node-1\ncustomer-db\n\n"
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, out, "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET) == ["ssherpa-node-1"]

    def test_order_is_by_name_not_by_power_state(self, monkeypatch):
        # virsh 는 켜진 것을 먼저 늘어놓는다. 노드 하나가 꺼지면 목록 순서가
        # 뒤집혀 읽기 어렵고, '첫 번째가 server' 라는 약속도 깨진다.
        out = " ssherpa-node-1\nssherpa-node-4\nssherpa-node-2\n"
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, out, "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET) == [
            "ssherpa-node-1",
            "ssherpa-node-2",
            "ssherpa-node-4",
        ]

    def test_host_without_virsh_reads_as_no_vms(self, monkeypatch):
        # host 모드만 쓰는 호스트에는 virsh 자체가 없다 — 오류가 아니다
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, "", "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET) == []

    def test_ten_nodes_are_not_ordered_as_text(self, monkeypatch):
        # 문자열 정렬은 node-10 을 node-2 앞에 놓는다. 32GB 호스트의 용량이
        # ~15대라 닿는 크기다.
        out = "\n".join(f"ssherpa-node-{i}" for i in (1, 10, 11, 2, 3)) + "\n"
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, out, "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET) == [
            "ssherpa-node-1",
            "ssherpa-node-2",
            "ssherpa-node-3",
            "ssherpa-node-10",
            "ssherpa-node-11",
        ]

    def test_server_is_first_even_past_ten(self, monkeypatch):
        # find() 는 이 순서를 그대로 믿고 첫 번째를 server 로 고른다
        out = "ssherpa-node-12\nssherpa-node-1\n"
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, out, "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET)[0] == "ssherpa-node-1"


class TestNodeOrder:
    def test_numbers_read_as_numbers(self):
        assert vm.node_order("ssherpa-node-2") < vm.node_order("ssherpa-node-10")

    def test_unnumbered_names_go_last(self):
        # 우리 접두사가 붙었어도 node-N 이 아니면 server 후보가 아니다
        assert vm.node_order("ssherpa-node-1") < vm.node_order("ssherpa-scratch")


class TestVmTarget:
    """VM 을 여느 target 처럼 — 기존 부품 전부가 이 Target 으로 동작해야 한다."""

    INFO = vm.VmInfo(
        name="ssherpa-node-1", ip="192.168.122.160",
        mac="52:54:00:b3:ab:70", already_existed=False,
    )

    def test_reaches_the_vm_through_the_host(self):
        built = vm.vm_target(TARGET, self.INFO)
        assert built.host == "192.168.122.160"
        assert built.jump == "ssherpa@192.0.2.10"

    def test_jump_keeps_the_host_port(self):
        host = Target(name="lab", host="10.0.0.1", user="admin", port=2222)
        assert vm.vm_target(host, self.INFO).jump == "admin@10.0.0.1:2222"

    def test_uses_the_dedicated_key_and_cloud_init_account(self):
        built = vm.vm_target(TARGET, self.INFO)
        assert built.user == "ssherpa"  # user_data 가 만드는 계정과 같아야 한다
        assert built.key == str(vm.local_key_paths()[0])

    def test_uses_our_own_known_hosts_file(self):
        built = vm.vm_target(TARGET, self.INFO)
        assert built.known_hosts == str(vm.known_hosts_path())

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


class TestLookupDoesNotWait:
    """조회 명령은 답이 좋아지기를 기다리면 안 된다.

    실측: 주소 없는 VM 앞에서 `ssherpa status` 가 180초 동안 멈춰 있었다.
    """

    def _leaseless(self, monkeypatch, slept):
        calls = []

        def run(target, command, timeout=30):  # noqa: ARG001
            calls.append(command)
            if "virsh list" in command:
                return CommandResult(0, "ssherpa-node-1\n", "")
            if "domstate" in command:
                return CommandResult(0, "running\n", "")
            if "domiflist" in command:
                return CommandResult(0, DOMIFLIST, "")
            return CommandResult(0, "", "")  # 임대 장부가 비어 있다

        monkeypatch.setattr(vm, "run", run)
        monkeypatch.setattr(vm.time, "sleep", lambda s: slept.append(s))
        return calls

    def test_zero_timeout_looks_once_and_gives_up(self, monkeypatch):
        slept = []
        calls = self._leaseless(monkeypatch, slept)
        with pytest.raises(vm.VmError):
            vm.wait_for_ip(TARGET, "ssherpa-node-1", timeout=0)
        assert slept == []
        assert sum("net-dhcp-leases" in c for c in calls) == 1

    def test_zero_timeout_says_what_is_true_now(self, monkeypatch):
        # '0초 안에 못 받았다' 가 아니라 '아직 주소가 없다' 가 사실이다
        self._leaseless(monkeypatch, [])
        with pytest.raises(vm.VmError) as excinfo:
            vm.wait_for_ip(TARGET, "ssherpa-node-1", timeout=0)
        assert "no address yet" in excinfo.value.message
        assert "0s" not in excinfo.value.message

    def test_find_passes_the_timeout_through(self, monkeypatch):
        slept = []
        self._leaseless(monkeypatch, slept)
        with pytest.raises(vm.VmError):
            vm.find(TARGET, timeout=0)
        assert slept == []

    def test_building_a_vm_still_waits(self, monkeypatch):
        # 만드는 쪽은 기다려야 한다 — 첫 부팅은 20초 남짓 걸린다
        slept = []
        self._leaseless(monkeypatch, slept)
        with pytest.raises(vm.VmError):
            vm.wait_for_ip(TARGET, "ssherpa-node-1", timeout=6)
        assert slept  # 폴링했다


class TestHostQueryFailuresAreNotSilence:
    """'virsh 가 없다' 와 'virsh 가 대답을 못 한다' 는 다른 사실이다.

    뭉뚱그리면 libvirtd 가 죽은 호스트에서 'VM 없음' 이라 답하게 되고,
    down 은 멀쩡한 클러스터를 두고 "지울 것이 없다" 며 끝난다.
    """

    def test_host_without_virsh_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, "SSHERPA_NO_VIRSH\n", "")  # noqa: ARG005
        )
        assert vm.list_vms(TARGET) == []

    def test_virsh_that_cannot_answer_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            vm,
            "run",
            lambda *a, **k: CommandResult(  # noqa: ARG005
                1, "", "error: failed to connect to the hypervisor"
            ),
        )
        with pytest.raises(vm.VmError) as excinfo:
            vm.list_vms(TARGET)
        assert "which VMs exist" in excinfo.value.message
        assert any("libvirtd" in hint for hint in excinfo.value.hints)

    def test_forwarding_leftovers_are_detectable(self, monkeypatch):
        # VM 을 손으로 지워도 유닛과 스크립트는 남는다
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, "", "")  # noqa: ARG005
        )
        assert vm.forwarding_installed(TARGET) is True

        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(1, "", "")  # noqa: ARG005
        )
        assert vm.forwarding_installed(TARGET) is False
