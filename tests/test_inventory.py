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
    update_target,
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
        assert list_targets() == ([], [])

    def test_sorted_by_name(self):
        for name in ["zulu", "alpha", "mike"]:
            add_target(name, host="10.0.0.1", user="admin")
        targets, broken = list_targets()
        assert [t.name for t in targets] == ["alpha", "mike", "zulu"]
        assert broken == []

    def test_remove(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        remove_target("lab-01")
        assert list_targets() == ([], [])

    def test_remove_missing_lists_known_targets(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        with pytest.raises(InventoryError, match="lab-01"):
            remove_target("nope")

    def test_get_missing_lists_known_targets(self):
        add_target("lab-01", host="10.0.0.1", user="admin")
        with pytest.raises(InventoryError, match="lab-01"):
            get_target("nope")


class TestUpdate:
    """클라우드 랩은 껐다 켤 때마다 주소가 바뀐다.

    지웠다 다시 등록하게 하면 그 사이 이름이 사라지는 창이 생기고, 적어두지
    않은 항목(키·포트)까지 함께 날아간다.
    """

    @pytest.fixture(autouse=True)
    def registered(self):
        add_target("lab-01", host="10.0.0.10", user="admin", port=2222, key="~/k")

    def test_only_the_address_changes(self):
        target, _ = update_target("lab-01", host="34.22.105.223")
        assert target.host == "34.22.105.223"
        # 나머지는 그대로 — 이게 remove + add 와 다른 점이다
        assert (target.user, target.port, target.key) == ("admin", 2222, "~/k")

    def test_it_survives_a_reload(self, temp_inventory):
        update_target("lab-01", host="34.22.105.223")
        assert get_target("lab-01").host == "34.22.105.223"
        assert "34.22.105.223" in temp_inventory.read_text(encoding="utf-8")

    def test_several_fields_at_once(self):
        target, changes = update_target("lab-01", host="10.0.0.20", port=22)
        assert (target.host, target.port) == ("10.0.0.20", 22)
        assert {c.field for c in changes} == {"host", "port"}

    def test_it_reports_what_moved(self):
        _, changes = update_target("lab-01", host="10.0.0.20")
        assert len(changes) == 1
        assert (changes[0].before, changes[0].after) == ("10.0.0.10", "10.0.0.20")

    def test_setting_a_field_that_was_unset(self):
        add_target("bare", host="10.0.0.30")
        _, changes = update_target("bare", user="root")
        assert changes[0].before is None
        assert get_target("bare").user == "root"

    def test_the_same_value_is_not_a_change(self):
        # '✓ 바뀜' 이라고 해놓고 아무것도 안 바뀌면 거짓말이다
        _, changes = update_target("lab-01", host="10.0.0.10")
        assert changes == []

    def test_nothing_given_is_refused(self):
        with pytest.raises(InventoryError, match="Nothing to change"):
            update_target("lab-01")

    def test_an_unknown_target_is_refused(self):
        with pytest.raises(InventoryError, match="not found"):
            update_target("nope", host="10.0.0.1")

    def test_an_option_like_address_is_refused(self):
        # add 와 같은 방어 — ssh 가 목적지 대신 옵션으로 읽는다
        with pytest.raises(InventoryError, match="not a valid address"):
            update_target("lab-01", host="-oProxyCommand=evil")

    def test_a_refused_update_changes_nothing(self):
        with pytest.raises(InventoryError):
            update_target("lab-01", host="-oProxyCommand=evil")
        assert get_target("lab-01").host == "10.0.0.10"

    def test_add_now_points_at_update(self):
        with pytest.raises(InventoryError, match="target update"):
            add_target("lab-01", host="10.0.0.99")


class TestUnset:
    """비우는 것은 따로 받는다 — 값에 '지워라' 를 겸하게 하면 실수로 지운다."""

    @pytest.fixture(autouse=True)
    def registered(self):
        add_target("lab-01", host="10.0.0.10", user="admin", port=2222, key="~/k")

    def test_a_setting_can_be_cleared(self):
        target, changes = update_target("lab-01", unset=["port"])
        assert target.port is None
        assert (changes[0].field, changes[0].before, changes[0].after) == (
            "port", 2222, None
        )

    def test_clearing_leaves_the_rest_alone(self):
        target, _ = update_target("lab-01", unset=["port"])
        assert (target.host, target.user, target.key) == ("10.0.0.10", "admin", "~/k")

    def test_several_at_once(self):
        target, changes = update_target("lab-01", unset=["port", "key"])
        assert (target.port, target.key) == (None, None)
        assert len(changes) == 2

    def test_it_can_be_combined_with_a_change(self):
        target, _ = update_target("lab-01", host="10.0.0.20", unset=["port"])
        assert (target.host, target.port) == ("10.0.0.20", None)

    def test_clearing_what_is_already_clear_is_not_a_change(self):
        add_target("bare", host="10.0.0.30")
        _, changes = update_target("bare", unset=["port"])
        assert changes == []

    def test_the_address_cannot_be_cleared(self):
        # 주소가 없는 타겟은 타겟이 아니다
        with pytest.raises(InventoryError, match="Cannot unset 'host'"):
            update_target("lab-01", unset=["host"])

    def test_an_unknown_field_is_refused(self):
        with pytest.raises(InventoryError, match="You can unset"):
            update_target("lab-01", unset=["distro"])

    def test_setting_and_clearing_the_same_field_is_refused(self):
        # 둘 중 하나를 조용히 이기게 하면 어느 쪽인지 알 수 없다
        with pytest.raises(InventoryError, match="contradict"):
            update_target("lab-01", port=22, unset=["port"])

    def test_a_refused_unset_changes_nothing(self):
        with pytest.raises(InventoryError):
            update_target("lab-01", unset=["host"])
        assert get_target("lab-01").port == 2222


class TestPortIsChecked:
    """0 과 음수는 포트가 아니다.

    add 는 `if port:` 로 걸러서 --port 0 을 말없이 버렸고, update 는
    `is not None` 이라 0 을 그대로 적었다. 같은 입력에 두 동작이었고 어느
    쪽도 유효한 포트가 아니다. 버리는 쪽이 특히 나쁘다 — 사용자가 적은
    값이 오류도 없이 사라진다.
    """

    @pytest.mark.parametrize("bad", [0, -1, -22, 65536, 99999])
    def test_add_refuses(self, bad):
        with pytest.raises(InventoryError, match="not a valid port"):
            add_target("lab-01", host="10.0.0.1", port=bad)

    @pytest.mark.parametrize("bad", [0, -1, 65536])
    def test_update_refuses(self, bad):
        add_target("lab-01", host="10.0.0.1")
        with pytest.raises(InventoryError, match="not a valid port"):
            update_target("lab-01", port=bad)

    @pytest.mark.parametrize("good", [1, 22, 2222, 65535])
    def test_the_usable_range_still_works(self, good):
        add_target("lab-01", host="10.0.0.1", port=good)
        assert get_target("lab-01").port == good

    def test_a_refused_port_is_not_written(self):
        with pytest.raises(InventoryError):
            add_target("lab-01", host="10.0.0.1", port=0)
        assert list_targets() == ([], [])

    def test_a_refused_update_leaves_the_old_port(self):
        add_target("lab-01", host="10.0.0.1", port=2222)
        with pytest.raises(InventoryError):
            update_target("lab-01", port=0)
        assert get_target("lab-01").port == 2222

    @pytest.mark.parametrize("bad", ["0", "-1", "70000"])
    def test_a_hand_edited_file_is_caught_too(self, temp_inventory, bad):
        # 숫자이기만 해서는 안 된다 — 그대로 두면 `ssh -p 0` 으로 나간다
        temp_inventory.write_text(
            "all:\n  hosts:\n    lab-01:\n"
            f"      ansible_host: 10.0.0.1\n      ansible_port: '{bad}'\n"
        )
        with pytest.raises(InventoryError, match="out of range"):
            get_target("lab-01")


class TestEmptyValuesAreRefused:
    """빈 값은 '안 적은 것' 과 뜻이 다르다.

    --unset host 는 "A target without an address is not a target." 로
    막는데, --host "" 는 정확히 그 상태를 만들 수 있었다. 규칙을 말로
    적어놓고 옆문을 열어두면 그 규칙은 없는 것이다.
    """

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_add_refuses_an_empty_address(self, blank):
        with pytest.raises(InventoryError, match="cannot be empty"):
            add_target("lab-01", host=blank)

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_update_refuses_an_empty_address(self, blank):
        add_target("lab-01", host="10.0.0.1")
        with pytest.raises(InventoryError, match="cannot be empty"):
            update_target("lab-01", host=blank)

    def test_the_reason_matches_the_unset_guard(self):
        # 같은 규칙이면 같은 말로 거절해야 한다
        with pytest.raises(InventoryError) as add_err:
            add_target("lab-01", host="")
        add_target("lab-01", host="10.0.0.1")
        with pytest.raises(InventoryError) as unset_err:
            update_target("lab-01", unset=["host"])
        shared = "A target without an address is not a target."
        assert shared in str(add_err.value)
        assert shared in str(unset_err.value)

    @pytest.mark.parametrize("field", ["user", "key"])
    def test_add_refuses_other_empty_values(self, field):
        with pytest.raises(InventoryError, match="cannot be empty"):
            add_target("lab-01", host="10.0.0.1", **{field: ""})

    @pytest.mark.parametrize("field", ["user", "key"])
    def test_update_refuses_other_empty_values(self, field):
        add_target("lab-01", host="10.0.0.1")
        with pytest.raises(InventoryError, match="cannot be empty"):
            update_target("lab-01", **{field: ""})

    def test_omitting_is_still_how_you_leave_it_to_ssh_config(self):
        # 빈 값을 막는다고 '안 적기' 까지 막으면 안 된다
        add_target("lab-01", host="10.0.0.1")
        target = get_target("lab-01")
        assert (target.user, target.port, target.key) == (None, None, None)

    def test_an_option_like_address_is_still_refused(self):
        with pytest.raises(InventoryError, match="not a valid address"):
            add_target("lab-01", host="-oProxyCommand=evil")


class TestHostCharset:
    """주소는 원격 셸 명령 안으로 들어간다(tls-san 설정 쓰기 등).

    따옴표가 섞인 주소는 거기서 셸 문법이 된다 — 자기 인벤토리를 자기가
    망가뜨리는 길이지만, 0.5.5 가 검사 자리(_checked_host)를 만들고도
    이 문은 열어뒀었다.
    """

    @pytest.mark.parametrize(
        "host",
        [
            "1.2.3.4' ; touch /tmp/x ; '",  # 실측했던 그 모양
            "host name",  # 공백
            "host`id`",
            "host$(id)",
            'host"quote',
            "host;semicolon",
        ],
    )
    def test_shell_looking_addresses_are_refused(self, host):
        with pytest.raises(InventoryError, match="not a valid address"):
            add_target("lab-01", host=host)

    def test_update_runs_the_same_check(self):
        add_target("lab-01", host="10.0.0.1")
        with pytest.raises(InventoryError, match="not a valid address"):
            update_target("lab-01", host="x' ; id ; '")

    @pytest.mark.parametrize(
        "host", ["10.0.0.1", "lab", "host.example.com", "my-lab_2", "k8s-node.a-b.io"]
    )
    def test_real_addresses_still_pass(self, host):
        add_target("t", host=host)
        assert get_target("t").host == host

    @pytest.mark.parametrize("host", ["2001:db8::1", "::1", "[2001:db8::1]"])
    def test_ipv6_is_refused_and_says_so(self, host):
        # 반쪽 지원이었다: 0.6.0 은 등록은 받았지만 kubeconfig URL 이
        # 대괄호 없이 깨졌고, -J 표기와 충돌하고, VM 포워딩(iptables v4)
        # 은 조용히 버렸다. 지원하게 되기 전까지는 명확히 거절하고,
        # 거절 문구가 그 사실을 말한다.
        with pytest.raises(InventoryError, match="IPv6 is not supported"):
            add_target("v6", host=host)


class TestBrokenEntriesDoNotSinkTheList:
    """한 항목이 깨졌다고 목록 전체를 거절하면, 그 항목을 고치려는
    사용자가 자기 목록조차 못 본다."""

    def _write(self, temp_inventory):
        temp_inventory.write_text(
            "all:\n  hosts:\n"
            "    good-1:\n      ansible_host: 10.0.0.1\n"
            "    broken:\n      ansible_host: 10.0.0.2\n      ansible_port: 'abc'\n"
            "    good-2:\n      ansible_host: 10.0.0.3\n"
        )

    def test_healthy_entries_survive(self, temp_inventory):
        self._write(temp_inventory)
        targets, broken = list_targets()
        assert [t.name for t in targets] == ["good-1", "good-2"]

    def test_the_broken_one_is_named_with_a_reason(self, temp_inventory):
        self._write(temp_inventory)
        _, broken = list_targets()
        assert len(broken) == 1
        name, reason = broken[0]
        assert name == "broken"
        assert "not a number" in reason

    def test_using_the_broken_one_still_refuses(self, temp_inventory):
        # 목록이 보여주는 것과 쓰는 것은 다르다 — 쓰려는 순간에는 거절
        self._write(temp_inventory)
        with pytest.raises(InventoryError, match="not a number"):
            get_target("broken")


class TestFileHandling:
    def test_missing_file_is_not_an_error(self):
        assert list_targets() == ([], [])

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
        assert list_targets() == ([], [])

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
        # 그 항목을 실제로 쓰려는 순간(get_target)에는 여전히 거절이다
        temp_inventory.write_text(
            "all:\n  hosts:\n    lab-01:\n"
            "      ansible_host: 10.0.0.1\n      ansible_port: []\n"
        )
        with pytest.raises(InventoryError) as excinfo:
            get_target("lab-01")
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
