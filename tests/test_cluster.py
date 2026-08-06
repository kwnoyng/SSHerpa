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

    def test_k3s_configures_before_installing(self):
        # config.yaml 이 설치보다 먼저 있어야 첫 기동 인증서에 SAN 이 들어간다
        steps = distro_mod.get("k3s").install_steps("h")
        assert "config.yaml" in steps[0].command
        assert "get.k3s.io" in steps[1].command

    def test_rke2_installs_then_starts(self):
        # RKE2 는 설치와 기동이 분리돼 있다
        steps = distro_mod.get("rke2").install_steps("h")
        assert len(steps) > 1
        assert any("systemctl" in step.command for step in steps)

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_tls_san_lives_in_config_file_not_flags(self, name):
        """플래그에 박으면 주소가 바뀌었을 때 고칠 방법이 없다."""
        steps = distro_mod.get(name).install_steps("10.0.0.1")
        config_steps = [s for s in steps if "config.yaml" in s.command]
        assert len(config_steps) == 1
        assert "10.0.0.1" in config_steps[0].command
        # 설치 명령 자체에는 주소가 없어야 한다
        install = [s for s in steps if "curl" in s.command]
        assert all("10.0.0.1" not in s.command for s in install)

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_every_step_has_a_label(self, name):
        for step in distro_mod.get(name).install_steps("h"):
            assert step.label.strip()


class TestAgentJoinSteps:
    """합류 절차는 배포판마다 문·포트가 다르다."""

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_token_and_server_address_are_carried(self, name):
        steps = distro_mod.get(name).agent_install_steps("10.0.0.1", "sekret")
        joined = "\n".join(s.command for s in steps)
        assert "10.0.0.1" in joined
        assert "sekret" in joined

    def test_k3s_joins_through_the_api_port(self):
        command = distro_mod.get("k3s").agent_install_steps("10.0.0.1", "t")[0].command
        assert "K3S_URL=https://10.0.0.1:6443" in command
        assert "K3S_TOKEN=" in command

    def test_rke2_joins_through_the_supervisor_port(self):
        # 9345 다. 6443 으로 보내면 붙는 듯하다 조용히 실패한다.
        joined = "\n".join(
            s.command for s in distro_mod.get("rke2").agent_install_steps("10.0.0.1", "t")
        )
        assert "9345" in joined
        assert "https://10.0.0.1:6443" not in joined

    def test_rke2_installs_as_agent_not_server(self):
        joined = "\n".join(
            s.command for s in distro_mod.get("rke2").agent_install_steps("h", "t")
        )
        assert "INSTALL_RKE2_TYPE=agent" in joined
        assert "rke2-agent" in joined

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_token_is_read_as_root(self, name):
        chosen = distro_mod.get(name)
        assert chosen.read_token_command().startswith("sudo ")
        assert chosen.token_path in chosen.read_token_command()

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_joining_allows_more_time_than_default(self, name):
        steps = distro_mod.get(name).agent_install_steps("h", "t")
        assert max(s.timeout for s in steps) >= 300


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


