"""CLI 출력이 환경에 관계없이 깨지지 않는지.

여기 있는 테스트는 실제로 프로세스를 띄운다. 인코딩 문제는 파이프/리다이렉트
상황에서만 드러나기 때문에, 함수 호출로는 재현되지 않는다.
"""

import subprocess
import sys

import pytest


def run_cli(args, env_extra=None, inventory=None):
    import os

    env = dict(os.environ)
    env.pop("SSHERPA_INVENTORY", None)
    if inventory is not None:
        env["SSHERPA_INVENTORY"] = str(inventory)
    if env_extra:
        env.update(env_extra)

    return subprocess.run(
        [sys.executable, "-m", "ssherpa", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )


class TestLegacyEncoding:
    """체크 기호(✓)를 인코딩 못 하는 로케일에서도 죽지 않아야 한다.

    한글 Windows 의 기본값인 cp949 가 대표적이다. 출력을 파이프로 넘기면
    Python 이 stdout 인코딩을 로케일로 잡아 UnicodeEncodeError 가 났었다.
    """

    LEGACY = {"PYTHONIOENCODING": "cp949"}

    def test_success_output_survives(self, tmp_path):
        result = run_cli(
            ["target", "add", "lab-01", "--host", "10.0.0.1", "--user", "admin"],
            env_extra=self.LEGACY,
            inventory=tmp_path / "inv.yml",
        )
        assert result.returncode == 0, result.stderr
        assert "UnicodeEncodeError" not in result.stderr

    def test_error_output_survives(self, tmp_path):
        result = run_cli(
            ["check", "nope"],
            env_extra=self.LEGACY,
            inventory=tmp_path / "inv.yml",
        )
        assert result.returncode == 1
        assert "UnicodeEncodeError" not in result.stderr


class TestPromptSafety:
    """출력이 파이프로 넘어가면 프롬프트를 띄울 수 없다.

    stdin 만 확인하면 안 된다. `ssherpa up lab | tee log.txt` 처럼 쓰면
    stdin 은 여전히 TTY 지만 화면 제어가 불가능해 프롬프트가 죽는다.
    """

    def test_needs_both_streams(self, monkeypatch):
        from ssherpa import cli

        class Stream:
            def __init__(self, tty):
                self._tty = tty

            def isatty(self):
                return self._tty

        monkeypatch.setattr(cli.sys, "stdin", Stream(True))
        monkeypatch.setattr(cli.sys, "stdout", Stream(False))
        assert cli._interactive() is False

        monkeypatch.setattr(cli.sys, "stdout", Stream(True))
        assert cli._interactive() is True

    def test_confirm_does_not_block_when_not_interactive(self, monkeypatch):
        from ssherpa import cli

        monkeypatch.setattr(cli, "_interactive", lambda: False)
        assert cli._confirm("Continue?", assume_yes=False) is True

    def test_confirm_skipped_with_yes_flag(self, monkeypatch):
        from ssherpa import cli

        monkeypatch.setattr(cli, "_interactive", lambda: True)
        # 프롬프트를 띄우면 테스트가 멈추므로, --yes 는 그 전에 빠져나가야 한다
        assert cli._confirm("Continue?", assume_yes=True) is True

    def test_broken_terminal_does_not_crash(self, monkeypatch):
        from ssherpa import cli

        def explode(*_args, **_kwargs):
            raise RuntimeError("No Windows console found")

        monkeypatch.setattr(cli, "_interactive", lambda: True)
        monkeypatch.setattr(cli.questionary, "confirm", explode)
        assert cli._confirm("Continue?", assume_yes=False) is True


class TestUnattendedDestruction:
    """물어볼 자리가 없는 것은 승낙이 아니다.

    up 은 되돌릴 수 있어서 조용히 진행해도 되지만(재실행이 곧 안전하다),
    down 은 클러스터를 되돌릴 수 없이 지운다. CI 나 파이프 뒤에서 확인
    없이 진행하면 사용자가 승인한 적 없는 파괴가 일어난다.
    """

    def _wire(self, monkeypatch, *, installed, vms):
        from ssherpa import cli
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: installed)
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: vms)
        monkeypatch.setattr(cli.vm_mod, "forwarding_installed", lambda _t: bool(vms))
        monkeypatch.setattr(cli, "_interactive", lambda: False)

    def _destroyed(self, monkeypatch):
        """실제 제거가 호출됐는지 기록하는 표식."""
        from ssherpa import cli

        marks = []
        monkeypatch.setattr(
            cli.cluster, "down", lambda *_a, **_k: marks.append("host") or True
        )
        monkeypatch.setattr(
            cli.vm_mod, "destroy", lambda *_a, **_k: marks.append("vm") or True
        )
        monkeypatch.setattr(cli.vm_mod, "unexpose_api", lambda *_a, **_k: None)
        return marks

    def test_unattended_down_refuses(self, monkeypatch):
        import typer

        from ssherpa import cli

        self._wire(monkeypatch, installed=["k3s"], vms=[])
        marks = self._destroyed(monkeypatch)
        with pytest.raises(typer.Exit) as caught:
            cli.down("lab-01", assume_yes=False)
        assert caught.value.exit_code == 1
        assert marks == []

    def test_refusal_names_the_flag_that_would_work(self, monkeypatch):
        import typer

        from ssherpa import cli

        self._wire(monkeypatch, installed=["k3s"], vms=[])
        self._destroyed(monkeypatch)
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.down("lab-01", assume_yes=False)
        assert "--yes" in captured.get()

    def test_unattended_vm_down_refuses_too(self, monkeypatch):
        import typer

        from ssherpa import cli

        self._wire(monkeypatch, installed=[], vms=["ssherpa-node-1"])
        marks = self._destroyed(monkeypatch)
        with pytest.raises(typer.Exit):
            cli.down("lab-01", assume_yes=False)
        assert marks == []

    def test_yes_still_works_unattended(self, monkeypatch):
        from ssherpa import cli

        self._wire(monkeypatch, installed=["k3s"], vms=[])
        marks = self._destroyed(monkeypatch)
        cli.down("lab-01", assume_yes=True)
        assert marks == ["host"]

    def test_empty_host_needs_no_confirmation(self, monkeypatch):
        # 지울 것이 없으면 물을 것도 없다 — 스크립트에서 그냥 통과해야 한다
        from ssherpa import cli

        self._wire(monkeypatch, installed=[], vms=[])
        self._destroyed(monkeypatch)
        cli.down("lab-01", assume_yes=False)


