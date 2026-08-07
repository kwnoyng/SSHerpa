"""권한: '못 읽은 것' 과 '없는 것' 은 다른 사실이다.

SSHerpa 는 원격에서 일반 계정으로 돌고 필요할 때만 sudo 를 쓴다. 그런데
존재·내용을 확인하는 명령에서 sudo 를 빠뜨리면, 권한이 모자라 실패한 것이
'없다' 로 읽힌다. 그 오독은 조용하고, 방향이 전부 나쁜 쪽이다:

  - 설정 파일을 못 읽음  -> 없는 줄 알고 남의 파일을 덮어쓴다
  - 바이너리를 못 읽음   -> 미설치인 줄 알고 도는 클러스터에 재설치한다
  - 유닛을 못 읽음       -> down 이 포워딩 청소를 건너뛴다

파일 모드는 우리가 정하지 않는다. root 의 umask 나 하드닝 정책이 정하고,
CIS 쿠버네티스 벤치마크는 설정 파일에 0600 을 요구한다 — 드문 상태가
아니라 권장 상태다.
"""

from ssherpa import cluster, vm
from ssherpa.distro import DISTROS
from ssherpa.ssh import CommandResult, Target

TARGET = Target(name="lab-01", host="192.0.2.10", user="ssherpa")


def capture(monkeypatch, module):
    """모듈의 run 을 가로채 실제로 나간 명령을 모은다."""
    sent: list[str] = []

    def fake_run(target, command, timeout=30):  # noqa: ARG001
        sent.append(command)
        return CommandResult(0, "", "")

    monkeypatch.setattr(module, "run", fake_run)
    return sent


class TestOwnershipCheck:
    """이 검사가 못 읽으면, 검사가 막으려던 사고를 검사 자신이 일으킨다."""

    def test_every_line_touching_the_config_asks_as_root(self):
        for distro in DISTROS.values():
            for line in distro.config_ownership_command().splitlines():
                if distro.config_path in line:
                    assert "sudo " in line, f"{distro.name}: {line}"

    def test_the_marker_is_grepped_as_root(self):
        # 여기서 실패하면 우리가 쓴 파일이 남의 것으로 오판된다
        for distro in DISTROS.values():
            command = distro.config_ownership_command()
            marker_line = next(
                line for line in command.splitlines() if "grep -qF" in line
            )
            assert marker_line.strip().startswith("sudo ")


class TestInstalledCheck:
    def test_is_installed_asks_as_root(self, monkeypatch):
        sent = capture(monkeypatch, cluster)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        cluster.is_installed(node, DISTROS["k3s"])
        assert sent == ["sudo test -x /usr/local/bin/k3s"]

    def test_status_probe_asks_as_root(self):
        for distro in DISTROS.values():
            command = distro.status_command()
            assert f"sudo test -x {distro.installed_marker}" in command


class TestHostFileChecks:
    def test_base_image_presence_asks_as_root(self):
        # 못 읽으면 실행마다 600MB 를 다시 받는다
        assert f"sudo test -f {vm.BASE_IMAGE}" in vm.download_step().command

    def test_forwarding_presence_asks_as_root(self, monkeypatch):
        sent = capture(monkeypatch, vm)
        vm.forwarding_installed(TARGET)
        assert len(sent) == 1
        for path in (vm.FORWARD_UNIT_PATH, vm.FORWARD_SCRIPT):
            assert f"sudo test -f {path}" in sent[0]