class TestHintCommands:
    """힌트에 적히는 명령은 사용자가 그대로 붙여넣어 동작해야 한다.

    vm 모드에서 node.target.name 은 'lab-01/ssherpa-node-1' 같은 합성 이름이다.
    그대로 안내하면 등록된 적 없는 타겟을 시키게 되어 'target not found' 로 끝난다.
    """

    HOST_NODE = cluster.nodes_for_host_mode(TARGET)[0]
    VM_NODE = cluster.Node(
        name="lab-01-vm",
        target=Target(
            name="lab-01/ssherpa-node-1", host="192.168.122.5", user="ssherpa"
        ),
        cli_name="lab-01",
        in_vm=True,
    )

    def test_host_mode_addresses_the_registered_target(self):
        assert self.HOST_NODE.cli_ssh() == "ssherpa ssh lab-01"
        assert self.HOST_NODE.cli_down() == "ssherpa down lab-01"
        assert self.HOST_NODE.cli_up() == "ssherpa up lab-01"

    def test_vm_mode_never_leaks_the_compound_name(self):
        for command in (
            self.VM_NODE.cli_ssh(),
            self.VM_NODE.cli_up(),
            self.VM_NODE.cli_down(),
        ):
            assert "/" not in command

    def test_vm_mode_reaches_inside_the_vm(self):
        # 클러스터는 VM 안에서 돈다 — --vm 없이 들어가면 호스트에 닿는다
        assert self.VM_NODE.cli_ssh() == "ssherpa ssh lab-01 --vm"
        assert self.VM_NODE.cli_up() == "ssherpa up lab-01 --vm"

    def test_down_needs_no_mode_flag(self):
        # down 은 호스트에 무엇이 있든 스스로 찾는다
        assert self.VM_NODE.cli_down() == "ssherpa down lab-01"

    def test_flags_precede_the_mode_flag(self):
        assert self.HOST_NODE.cli_up("--distro k3s") == "ssherpa up lab-01 --distro k3s"

    def test_anonymous_target_leaves_no_double_space(self):
        anonymous = cluster.nodes_for_host_mode(
            Target(name=None, host="10.0.0.1", user="admin")
        )[0]
        for command in (anonymous.cli_ssh(), anonymous.cli_up(), anonymous.cli_down()):
            assert "  " not in command
            assert command == command.strip()

    def test_ready_timeout_hint_points_into_the_vm(self, monkeypatch):
        # 힌트를 짓는 쪽이 아니라 실제로 터지는 자리에서 확인한다.
        monkeypatch.setattr(cluster, "READY_TIMEOUT", 0)
        with pytest.raises(cluster.ClusterError) as caught:
            cluster.wait_for_ready(self.VM_NODE, distro_mod.get("k3s"))
        joined = "\n".join(caught.value.hints)
        assert "ssherpa ssh lab-01 --vm" in joined
        assert "ssherpa-node-1" not in joined


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


class TestCertificateCoverage:
    """IP 가 바뀐 뒤 재실행하면 kubeconfig 만 갱신되고 인증서는 옛 주소로
    남는다. 그 상태를 감지하지 못하면 '성공했는데 kubectl 이 전부 거부되는'
    kubeconfig 를 내주게 된다."""

    # 실서버(gcp-lab)의 openssl 출력 그대로
    OPENSSL_OUT = (
        "X509v3 Subject Alternative Name: \n"
        "    DNS:kubernetes, DNS:kubernetes.default, "
        "DNS:kubernetes.default.svc, DNS:kubernetes.default.svc.cluster.local, "
        "DNS:localhost, DNS:ssherpa-lab.asia-northeast3-c.c.ssherpa.internal, "
        "IP Address:34.50.34.61, IP Address:127.0.0.1, "
        "IP Address:10.178.0.2, IP Address:10.43.0.1\n"
    )

    def test_parses_both_dns_and_ip_entries(self):
        sans = cluster.parse_san_list(self.OPENSSL_OUT)
        assert "34.50.34.61" in sans
        assert "localhost" in sans
        assert "kubernetes.default.svc.cluster.local" in sans

    def test_exact_match_only(self):
        # '4.50.34.6' 같은 부분 문자열이 통과하면 안 된다
        sans = cluster.parse_san_list(self.OPENSSL_OUT)
        assert "4.50.34.6" not in sans
        assert "34.50.34.615" not in sans

    def _with_output(self, monkeypatch, rc, stdout):
        from ssherpa.ssh import CommandResult

        def _run(*_args, **_kwargs):
            return CommandResult(rc, stdout, "")

        monkeypatch.setattr(cluster, "run", _run)

    def test_covered_address(self, monkeypatch):
        self._with_output(monkeypatch, 0, self.OPENSSL_OUT)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        assert cluster.certificate_covers(node, distro_mod.get("k3s"), "34.50.34.61") is True

    def test_changed_address_is_detected(self, monkeypatch):
        self._with_output(monkeypatch, 0, self.OPENSSL_OUT)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        assert cluster.certificate_covers(node, distro_mod.get("k3s"), "35.1.2.3") is False

    def test_hostname_target_works(self, monkeypatch):
        self._with_output(monkeypatch, 0, self.OPENSSL_OUT)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        assert cluster.certificate_covers(node, distro_mod.get("k3s"), "localhost") is True

    def test_unreadable_cert_is_indeterminate(self, monkeypatch):
        # openssl 이 없거나 파일이 없으면 판단 불가 — 막지 않는다
        self._with_output(monkeypatch, 1, "")
        node = cluster.nodes_for_host_mode(TARGET)[0]
        assert cluster.certificate_covers(node, distro_mod.get("k3s"), "x") is None

    @pytest.mark.parametrize("name", ["k3s", "rke2"])
    def test_refresh_rewrites_config_and_restarts(self, name):
        chosen = distro_mod.get(name)
        step = chosen.refresh_certificate_step("35.1.2.3")
        assert "35.1.2.3" in step.command
        assert chosen.config_path in step.command
        assert f"systemctl restart {chosen.service}" in step.command