class TestStatusSurvivesALostConnection:
    """호스트에 묻는 일이 출력 도중에 남아 있으면 그 실패는 보호 밖이다.

    VM 이 있는 호스트의 status 는 표를 그린 뒤 VM 상태를 한 번 더 물었다.
    그 사이 연결이 끊기면 이 도구가 공들인 '사람이 읽는 오류' 대신
    스택트레이스가 나갔다.
    """

    def _wire(self, monkeypatch, vm_state):
        from ssherpa import cli
        from ssherpa.cluster import DistroStatus, HostStatus
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(
            cli.cluster,
            "status",
            lambda *_a, **_k: HostStatus(
                distros=[DistroStatus("k3s", installed=False, service_state="")]
            ),
        )
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: ["ssherpa-node-1"])
        monkeypatch.setattr(cli.vm_mod, "vm_state", vm_state)
        monkeypatch.setattr(cli.vm_mod, "find", lambda *_a, **_k: None)

    def test_lost_connection_becomes_a_readable_failure(self, monkeypatch):
        import typer

        from ssherpa import cli
        from ssherpa.ssh import SSHError

        def drop(*_a, **_k):
            raise SSHError("connection refused (10.0.0.1)", ["Check that sshd is running"])

        self._wire(monkeypatch, drop)
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit) as caught:
            cli.status("lab-01")
        assert caught.value.exit_code == 1
        assert "connection refused" in captured.get()

    def test_unreachable_vm_does_not_sink_the_report(self, monkeypatch):
        # 꺼진 VM 이나 안 뜬 k3s 때문에 status 가 실패하면, 정작 상태를
        # 알아야 할 때 아무것도 못 듣는다. 호스트 접속은 이미 성공했다.
        from ssherpa import cli
        from ssherpa.vm import VmError

        self._wire(monkeypatch, lambda *_a, **_k: "shut off")

        def not_running(*_a, **_k):
            raise VmError("ssherpa-node-1 is not running (shut off)")

        monkeypatch.setattr(cli.vm_mod, "find", not_running)
        with cli.console.capture() as captured:
            cli.status("lab-01")
        assert "ssherpa-node-1" in captured.get()

    def test_healthy_host_still_reports_the_vm(self, monkeypatch):
        from ssherpa import cli

        self._wire(monkeypatch, lambda *_a, **_k: "running")
        with cli.console.capture() as captured:
            cli.status("lab-01")
        out = captured.get()
        assert "ssherpa-node-1" in out
        assert "running" in out


