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

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: installed)
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: vms)
        monkeypatch.setattr(cli.vm_mod, "list_reservations", lambda _t: [])
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

        target = Target(name="lab-01", host="192.0.2.10")
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


class TestStatusLeadsWithTheAnswer:
    """VM 클러스터가 도는 호스트에서 'k3s — not installed' 두 줄이 먼저
    나오면, 멀쩡한 클러스터 앞에서 아무것도 없다는 인상을 준다 (실사용
    지적). 답을 먼저, '없음'은 마지막에.
    """

    def _wire(self, monkeypatch, *, vms, host_installed, inside_running):
        from ssherpa import cli
        from ssherpa.cluster import DistroStatus, HostStatus
        from ssherpa.ssh import Target
        from ssherpa.vm import VmInfo

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)

        def status(node, _known):
            if getattr(node, "in_vm", False):
                return HostStatus(
                    distros=[
                        DistroStatus("k3s", installed=False, service_state=""),
                        DistroStatus(
                            "rke2",
                            installed=inside_running,
                            service_state="active" if inside_running else "",
                        ),
                    ],
                    node_lines=(
                        ["node-1 Ready control-plane 5m v1"] if inside_running else []
                    ),
                )
            return HostStatus(
                distros=[
                    DistroStatus(
                        "k3s",
                        installed=host_installed,
                        service_state="active" if host_installed else "",
                    ),
                    DistroStatus("rke2", installed=False, service_state=""),
                ]
            )

        monkeypatch.setattr(cli.cluster, "status", status)
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: list(vms))
        monkeypatch.setattr(cli.vm_mod, "vm_state", lambda *_a, **_k: "running")
        monkeypatch.setattr(
            cli.vm_mod,
            "find",
            lambda *_a, **_k: (
                VmInfo(vms[0], "192.168.122.10", "52:54:00:00:00:01", True)
                if vms
                else None
            ),
        )
        monkeypatch.setattr(
            cli.vm_mod,
            "vm_target",
            lambda _h, info: Target(name=f"vm/{info.name}", host=info.ip),
        )
        return cli

    def test_a_vm_cluster_is_not_announced_with_not_installed(self, monkeypatch):
        cli = self._wire(
            monkeypatch,
            vms=["ssherpa-node-1"],
            host_installed=False,
            inside_running=True,
        )
        with cli.console.capture() as captured:
            cli.status("lab-01")
        out = captured.get()
        assert "not installed" not in out
        assert "rke2 in the VMs" in out

    def test_the_cluster_line_comes_before_the_vm_list(self, monkeypatch):
        # 답(클러스터)이 목록(VM)보다 먼저다
        cli = self._wire(
            monkeypatch,
            vms=["ssherpa-node-1"],
            host_installed=False,
            inside_running=True,
        )
        with cli.console.capture() as captured:
            cli.status("lab-01")
        out = captured.get()
        assert out.index("cluster:") < out.index("vm:")

    def test_the_hosts_cleanliness_is_still_stated(self, monkeypatch):
        # 표를 걷어낸 자리의 정보는 남는다 — 호스트 직접 설치가 없다는
        # 사실은 down/up 의 동작을 예측하는 데 쓰인다
        cli = self._wire(
            monkeypatch,
            vms=["ssherpa-node-1"],
            host_installed=False,
            inside_running=True,
        )
        with cli.console.capture() as captured:
            cli.status("lab-01")
        assert "host itself has nothing installed" in captured.get()

    def test_an_empty_host_keeps_the_table(self, monkeypatch):
        # 아무것도 없을 때는 'not installed' 가 곧 답이다
        cli = self._wire(
            monkeypatch, vms=[], host_installed=False, inside_running=False
        )
        with cli.console.capture() as captured:
            cli.status("lab-01")
        out = captured.get()
        assert "not installed" in out
        assert "Nothing installed" in out

    def test_a_host_mode_install_keeps_the_table(self, monkeypatch):
        cli = self._wire(
            monkeypatch, vms=[], host_installed=True, inside_running=False
        )
        with cli.console.capture() as captured:
            cli.status("lab-01")
        out = captured.get()
        assert "k3s" in out
        assert "installed" in out

    def test_a_host_install_beside_vms_is_called_a_fight(self, monkeypatch):
        # SSHerpa 는 이 상태를 만들지 않는다 — 손으로 깔았을 때만 생긴다.
        # 둘 다 초록불로 보여주면 6443 싸움이 숨는다: 포워딩이 바깥
        # 트래픽을 VM 으로 보내므로 호스트 쪽은 밖에서 조용히 끊긴다.
        cli = self._wire(
            monkeypatch,
            vms=["ssherpa-node-1"],
            host_installed=True,
            inside_running=True,
        )
        with (
            cli.console.capture() as out_cap,
            cli.err_console.capture() as err_cap,
        ):
            cli.status("lab-01")
        assert "both need port 6443" in err_cap.get()
        assert "cut off" in err_cap.get()
        # 둘 다 사실대로 보인다 — 경고는 추가지 은폐가 아니다
        assert "k3s" in out_cap.get()
        assert "rke2 in the VMs" in out_cap.get()

    def test_the_fight_warning_suggests_a_command_that_exists(self, monkeypatch):
        # 예전 충돌 안내는 down --distro 를 권했는데 down 에 그 옵션이 없다.
        # 안내가 시키는 명령은 실행 가능해야 한다.
        cli = self._wire(
            monkeypatch,
            vms=["ssherpa-node-1"],
            host_installed=True,
            inside_running=True,
        )
        with cli.console.capture(), cli.err_console.capture() as err_cap:
            cli.status("lab-01")
        err = err_cap.get()
        assert "ssherpa down lab-01" in err
        assert "down lab-01 --distro" not in err

    def test_empty_vms_are_not_reported_as_no_cluster(self, monkeypatch):
        # VM 은 있는데 안이 비었다 — 만들다 만 상태. 침묵하면 '클러스터가
        # 없다' 로 읽히므로, 상태와 다음 걸음을 그대로 말한다.
        cli = self._wire(
            monkeypatch,
            vms=["ssherpa-node-1"],
            host_installed=False,
            inside_running=False,
        )
        with cli.console.capture() as captured:
            cli.status("lab-01")
        out = captured.get()
        assert "nothing installed in the VMs yet" in out
        assert "up lab-01 --vm" in out


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

        target = Target(name="lab-01", host="192.0.2.10")
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

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: [])
        monkeypatch.setattr(
            cli.virt,
            "setup",
            lambda *_a, **_k: virt.SetupResult(
                already_installed=True,
                virsh_version="10.0.0",
                # 대수가 아니라 메모리가 사실이다 — 대수는 배포판이 정해진
                # 자리에서 계산된다 (k3s 2GB 기준으로 환산해 준다)
                usable_memory_mb=None if capacity is None else capacity * 2048,
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

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: existing)
        monkeypatch.setattr(
            cli.virt,
            "setup",
            lambda *_a, **_k: virt.SetupResult(
                already_installed=True, virsh_version="10.0.0",
                usable_memory_mb=7 * 2048,
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


class TestBrokenClusterShapes:
    """이름이 1..N 로 연속일 때만 개수 기반 계획이 성립한다.

    VM 을 손으로(virsh) 지우는 것은 인정된 사용법이다 — 고아 예약 청소가
    그 실측에서 나왔다. 그렇게 생긴 구멍을 모른 채 세면 server 재사용
    전제가 무너진다: 실측에서 rke2 컨트롤플레인이 기본 크기 2GB 로
    만들어지고, 빈 VM 에게 배포판을 물었다.
    """

    def _wire(self, monkeypatch, existing):
        from ssherpa import cli, virt
        from ssherpa.ssh import Target
        from ssherpa.vm import VmInfo

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_api_address", lambda _t: "192.0.2.10")
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: list(existing))
        monkeypatch.setattr(
            cli.virt,
            "setup",
            lambda *_a, **_k: virt.SetupResult(
                already_installed=True, virsh_version="10", usable_memory_mb=14000
            ),
        )

        built = []

        def create(_t, *, spec, reporter=None):  # noqa: ARG001
            built.append(spec.name)
            return VmInfo(spec.name, "192.168.122.10", "52:54:00:00:00:01", True)

        monkeypatch.setattr(cli.vm_mod, "create", create)
        monkeypatch.setattr(
            cli.vm_mod,
            "vm_target",
            lambda _h, info: Target(name=f"vm/{info.name}", host=info.ip),
        )

        def stop(*_a, **_k):
            raise RuntimeError("stop at expose")

        monkeypatch.setattr(cli.vm_mod, "expose_api", stop)
        return cli, built

    def test_a_missing_server_vm_is_refused_not_rebuilt(self, monkeypatch):
        import typer

        cli, built = self._wire(
            monkeypatch, ["ssherpa-node-2", "ssherpa-node-3"]
        )
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.up("lab-01", distro="rke2", vm_mode=True, nodes=None, assume_yes=True)
        out = captured.get()
        assert "no server VM" in out
        assert "join token" in out  # 왜 제자리 수리가 안 되는지
        assert "down lab-01" in out
        # 복구 제안은 원래 크기다 — node-2,3 이 남았으면 3노드였다
        assert "--nodes 3" in out
        assert built == []  # 기본 크기 server 를 짓기 전에 멈췄다

    def test_a_gap_in_the_names_is_refused(self, monkeypatch):
        import typer

        cli, built = self._wire(
            monkeypatch, ["ssherpa-node-1", "ssherpa-node-3"]
        )
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=None, assume_yes=True)
        out = captured.get()
        assert "gap" in out
        assert "ssherpa-node-3" in out
        assert built == []

    def test_contiguous_names_still_proceed(self, monkeypatch):
        cli, built = self._wire(
            monkeypatch, ["ssherpa-node-1", "ssherpa-node-2"]
        )
        with pytest.raises(RuntimeError, match="stop at expose"):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=None, assume_yes=True)
        assert built  # 정상 경로는 그대로 지나간다


