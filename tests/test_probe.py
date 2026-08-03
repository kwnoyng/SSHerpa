"""PROBE 출력 파싱과 sudo 판정.

원격에서 한 번에 받아온 문자열을 구간별로 자르고 해석하는 부분이라,
실제 서버에서 나오는 문구를 그대로 넣어 검증한다.
"""

from ssherpa.cli import _judge_sudo, _split_probe

OS_RELEASE = 'ID="rocky"\nVERSION_ID="9.8"\n'


def probe(uid="1000", sudo_lines="", os_release=OS_RELEASE):
    """실제 PROBE 가 만들어내는 형태의 stdout 을 조립한다."""
    return (
        f"SSHERPA_UID={uid}\n"
        "SSHERPA_SUDO\n"
        f"{sudo_lines}"
        "SSHERPA_OSRELEASE\n"
        f"{os_release}"
    )


class TestSplitProbe:
    def test_splits_three_sections(self):
        uid, sudo, osr = _split_probe(probe(sudo_lines="SSHERPA_SUDO_RC=0\n"))
        assert uid == "1000"
        assert "SSHERPA_SUDO_RC=0" in sudo
        assert 'ID="rocky"' in osr

    def test_os_release_section_is_clean(self):
        # sudo 구간의 문구가 os-release 로 새면 OS 감지가 깨진다
        _, _, osr = _split_probe(
            probe(sudo_lines="sudo: a password is required\nSSHERPA_SUDO_RC=1\n")
        )
        assert "password" not in osr

    def test_missing_os_release(self):
        _, _, osr = _split_probe(probe(os_release=""))
        assert osr == ""

    def test_garbage_input_does_not_crash(self):
        assert _split_probe("unexpected output") == ("", "", "")


class TestJudgeSudo:
    def test_root_needs_no_sudo(self):
        ok, detail, _ = _judge_sudo("0", "")
        assert ok is True
        assert detail == "root account"

    def test_nopasswd(self):
        ok, detail, _ = _judge_sudo("1000", "SSHERPA_SUDO_RC=0\n")
        assert ok is True
        assert detail == "NOPASSWD"

    def test_password_required(self):
        # sudoers 에 없는 계정도 sudo 는 일단 비밀번호부터 묻는다
        ok, detail, _ = _judge_sudo(
            "1000", "sudo: a password is required\nSSHERPA_SUDO_RC=1\n"
        )
        assert ok is False
        assert detail == "password required"

    def test_not_in_sudoers(self):
        ok, detail, _ = _judge_sudo(
            "1000", "user is not in the sudoers file.\nSSHERPA_SUDO_RC=1\n"
        )
        assert ok is False
        assert detail == "not in sudoers"

    def test_sudo_not_installed(self):
        ok, detail, hints = _judge_sudo(
            "1000", "bash: sudo: command not found\nSSHERPA_SUDO_RC=127\n"
        )
        assert ok is False
        assert detail == "sudo is not installed"
        assert hints  # 해결 방법을 반드시 제시한다

    def test_unknown_failure_surfaces_original_text(self):
        ok, detail, hints = _judge_sudo(
            "1000", "sudo: something bizarre happened\nSSHERPA_SUDO_RC=1\n"
        )
        assert ok is False
        assert detail == "sudo is unavailable"
        assert any("bizarre" in hint for hint in hints)