class TestDistroSelection:
    """빈 호스트에서만 고를 일이 생긴다: 사람은 화살표, 스크립트는 --distro,
    지정 없는 스크립트는 k3s 기본 + 그 사실을 로그에 남긴다."""

    def _node(self):
        from ssherpa import cluster
        from ssherpa.ssh import Target

        return cluster.nodes_for_host_mode(Target(name="lab", host="h"))[0]

    def _wire(self, monkeypatch, installed, interactive=False):
        from ssherpa import cli

        monkeypatch.setattr(cli, "_installed_on", lambda _n: installed)
        monkeypatch.setattr(cli, "_interactive", lambda: interactive)

    def test_script_can_choose_with_distro(self, monkeypatch):
        from ssherpa import cli

        self._wire(monkeypatch, installed=[])
        assert cli._distro_to_install(self._node(), "rke2").name == "rke2"

    def test_mismatch_with_installed_is_refused(self, monkeypatch):
        # 예전 사고 재발 방지: rke2 가 있는데 k3s 를 요청하면 6443 충돌로
        # 몇 분 뒤에 죽는 대신, 시작 전에 거부해야 한다
        import typer

        from ssherpa import cli

        self._wire(monkeypatch, installed=["rke2"])
        with pytest.raises(typer.Exit):
            cli._distro_to_install(self._node(), "k3s")

    def test_matching_request_follows_installed(self, monkeypatch):
        from ssherpa import cli

        self._wire(monkeypatch, installed=["rke2"])
        assert cli._distro_to_install(self._node(), "rke2").name == "rke2"

    def test_silent_default_announces_itself(self, monkeypatch):
        from ssherpa import cli

        self._wire(monkeypatch, installed=[], interactive=False)
        with cli.console.capture() as captured:
            chosen = cli._distro_to_install(self._node(), None)
        assert chosen.name == "k3s"
        assert "defaulting to k3s" in captured.get()

    def test_explicit_choice_is_not_announced(self, monkeypatch):
        from ssherpa import cli

        self._wire(monkeypatch, installed=[], interactive=False)
        with cli.console.capture() as captured:
            cli._distro_to_install(self._node(), "k3s")
        assert "defaulting" not in captured.get()


class TestExitCodes:
    """verify.ps1 / verify.sh 가 종료 코드로 판정하므로 계약이 고정돼야 한다."""

    def test_version(self):
        result = run_cli(["--version"])
        assert result.returncode == 0
        assert "ssherpa" in result.stdout

    def test_help_lists_both_commands(self):
        result = run_cli(["--help"])
        assert result.returncode == 0
        assert "check" in result.stdout
        assert "target" in result.stdout

    def test_old_connect_name_is_gone(self):
        assert run_cli(["connect", "lab-01"]).returncode != 0

    def test_missing_target_exits_1(self, tmp_path):
        result = run_cli(["check", "nope"], inventory=tmp_path / "inv.yml")
        assert result.returncode == 1

    def test_check_without_arguments_exits_1(self, tmp_path):
        result = run_cli(["check"], inventory=tmp_path / "inv.yml")
        assert result.returncode == 1

    def test_name_and_host_together_rejected(self, tmp_path):
        result = run_cli(
            ["check", "lab-01", "--host", "10.0.0.1"],
            inventory=tmp_path / "inv.yml",
        )
        assert result.returncode == 1

    @pytest.mark.parametrize("args", [["target", "list"], ["target"]])
    def test_target_commands_do_not_crash(self, args, tmp_path):
        result = run_cli(args, inventory=tmp_path / "inv.yml")
        assert "Traceback" not in result.stderr


class TestNodesFlag:
    """노드는 기계다. 기계가 하나뿐인 곳에 노드를 더 달라고 할 수는 없다."""

    def _wire(self, monkeypatch):
        from ssherpa import cli
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        # 이 지점보다 뒤로는 가지 않아야 한다 — 접속이 일어나면 실패다
        monkeypatch.setattr(
            cli, "_installed_on", lambda _n: pytest.fail("touched the host")
        )
        return cli

    def test_nodes_without_vm_is_refused(self, monkeypatch):
        import typer

        cli = self._wire(monkeypatch)
        with pytest.raises(typer.Exit) as caught:
            cli.up("lab-01", distro=None, vm_mode=False, nodes=3, assume_yes=True)
        assert caught.value.exit_code == 1

    def test_refusal_shows_the_command_that_works(self, monkeypatch):
        import typer

        cli = self._wire(monkeypatch)
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.up("lab-01", distro=None, vm_mode=False, nodes=3, assume_yes=True)
        assert "--vm --nodes 3" in captured.get()

    def test_zero_nodes_is_refused(self, monkeypatch):
        import typer

        cli = self._wire(monkeypatch)
        with pytest.raises(typer.Exit):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=0, assume_yes=True)