class TestUpHealOrchestration:
    """up() 이 인증서 불일치를 만나면 치유 → 재검증까지 스스로 해야 한다.

    부품(certificate_covers, refresh step)은 실서버에서 검증했지만,
    가짜 주소로는 SSH 가 안 되므로 분기 자체는 여기서 mock 으로 돈다.
    """

    def _wire(self, monkeypatch, tmp_path, coverage_answers):
        """up() 의 원격 의존을 전부 가짜로 바꾼다."""
        calls = {"steps": [], "coverage": list(coverage_answers)}

        fake_kubeconfig = tmp_path / "kc.yaml"
        fake_kubeconfig.write_text("apiVersion: v1\n")

        monkeypatch.setattr(cluster, "is_installed", lambda *_a: True)
        monkeypatch.setattr(cluster, "wait_for_ready", lambda *_a: None)
        monkeypatch.setattr(cluster, "api_reachable", lambda *_a, **_k: True)
        monkeypatch.setattr(cluster, "fetch_kubeconfig", lambda *_a: fake_kubeconfig)
        monkeypatch.setattr(
            cluster.kubeconf,
            "merge",
            lambda *_a, **_k: cluster.kubeconf.MergeResult(
                context="ssherpa-t", became_current=True, backup=None
            ),
        )
        monkeypatch.setattr(
            cluster,
            "certificate_covers",
            lambda *_a: calls["coverage"].pop(0),
        )
        monkeypatch.setattr(
            cluster,
            "_run_step",
            lambda _node, step: calls["steps"].append(step.label),
        )
        return calls

    def test_mismatch_triggers_refresh_and_reports_it(self, monkeypatch, tmp_path):
        calls = self._wire(monkeypatch, tmp_path, coverage_answers=[False, True])
        node = cluster.nodes_for_host_mode(TARGET)[0]

        result = cluster.up(node, distro_mod.get("k3s"))

        assert calls["steps"] == ["refresh certificate"]
        assert result.certificate_refreshed is True

    def test_matching_certificate_is_left_alone(self, monkeypatch, tmp_path):
        calls = self._wire(monkeypatch, tmp_path, coverage_answers=[True])
        node = cluster.nodes_for_host_mode(TARGET)[0]

        result = cluster.up(node, distro_mod.get("k3s"))

        assert calls["steps"] == []
        assert result.certificate_refreshed is False

    def test_indeterminate_certificate_does_not_block(self, monkeypatch, tmp_path):
        # openssl 이 없어 판단 불가(None)면 치유를 시도하지 않고 진행한다
        calls = self._wire(monkeypatch, tmp_path, coverage_answers=[None])
        node = cluster.nodes_for_host_mode(TARGET)[0]

        result = cluster.up(node, distro_mod.get("k3s"))

        assert calls["steps"] == []
        assert result.certificate_refreshed is False

    def test_failed_heal_raises_instead_of_lying(self, monkeypatch, tmp_path):
        # 치유했는데도 인증서가 그대로면 'Cluster ready' 라고 하면 안 된다
        self._wire(monkeypatch, tmp_path, coverage_answers=[False, False])
        node = cluster.nodes_for_host_mode(TARGET)[0]

        with pytest.raises(cluster.ClusterError, match="still does not cover"):
            cluster.up(node, distro_mod.get("k3s"))

    def test_api_address_override_reaches_cert_and_kubeconfig(
        self, monkeypatch, tmp_path
    ):
        # vm 모드: 노드 주소(NAT 안의 VM)와 kubectl 이 접속할 주소(호스트)가
        # 다르다. 오버라이드가 인증서 검사와 kubeconfig 양쪽에 스며야 한다 —
        # 한쪽만 바뀌면 '접속은 되는데 인증서가 거부'가 된다.
        self._wire(monkeypatch, tmp_path, coverage_answers=[True])
        seen = {}
        fake_kc = tmp_path / "kc2.yaml"
        fake_kc.write_text("apiVersion: v1\n")

        def record_cert(_node, _distro, address):
            seen["certificate"] = address
            return True

        def record_fetch(_node, _distro, address):
            seen["kubeconfig"] = address
            return fake_kc

        monkeypatch.setattr(cluster, "certificate_covers", record_cert)
        monkeypatch.setattr(cluster, "fetch_kubeconfig", record_fetch)

        vm_node = cluster.Node(
            name="gcp-lab-vm",
            target=Target(name=None, host="192.168.122.160", user="ssherpa"),
        )
        cluster.up(vm_node, distro_mod.get("k3s"), api_address="34.22.85.249")

        assert seen == {
            "certificate": "34.22.85.249",
            "kubeconfig": "34.22.85.249",
        }


