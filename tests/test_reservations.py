"""주소 예약 — 읽기, 고아 판별, 걷기.

VM 을 `ssherpa down` 이 아니라 손으로(virsh) 지우면 예약만 남는다.
포워딩 장치가 남던 것과 똑같은 모양의 누수인데, 그쪽은 0.5.0 에서 걷게
했고 이쪽은 걷지 않아 주소를 붙든 채 쌓였다 (실측: node-3/4/5 가 세 개의
주소를 붙들고 있었다).
"""

import pytest

from ssherpa import vm
from ssherpa.ssh import CommandResult, Target

TARGET = Target(name="lab-01", host="192.0.2.10", user="ssherpa")

# virsh net-dumpxml default 가 실제로 내놓는 모양
NETWORK_XML = """<network>
  <name>default</name>
  <bridge name='virbr0' stp='on' delay='0'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'>
    <dhcp>
      <range start='192.168.122.2' end='192.168.122.254'/>
      <host mac='52:54:00:42:5a:b2' name='ssherpa-node-3' ip='192.168.122.161'/>
      <host mac='52:54:00:1d:b7:5d' name='ssherpa-node-4' ip='192.168.122.121'/>
      <host mac='52:54:00:aa:bb:cc' name='customer-db' ip='192.168.122.50'/>
    </dhcp>
  </ip>
</network>
"""


class TestParsing:
    def test_reads_our_reservations(self):
        found = vm.parse_reservations(NETWORK_XML)
        assert [r.name for r in found] == ["ssherpa-node-3", "ssherpa-node-4"]
        assert found[0].mac == "52:54:00:42:5a:b2"
        assert found[0].ip == "192.168.122.161"

    def test_someone_elses_reservation_is_not_ours(self):
        # 고객사 호스트에는 남의 예약이 있다 — VM 과 같은 소유 규칙이다
        names = [r.name for r in vm.parse_reservations(NETWORK_XML)]
        assert "customer-db" not in names

    def test_the_range_is_not_a_reservation(self):
        assert len(vm.parse_reservations(NETWORK_XML)) == 2

    def test_a_network_without_reservations(self):
        xml = "<network><name>default</name></network>"
        assert vm.parse_reservations(xml) == []

    def test_garbage_does_not_crash(self):
        # 읽을 수 없는 것은 '예약이 없다' 로 본다 — 여기서 멈추면 진짜 할 일
        # (VM 제거)까지 막힌다
        assert vm.parse_reservations("not xml at all") == []

    def test_attribute_order_does_not_matter(self):
        # 속성 순서는 libvirt 가 정한다. 정규식으로 읽었다면 여기서 깨진다.
        xml = (
            "<network><ip><dhcp>"
            "<host ip='192.168.122.9' name='ssherpa-node-1' mac='52:54:00:00:00:01'/>"
            "</dhcp></ip></network>"
        )
        found = vm.parse_reservations(xml)
        assert (found[0].name, found[0].mac, found[0].ip) == (
            "ssherpa-node-1", "52:54:00:00:00:01", "192.168.122.9"
        )


class TestListing:
    def test_reads_the_network(self, monkeypatch):
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, NETWORK_XML, "")  # noqa: ARG005
        )
        assert len(vm.list_reservations(TARGET)) == 2

    def test_a_host_without_the_network_has_no_reservations(self, monkeypatch):
        # virsh 가 없거나 default 네트워크가 없는 호스트 — 오류가 아니다
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(1, "", "no such network")  # noqa: ARG005
        )
        assert vm.list_reservations(TARGET) == []