class TestNodeCapacity:
    """만들다 메모리가 떨어지면 지워야 할 VM 몇 대와 이유 모를 실패만 남는다.

    doctor 가 보여주는 것과 같은 계산으로, 만들기 전에 거절해야 한다.
    """

    def _wire(self, monkeypatch, capacity):
        from ssherpa import cli, virt
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: [])
        monkeypatch.setattr(
            cli.virt,
            "setup",
            lambda *_a, **_k: virt.SetupResult(
                already_installed=True, virsh_version="10.0.0", vm_capacity=capacity
            ),
        )
        def built(*_a, **_k):
            # pytest.fail 은 BaseException 이라 '여기까지 왔는지'를
            # 검사하는 쪽에서 잡을 수 없다. 평범한 예외를 표식으로 쓴다.
            raise RuntimeError("built a VM anyway")

        monkeypatch.setattr(cli.vm_mod, "create", built)
        return cli

    def test_too_many_nodes_refused_before_building(self, monkeypatch):
        import typer

        cli = self._wire(monkeypatch, capacity=3)
        with pytest.raises(typer.Exit) as caught:
            cli.up("lab-01", distro=None, vm_mode=True, nodes=5, assume_yes=True)
        assert caught.value.exit_code == 1

    def test_refusal_names_both_numbers(self, monkeypatch):
        import typer

        cli = self._wire(monkeypatch, capacity=3)
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=5, assume_yes=True)
        out = captured.get()
        assert "3" in out and "5" in out
        assert "doctor" in out

    def test_unknown_capacity_does_not_block(self, monkeypatch):
        # 메모리를 못 읽었다고 해서 막으면, 읽기 실패가 곧 사용 불가가 된다
        import typer

        cli = self._wire(monkeypatch, capacity=None)
        with pytest.raises((typer.Exit, Exception)) as caught:
            cli.up("lab-01", distro=None, vm_mode=True, nodes=5, assume_yes=True)
        # create 까지 갔다는 뜻 — pytest.fail 이 잡힌다
        assert "built a VM anyway" in str(caught.value)


class TestExistingClusterSize:
    """'몇 대인지 말하지 않았다' 와 '한 대라고 말했다' 는 다른 요청이다.

    실측: 3노드가 도는 호스트에서 `up --vm` 이 'Cluster ready' 를 1노드처럼
    출력했다. 기본값 1 을 요청으로 단정했기 때문이다.
    """

    def _wire(self, monkeypatch, existing):
        from ssherpa import cli, virt
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: existing)
        monkeypatch.setattr(
            cli.virt,
            "setup",
            lambda *_a, **_k: virt.SetupResult(
                already_installed=True, virsh_version="10.0.0", vm_capacity=7
            ),
        )

        built = []

        def create(_t, *, spec, reporter=None):  # noqa: ARG001
            built.append(spec.name)
            raise RuntimeError("stop here")

        monkeypatch.setattr(cli.vm_mod, "create", create)
        return cli, built

    def test_no_flag_adopts_the_existing_size(self, monkeypatch):
        # 3대가 있으면 3노드로 다룬다 — 1노드라고 보고하지 않는다
        cli, built = self._wire(
            monkeypatch, ["ssherpa-node-1", "ssherpa-node-2", "ssherpa-node-3"]
        )
        with cli.console.capture() as captured, pytest.raises(RuntimeError):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=None, assume_yes=True)
        assert "3 VMs" in captured.get()

    def test_no_flag_on_an_empty_host_means_one(self, monkeypatch):
        cli, built = self._wire(monkeypatch, [])
        with cli.console.capture() as captured, pytest.raises(RuntimeError):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=None, assume_yes=True)
        assert "a VM" in captured.get()
        assert built == ["ssherpa-node-1"]

    def test_asking_for_fewer_is_refused(self, monkeypatch):
        import typer

        cli, built = self._wire(
            monkeypatch, ["ssherpa-node-1", "ssherpa-node-2", "ssherpa-node-3"]
        )
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=1, assume_yes=True)
        out = captured.get()
        assert "3 nodes already exist" in out
        assert "down lab-01" in out  # 줄이려면 걷어내고 다시 세운다
        assert built == []  # 아무것도 만들지 않았다

    def test_asking_for_more_grows(self, monkeypatch):
        # 늘리는 것은 파괴가 아니라 추가다 — 새 노드만 만들어 합류시킨다
        cli, built = self._wire(monkeypatch, ["ssherpa-node-1"])
        with pytest.raises(RuntimeError):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=3, assume_yes=True)
        assert built == ["ssherpa-node-1"]  # create 가 첫 노드에서 멈춘다