class TestCountReady:
    """'NotReady' 도 두 번째 칸에 온다 — 부분 문자열로 세면 안 된다."""

    OUTPUT = (
        "ssherpa-node-1   Ready      control-plane   5m    v1.36.2+k3s1\n"
        "ssherpa-node-2   Ready      <none>          1m    v1.36.2+k3s1\n"
        "ssherpa-node-3   NotReady   <none>          10s   v1.36.2+k3s1\n"
    )

    def test_counts_only_ready(self):
        assert cluster.count_ready(self.OUTPUT) == 2

    def test_empty_output(self):
        assert cluster.count_ready("") == 0


class TestJoin:
    """agent 는 server 가 발급한 토큰으로만 들어올 수 있다."""

    def _nodes(self, count):
        return [
            cluster.Node(
                name=f"n{i}",
                target=Target(name=None, host=f"192.168.122.{10 + i}", user="ssherpa"),
                role="server" if i == 1 else "agent",
            )
            for i in range(1, count + 1)
        ]

    def test_token_is_read_from_the_server(self, monkeypatch):
        seen = {}

        def fake_run(target, command, timeout=30):  # noqa: ARG001
            seen["host"] = target.host
            return cluster.CommandResult(0, "K10abc::server:secret\n", "")

        monkeypatch.setattr(cluster, "run", fake_run)
        nodes = self._nodes(2)
        token = cluster.read_token(nodes[0], distro_mod.get("k3s"))
        assert token == "K10abc::server:secret"
        assert seen["host"] == "192.168.122.11"  # agent 가 아니라 server 에게 묻는다

    def test_missing_token_is_not_silently_empty(self, monkeypatch):
        # 빈 토큰으로 합류를 시도하면 agent 가 알 수 없는 이유로 실패한다
        monkeypatch.setattr(
            cluster, "run", lambda *a, **k: cluster.CommandResult(0, "\n", "")  # noqa: ARG005
        )
        with pytest.raises(cluster.ClusterError, match="join token"):
            cluster.read_token(self._nodes(1)[0], distro_mod.get("k3s"))

    def test_agents_are_pointed_at_the_server_node_address(self, monkeypatch):
        # 밖에서 닿는 주소가 아니라 노드 주소로 붙어야 한다 — NAT 안에서는
        # 호스트 주소로 자기 자신을 찾아갈 수 없다.
        commands = []
        monkeypatch.setattr(
            cluster, "run", lambda *a, **k: cluster.CommandResult(0, "tok\n", "")  # noqa: ARG005
        )
        monkeypatch.setattr(
            cluster, "_run_step", lambda _n, step: commands.append(step.command)
        )
        nodes = self._nodes(3)
        cluster.join_agents(nodes[0], nodes[1:], distro_mod.get("k3s"))
        assert len(commands) == 2  # agent 두 대
        assert all("https://192.168.122.11:6443" in c for c in commands)
        assert all("K3S_TOKEN='tok'" in c for c in commands)