class TestStale:
    def _wire(self, monkeypatch):
        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, NETWORK_XML, "")  # noqa: ARG005
        )

    def test_a_reservation_whose_vm_is_gone_is_stale(self, monkeypatch):
        self._wire(monkeypatch)
        stale = vm.stale_reservations(TARGET, live=[])
        assert [r.name for r in stale] == ["ssherpa-node-3", "ssherpa-node-4"]

    def test_a_live_vm_keeps_its_reservation(self, monkeypatch):
        # 돌고 있는 VM 의 주소를 걷으면 재부팅 때 주소가 바뀐다
        self._wire(monkeypatch)
        stale = vm.stale_reservations(TARGET, live=["ssherpa-node-3"])
        assert [r.name for r in stale] == ["ssherpa-node-4"]

    def test_nothing_is_stale_when_every_vm_is_live(self, monkeypatch):
        self._wire(monkeypatch)
        live = ["ssherpa-node-3", "ssherpa-node-4"]
        assert vm.stale_reservations(TARGET, live=live) == []

    def test_it_does_not_ask_the_host_which_vms_exist(self, monkeypatch):
        # 호출부가 이미 아는 것을 다시 묻지 않는다 — 왕복이 하나 줄고,
        # 무엇을 고아로 볼지 정하는 규칙이 한 곳에만 남는다.
        sent = []

        def record(target, command, timeout=30):  # noqa: ARG001
            sent.append(command)
            return CommandResult(0, NETWORK_XML, "")

        monkeypatch.setattr(vm, "run", record)
        vm.stale_reservations(TARGET, live=[])
        assert len(sent) == 1
        assert "virsh list" not in sent[0]


class TestReleaseStep:
    """걷는 쪽이 예약하는 쪽보다 눈이 좁으면 조용히 남는다.

    reserve_ip_step 은 mac·ip·name 셋으로 지운다 — 옛 예약이 어느 쪽으로
    남아 있을지 알 수 없어서다. release 는 mac 하나만 봤고, 아래 `|| true`
    가 실패를 삼키므로 남은 예약을 '걷었다' 고 보고했다.
    """

    def test_a_mac_alone_still_works(self):
        command = vm.release_ip_step(mac="52:54:00:aa:bb:cc").command
        assert "mac='52:54:00:aa:bb:cc'" in command

    def test_both_identifiers_are_used(self):
        command = vm.release_ip_step(
            mac="52:54:00:aa:bb:cc", name="ssherpa-node-1"
        ).command
        assert "mac='52:54:00:aa:bb:cc'" in command
        assert "name='ssherpa-node-1'" in command
        assert command.count("net-update") == 2

    def test_a_name_alone_still_removes_it(self):
        # libvirt 는 MAC 없이 이름만 있는 예약도 받는다. mac='' 로 지우려
        # 들면 virsh 가 실패하고 || true 가 그것을 삼킨다.
        command = vm.release_ip_step(name="ssherpa-node-9").command
        assert "name='ssherpa-node-9'" in command
        assert "mac=''" not in command

    def test_it_never_matches_on_the_address(self):
        # 그 주소를 쥔 남의 항목까지 지울 수 있고, reserve 와 달리 자리를
        # 비워야 할 이유도 없다
        command = vm.release_ip_step(
            mac="52:54:00:aa:bb:cc", name="ssherpa-node-1"
        ).command
        assert "ip=" not in command

    def test_nothing_to_match_on_is_an_error_not_a_no_op(self):
        # 조용히 아무것도 안 하면 '걷었다' 는 보고가 거짓이 된다
        with pytest.raises(vm.VmError, match="without a MAC or a name"):
            vm.release_ip_step()


