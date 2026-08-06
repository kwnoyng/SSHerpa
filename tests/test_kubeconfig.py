"""~/.kube/config 병합.

이 파일은 사용자의 다른 클러스터 자격증명이 살고 있을 수 있는 곳이다.
여기 테스트의 대부분은 '우리 것만 만지고 남의 것은 절대 건드리지 않는다'
는 규칙을 고정한다.
"""

import pytest
import yaml

from ssherpa import kubeconfig

# fetch_kubeconfig 가 저장하는 형태 (k3s 원본, 주소는 이미 재작성됨)
FETCHED = """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: Q0EtREFUQQ==
    server: https://34.22.85.249:6443
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
    client-certificate-data: Q0VSVA==
    client-key-data: S0VZ
"""

# 사용자가 이미 갖고 있던 회사 클러스터 설정
EXISTING = """apiVersion: v1
kind: Config
clusters:
- name: company-eks
  cluster:
    server: https://eks.company.example
contexts:
- name: company-eks
  context:
    cluster: company-eks
    user: company-user
users:
- name: company-user
  user:
    token: SECRET
current-context: company-eks
"""


@pytest.fixture
def kube_path(tmp_path):
    return tmp_path / "config"


class TestMergeIntoFreshFile:
    """~/.kube/config 가 없던 사용자 (kubectl 처음 쓰는 사람)."""

    def test_creates_the_file(self, kube_path):
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        assert kube_path.exists()

    def test_entries_are_renamed_from_default(self, kube_path):
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        assert data["clusters"][0]["name"] == "ssherpa-gcp-lab"
        assert data["users"][0]["name"] == "ssherpa-gcp-lab"
        assert data["contexts"][0]["name"] == "ssherpa-gcp-lab"

    def test_context_wires_cluster_to_user(self, kube_path):
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        context = data["contexts"][0]["context"]
        assert context == {"cluster": "ssherpa-gcp-lab", "user": "ssherpa-gcp-lab"}

    def test_becomes_current_context(self, kube_path):
        # 비어 있던 사용자에겐 기본값까지 잡아줘야 kubectl 이 바로 된다
        result = kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        assert data["current-context"] == "ssherpa-gcp-lab"
        assert result.became_current is True

    def test_credentials_survive_the_copy(self, kube_path):
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        assert data["users"][0]["user"]["client-key-data"] == "S0VZ"
        assert data["clusters"][0]["cluster"]["server"] == "https://34.22.85.249:6443"

    def test_no_backup_when_there_was_nothing(self, kube_path):
        result = kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        assert result.backup is None