class TestUpWithAgents:
    """멀티노드: server 를 세우고, 토큰을 읽고, 나머지를 합류시킨다."""

    def _wire(self, monkeypatch, tmp_path, installed):
        calls = {"steps": [], "waits": []}
        kc = tmp_path / "kc.yaml"
        kc.write_text("apiVersion: v1\n")

        monkeypatch.setattr(cluster, "is_installed", lambda node, _d: installed[node.name])
        monkeypatch.setattr(cluster, "api_reachable", lambda *_a, **_k: True)
        monkeypatch.setattr(cluster, "fetch_kubeconfig", lambda *_a: kc)
        monkeypatch.setattr(cluster, "certificate_covers", lambda *_a: True)
        monkeypatch.setattr(
            cluster.kubeconf,
            "merge",
            lambda *_a, **_k: cluster.kubeconf.MergeResult(
                context="c", became_current=True, backup=None
            ),
        )
        monkeypatch.setattr(
            cluster,
            "wait_for_ready",
            lambda _n, _d, expected=1: calls["waits"].append(expected),
        )
        monkeypatch.setattr(
            cluster, "run", lambda *a, **k: cluster.CommandResult(0, "tok\n", "")  # noqa: ARG005
        )
        monkeypatch.setattr(
            cluster, "_run_step", lambda node, step: calls["steps"].append(
                (node.name, step.label)
            )
        )
        return calls

    def _nodes(self):
        return [
            cluster.Node(
                name=f"n{i}",
                target=Target(name=None, host=f"10.0.0.{i}", user="u"),
                role="server" if i == 1 else "agent",
            )
            for i in (1, 2, 3)
        ]

    def test_waits_for_every_node(self, monkeypatch, tmp_path):
        nodes = self._nodes()
        calls = self._wire(
            monkeypatch, tmp_path, {"n1": False, "n2": False, "n3": False}
        )
        result = cluster.up(
            nodes[0], distro_mod.get("k3s"), agents=nodes[1:], api_address="1.2.3.4"
        )
        # server 혼자 Ready 를 먼저 확인하고, 합류 뒤 전체를 다시 확인한다
        assert calls["waits"] == [1, 3]
        assert result.node_count == 3

    def test_agents_install_after_the_server(self, monkeypatch, tmp_path):
        nodes = self._nodes()
        calls = self._wire(
            monkeypatch, tmp_path, {"n1": False, "n2": False, "n3": False}
        )
        cluster.up(nodes[0], distro_mod.get("k3s"), agents=nodes[1:])
        who = [name for name, _label in calls["steps"]]
        assert who.index("n1") < who.index("n2")
        assert {"n2", "n3"} <= set(who)

    def test_already_joined_agents_are_left_alone(self, monkeypatch, tmp_path):
        # 재실행이 안전해야 한다 — 이미 붙어 있는 노드를 다시 설치하지 않는다
        nodes = self._nodes()
        calls = self._wire(
            monkeypatch, tmp_path, {"n1": True, "n2": True, "n3": True}
        )
        cluster.up(nodes[0], distro_mod.get("k3s"), agents=nodes[1:])
        assert calls["steps"] == []
        assert calls["waits"] == [1, 3]

    def test_single_node_behaves_as_before(self, monkeypatch, tmp_path):
        nodes = self._nodes()
        calls = self._wire(monkeypatch, tmp_path, {"n1": True})
        result = cluster.up(nodes[0], distro_mod.get("k3s"))
        assert calls["waits"] == [1]  # 전체 대기 단계가 없다
        assert result.node_count == 1


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