class TestReleasing:
    def test_each_orphan_is_released_by_mac(self, monkeypatch):
        sent = []

        def record(target, command, timeout=30):  # noqa: ARG001
            sent.append(command)
            return CommandResult(0, "", "")

        monkeypatch.setattr(vm, "run", record)
        orphans = vm.parse_reservations(NETWORK_XML)
        vm.release_reservations(TARGET, orphans)

        assert len(sent) == 2
        assert "52:54:00:42:5a:b2" in sent[0]
        assert "52:54:00:1d:b7:5d" in sent[1]

    def test_the_name_goes_along_with_the_mac(self, monkeypatch):
        sent = []

        def record(target, command, timeout=30):  # noqa: ARG001
            sent.append(command)
            return CommandResult(0, "", "")

        monkeypatch.setattr(vm, "run", record)
        vm.release_reservations(TARGET, vm.parse_reservations(NETWORK_XML))
        assert "name='ssherpa-node-3'" in sent[0]
        assert "name='ssherpa-node-4'" in sent[1]

    def test_a_reservation_without_a_mac_is_still_removed(self, monkeypatch):
        sent = []

        def record(target, command, timeout=30):  # noqa: ARG001
            sent.append(command)
            return CommandResult(0, "", "")

        monkeypatch.setattr(vm, "run", record)
        vm.release_reservations(
            TARGET, [vm.Reservation(name="ssherpa-node-9", mac="", ip="192.168.122.9")]
        )
        assert len(sent) == 1
        assert "name='ssherpa-node-9'" in sent[0]

    def test_nothing_to_release_touches_the_host_not_at_all(self, monkeypatch):
        def explode(*_a, **_k):
            raise AssertionError("걷을 것이 없으면 묻지도 않아야 한다")

        monkeypatch.setattr(vm, "run", explode)
        vm.release_reservations(TARGET, [])

    def test_the_step_says_how_many(self, monkeypatch):
        labels = []

        class Reporter:
            def step(self, label):
                labels.append(label)
                from contextlib import nullcontext

                return nullcontext()

        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, "", "")  # noqa: ARG005
        )
        vm.release_reservations(
            TARGET, vm.parse_reservations(NETWORK_XML), Reporter()
        )
        assert labels == ["release 2 stale address reservations"]

    def test_one_orphan_reads_as_one(self, monkeypatch):
        labels = []

        class Reporter:
            def step(self, label):
                labels.append(label)
                from contextlib import nullcontext

                return nullcontext()

        monkeypatch.setattr(
            vm, "run", lambda *a, **k: CommandResult(0, "", "")  # noqa: ARG005
        )
        one = vm.parse_reservations(NETWORK_XML)[:1]
        vm.release_reservations(TARGET, one, Reporter())
        assert labels == ["release 1 stale address reservation"]