class TestDistroChoiceLines:
    """프롬프트의 메모리 숫자는 모드를 따라야 한다.

    호스트 모드는 '이 호스트에서 도는가'(바닥)가, vm 모드는 '얼마짜리
    VM 을 만드는가'(할당)가 답이다. 실측: 프롬프트는 'runs in 1 GB' 인데
    doctor 는 '2 GB k3s VMs' 라고 답하고 있었다 — 같은 화면 두 개가
    다른 수를 말하면 한쪽이 거짓으로 읽힌다.
    """

    def _titles(self, vm):
        from ssherpa import cli

        return {value: title for title, value in cli._distro_choices(vm=vm)}

    def test_vm_mode_shows_the_vm_size(self):
        titles = self._titles(vm=True)
        assert "2 GB VM" in titles["k3s"]  # doctor 의 '2 GB k3s' 와 같은 수
        assert "4 GB VM" in titles["rke2"]

    def test_host_mode_shows_the_floor(self):
        titles = self._titles(vm=False)
        assert "~1 GB" in titles["k3s"]
        assert "~4 GB" in titles["rke2"]  # 3.4GB 를 내림해 3 이라 하면 모자란 호스트에 권하게 된다

    def test_the_floor_does_not_leak_into_vm_mode(self):
        # k3s 는 바닥(1GB)과 VM 크기(2GB)가 다른 유일한 배포판 —
        # 여기가 섞이면 실측했던 그 어긋남이 되살아난다
        assert "1 GB" not in self._titles(vm=True)["k3s"]

    def test_the_qualitative_part_survives_in_both(self):
        for vm in (True, False):
            titles = self._titles(vm=vm)
            assert "lightweight" in titles["k3s"]
            assert "security-hardened" in titles["rke2"]


