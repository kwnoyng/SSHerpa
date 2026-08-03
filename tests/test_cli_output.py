"""CLI 출력이 환경에 관계없이 깨지지 않는지.

여기 있는 테스트는 실제로 프로세스를 띄운다. 인코딩 문제는 파이프/리다이렉트
상황에서만 드러나기 때문에, 함수 호출로는 재현되지 않는다.
"""

import subprocess
import sys

import pytest


def run_cli(args, env_extra=None, inventory=None):
    import os

    env = dict(os.environ)
    env.pop("SSHERPA_INVENTORY", None)
    if inventory is not None:
        env["SSHERPA_INVENTORY"] = str(inventory)
    if env_extra:
        env.update(env_extra)

    return subprocess.run(
        [sys.executable, "-m", "ssherpa", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )


class TestLegacyEncoding:
    """체크 기호(✓)를 인코딩 못 하는 로케일에서도 죽지 않아야 한다.

    한글 Windows 의 기본값인 cp949 가 대표적이다. 출력을 파이프로 넘기면
    Python 이 stdout 인코딩을 로케일로 잡아 UnicodeEncodeError 가 났었다.
    """

    LEGACY = {"PYTHONIOENCODING": "cp949"}

    def test_success_output_survives(self, tmp_path):
        result = run_cli(
            ["target", "add", "lab-01", "--host", "10.0.0.1", "--user", "admin"],
            env_extra=self.LEGACY,
            inventory=tmp_path / "inv.yml",
        )
        assert result.returncode == 0, result.stderr
        assert "UnicodeEncodeError" not in result.stderr

    def test_error_output_survives(self, tmp_path):
        result = run_cli(
            ["check", "nope"],
            env_extra=self.LEGACY,
            inventory=tmp_path / "inv.yml",
        )
        assert result.returncode == 1
        assert "UnicodeEncodeError" not in result.stderr


class TestExitCodes:
    """verify.ps1 / verify.sh 가 종료 코드로 판정하므로 계약이 고정돼야 한다."""

    def test_version(self):
        result = run_cli(["--version"])
        assert result.returncode == 0
        assert "ssherpa" in result.stdout

    def test_help_lists_both_commands(self):
        result = run_cli(["--help"])
        assert result.returncode == 0
        assert "check" in result.stdout
        assert "target" in result.stdout

    def test_old_connect_name_is_gone(self):
        assert run_cli(["connect", "lab-01"]).returncode != 0

    def test_missing_target_exits_1(self, tmp_path):
        result = run_cli(["check", "nope"], inventory=tmp_path / "inv.yml")
        assert result.returncode == 1

    def test_check_without_arguments_exits_1(self, tmp_path):
        result = run_cli(["check"], inventory=tmp_path / "inv.yml")
        assert result.returncode == 1

    def test_name_and_host_together_rejected(self, tmp_path):
        result = run_cli(
            ["check", "lab-01", "--host", "10.0.0.1"],
            inventory=tmp_path / "inv.yml",
        )
        assert result.returncode == 1

    @pytest.mark.parametrize("args", [["target", "list"], ["target"]])
    def test_target_commands_do_not_crash(self, args, tmp_path):
        result = run_cli(args, inventory=tmp_path / "inv.yml")
        assert "Traceback" not in result.stderr
