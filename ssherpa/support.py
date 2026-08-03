"""지원 OS 매트릭스.

새 배포판/버전을 추가하려면 SUPPORTED 에 항목만 추가하면 된다.
"""

from typing import Optional

# 배포판 ID(/etc/os-release 의 ID) -> 지원 버전 목록
#
# 버전 비교에 쓰는 값은 family 에 따라 다르다(version_key 참고).
#   Debian 계열 : VERSION_ID 전체   ("22.04", "24.04")
#   RedHat 계열 : major 만          ("9"  <- 9.3, 9.4 ...)
SUPPORTED: dict[str, list[str]] = {
    "ubuntu": ["22.04", "24.04"],
    "rocky": ["9"],
    "almalinux": ["9"],
}


def version_key(family: Optional[str], version_id: Optional[str]) -> str:
    """지원 여부 비교에 사용할 버전 문자열을 만든다."""
    if not version_id:
        return ""
    if family == "RedHat":
        return version_id.split(".")[0]
    return version_id


def is_supported(
    distro_id: Optional[str],
    family: Optional[str],
    version_id: Optional[str],
) -> tuple[bool, str]:
    """(지원여부, 사람이 읽을 사유) 를 돌려준다."""
    if not distro_id:
        return False, "could not identify the distribution"

    allowed = SUPPORTED.get(distro_id)
    if allowed is None:
        return False, f"unsupported distribution ({distro_id})"

    key = version_key(family, version_id)
    if key not in allowed:
        return False, f"unsupported version ({distro_id} {version_id or '?'})"

    return True, "supported"


def supported_summary() -> str:
    """지원 목록을 한 줄로 요약한다. 오류 메시지에 붙여 쓴다."""
    parts = []
    for distro, versions in SUPPORTED.items():
        parts.append(f"{distro} {'/'.join(versions)}")
    return ", ".join(parts)