class TestDownSweeps:
    """down 이 실제로 걷는지 — 포워딩과 같은 자리에서."""

    def _wire(self, monkeypatch, *, vms, reservations):
        from ssherpa import cli

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: vms)
        monkeypatch.setattr(cli.vm_mod, "forwarding_installed", lambda _t: False)
        monkeypatch.setattr(cli.vm_mod, "list_reservations", lambda _t: reservations)
        monkeypatch.setattr(cli.vm_mod, "destroy", lambda *_a, **_k: True)
        monkeypatch.setattr(cli.vm_mod, "unexpose_api", lambda *_a, **_k: None)

        released = []
        monkeypatch.setattr(
            cli.vm_mod,
            "release_reservations",
            lambda _t, rs, _r=None: released.extend(r.name for r in rs),
        )
        return cli, released

    def test_orphans_are_swept_with_no_vms_left(self, monkeypatch):
        orphans = vm.parse_reservations(NETWORK_XML)
        cli, released = self._wire(monkeypatch, vms=[], reservations=orphans)
        cli.down("lab-01", assume_yes=True)
        assert released == ["ssherpa-node-3", "ssherpa-node-4"]

    def test_a_live_vms_reservation_is_left_alone(self, monkeypatch):
        orphans = vm.parse_reservations(NETWORK_XML)
        cli, released = self._wire(
            monkeypatch, vms=["ssherpa-node-3"], reservations=orphans
        )
        cli.down("lab-01", assume_yes=True)
        # node-3 은 destroy 가 자기 예약을 걷는다 — 여기서 건드리면 두 번이다
        assert released == ["ssherpa-node-4"]

    def test_a_host_with_only_orphans_is_not_called_empty(self, monkeypatch):
        # "Nothing is installed" 라고 하고 끝내면 예약은 영영 남는다
        orphans = vm.parse_reservations(NETWORK_XML)
        cli, released = self._wire(monkeypatch, vms=[], reservations=orphans)
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        out = captured.get()
        assert "Nothing is installed" not in out
        assert "2 stale address reservations" in out
        assert released

    def test_a_truly_empty_host_still_says_so(self, monkeypatch):
        cli, released = self._wire(monkeypatch, vms=[], reservations=[])
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        assert "Nothing is installed" in captured.get()
        assert released == []

    def test_it_does_not_claim_to_destroy_a_cluster_that_is_not_there(
        self, monkeypatch
    ):
        # 없는 일을 경고하면 다음부터 그 경고가 안 읽힌다 — 진짜로
        # 클러스터가 걸린 순간에도.
        orphans = vm.parse_reservations(NETWORK_XML)
        cli, _ = self._wire(monkeypatch, vms=[], reservations=orphans)
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        out = captured.get()
        assert "destroys the cluster" not in out
        assert "Workloads and cluster state are lost" not in out
        assert "these are leftovers" in out

    def test_a_real_cluster_still_gets_the_warning(self, monkeypatch):
        cli, _ = self._wire(monkeypatch, vms=["ssherpa-node-1"], reservations=[])
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        out = captured.get()
        assert "destroys the cluster" in out
        assert "Workloads and cluster state are lost" in out

    def test_a_sweep_alongside_a_cluster_is_announced(self, monkeypatch):
        # 예고는 "지울 것" 전부여야 한다. 진행 표시에서 처음 보게 하면
        # 말하지 않은 일을 한 것이 된다.
        orphans = vm.parse_reservations(NETWORK_XML)
        cli, released = self._wire(
            monkeypatch, vms=["ssherpa-node-1"], reservations=orphans
        )
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        out = captured.get()
        assert "destroys the cluster" in out          # 클러스터도 지운다
        assert "Also sweeping" in out                  # 그리고 이것도
        assert "2 stale address reservations" in out
        assert released == ["ssherpa-node-3", "ssherpa-node-4"]

    def test_no_extra_line_when_there_is_nothing_extra(self, monkeypatch):
        cli, _ = self._wire(monkeypatch, vms=["ssherpa-node-1"], reservations=[])
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        assert "Also sweeping" not in captured.get()

    def test_forwarding_is_not_called_leftover_while_vms_exist(self, monkeypatch):
        # VM 이 있으면 포워딩은 잔재가 아니라 그 클러스터로 가는 길이다
        from ssherpa import cli as cli_mod

        cli, _ = self._wire(monkeypatch, vms=["ssherpa-node-1"], reservations=[])
        monkeypatch.setattr(cli_mod.vm_mod, "forwarding_installed", lambda _t: True)
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        assert "leftover API forwarding" not in captured.get()

    def test_leftovers_can_be_swept_from_a_script(self, monkeypatch):
        # 잃을 것이 없는 자리에서 --yes 를 요구하면 뒷정리를 자동화할 수 없다
        orphans = vm.parse_reservations(NETWORK_XML)
        cli, released = self._wire(monkeypatch, vms=[], reservations=orphans)
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        cli.down("lab-01", assume_yes=False)
        assert released == ["ssherpa-node-3", "ssherpa-node-4"]

    def test_a_real_cluster_still_needs_saying_so(self, monkeypatch):
        import typer

        cli, _ = self._wire(monkeypatch, vms=["ssherpa-node-1"], reservations=[])
        monkeypatch.setattr(cli, "_interactive", lambda: False)
        with pytest.raises(typer.Exit):
            cli.down("lab-01", assume_yes=False)


@pytest.mark.parametrize("count", [1, 2, 5])
def test_every_orphan_is_released_regardless_of_count(monkeypatch, count):
    sent = []

    def record(target, command, timeout=30):  # noqa: ARG001
        sent.append(command)
        return CommandResult(0, "", "")

    monkeypatch.setattr(vm, "run", record)
    orphans = [
        vm.Reservation(name=f"ssherpa-node-{i}", mac=f"52:54:00:00:00:{i:02x}", ip="x")
        for i in range(count)
    ]
    vm.release_reservations(TARGET, orphans)
    assert len(sent) == count
