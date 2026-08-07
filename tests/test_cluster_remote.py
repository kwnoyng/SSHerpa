"""호스트에게 묻고 그 대답을 해석하는 부분 — status, down, kubeconfig 회수.

이 셋은 지금까지 전부 monkeypatch 로 대체돼서 한 번도 실행된 적이 없었다.
그 안에는 파서 하나와 안전장치 하나가 들어 있는데, 둘 다 실측 사고에서
나온 것이다: status 의 SSHERPA_D 파서, 그리고 제거 스크립트가 종료 코드 0
을 주고도 실제로는 안 지운 경우를 잡는 'verify removal'.

형제 파서(doctor.parse_probe, virt.parse_verify)는 전부 테스트가 있다.
"""

import os

import pytest

from ssherpa import cluster
from ssherpa import distro as distro_mod
from ssherpa import kubeconfig as kubeconf
from ssherpa.ssh import CommandResult, Target

TARGET = Target(name="lab-01", host="192.0.2.10", user="ssherpa")
NODE = cluster.Node(name="lab-01", target=TARGET, cli_name="lab-01")
K3S = distro_mod.get("k3s")

# k3s 가 실제로 쓰는 형태 — 블록 스타일에 따옴표 없음. 주소를 바꾸는
# 정규식이 이 모양을 전제하므로 픽스처도 이 모양이어야 한다.
KUBECONFIG = """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: LS0tLS1CRUdJTi==
    server: https://127.0.0.1:6443
  name: default
contexts:
- context:
    cluster: default
    user: default
  name: default
current-context: default
kind: Config
users:
- name: default
  user:
    client-certificate-data: LS0tLS1CRUdJTi==
"""


