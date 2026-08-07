"""up 이 마지막에 내놓는 안내.

여기 적히는 것은 사용자가 그대로 붙여넣거나, 그대로 믿는 문장이다.
붙여넣는 것은 사용자의 셸에서 돌아가야 하고, 믿는 것은 사실이어야 한다.
"""

from pathlib import Path

import pytest

from ssherpa import cli
from ssherpa.cluster import UpResult
from ssherpa.ssh import Target

TARGET = Target(name="lab-01", host="192.0.2.10", user="admin")


def result(**kwargs):
    fields = {
        "kubeconfig": Path("/home/u/.ssherpa/kubeconfig/lab-01.yaml"),
        "api_address": "192.0.2.10",
        "api_reachable": True,
        "already_installed": False,
        "context": "ssherpa-lab-01",
        "context_is_current": True,
    }
    fields.update(kwargs)
    return UpResult(**fields)


class TestKubectlLine:
    """이 줄은 원격이 아니라 사용자의 터미널에서 실행된다."""

    def test_powershell_syntax_on_windows(self, monkeypatch):
        monkeypatch.setattr(cli.os, "name", "nt")
        line = cli._kubectl_with_config("C:/k/config.yaml")
        assert line == '$env:KUBECONFIG="C:/k/config.yaml"; kubectl'

    def test_posix_syntax_elsewhere(self, monkeypatch):
        # README 는 클라이언트로 macOS/Linux 를 먼저 든다. 거기에
        # PowerShell 문법을 내주면 붙여넣는 순간 깨진다.
        monkeypatch.setattr(cli.os, "name", "posix")
        line = cli._kubectl_with_config("/home/u/config.yaml")
        assert line == "KUBECONFIG=/home/u/config.yaml kubectl"
        assert "$env:" not in line

    def test_the_merge_failure_path_uses_it(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.os, "name", "posix")
        cli._print_up_result(result(merge_error="broken"), "k3s", TARGET)
        assert "KUBECONFIG=" in capsys.readouterr().out


class TestForwardingExposure:
    """열린 것을 알려주는 안내가 없었다 — 막힌 것만 알려주고 있었다."""

    def test_vm_mode_says_the_port_is_open(self, capsys):
        cli._print_up_result(result(), "k3s", TARGET, via_vm=True)
        out = capsys.readouterr().out
        assert "6443" in out
        assert "firewall" in out

    def test_it_says_the_host_firewall_will_not_stop_it(self, capsys):
        cli._print_up_result(result(), "k3s", TARGET, via_vm=True)
        out = capsys.readouterr().out
        assert "INPUT" in out

    def test_it_does_not_overstate_the_reach(self, capsys):
        # 클라우드/네트워크 방화벽은 호스트 바깥이라 그대로 유효하다.
        # "다 뚫린다" 로 읽히면 사용자는 없는 사고를 쫓는다.
        cli._print_up_result(result(), "k3s", TARGET, via_vm=True)
        assert "upstream" in capsys.readouterr().out

    def test_it_says_how_to_close_it(self, capsys):
        cli._print_up_result(result(), "k3s", TARGET, via_vm=True)
        assert "ssherpa down lab-01" in capsys.readouterr().out

    def test_host_mode_says_nothing_about_forwarding(self, capsys):
        # 호스트 모드는 포워딩 규칙을 세우지 않는다 — 없는 일을 경고하면
        # 다음부터 경고를 안 읽는다.
        cli._print_up_result(result(), "k3s", TARGET)
        assert "INPUT" not in capsys.readouterr().out

    @pytest.mark.parametrize("reachable", [True, False])
    def test_the_unreachable_path_still_offers_a_tunnel(self, reachable, capsys):
        cli._print_up_result(
            result(api_reachable=reachable), "k3s", TARGET, via_vm=True
        )
        out = capsys.readouterr().out
        assert ("ssh -L" in out) is not reachable