class TestLocalTracesAreRemoved:
    """down 이 로컬 흔적을 남기면 죽은 클러스터를 가리키는 컨텍스트가 쌓인다.

    실측: 멀티노드로 이름 규칙이 바뀌었는데 down 만 옛 이름을 지우고 있어서,
    5노드를 걷어낸 뒤에도 kubeconfig 파일과 컨텍스트가 살아남았다.
    """

    def test_up_and_down_agree_on_the_name(self):
        # 두 곳이 각자 이름을 지으면 다시 어긋난다 — 한 함수만 쓴다
        from ssherpa import vm

        assert vm.node_label("gcp-lab", "ssherpa-node-1") == "gcp-lab-node-1"

    def test_down_removes_every_node_entry(self, monkeypatch, tmp_path):
        from ssherpa import cli
        from ssherpa.ssh import Target

        vms = ["ssherpa-node-1", "ssherpa-node-2"]
        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: vms)
        monkeypatch.setattr(cli.vm_mod, "forwarding_installed", lambda _t: True)
        monkeypatch.setattr(cli.vm_mod, "destroy", lambda *_a, **_k: True)
        monkeypatch.setattr(cli.vm_mod, "unexpose_api", lambda *_a, **_k: None)
        monkeypatch.setattr(cli.cluster, "kubeconfig_dir", lambda: tmp_path)

        removed = []
        monkeypatch.setattr(cli.kubeconf, "remove", lambda entry: removed.append(entry))

        for entry in ("lab-01-node-1", "lab-01-node-2"):
            (tmp_path / f"{entry}.yaml").write_text("apiVersion: v1\n")

        cli.down("lab-01", assume_yes=True)

        assert removed == ["lab-01-node-1", "lab-01-node-2"]
        assert list(tmp_path.glob("*.yaml")) == []


class TestStatusSaysWhenItCouldNotLook:
    """못 물어봤다는 사실 자체가 보고할 내용이다.

    실측: server 노드가 꺼져 있으면 클러스터에 대해 아무 줄도 나오지 않아,
    침묵이 '클러스터가 없다' 로 읽혔다.
    """

    def _wire(self, monkeypatch, find):
        from ssherpa import cli
        from ssherpa.cluster import DistroStatus, HostStatus
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(
            cli.cluster,
            "status",
            lambda *_a, **_k: HostStatus(
                distros=[DistroStatus("k3s", installed=False, service_state="")]
            ),
        )
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: ["ssherpa-node-1"])
        monkeypatch.setattr(cli.vm_mod, "vm_state", lambda *_a, **_k: "shut off")
        monkeypatch.setattr(cli.vm_mod, "find", find)
        return cli

    def test_reports_why_it_could_not_check(self, monkeypatch):
        from ssherpa.vm import VmError

        def stopped(*_a, **_k):
            raise VmError("ssherpa-node-1 is not running (shut off)")

        cli = self._wire(monkeypatch, stopped)
        with cli.console.capture() as captured:
            cli.status("lab-01")
        out = captured.get()
        assert "could not ask" in out
        assert "not running" in out

    def test_stays_quiet_when_there_is_nothing_to_say(self, monkeypatch):
        # 물어봤는데 VM 이 없더라 — 그건 보고할 실패가 아니다
        cli = self._wire(monkeypatch, lambda *_a, **_k: None)
        with cli.console.capture() as captured:
            cli.status("lab-01")
        assert "could not ask" not in captured.get()


