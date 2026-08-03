"""지원 매트릭스 판정."""

import pytest

from ssherpa import support


class TestVersionKey:
    def test_redhat_uses_major_only(self):
        # Rocky 9.3 / 9.8 을 구분할 이유가 없다
        assert support.version_key("RedHat", "9.8") == "9"

    def test_debian_uses_full_version(self):
        # 22.04 와 24.04 는 완전히 다른 릴리스다
        assert support.version_key("Debian", "24.04") == "24.04"

    def test_missing_version(self):
        assert support.version_key("RedHat", None) == ""


class TestIsSupported:
    @pytest.mark.parametrize("version_id", ["9.0", "9.3", "9.8"])
    def test_any_rocky_9_minor_passes(self, version_id):
        assert support.is_supported("rocky", "RedHat", version_id)[0] is True

    @pytest.mark.parametrize("distro", ["rocky", "almalinux"])
    def test_rhel_rebuilds_are_interchangeable(self, distro):
        assert support.is_supported(distro, "RedHat", "9.4")[0] is True

    @pytest.mark.parametrize("version_id", ["22.04", "24.04"])
    def test_supported_ubuntu_versions(self, version_id):
        assert support.is_supported("ubuntu", "Debian", version_id)[0] is True

    def test_old_ubuntu_rejected(self):
        ok, reason = support.is_supported("ubuntu", "Debian", "20.04")
        assert ok is False
        assert "unsupported version" in reason

    def test_rocky_8_rejected(self):
        ok, reason = support.is_supported("rocky", "RedHat", "8.9")
        assert ok is False
        assert "unsupported version" in reason

    def test_unknown_distro_rejected(self):
        ok, reason = support.is_supported("centos", "RedHat", "7")
        assert ok is False
        assert "unsupported distribution" in reason

    def test_missing_id_rejected(self):
        ok, reason = support.is_supported(None, None, None)
        assert ok is False
        assert "could not identify" in reason


def test_summary_lists_every_supported_distro():
    summary = support.supported_summary()
    for distro in support.SUPPORTED:
        assert distro in summary
