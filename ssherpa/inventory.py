"""타겟 인벤토리 읽기/쓰기.

인벤토리는 '주소록'이다. 접속에 필요한 정보(주소/계정/키)만 담는다.
OS, VM 목록, 스냅샷 목록 같은 '실제 상태'는 저장하지 않는다 —
그건 매번 호스트에 물어본다. 그래야 파일과 실제가 어긋나지 않는다.

포맷은 Ansible 인벤토리(YAML)를 그대로 쓴다. 이후 단계에서 Ansible
롤을 붙일 때 변환 없이 재사용하기 위함이다.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .ssh import Target, looks_like_option

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# 주소로 인정하는 문자들. IP·호스트명·~/.ssh/config 별칭은 전부 이 안에
# 들어온다. 이보다 넓히면 안 되는 이유가 있다: 주소는 원격 셸 명령
# 안으로 들어간다(tls-san 설정 쓰기 등) — 따옴표가 섞인 주소는 거기서
# 셸 문법이 된다. 자기 인벤토리를 자기가 망가뜨리는 길이지만, 검사할
# 자리가 있는데 열어둘 이유가 없다.
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# 주소가 '-' 로 시작하면 ssh 가 그것을 옵션으로 읽는다. 인벤토리는 한 번
# 적어두면 이후 모든 명령이 말없이 쓰는 값이라, 들어오는 자리에서 막는다.
HOST_OPTION_HINT = [
    "An address cannot start with '-' — ssh would read it as an option",
    "instead of a destination.",
]


PORT_MIN, PORT_MAX = 1, 65535


class InventoryError(Exception):
    pass


def _checked_host(host: str) -> str:
    """주소로 쓸 수 있는 값인가.

    add 와 update 가 같은 검사를 쓴다. 한쪽에만 두면 규칙이 갈라진다 —
    실제로 갈라져 있었다: add 는 --port 0 을 말없이 버리고 update 는
    그대로 적었다.

    빈 값을 막는 이유는 --unset host 를 막는 이유와 같다. 그쪽에만 규칙을
    적어두고 이 문으로 같은 상태를 만들 수 있으면, 코드가 지킨다고 말한
    것을 안 지키는 셈이다.
    """
    if not host or not host.strip():
        raise InventoryError(
            "An address cannot be empty.\n"
            "A target without an address is not a target."
        )
    if looks_like_option(host):
        raise InventoryError(
            f"'{host}' is not a valid address.\n" + "\n".join(HOST_OPTION_HINT)
        )
    if not HOST_PATTERN.match(host):
        # IPv6 도 여기서 거절된다(콜론). 의도다 — 콜론만 허용하면 등록은
        # 되는데 kubeconfig URL 은 대괄호 없이 깨지고, -J 의 '주소:포트'
        # 표기와 충돌하고, VM 포워딩은 iptables(v4) 전용이라 조용히
        # 버려진다. 반쯤 되는 것보다 명확한 거절이 낫다.
        raise InventoryError(
            f"'{host}' is not a valid address.\n"
            "An address is letters, digits, dots, dashes and underscores —\n"
            "an IPv4 address, a hostname, or a Host alias from ~/.ssh/config.\n"
            "IPv6 is not supported yet."
        )
    return host


def _checked_port(port: int) -> int:
    """ssh 에 넘길 수 있는 포트인가.

    0 이나 음수를 그대로 적어두면 이후 모든 명령이 `ssh -p 0` 으로 나가
    영문 모를 실패를 한다. 값이 들어오는 자리에서 막는 편이 낫다.
    """
    if not PORT_MIN <= port <= PORT_MAX:
        raise InventoryError(
            f"'{port}' is not a valid port.\n"
            f"A port is a number from {PORT_MIN} to {PORT_MAX}."
        )
    return port


def _checked_text(flag: str, value: str) -> str:
    """비어 있지 않은 값인가. 빈 값은 '안 적은 것' 과 뜻이 다르다."""
    if not value.strip():
        raise InventoryError(
            f"--{flag} cannot be empty.\n"
            "Omit it to let ~/.ssh/config decide."
        )
    return value


def inventory_path() -> Path:
    """인벤토리 파일 경로. SSHERPA_INVENTORY 로 덮어쓸 수 있다."""
    override = os.environ.get("SSHERPA_INVENTORY")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ssherpa" / "inventory.yml"


def _load_raw() -> dict:
    path = inventory_path()
    if not path.exists():
        return {"all": {"hosts": {}}}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise InventoryError(f"Could not read inventory file: {path}\n{exc}") from exc

    if not isinstance(data, dict):
        raise InventoryError(f"Invalid inventory format: {path}")

    data.setdefault("all", {}).setdefault("hosts", {})
    if data["all"]["hosts"] is None:
        data["all"]["hosts"] = {}
    return data


def _save_raw(data: dict) -> None:
    path = inventory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _to_target(name: str, vars_: dict) -> Target:
    return Target(
        name=name,
        host=vars_.get("ansible_host", ""),
        user=vars_.get("ansible_user"),
        port=_port_of(name, vars_.get("ansible_port")),
        key=vars_.get("ansible_ssh_private_key_file"),
    )


def _port_of(name: str, port) -> Optional[int]:
    """인벤토리의 포트 값을 읽는다. 숫자가 아니면 우리 말로 설명한다.

    이 파일은 손으로 고쳐도 된다고 문서가 열어둔 파일이다. 그렇게 열어둔
    이상 잘못 적힌 값은 예상 범위 안이고, 그때 나가야 할 것은 우리가 쓴
    한 줄이지 int() 의 스택트레이스가 아니다.
    """
    if port is None:
        return None
    try:
        number = int(port)
    except (TypeError, ValueError):
        raise InventoryError(
            f"Target '{name}' has an ansible_port that is not a number: {port!r}\n"
            f"Fix it in {inventory_path()}"
        ) from None
    # 손으로 적은 값은 숫자이기만 해서는 안 된다 — 범위도 봐야 `ssh -p 0`
    # 으로 나가는 일이 없다.
    if not PORT_MIN <= number <= PORT_MAX:
        raise InventoryError(
            f"Target '{name}' has an ansible_port out of range: {number}\n"
            f"A port is a number from {PORT_MIN} to {PORT_MAX}.\n"
            f"Fix it in {inventory_path()}"
        )
    return number


def list_targets() -> tuple[list[Target], list[tuple[str, str]]]:
    """(성한 타겟들, 깨진 (이름, 사유) 목록).

    한 항목이 깨졌다고 목록 전체를 거절하면, 그 항목을 고치려는 사용자가
    자기 목록조차 못 본다 — 뭐가 있는지 보는 길이 list 인데. 깨진 항목은
    숨기지 않고 사유와 함께 따로 돌려준다. 그 항목을 실제로 쓰는
    get_target 은 지금처럼 거절한다.
    """
    hosts = _load_raw()["all"]["hosts"]
    targets: list[Target] = []
    broken: list[tuple[str, str]] = []
    for name, vars_ in sorted(hosts.items()):
        try:
            targets.append(_to_target(name, vars_ or {}))
        except InventoryError as exc:
            broken.append((name, str(exc).splitlines()[0]))
    return targets, broken


def get_target(name: str) -> Target:
    hosts = _load_raw()["all"]["hosts"]
    if name not in hosts:
        known = ", ".join(sorted(hosts)) or "(none)"
        raise InventoryError(
            f"Target '{name}' not found.\nRegistered targets: {known}"
        )
    return _to_target(name, hosts[name] or {})


def add_target(
    name: str,
    host: str,
    user: Optional[str] = None,
    port: Optional[int] = None,
    key: Optional[str] = None,
) -> Target:
    if not NAME_PATTERN.match(name):
        raise InventoryError(
            f"'{name}' is not a valid target name.\n"
            "Must start with a letter or digit and contain only "
            "letters, digits, '-', '_', '.'"
        )

    host = _checked_host(host)

    data = _load_raw()
    if name in data["all"]["hosts"]:
        raise InventoryError(
            f"'{name}' is already registered.\n"
            f"Change it:  ssherpa target update {name} --host <address>\n"
            f"Or start over:  ssherpa target remove {name}"
        )

    # 지정한 값만 기록한다. 안 적은 항목은 접속할 때 ~/.ssh/config 가 정한다.
    #
    # 'is not None' 이지 'if value' 가 아니다. 후자는 --port 0 을 말없이
    # 버렸다 — 사용자가 적은 값이 오류도 없이 사라지는, 이 프로젝트가
    # 0.5.3 에서 닫기로 한 바로 그 종류다.
    vars_: dict[str, object] = {"ansible_host": host}
    if user is not None:
        vars_["ansible_user"] = _checked_text("user", user)
    if port is not None:
        vars_["ansible_port"] = _checked_port(port)
    if key is not None:
        vars_["ansible_ssh_private_key_file"] = _checked_text("key", key)

    data["all"]["hosts"][name] = vars_
    _save_raw(data)
    return _to_target(name, vars_)


VAR_OF = {
    "host": "ansible_host",
    "user": "ansible_user",
    "port": "ansible_port",
    "key": "ansible_ssh_private_key_file",
}


@dataclass
class Change:
    """바뀐 항목 하나. 사용자에게 무엇이 무엇으로 바뀌었는지 보이기 위한 것."""

    field: str
    before: Optional[object]
    after: object


UNSETTABLE = ("user", "port", "key")


def update_target(
    name: str,
    host: Optional[str] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
    key: Optional[str] = None,
    unset: Optional[list[str]] = None,
) -> tuple[Target, list[Change]]:
    """등록된 타겟의 접속 정보를 고친다. 준 것만 바꾼다.

    클라우드 랩은 껐다 켤 때마다 주소가 바뀐다. 그때마다 지웠다 다시
    등록하게 하면, 그 사이 이름이 사라져서 다른 명령이 못 찾는 창이 생기고
    적어두지 않은 항목(키·포트)까지 함께 날아간다.

    비우는 것은 --unset 으로 따로 받는다. '값을 준 것' 과 '비우라는 것' 을
    한 인자에 실으면(빈 문자열 같은 것) 실수로 지우는 길이 생긴다. 주소는
    비울 수 없다 — 주소가 없는 타겟은 타겟이 아니다.
    """
    data = _load_raw()
    if name not in data["all"]["hosts"]:
        known = ", ".join(sorted(data["all"]["hosts"])) or "(none)"
        raise InventoryError(
            f"Target '{name}' not found.\nRegistered targets: {known}"
        )

    unset = list(unset or [])
    for field in unset:
        if field not in UNSETTABLE:
            allowed = ", ".join(UNSETTABLE)
            extra = (
                "\nA target without an address is not a target."
                if field == "host"
                else ""
            )
            raise InventoryError(
                f"Cannot unset '{field}'.\nYou can unset: {allowed}{extra}"
            )

    given = {"host": host, "user": user, "port": port, "key": key}
    if all(value is None for value in given.values()) and not unset:
        raise InventoryError(
            f"Nothing to change for '{name}'.\n"
            "Pass at least one of --host, --user, --port, --key, --unset."
        )

    both = [f for f in unset if given.get(f) is not None]
    if both:
        raise InventoryError(
            f"--{both[0]} and --unset {both[0]} contradict each other.\n"
            "Pass one or the other."
        )

    # add 와 같은 검사를 쓴다 — 들어오는 문이 둘이면 규칙도 하나여야 한다.
    check = {
        "host": _checked_host,
        "user": lambda v: _checked_text("user", v),
        "port": _checked_port,
        "key": lambda v: _checked_text("key", v),
    }

    vars_ = dict(data["all"]["hosts"][name] or {})
    changes = []

    for field, value in given.items():
        if value is None:
            continue
        value = check[field](value)
        var = VAR_OF[field]
        before = vars_.get(var)
        if before == value:
            continue  # 같은 값을 다시 쓴 것은 변경이 아니다
        changes.append(Change(field=field, before=before, after=value))
        vars_[var] = value

    for field in unset:
        var = VAR_OF[field]
        if var not in vars_:
            continue  # 이미 없는 것을 지우는 것은 변경이 아니다
        changes.append(Change(field=field, before=vars_.pop(var), after=None))

    data["all"]["hosts"][name] = vars_
    _save_raw(data)
    return _to_target(name, vars_), changes


def remove_target(name: str) -> None:
    data = _load_raw()
    if name not in data["all"]["hosts"]:
        known = ", ".join(sorted(data["all"]["hosts"])) or "(none)"
        raise InventoryError(
            f"Target '{name}' not found.\nRegistered targets: {known}"
        )
    del data["all"]["hosts"][name]
    _save_raw(data)