class TestDoctorHintsAreRunnable:
    """doctor 는 일회성 --host 로도 부를 수 있는데, up 은 등록된 이름만
    받는다. 주소를 그대로 끼워 넣으면 'target not found' 로 끝난다."""

    def _diagnose(self, monkeypatch, hints):
        from ssherpa import cli
        from ssherpa.doctor import Diagnosis
        from ssherpa.ssh import CommandResult

        monkeypatch.setattr(cli, "run", lambda *_a, **_k: CommandResult(0, "", ""))
        monkeypatch.setattr(cli.doctor_mod, "parse_probe", lambda _s: None)
        monkeypatch.setattr(
            cli.doctor_mod,
            "diagnose",
            lambda _f: Diagnosis(
                rows=[], host_type="virtual machine (google)", capable=False,
                failure="this host cannot create VMs", hints=hints,
            ),
        )
        return cli

    HINTS = ["Or use host mode as is:", "    ssherpa up <target>"]

    def test_registered_target_is_named_directly(self, monkeypatch, tmp_path):
        import typer

        cli = self._diagnose(monkeypatch, self.HINTS)
        inv = tmp_path / "inv.yml"
        inv.write_text(
            "all:\n  hosts:\n    lab-01:\n      ansible_host: 10.0.0.1\n", encoding="utf-8"
        )
        monkeypatch.setenv("SSHERPA_INVENTORY", str(inv))
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.doctor("lab-01", host=None, user=None, key=None, port=None)
        assert "ssherpa up lab-01" in captured.get()

    def test_one_off_host_is_told_to_register_first(self, monkeypatch):
        import typer

        cli = self._diagnose(monkeypatch, self.HINTS)
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.doctor(None, host="10.0.0.10", user="admin", key=None, port=None)
        out = captured.get()
        assert "ssherpa up 10.0.0.10" not in out  # 등록된 적 없는 이름
        assert "target add" in out


class TestBrokenPromptIsNotConsent:
    """물어보지 못한 것은 승낙이 아니다.

    되돌릴 수 있는 동작은 막지 않는 편이 낫지만, 파괴는 다르다.
    """

    def test_reversible_actions_still_proceed(self, monkeypatch):
        from ssherpa import cli

        monkeypatch.setattr(cli, "_interactive", lambda: True)
        monkeypatch.setattr(
            cli.questionary, "confirm", lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("No Windows console found")
            )
        )
        assert cli._confirm("Continue?", assume_yes=False) is True

    def test_destruction_stops(self, monkeypatch):
        from ssherpa import cli

        monkeypatch.setattr(cli, "_interactive", lambda: True)
        monkeypatch.setattr(
            cli.questionary, "confirm", lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("No Windows console found")
            )
        )
        assert cli._confirm("Continue?", assume_yes=False, fallback=False) is False

    def test_down_uses_the_strict_fallback(self, monkeypatch):
        import typer

        from ssherpa import cli
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: ["k3s"])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: [])
        monkeypatch.setattr(cli.vm_mod, "forwarding_installed", lambda _t: False)
        monkeypatch.setattr(cli, "_interactive", lambda: True)
        monkeypatch.setattr(
            cli.questionary, "confirm", lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("no console")
            )
        )
        destroyed = []
        monkeypatch.setattr(
            cli.cluster, "down", lambda *_a, **_k: destroyed.append("host") or True
        )
        with pytest.raises(typer.Exit):
            cli.down("lab-01", assume_yes=False)
        assert destroyed == []


class TestLeftoverForwardingIsRemovable:
    """VM 을 손으로 지운 뒤 down 하면 목록은 비어 있지만 포워딩은 남는다."""

    def _wire(self, monkeypatch, forwarding):
        from ssherpa import cli
        from ssherpa.ssh import Target

        target = Target(name="lab-01", host="10.0.0.1")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: [])
        monkeypatch.setattr(cli.vm_mod, "forwarding_installed", lambda _t: forwarding)
        closed = []
        monkeypatch.setattr(
            cli.vm_mod, "unexpose_api", lambda *_a, **_k: closed.append("closed")
        )
        return cli, closed

    def test_leftovers_are_swept(self, monkeypatch):
        cli, closed = self._wire(monkeypatch, forwarding=True)
        cli.down("lab-01", assume_yes=True)
        assert closed == ["closed"]

    def test_truly_empty_host_says_so(self, monkeypatch):
        cli, closed = self._wire(monkeypatch, forwarding=False)
        with cli.console.capture() as captured:
            cli.down("lab-01", assume_yes=True)
        assert "Nothing is installed" in captured.get()
        assert closed == []
