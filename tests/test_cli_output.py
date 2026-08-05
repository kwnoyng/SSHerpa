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