class TestStepLabels:
    """어느 노드의 일인지 보이지 않으면 이미 있는 노드까지 새로 만드는
    것처럼 읽힌다 — 실사용에서 실제로 그렇게 오해됐다."""

    def test_short_name_is_used_when_given(self):
        node = cluster.Node(
            name="gcp-lab-node-2", target=TARGET, short_name="node-2"
        )
        assert node.label() == "node-2"

    def test_falls_back_to_the_full_name(self):
        assert cluster.Node(name="lab-01", target=TARGET).label() == "lab-01"

    def test_join_steps_say_which_node(self, monkeypatch):
        labels = []

        class Reporter:
            def step(self, label):
                labels.append(label)
                from contextlib import nullcontext

                return nullcontext()

        monkeypatch.setattr(
            cluster, "run", lambda *a, **k: cluster.CommandResult(0, "tok\n", "")  # noqa: ARG005
        )
        monkeypatch.setattr(cluster, "_run_step", lambda *_a: None)
        agents = [
            cluster.Node(name="lab-node-2", target=TARGET, short_name="node-2"),
            cluster.Node(name="lab-node-3", target=TARGET, short_name="node-3"),
        ]
        server = cluster.Node(name="lab-node-1", target=TARGET, short_name="node-1")
        cluster.join_agents(server, agents, distro_mod.get("k3s"), Reporter())

        assert any(label.startswith("node-2: ") for label in labels)
        assert any(label.startswith("node-3: ") for label in labels)


class TestServiceNameInHints:
    """유닛 이름은 배포판이 정한다 — 이름에서 지어내면 k3s 쪽이 틀린다."""

    def test_hint_uses_the_declared_service(self, monkeypatch):
        monkeypatch.setattr(cluster, "run", lambda *a, **k: cluster.CommandResult(0, "", ""))  # noqa: ARG005
        monkeypatch.setattr(cluster, "READY_TIMEOUT", 0)
        node = cluster.nodes_for_host_mode(TARGET)[0]
        for name in ("k3s", "rke2"):
            chosen = distro_mod.get(name)
            with pytest.raises(cluster.ClusterError) as excinfo:
                cluster.wait_for_ready(node, chosen)
            line = next(h for h in excinfo.value.hints if "journalctl" in h)
            assert f"-u {chosen.service} " in line

    def test_k3s_unit_is_not_k3s_server(self):
        # 실측: 'k3s-server' 는 존재하지 않아 journalctl 이 빈 결과를 낸다
        assert distro_mod.get("k3s").service == "k3s"


