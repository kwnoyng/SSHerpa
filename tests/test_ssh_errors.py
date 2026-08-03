"""ssh stderr -> 사람이 읽을 수 있는 오류로 변환하는 로직.

컨테이너로는 재현하기 어렵거나 느린 분기가 많아(호스트 키 변경, 라우팅 실패,
타임아웃) 실제 ssh 가 뱉는 문구를 그대로 넣어 검증한다.
"""

from ssherpa import ssh
from ssherpa.ssh import Target

TARGET = Target(name="lab-01", host="192.168.0.51", user="admin", port=22)
TARGET_WITH_KEY = Target(name="lab-01", host="h", user="admin", key="/tmp/k")


def classify(stderr, target=TARGET):
    return ssh._classify(stderr, target)


class TestClassify:
    def test_dns_failure(self):
        err = classify("ssh: Could not resolve hostname nope: Name or service not known")
        assert "could not resolve hostname" in err.message

    def test_timeout(self):
        err = classify("ssh: connect to host 192.168.0.51 port 22: Connection timed out")
        assert "timed out" in err.message

    def test_connection_refused(self):
        err = classify("ssh: connect to host x port 22: Connection refused")
        assert "connection refused" in err.message
        assert any("sshd" in hint for hint in err.hints)

    def test_no_route_to_host(self):
        err = classify("ssh: connect to host x port 22: No route to host")
        assert "network unreachable" in err.message

    def test_host_key_changed(self):
        err = classify(
            "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
            "Host key verification failed."
        )
        assert "host key verification failed" in err.message
        # 재설치 시 해결법과 보안 경고를 모두 안내해야 한다
        assert any("ssh-keygen -R" in hint for hint in err.hints)
        assert any("man-in-the-middle" in hint for hint in err.hints)

    def test_unknown_error_surfaces_raw_stderr(self):
        err = classify("something completely unexpected")
        assert "SSH connection failed" in err.message
        assert "something completely unexpected" in err.hints[0]

    def test_empty_stderr_does_not_crash(self):
        assert classify("").hints


class TestPermissionOrdering:
    """ssh 는 권한 때문에 키를 무시한 뒤 Permission denied 도 함께 뱉는다.

    분류 순서가 뒤바뀌면 원인(파일 권한) 대신 증상(인증 실패)만 안내하게 된다.
    """

    RAW = (
        "@@@ WARNING: UNPROTECTED PRIVATE KEY FILE! @@@\n"
        "Permissions 0777 for '/tmp/k' are too open.\n"
        "This private key will be ignored.\n"
        "admin@h: Permission denied (publickey)."
    )

    def test_reports_permissions_not_auth(self):
        err = classify(self.RAW, TARGET_WITH_KEY)
        assert "permissions are too open" in err.message

    def test_suggests_chmod_with_actual_path(self):
        err = classify(self.RAW, TARGET_WITH_KEY)
        assert any("chmod 600 /tmp/k" in hint for hint in err.hints)


class TestAuthHints:
    DENIED = "admin@h: Permission denied (publickey)."

    def test_named_key_is_echoed_back(self):
        err = classify(self.DENIED, TARGET_WITH_KEY)
        assert any("/tmp/k" in hint for hint in err.hints)

    def test_lists_default_keys_that_were_tried(self, monkeypatch):
        monkeypatch.setattr(
            ssh, "_find_default_keys", lambda: ["/home/me/.ssh/id_rsa"]
        )
        err = classify(self.DENIED, TARGET)
        assert any("id_rsa" in hint for hint in err.hints)

    def test_tells_user_to_create_a_key_when_none_exist(self, monkeypatch):
        monkeypatch.setattr(ssh, "_find_default_keys", lambda: [])
        err = classify(self.DENIED, TARGET)
        assert any("ssh-keygen -t ed25519" in hint for hint in err.hints)


class TestBuildCommand:
    def test_batch_mode_is_always_set(self):
        # 비밀번호 프롬프트가 뜨면 자동화가 멈춘다
        argv = ssh._build_command(TARGET, "true")
        assert "BatchMode=yes" in argv

    def test_rejects_changed_host_keys(self):
        argv = ssh._build_command(TARGET, "true")
        assert "StrictHostKeyChecking=accept-new" in argv

    def test_key_is_passed_as_identity(self):
        argv = ssh._build_command(TARGET_WITH_KEY, "true")
        assert "-i" in argv

    def test_no_identity_flag_without_key(self):
        # -i 를 안 넘겨야 ssh 가 기본 키를 스스로 찾는다
        assert "-i" not in ssh._build_command(TARGET, "true")

    def test_port_and_destination(self):
        argv = ssh._build_command(TARGET, "whoami")
        assert argv[-2:] == ["admin@192.168.0.51", "whoami"]
        assert "22" in argv
