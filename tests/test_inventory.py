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

    def test_port_defaults_to_22(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        assert get_target("lab-01").port == 22

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

    def test_parent_directory_is_created(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "inventory.yml"
        monkeypatch.setenv("SSHERPA_INVENTORY", str(nested))
        add_target("lab-01", host="10.0.0.1", user="admin")
        assert nested.exists()


def test_default_path_is_under_home(monkeypatch):
    monkeypatch.delenv("SSHERPA_INVENTORY", raising=False)
    assert inventory.inventory_path().name == "inventory.yml"
    assert ".ssherpa" in str(inventory.inventory_path())