class TestForeignConfigIsNotOverwritten:
    """tls-san 은 설정 파일을 통째로 다시 써서 넣는다. 이미 k3s 가 깔린
    호스트를 타겟으로 잡으면 그 파일은 사용자의 것일 수 있다.

    실측(재현): disable/kube-apiserver-arg/node-label 이 적힌 config.yaml 이
    두 줄짜리 tls-san 으로 바뀌고 k3s 가 재시작돼, 꺼뒀던 traefik 이
    파드 4개로 되살아났다.
    """

    def _judge(self, monkeypatch, stdout):
        monkeypatch.setattr(
            cluster, "run", lambda *a, **k: cluster.CommandResult(0, stdout, "")  # noqa: ARG005
        )
        node = cluster.nodes_for_host_mode(TARGET)[0]
        return cluster.config_is_ours(node, distro_mod.get("k3s"))

    def test_absent_file_is_ours_to_create(self, monkeypatch):
        assert self._judge(monkeypatch, f"{distro_mod.CONFIG_OURS}\n") is True

    def test_marked_file_is_ours(self, monkeypatch):
        assert self._judge(monkeypatch, f"{distro_mod.CONFIG_OURS}\n") is True

    def test_foreign_file_is_not(self, monkeypatch):
        assert self._judge(monkeypatch, f"{distro_mod.CONFIG_FOREIGN}\n") is False

    def test_probe_recognises_our_own_shape(self):
        # 표식을 남기기 전(0.5.0 이하) 우리가 쓰던 파일은 tls-san 만 있다.
        # 그것까지 남의 것으로 보면 기존 사용자가 갱신을 못 하게 된다.
        command = distro_mod.get("k3s").config_ownership_command()
        assert "tls-san:" in command
        assert distro_mod.CONFIG_MARKER in command

    def test_written_file_carries_the_marker(self):
        # 표식이 없으면 다음 실행이 '내가 쓴 것' 임을 알 수 없다
        command = distro_mod.get("k3s").tls_config_step("1.2.3.4").command
        assert distro_mod.CONFIG_MARKER in command

    def test_refresh_stops_instead_of_overwriting(self, monkeypatch, tmp_path):
        kc = tmp_path / "kc.yaml"
        kc.write_text("apiVersion: v1\n")
        steps = []
        monkeypatch.setattr(cluster, "is_installed", lambda *_a: True)
        monkeypatch.setattr(cluster, "wait_for_ready", lambda *_a, **_k: None)
        monkeypatch.setattr(cluster, "certificate_covers", lambda *_a: False)
        monkeypatch.setattr(cluster, "config_is_ours", lambda *_a: False)
        monkeypatch.setattr(cluster, "fetch_kubeconfig", lambda *_a: kc)
        monkeypatch.setattr(cluster, "_run_step", lambda _n, s: steps.append(s.label))

        node = cluster.nodes_for_host_mode(TARGET)[0]
        with pytest.raises(cluster.ClusterError) as excinfo:
            cluster.up(node, distro_mod.get("k3s"))

        assert "was not written by SSHerpa" in excinfo.value.message
        assert steps == []  # 파일을 건드리지 않았다
        assert any("tls-san:" in hint for hint in excinfo.value.hints)

    def test_install_also_refuses_a_prepared_file(self, monkeypatch):
        steps = []
        monkeypatch.setattr(cluster, "is_installed", lambda *_a: False)
        monkeypatch.setattr(cluster, "check_memory", lambda *_a: None)
        monkeypatch.setattr(cluster, "config_is_ours", lambda *_a: False)
        monkeypatch.setattr(cluster, "_run_step", lambda _n, s: steps.append(s.label))

        node = cluster.nodes_for_host_mode(TARGET)[0]
        with pytest.raises(cluster.ClusterError):
            cluster.up(node, distro_mod.get("k3s"))
        assert steps == []

    def test_ours_still_heals(self, monkeypatch, tmp_path):
        # 우리 파일이면 예전처럼 고쳐야 한다 — 가드가 치유를 막으면 안 된다
        kc = tmp_path / "kc.yaml"
        kc.write_text("apiVersion: v1\n")
        steps = []
        answers = [False, True]
        monkeypatch.setattr(cluster, "is_installed", lambda *_a: True)
        monkeypatch.setattr(cluster, "wait_for_ready", lambda *_a, **_k: None)
        monkeypatch.setattr(cluster, "certificate_covers", lambda *_a: answers.pop(0))
        monkeypatch.setattr(cluster, "config_is_ours", lambda *_a: True)
        monkeypatch.setattr(cluster, "fetch_kubeconfig", lambda *_a: kc)
        monkeypatch.setattr(cluster, "api_reachable", lambda *_a, **_k: True)
        monkeypatch.setattr(
            cluster.kubeconf, "merge",
            lambda *_a, **_k: cluster.kubeconf.MergeResult("c", True, None),
        )
        monkeypatch.setattr(cluster, "_run_step", lambda _n, s: steps.append(s.label))

        node = cluster.nodes_for_host_mode(TARGET)[0]
        result = cluster.up(node, distro_mod.get("k3s"))
        assert steps == ["refresh certificate"]
        assert result.certificate_refreshed is True
