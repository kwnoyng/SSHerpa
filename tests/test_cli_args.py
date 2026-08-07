"""CLI 가 인자를 받아들이는 규칙.

한 명령 안에서 어떤 옵션은 거절하고 어떤 옵션은 말없이 버리면, 사용자는
버려진 쪽이 먹혔다고 믿는다. 조용한 무시가 거절보다 나쁜 이유다.
"""

import pytest
import typer

from ssherpa import cli
from ssherpa.inventory import add_target


@pytest.fixture(autouse=True)
def temp_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("SSHERPA_INVENTORY", str(tmp_path / "inventory.yml"))
    add_target("lab-01", host="10.0.0.10", user="admin", port=22)


def resolve(**kwargs):
    args = {"name": None, "host": None, "user": None, "port": None, "key": None}
    args.update(kwargs)
    return cli._resolve_target(**args)


class TestNameAndConnectionFlags:
    """이름을 줬으면 접속 정보는 전부 인벤토리에서 온다."""

    @pytest.mark.parametrize(
        ("flag", "value"),
        [("host", "1.2.3.4"), ("user", "root"), ("port", 2222), ("key", "~/k")],
    )
    def test_each_connection_flag_is_refused(self, flag, value, capsys):
        with pytest.raises(typer.Exit):
            resolve(name="lab-01", **{flag: value})
        assert f"--{flag}" in capsys.readouterr().err

    def test_the_message_names_every_flag_given(self, capsys):
        with pytest.raises(typer.Exit):
            resolve(name="lab-01", port=2222, key="~/k")
        err = capsys.readouterr().err
        assert "--port" in err and "--key" in err

    def test_the_fix_points_at_update_not_add(self, capsys):
        # add 를 시키면 '이미 등록됨' 거절이 다시 update 를 가리킨다 —
        # 안내가 원을 그리면 두 번 실패하고서야 도착한다
        with pytest.raises(typer.Exit):
            resolve(name="lab-01", port=2222)
        err = capsys.readouterr().err
        assert "target update lab-01" in err
        assert "target add" not in err

    def test_hints_speak_the_command_that_was_run(self, capsys):
        # doctor 의 오류가 check 를 시키면 사용자는 엉뚱한 명령을 배운다
        with pytest.raises(typer.Exit):
            resolve(verb="doctor")
        err = capsys.readouterr().err
        assert "ssherpa doctor" in err
        assert "ssherpa check" not in err

    def test_a_bare_name_still_resolves(self):
        target = resolve(name="lab-01")
        assert (target.host, target.user) == ("10.0.0.10", "admin")

    def test_one_off_flags_are_untouched(self):
        # 이름 없이 쓰는 --host 조합은 여전히 전부 받는다
        target = resolve(host="10.0.0.20", user="root", port=2222, key="~/k")
        assert (target.host, target.port, target.key) == ("10.0.0.20", 2222, "~/k")

    def test_a_one_off_ipv6_host_is_allowed(self):
        # 등록은 IPv6 를 거절하지만(반쪽 지원 방지), 일회성 진단은 받는다 —
        # check/doctor 는 IPv6 가 부러지는 네 자리 중 어느 것도 안 건드린다.
        # 이 비대칭은 의도이며, 여기서 바뀌면 그 결정을 다시 한 것이다.
        target = resolve(host="2001:db8::1", user="admin")
        assert target.host == "2001:db8::1"


class TestNodeCount:
    def test_zero_is_refused_before_the_mode_is_considered(self, capsys):
        # 모드를 먼저 보면 호스트 모드의 `--nodes 0` 이 '--vm 을 붙이라' 는
        # 안내를 받는데, 붙여도 거절이다 — 고칠 수 없는 명령을 시키게 된다.
        with pytest.raises(typer.Exit):
            cli.up("lab-01", distro=None, vm_mode=False, nodes=0, assume_yes=True)
        err = capsys.readouterr().err
        assert "at least 1" in err
        assert "--vm" not in err

    def test_more_than_one_node_still_asks_for_vm(self, capsys):
        with pytest.raises(typer.Exit):
            cli.up("lab-01", distro=None, vm_mode=False, nodes=3, assume_yes=True)
        err = capsys.readouterr().err
        assert "--nodes needs --vm" in err
        assert "--vm --nodes 3" in err