class TestMergeIntoExistingFile:
    """다른 클러스터를 이미 쓰던 사용자 — 절대 다치면 안 되는 경우."""

    def test_existing_entries_survive(self, kube_path):
        kube_path.write_text(EXISTING)
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        names = [c["name"] for c in data["clusters"]]
        assert "company-eks" in names and "ssherpa-gcp-lab" in names
        assert data["users"][0]["user"]["token"] == "SECRET"

    def test_current_context_is_not_stolen(self, kube_path):
        # 회사 클러스터를 쓰던 사람의 기본값을 몰래 바꾸면 그쪽 운영 사고다
        kube_path.write_text(EXISTING)
        result = kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        assert data["current-context"] == "company-eks"
        assert result.became_current is False

    def test_backup_is_written_first(self, kube_path):
        kube_path.write_text(EXISTING)
        result = kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        assert result.backup is not None
        assert "company-eks" in result.backup.read_text()

    def test_remerge_is_idempotent(self, kube_path):
        # IP 가 바뀌어 다시 병합해도 항목이 늘어나면 안 된다 — 교체돼야 한다
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        updated = FETCHED.replace("34.22.85.249", "35.1.2.3")
        kubeconfig.merge(updated, "gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        assert len(data["clusters"]) == 1
        assert data["clusters"][0]["cluster"]["server"] == "https://35.1.2.3:6443"

    def test_two_targets_coexist(self, kube_path):
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        kubeconfig.merge(FETCHED, "aws-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        names = {c["name"] for c in data["contexts"]}
        assert names == {"ssherpa-gcp-lab", "ssherpa-aws-lab"}


class TestMergeFailsSafely:
    def test_corrupt_file_raises_without_writing(self, kube_path):
        kube_path.write_text("{[not yaml")
        with pytest.raises(kubeconfig.KubeconfigError):
            kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        assert kube_path.read_text() == "{[not yaml"  # 원본 그대로

    def test_malformed_fetched_config_raises(self, kube_path):
        with pytest.raises(kubeconfig.KubeconfigError, match="malformed"):
            kubeconfig.merge("apiVersion: v1\n", "gcp-lab", path=kube_path)


class TestRemove:
    def test_removes_only_our_entries(self, kube_path):
        kube_path.write_text(EXISTING)
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        changed = kubeconfig.remove("gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        assert changed is True
        assert [c["name"] for c in data["clusters"]] == ["company-eks"]
        assert data["current-context"] == "company-eks"

    def test_clears_current_context_if_it_was_ours(self, kube_path):
        # 죽은 클러스터가 기본값으로 남으면 다음 kubectl 이 영문 모를 실패를 한다
        kubeconfig.merge(FETCHED, "gcp-lab", path=kube_path)
        kubeconfig.remove("gcp-lab", path=kube_path)
        data = yaml.safe_load(kube_path.read_text())
        assert data["current-context"] == ""

    def test_missing_file_is_fine(self, kube_path):
        assert kubeconfig.remove("gcp-lab", path=kube_path) is False

    def test_nothing_of_ours_is_a_noop(self, kube_path):
        kube_path.write_text(EXISTING)
        before = kube_path.read_text()
        assert kubeconfig.remove("gcp-lab", path=kube_path) is False
        assert kube_path.read_text() == before


class TestEmptiedByKubectl:
    """kubectl 은 마지막 항목을 지우면 키를 남기고 값만 null 로 쓴다.

    실측: `kubectl config delete-cluster` 로 정리한 직후의 ~/.kube/config 를
    '망가진 파일' 로 보고 병합을 거부했다. 방금 정리한 사용자가 벌을 받는 셈이다.
    """

    EMPTIED = (
        "apiVersion: v1\n"
        "clusters: null\n"
        "contexts: null\n"
        "current-context: ssherpa-gone\n"
        "kind: Config\n"
        "users: null\n"
    )

    def test_null_sections_are_treated_as_empty(self, tmp_path):
        path = tmp_path / "config"
        path.write_text(self.EMPTIED, encoding="utf-8")
        result = kubeconfig.merge(FETCHED, "lab-01", path=path)
        assert result.context == "ssherpa-lab-01"

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert [c["name"] for c in data["clusters"]] == ["ssherpa-lab-01"]
        assert [c["name"] for c in data["contexts"]] == ["ssherpa-lab-01"]

    def test_dangling_current_context_is_replaced(self, tmp_path):
        # 가리키는 컨텍스트가 없는 current-context 를 존중하면, kubectl 은
        # 계속 없는 것을 찾는다 — 비어 있는 것으로 본다
        path = tmp_path / "config"
        path.write_text(self.EMPTIED, encoding="utf-8")
        result = kubeconfig.merge(FETCHED, "lab-01", path=path)
        assert result.became_current is True

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["current-context"] == "ssherpa-lab-01"

    def test_a_real_current_context_is_still_respected(self, tmp_path):
        path = tmp_path / "config"
        path.write_text(
            "apiVersion: v1\n"
            "kind: Config\n"
            "clusters:\n"
            "- name: prod\n"
            "  cluster: {server: 'https://prod:6443'}\n"
            "contexts:\n"
            "- name: prod\n"
            "  context: {cluster: prod, user: prod}\n"
            "users:\n"
            "- name: prod\n"
            "  user: {}\n"
            "current-context: prod\n",
            encoding="utf-8",
        )
        result = kubeconfig.merge(FETCHED, "lab-01", path=path)
        assert result.became_current is False

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["current-context"] == "prod"


class TestNeverStealsAnotherDefault:
    """KUBECONFIG 로 파일을 여러 개 엮어 쓰면 컨텍스트 정의는 다른 파일에
    있고 current-context 만 ~/.kube/config 에 남는다. 그걸 고아로 오판해
    덮어쓰면 운영 클러스터를 쓰던 사람의 기본값이 조용히 랩으로 바뀐다.
    """

    def test_context_defined_elsewhere_is_left_alone(self, tmp_path):
        path = tmp_path / "config"
        path.write_text(
            "apiVersion: v1\n"
            "kind: Config\n"
            "clusters:\n"
            "- name: staging\n"
            "  cluster: {server: 'https://staging:6443'}\n"
            "contexts:\n"
            "- name: staging\n"
            "  context: {cluster: staging, user: staging}\n"
            "users:\n"
            "- name: staging\n"
            "  user: {}\n"
            # prod-eks 는 KUBECONFIG 의 다른 파일에 정의돼 있다
            "current-context: prod-eks\n",
            encoding="utf-8",
        )
        result = kubeconfig.merge(FETCHED, "lab-01", path=path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["current-context"] == "prod-eks"
        assert result.became_current is False

    def test_first_context_in_an_empty_file_may_claim_it(self, tmp_path):
        # 컨텍스트가 하나도 없던 파일에는 빼앗을 남의 것이 없다
        path = tmp_path / "config"
        path.write_text(
            "apiVersion: v1\nkind: Config\n"
            "clusters: null\ncontexts: null\nusers: null\n"
            "current-context: ssherpa-gone\n",
            encoding="utf-8",
        )
        result = kubeconfig.merge(FETCHED, "lab-01", path=path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["current-context"] == "ssherpa-lab-01"
        assert result.became_current is True
