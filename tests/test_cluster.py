"""배포판 정의와 클러스터 설치 로직 중 SSH 없이 검증 가능한 부분."""

import pytest

from ssherpa import cluster
from ssherpa import distro as distro_mod
from ssherpa.ssh import Target

TARGET = Target(name="lab-01", host="34.50.34.61", user="ssherpa")

# k3s 가 실제로 만들어내는 kubeconfig 형태
K3S_KUBECONFIG = """apiVersion: v1
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
"""


class TestDistroRegistry:
    def test_both_distros_available(self):
        assert distro_mod.get("k3s") is not None
        assert distro_mod.get("rke2") is not None

    def test_unknown_distro(self):
        assert distro_mod.get("nope") is None

    def test_names_lists_everything(self):
        listed = distro_mod.names()
        for name in distro_mod.DISTROS:
            assert name in listed


class TestInstallSteps:
    """설치 절차는 배포판마다 단계 수가 다르다."""

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_tls_san_is_always_passed(self, name):
        """--tls-san 이 빠지면 외부 주소로 접속할 때 인증서 검증이 깨진다."""
        steps = distro_mod.get(name).install_steps("34.50.34.61")
        assert any("34.50.34.61" in step.command for step in steps)

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_install_allows_more_time_than_default(self, name):
        # 설치는 이미지 내려받기까지 포함해 수 분이 걸릴 수 있다
        steps = distro_mod.get(name).install_steps("h")
        assert max(step.timeout for step in steps) >= 300

    def test_k3s_is_a_single_step(self):
        assert len(distro_mod.get("k3s").install_steps("h")) == 1

    def test_rke2_installs_then_starts(self):
        # RKE2 는 설치와 기동이 분리돼 있다
        steps = distro_mod.get("rke2").install_steps("h")
        assert len(steps) > 1
        assert any("systemctl" in step.command for step in steps)

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_every_step_has_a_label(self, name):
        for step in distro_mod.get(name).install_steps("h"):
            assert step.label.strip()


class TestDistroPaths:
    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_uninstall_tolerates_missing_script(self, name):
        """설치되지 않은 호스트에서 제거를 돌려도 실패하면 안 된다."""
        steps = distro_mod.get(name).uninstall_steps()
        assert all("if [ -x" in step.command for step in steps)

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_kubeconfig_needs_root(self, name):
        chosen = distro_mod.get(name)
        assert chosen.read_kubeconfig_command().startswith("sudo ")
        assert chosen.kubeconfig_path in chosen.read_kubeconfig_command()

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_node_status_uses_the_right_kubeconfig(self, name):
        chosen = distro_mod.get(name)
        command = chosen.node_status_command()
        assert chosen.kubeconfig_path in command
        assert "get nodes" in command


class TestNodes:
    def test_host_mode_uses_the_target_itself(self):
        nodes = cluster.nodes_for_host_mode(TARGET)
        assert len(nodes) == 1
        assert nodes[0].target is TARGET
        assert nodes[0].role == "server"

    def test_node_name_falls_back_to_host(self):
        anonymous = Target(name=None, host="10.0.0.1", user="admin")
        assert cluster.nodes_for_host_mode(anonymous)[0].name == "10.0.0.1"


class TestMemoryPreflight:
    """메모리가 모자라면 설치해도 노드가 Ready 가 되지 않는다.

    2GB 호스트에 RKE2 를 실제로 깔아본 결과: 컨트롤 플레인은 뜨지만 CNI 를
    설치할 파드가 Pending 에 걸려 5분 뒤 타임아웃으로만 알 수 있었다.
    그래서 시작 전에 막는다.
    """

    def _fake_run(self, result):
        from ssherpa.ssh import CommandResult

        def _run(*_args, **_kwargs) -> CommandResult:
            return result

        return _run

    def _with_memory(self, monkeypatch, kb):
        from ssherpa.ssh import CommandResult

        monkeypatch.setattr(
            cluster,
            "run",
            self._fake_run(CommandResult(0, f"MemTotal:  {kb} kB\n", "")),
        )

    def test_reads_total_memory(self, monkeypatch):
        self._with_memory(monkeypatch, 2035224)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        assert cluster.total_memory_mb(node) == 1987

    def test_unreadable_meminfo_is_not_fatal(self, monkeypatch):
        from ssherpa.ssh import CommandResult

        monkeypatch.setattr(
            cluster, "run", self._fake_run(CommandResult(1, "", "no such file"))
        )
        node = cluster.nodes_for_host_mode(TARGET)[0]
        assert cluster.total_memory_mb(node) is None
        # 읽지 못했다고 설치를 막지는 않는다
        cluster.check_memory(node, distro_mod.get("k3s"))

    def test_rke2_rejected_on_a_2gb_host(self, monkeypatch):
        self._with_memory(monkeypatch, 2035224)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        with pytest.raises(cluster.ClusterError, match="not enough memory"):
            cluster.check_memory(node, distro_mod.get("rke2"))

    def test_k3s_accepted_on_the_same_host(self, monkeypatch):
        self._with_memory(monkeypatch, 2035224)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        cluster.check_memory(node, distro_mod.get("k3s"))

    def test_suggests_k3s_as_a_fallback(self, monkeypatch):
        self._with_memory(monkeypatch, 2035224)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        with pytest.raises(cluster.ClusterError) as exc:
            cluster.check_memory(node, distro_mod.get("rke2"))
        assert any("--distro k3s" in hint for hint in exc.value.hints)

    def test_rke2_accepted_when_memory_is_enough(self, monkeypatch):
        self._with_memory(monkeypatch, 4194304)  # 4 GB
        node = cluster.nodes_for_host_mode(TARGET)[0]
        cluster.check_memory(node, distro_mod.get("rke2"))

    def test_rke2_needs_more_than_k3s(self):
        assert (
            distro_mod.get("rke2").min_memory_mb > distro_mod.get("k3s").min_memory_mb
        )


class TestRewriteKubeconfig:
    """127.0.0.1 그대로 두면 내 PC 에서 kubectl 이 자기 자신에게 접속한다."""

    def test_server_address_is_replaced(self):
        out = cluster.rewrite_kubeconfig(K3S_KUBECONFIG, "34.50.34.61")
        assert "server: https://34.50.34.61:6443" in out
        assert "127.0.0.1" not in out

    def test_rest_of_the_file_is_untouched(self):
        out = cluster.rewrite_kubeconfig(K3S_KUBECONFIG, "34.50.34.61")
        assert "certificate-authority-data: LS0tLS1CRUdJTi==" in out
        assert out.count("\n") == K3S_KUBECONFIG.count("\n")

    def test_hostname_works_too(self):
        out = cluster.rewrite_kubeconfig(K3S_KUBECONFIG, "lab.example.com")
        assert "server: https://lab.example.com:6443" in out

    def test_already_external_address_is_replaced(self):
        once = cluster.rewrite_kubeconfig(K3S_KUBECONFIG, "1.1.1.1")
        twice = cluster.rewrite_kubeconfig(once, "2.2.2.2")
        assert "server: https://2.2.2.2:6443" in twice
        assert "1.1.1.1" not in twice


class TestKubeconfigPath:
    def test_named_per_target(self):
        assert cluster.kubeconfig_path("lab-01").name == "lab-01.yaml"

    def test_lives_under_ssherpa_home(self):
        assert ".ssherpa" in str(cluster.kubeconfig_path("lab-01"))
