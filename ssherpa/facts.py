"""원격 호스트의 OS 정보 감지 및 정규화."""

from dataclasses import dataclass
from typing import Optional

# /etc/os-release 의 ID -> family
FAMILY_BY_ID: dict[str, str] = {
    "ubuntu": "Debian",
    "debian": "Debian",
    "rocky": "RedHat",
    "rhel": "RedHat",
    "almalinux": "RedHat",
    "centos": "RedHat",
    "fedora": "RedHat",
}

# ID 를 모를 때 ID_LIKE 로 한 번 더 시도한다.
FAMILY_BY_ID_LIKE: dict[str, str] = {
    "debian": "Debian",
    "ubuntu": "Debian",
    "rhel": "RedHat",
    "fedora": "RedHat",
    "centos": "RedHat",
}


@dataclass
class OsInfo:
    id: Optional[str]
    version_id: Optional[str]
    pretty_name: Optional[str]
    family: Optional[str]

    def describe(self) -> str:
        name = self.pretty_name or self.id or "unknown"
        return f"{name}  (family: {self.family or 'unknown'})"


def parse_os_release(text: str) -> dict[str, str]:
    """/etc/os-release 내용을 dict 로 파싱한다.

    KEY=VALUE 형식이며 값은 따옴표로 감싸져 있을 수 있다.
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key.strip()] = value
    return result


def resolve_family(fields: dict[str, str]) -> Optional[str]:
    """ID 로 family 를 찾고, 실패하면 ID_LIKE 로 재시도한다."""
    distro_id = (fields.get("ID") or "").lower()
    if distro_id in FAMILY_BY_ID:
        return FAMILY_BY_ID[distro_id]

    # ID_LIKE 는 공백으로 구분된 여러 값일 수 있다: ID_LIKE="rhel centos fedora"
    for token in (fields.get("ID_LIKE") or "").lower().split():
        if token in FAMILY_BY_ID_LIKE:
            return FAMILY_BY_ID_LIKE[token]

    return None


def detect(os_release_text: str) -> OsInfo:
    fields = parse_os_release(os_release_text)
    return OsInfo(
        id=(fields.get("ID") or "").lower() or None,
        version_id=fields.get("VERSION_ID") or None,
        pretty_name=fields.get("PRETTY_NAME") or None,
        family=resolve_family(fields),
    )