class TestVmDistroSelection:
    """VM 모드의 배포판 선택 — 답은 VM 안에 있고, 빈 호스트에서만 고른다.

    0.5.5 까지 VM 모드는 k3s 전용이었다(VM 이 2GB 고정이라 rke2 가 못
    들어갔다). 크기가 배포판을 따라가면서 그 이유가 사라졌다 — 이제
    호스트 모드와 같은 규칙으로 정한다.
    """

    def _wire(self, monkeypatch, *, existing, inside, usable_mb=6 * 2048):
        from ssherpa import cli, virt
        from ssherpa.ssh import Target
        from ssherpa.vm import VmInfo

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_api_address", lambda _t: "192.0.2.10")

        def installed_on(node):
            # 호스트 자신(in_vm=False)과 VM 안(in_vm=True)은 다른 질문이다
            return list(inside) if getattr(node, "in_vm", False) else []

        monkeypatch.setattr(cli, "_installed_on", installed_on)
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: list(existing))
        monkeypatch.setattr(
            cli.virt,
            "setup",
            lambda *_a, **_k: virt.SetupResult(
                already_installed=True,
                virsh_version="10.0.0",
                usable_memory_mb=usable_mb,
            ),
        )

        built = []

        def create(_t, *, spec, reporter=None):  # noqa: ARG001
            built.append(spec)
            return VmInfo(spec.name, "192.168.122.10", "52:54:00:00:00:01", True)

        monkeypatch.setattr(cli.vm_mod, "create", create)
        monkeypatch.setattr(
            cli.vm_mod,
            "vm_target",
            lambda _host, info: Target(name=f"vm/{info.name}", host=info.ip),
        )

        def stop(*_a, **_k):
            # 여기까지 왔으면 선택·검사·생성이 전부 끝난 것이다
            raise RuntimeError("stop at expose")

        monkeypatch.setattr(cli.vm_mod, "expose_api", stop)
        return cli, built

    def test_rke2_is_no_longer_refused(self, monkeypatch):
        # 빈 호스트 + --distro rke2 → 4GB/20GB 짜리 VM 으로 만든다
        cli, built = self._wire(monkeypatch, existing=[], inside=[])
        with pytest.raises(RuntimeError, match="stop at expose"):
            cli.up("lab-01", distro="rke2", vm_mode=True, nodes=1, assume_yes=True)
        assert built[0].memory_mb == 4096
        assert built[0].disk_gb == 20

    def test_k3s_keeps_its_size(self, monkeypatch):
        # 기존 사용자의 VM 크기가 바뀌면 용량 계산과 재사용이 다 흔들린다
        cli, built = self._wire(monkeypatch, existing=[], inside=[])
        with pytest.raises(RuntimeError, match="stop at expose"):
            cli.up("lab-01", distro="k3s", vm_mode=True, nodes=1, assume_yes=True)
        assert built[0].memory_mb == 2048
        assert built[0].disk_gb == 10

    def test_mismatch_with_whats_inside_is_refused(self, monkeypatch):
        # k3s 가 든 VM 에 rke2 를 얹으면 둘 다 6443 을 잡으려 한다 —
        # 호스트 모드가 거절하는 것과 같은 이유로 거절한다
        import typer

        cli, built = self._wire(
            monkeypatch, existing=["ssherpa-node-1"], inside=["k3s"]
        )
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.up("lab-01", distro="rke2", vm_mode=True, nodes=1, assume_yes=True)
        out = captured.get()
        assert "k3s is already installed" in out
        assert "down lab-01" in out

    def test_whats_inside_wins_without_a_request(self, monkeypatch):
        # 재실행이 배포판을 물어보면 안 된다 — 이미 결정된 것은 다시 묻지 않는다
        cli, built = self._wire(
            monkeypatch, existing=["ssherpa-node-1"], inside=["k3s"]
        )
        with cli.console.capture() as captured, pytest.raises(RuntimeError):
            cli.up("lab-01", distro=None, vm_mode=True, nodes=1, assume_yes=True)
        assert "k3s runs inside these VMs" in captured.get()

    def test_capacity_is_counted_in_the_chosen_distros_size(self, monkeypatch):
        # usable 12288MB: k3s(2GB) 로는 6대, rke2(4GB) 로는 3대다.
        # 4대 요청은 k3s 면 통과, rke2 면 거절이어야 한다.
        import typer

        cli, built = self._wire(monkeypatch, existing=[], inside=[])
        with cli.err_console.capture() as captured, pytest.raises(typer.Exit):
            cli.up("lab-01", distro="rke2", vm_mode=True, nodes=4, assume_yes=True)
        out = captured.get()
        assert "3 rke2" in out
        assert "4 GB" in out
        assert built == []  # 만들기 전에 거절했다

    def test_the_same_count_passes_as_k3s(self, monkeypatch):
        cli, built = self._wire(monkeypatch, existing=[], inside=[])
        with pytest.raises(RuntimeError, match="stop at expose"):
            cli.up("lab-01", distro="k3s", vm_mode=True, nodes=4, assume_yes=True)
        assert len(built) == 4


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
        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: vms)
        monkeypatch.setattr(cli.vm_mod, "list_reservations", lambda _t: [])
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

        target = Target(name="lab-01", host="192.0.2.10")
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

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: ["k3s"])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: [])
        monkeypatch.setattr(cli.vm_mod, "list_reservations", lambda _t: [])
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

        target = Target(name="lab-01", host="192.0.2.10")
        monkeypatch.setattr(cli, "_load_target", lambda _n: target)
        monkeypatch.setattr(cli, "_installed_on", lambda _n: [])
        monkeypatch.setattr(cli.vm_mod, "list_vms", lambda _t: [])
        monkeypatch.setattr(cli.vm_mod, "list_reservations", lambda _t: [])
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


class TestSourceEncoding:
    def test_no_source_file_carries_a_bom(self):
        # 실측: PowerShell 의 Set-Content -Encoding utf8 이 BOM 을 붙이고,
        # Get-Content 는 UTF-8 을 cp949 로 읽어 한글 docstring 을 통째로
        # 깨뜨렸다. 소스는 BOM 없는 UTF-8 이어야 한다.
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "ssherpa"
        for source in sorted(root.glob("*.py")):
            raw = source.read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), f"{source.name} has a BOM"
            raw.decode("utf-8")  # 깨진 인코딩이면 여기서 터진다

    def test_the_package_docstring_is_not_mojibake(self):
        import ssherpa

        # cp949 왕복으로 깨지면 한글이 '?곕Ⅴ??' 류의 잔해가 된다
        assert "셰르파" in (ssherpa.__doc__ or "")