class Host:
    """원격 명령에 준비된 대답을 주고, 무엇을 물었는지 기록한다."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.commands: list[str] = []

    def run(self, target, command, timeout=30):  # noqa: ARG002
        self.commands.append(command)
        for fragment, reply in self.replies.items():
            if fragment in command:
                return reply
        return CommandResult(0, "", "")

    def ran(self, fragment):
        return any(fragment in command for command in self.commands)


@pytest.fixture
def host(monkeypatch):
    def build(replies=None):
        fake = Host(replies)
        monkeypatch.setattr(cluster, "run", fake.run)
        return fake

    return build


def probe_line(name, installed, state):
    return f"SSHERPA_D {name} {installed} {state}"


class TestStatusParser:
    ALL = list(distro_mod.DISTROS.values())

    def test_reads_installed_and_service_state(self, host):
        host({"SSHERPA_D": CommandResult(
            0, probe_line("k3s", "yes", "active") + "\n"
               + probe_line("rke2", "no", "") + "\n", ""
        )})
        state = cluster.status(NODE, self.ALL)
        by_name = {d.name: d for d in state.distros}
        assert by_name["k3s"].installed is True
        assert by_name["k3s"].running is True
        assert by_name["rke2"].installed is False

    def test_a_distro_missing_from_the_output_is_not_installed(self, host):
        # 프로브가 한 줄만 돌려줘도 목록은 물어본 만큼 나와야 한다
        host({"SSHERPA_D": CommandResult(0, probe_line("k3s", "no", "") + "\n", "")})
        state = cluster.status(NODE, self.ALL)
        assert [d.name for d in state.distros] == [d.name for d in self.ALL]
        assert state.installed == []

    def test_order_follows_what_was_asked_not_what_came_back(self, host):
        host({"SSHERPA_D": CommandResult(
            0, probe_line("rke2", "no", "") + "\n" + probe_line("k3s", "no", "") + "\n",
            "",
        )})
        state = cluster.status(NODE, self.ALL)
        assert [d.name for d in state.distros] == [d.name for d in self.ALL]

    def test_noise_between_the_lines_is_ignored(self, host):
        # sudo 배너나 셸 경고가 섞여 나오는 호스트가 있다
        host({"SSHERPA_D": CommandResult(
            0, "sudo: unable to resolve host\n" + probe_line("k3s", "yes", "active"),
            "",
        )})
        assert cluster.status(NODE, self.ALL).running[0].name == "k3s"

    def test_a_service_state_column_may_be_missing(self, host):
        # systemctl 이 아무것도 안 찍으면 네 번째 칸이 없다
        host({"SSHERPA_D": CommandResult(0, "SSHERPA_D k3s yes\n", "")})
        state = cluster.status(NODE, self.ALL)
        assert state.installed[0].service_state == ""
        assert state.running == []

    def test_two_distros_installed_is_a_conflict(self, host):
        host({"SSHERPA_D": CommandResult(
            0, probe_line("k3s", "yes", "active") + "\n"
               + probe_line("rke2", "yes", "active") + "\n", ""
        )})
        # 둘 다 6443 을 잡으려 한다
        assert cluster.status(NODE, self.ALL).conflicted is True

    def test_nodes_are_listed_only_when_something_runs(self, host):
        fake = host({"SSHERPA_D": CommandResult(
            0, probe_line("k3s", "yes", "inactive") + "\n", ""
        )})
        assert cluster.status(NODE, self.ALL).node_lines == []
        assert not fake.ran("get nodes")

    def test_a_running_distro_is_asked_for_its_nodes(self, host):
        host({
            "SSHERPA_D": CommandResult(
                0, probe_line("k3s", "yes", "active") + "\n", ""
            ),
            "get nodes": CommandResult(
                0, "n1 Ready control-plane 5m v1\nn2 Ready,SchedulingDisabled x 1m v1\n",
                "",
            ),
        })
        state = cluster.status(NODE, self.ALL)
        assert len(state.node_lines) == 2
        assert state.ready_count == 2  # cordon 한 노드도 Ready 다


class TestDown:
    def test_nothing_installed_is_not_a_failure(self, host):
        fake = host({"test -x": CommandResult(1, "", "")})
        assert cluster.down(NODE, K3S) is False
        assert not fake.ran("uninstall")

    def test_the_uninstall_script_is_run(self, host):
        fake = host({"test -x": CommandResult(0, "", "")})
        # 스크립트가 돈 뒤에도 test -x 가 0 이면 남아 있다는 뜻이라 실패한다
        with pytest.raises(cluster.ClusterError):
            cluster.down(NODE, K3S)
        assert fake.ran(K3S.uninstall_path)

    def test_a_binary_left_behind_is_reported(self, host):
        # 실측: rke2 가 종료 코드 0 을 주고도 남아 6443 을 계속 점유했고,
        # 다음 설치가 이유 없이 실패했다. 종료 코드를 믿으면 안 된다.
        host({"test -x": CommandResult(0, "", "")})
        with pytest.raises(cluster.ClusterError) as excinfo:
            cluster.down(NODE, K3S)
        assert "still installed" in excinfo.value.message
        assert any("systemctl status k3s" in hint for hint in excinfo.value.hints)

    def test_a_real_removal_cleans_up_locally(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cluster, "kubeconfig_dir", lambda: tmp_path)
        stale = tmp_path / "lab-01.yaml"
        stale.write_text("stale", encoding="utf-8")

        kube = tmp_path / "config"
        monkeypatch.setattr(kubeconf, "default_path", lambda: kube)
        kubeconf.merge(KUBECONFIG, "lab-01", path=kube)

        gone = {"yet": False}

        def run(target, command, timeout=30):  # noqa: ARG001
            if "test -x" in command:
                return CommandResult(1 if gone["yet"] else 0, "", "")
            if K3S.uninstall_path in command:
                gone["yet"] = True
            return CommandResult(0, "", "")

        monkeypatch.setattr(cluster, "run", run)

        assert cluster.down(NODE, K3S) is True
        assert not stale.exists()  # 죽은 클러스터를 가리키는 파일은 남기지 않는다
        import yaml

        data = yaml.safe_load(kube.read_text(encoding="utf-8"))
        assert data["clusters"] == []


class TestFetchKubeconfig:
    def test_the_loopback_address_is_rewritten(self, host, tmp_path, monkeypatch):
        monkeypatch.setattr(cluster, "kubeconfig_dir", lambda: tmp_path)
        host({"cat": CommandResult(0, KUBECONFIG, "")})
        path = cluster.fetch_kubeconfig(NODE, K3S, "192.0.2.10")
        text = path.read_text(encoding="utf-8")
        assert "https://192.0.2.10:6443" in text
        assert "127.0.0.1" not in text

    def test_an_unreadable_kubeconfig_says_where_it_looked(self, host):
        host({"cat": CommandResult(1, "", "Permission denied")})
        with pytest.raises(cluster.ClusterError) as excinfo:
            cluster.fetch_kubeconfig(NODE, K3S, "192.0.2.10")
        assert any(K3S.kubeconfig_path in hint for hint in excinfo.value.hints)

    def test_an_empty_answer_is_not_a_kubeconfig(self, host):
        # 종료 코드 0 에 빈 출력이면 파일을 쓴 뒤에야 깨진 걸 알게 된다
        host({"cat": CommandResult(0, "   \n", "")})
        with pytest.raises(cluster.ClusterError):
            cluster.fetch_kubeconfig(NODE, K3S, "192.0.2.10")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX 권한 개념이 없다")
    def test_the_saved_file_is_readable_only_by_its_owner(
        self, host, tmp_path, monkeypatch
    ):
        # 이 파일은 클러스터 관리자 자격증명이다
        monkeypatch.setattr(cluster, "kubeconfig_dir", lambda: tmp_path)
        host({"cat": CommandResult(0, KUBECONFIG, "")})
        path = cluster.fetch_kubeconfig(NODE, K3S, "192.0.2.10")
        assert path.stat().st_mode & 0o777 == 0o600
