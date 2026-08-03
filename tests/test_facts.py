"""/etc/os-release 파싱과 family 정규화."""

from ssherpa import facts

ROCKY_9 = """NAME="Rocky Linux"
VERSION="9.8 (Blue Onyx)"
ID="rocky"
ID_LIKE="rhel centos fedora"
VERSION_ID="9.8"
PRETTY_NAME="Rocky Linux 9.8 (Blue Onyx)"
"""

UBUNTU_24 = """PRETTY_NAME="Ubuntu 24.04.4 LTS"
NAME="Ubuntu"
VERSION_ID="24.04"
ID=ubuntu
ID_LIKE=debian
"""


class TestParseOsRelease:
    def test_strips_quotes(self):
        assert facts.parse_os_release('ID="rocky"')["ID"] == "rocky"

    def test_accepts_unquoted(self):
        assert facts.parse_os_release("ID=ubuntu")["ID"] == "ubuntu"

    def test_keeps_equals_inside_value(self):
        parsed = facts.parse_os_release('HOME_URL="https://x/?a=b"')
        assert parsed["HOME_URL"] == "https://x/?a=b"

    def test_ignores_comments_and_blanks(self):
        parsed = facts.parse_os_release("# comment\n\nID=rocky\n")
        assert parsed == {"ID": "rocky"}

    def test_empty_input(self):
        assert facts.parse_os_release("") == {}


class TestDetect:
    def test_rocky_is_redhat_family(self):
        info = facts.detect(ROCKY_9)
        assert (info.id, info.version_id, info.family) == ("rocky", "9.8", "RedHat")

    def test_ubuntu_is_debian_family(self):
        info = facts.detect(UBUNTU_24)
        assert (info.id, info.version_id, info.family) == ("ubuntu", "24.04", "Debian")

    def test_falls_back_to_id_like(self):
        # ID 를 모르는 배포판이라도 ID_LIKE 로 계열은 알아낸다
        info = facts.detect('ID=totally-unknown\nID_LIKE="rhel fedora"\n')
        assert info.family == "RedHat"

    def test_unknown_family_is_none(self):
        info = facts.detect('ID=alpine\nVERSION_ID="3.19"\n')
        assert info.family is None

    def test_missing_fields_do_not_crash(self):
        info = facts.detect("")
        assert (info.id, info.version_id, info.family) == (None, None, None)

    def test_describe_is_readable(self):
        assert "family: RedHat" in facts.detect(ROCKY_9).describe()
