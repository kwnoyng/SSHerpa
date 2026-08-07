"""인벤토리 CRUD.

SSHERPA_INVENTORY 로 임시 파일을 가리켜 실제 홈 디렉토리를 건드리지 않는다.
"""

import pytest
import yaml

from ssherpa import inventory
from ssherpa.inventory import (
    InventoryError,
    add_target,
    get_target,
    inventory_path,
    list_targets,
    remove_target,
)


@pytest.fixture(autouse=True)
def temp_inventory(tmp_path, monkeypatch):
    path = tmp_path / "inventory.yml"
    monkeypatch.setenv("SSHERPA_INVENTORY", str(path))
    return path


class TestAdd:
    def test_roundtrip(self):
        add_target("lab-01", host="10.0.0.1", user="admin", port=2222, key="~/.ssh/k")
        target = get_target("lab-01")
        assert (target.host, target.user, target.port) == ("10.0.0.1", "admin", 2222)
        assert target.key == "~/.ssh/k"

    def test_unspecified_port_stays_unspecified(self, temp_inventory):
        # 우리가 22 를 채워 기록하면 접속 때 -p 22 가 강제되어
        # ~/.ssh/config 의 Port 설정을 덮어써 버린다
        add_target("lab-01", host="10.0.0.1", user="admin")
        assert get_target("lab-01").port is None
        data = yaml.safe_load(temp_inventory.read_text())
        assert "ansible_port" not in data["all"]["hosts"]["lab-01"]

    def test_alias_only_registration(self, temp_inventory):
        # ~/.ssh/config 별칭만으로 등록 — user 도 port 도 없다
        add_target("mylab", host="lab")
        target = get_target("mylab")
        assert (target.host, target.user, target.port) == ("lab", None, None)
        data = yaml.safe_load(temp_inventory.read_text())
        assert data["all"]["hosts"]["mylab"] == {"ansible_host": "lab"}

    def test_key_is_omitted_when_not_given(self, temp_inventory):
        add_target("lab-01", host="10.0.0.1", user="admin")
        data = yaml.safe_load(temp_inventory.read_text())
        assert "ansible_ssh_private_key_file" not in data["all"]["hosts"]["lab-01"]

    def test_duplicate_is_rejected(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        with pytest.raises(InventoryError, match="already registered"):
            add_target("lab-01", host="10.0.0.2", user="root")

    @pytest.mark.parametrize("name", ["bad name", "-leading", "with/slash", "", "a b"])
    def test_invalid_names_rejected(self, name):
        with pytest.raises(InventoryError, match="not a valid target name"):
            add_target(name, host="10.0.0.1", user="admin")

    @pytest.mark.parametrize("name", ["lab-01", "dev_1", "host.example", "a"])
    def test_valid_names_accepted(self, name):
        add_target(name, host="10.0.0.1", user="admin")
        assert get_target(name).name == name

    @pytest.mark.parametrize(
        "host", ["-oProxyCommand=touch /tmp/pwn", "-J evil", "-p2222"]
    )
    def test_addresses_ssh_would_read_as_options_are_rejected(self, host):
        # 인벤토리는 한 번 적히면 이후 모든 명령이 말없이 쓰는 값이다.
        # 대화형 `ssherpa ssh` 는 추가 인자를 그대로 넘겨야 해서 '--' 로
        # 막을 수 없으므로, 들어오는 자리에서 거른다.
        with pytest.raises(InventoryError, match="not a valid address"):
            add_target("lab-01", host=host, user="admin")

    def test_rejected_address_is_not_persisted(self, temp_inventory):
        with pytest.raises(InventoryError):
            add_target("lab-01", host="-oProxyCommand=x", user="admin")
        assert not temp_inventory.exists()

    @pytest.mark.parametrize("host", ["10.0.0.1", "lab", "host.example.com"])
    def test_real_addresses_still_accepted(self, host):
        add_target("lab-01", host=host)
        assert get_target("lab-01").host == host


class TestAnsibleFormat:
    """v0.2 에서 Ansible 롤을 변환 없이 붙이려면 이 구조가 유지돼야 한다."""

    def test_uses_ansible_variable_names(self, temp_inventory):
        add_target("lab-01", host="10.0.0.1", user="admin", port=2222, key="/k")
        data = yaml.safe_load(temp_inventory.read_text())
        assert data["all"]["hosts"]["lab-01"] == {
            "ansible_host": "10.0.0.1",
            "ansible_user": "admin",
            "ansible_port": 2222,
            "ansible_ssh_private_key_file": "/k",
        }


class TestListAndRemove:
    def test_empty_inventory(self):
        assert list_targets() == []

    def test_sorted_by_name(self):
        for name in ["zulu", "alpha", "mike"]:
            add_target(name, host="10.0.0.1", user="admin")
        assert [t.name for t in list_targets()] == ["alpha", "mike", "zulu"]

    def test_remove(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        remove_target("lab-01")
        assert list_targets() == []

    def test_remove_missing_lists_known_targets(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        with pytest.raises(InventoryError, match="lab-01"):
            remove_target("nope")

    def test_get_missing_lists_known_targets(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        with pytest.raises(InventoryError, match="lab-01"):
            get_target("nope")


class TestFileHandling:
    def test_missing_file_is_not_an_error(self):
        assert list_targets() == []

    def test_corrupt_yaml_reports_path(self, temp_inventory):
        temp_inventory.write_text("all: [unclosed\n")
        with pytest.raises(InventoryError, match="Could not read inventory file"):
            list_targets()

    def test_non_mapping_yaml_rejected(self, temp_inventory):
        temp_inventory.write_text("- just\n- a list\n")
        with pytest.raises(InventoryError, match="Invalid inventory format"):
            list_targets()

    def test_empty_hosts_block(self, temp_inventory):
        temp_inventory.write_text("all:\n  hosts:\n")
        assert list_targets() == []

    def test_env_override_is_honored(self, temp_inventory):
        assert inventory_path() == temp_inventory

    def test_a_port_that_is_not_a_number_is_our_error(self, temp_inventory):
        # 문서가 손으로 고쳐도 된다고 열어둔 파일이다. 잘못 적힌 값에
        # int() 의 스택트레이스로 답하면 안 된다.
        temp_inventory.write_text(
            "all:\n  hosts:\n    lab-01:\n"
            "      ansible_host: 10.0.0.1\n"
            "      ansible_port: 'abc'\n"
        )
        with pytest.raises(InventoryError, match="not a number"):
            get_target("lab-01")

    def test_the_bad_port_error_says_where_to_fix_it(self, temp_inventory):
        temp_inventory.write_text(
            "all:\n  hosts:\n    lab-01:\n"
            "      ansible_host: 10.0.0.1\n      ansible_port: []\n"
        )
        with pytest.raises(InventoryError) as excinfo:
            list_targets()
        assert str(temp_inventory) in str(excinfo.value)

    def test_a_port_written_as_a_string_still_works(self, temp_inventory):
        # YAML 에서 따옴표를 붙이는 건 흔한 일이고, 잘못은 아니다
        temp_inventory.write_text(
            "all:\n  hosts:\n    lab-01:\n"
            "      ansible_host: 10.0.0.1\n      ansible_port: '2222'\n"
        )
        assert get_target("lab-01").port == 2222

    def test_parent_directory_is_created(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "inventory.yml"
        monkeypatch.setenv("SSHERPA_INVENTORY", str(nested))
        add_target("lab-01", host="10.0.0.1", user="admin")
        assert nested.exists()


def test_default_path_is_under_home(monkeypatch):
    monkeypatch.delenv("SSHERPA_INVENTORY", raising=False)
    assert inventory.inventory_path().name == "inventory.yml"
    assert ".ssherpa" in str(inventory.inventory_path())
