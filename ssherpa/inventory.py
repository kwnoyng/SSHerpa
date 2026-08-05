"""타겟 인벤토리 읽기/쓰기.

인벤토리는 '주소록'이다. 접속에 필요한 정보(주소/계정/키)만 담는다.
OS, VM 목록, 스냅샷 목록 같은 '실제 상태'는 저장하지 않는다 —
그건 매번 호스트에 물어본다. 그래야 파일과 실제가 어긋나지 않는다.

포맷은 Ansible 인벤토리(YAML)를 그대로 쓴다. 이후 단계에서 Ansible
롤을 붙일 때 변환 없이 재사용하기 위함이다.
"""

import os
import re
from pathlib import Path
from typing import Optional

import yaml

from .ssh import Target, looks_like_option

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# 주소가 '-' 로 시작하면 ssh 가 그것을 옵션으로 읽는다. 인벤토리는 한 번
# 적어두면 이후 모든 명령이 말없이 쓰는 값이라, 들어오는 자리에서 막는다.
HOST_OPTION_HINT = [
    "An address cannot start with '-' — ssh would read it as an option",
    "instead of a destination.",
]


class InventoryError(Exception):
    pass


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
    port = vars_.get("ansible_port")
    return Target(
        name=name,
        host=vars_.get("ansible_host", ""),
        user=vars_.get("ansible_user"),
        port=int(port) if port is not None else None,
        key=vars_.get("ansible_ssh_private_key_file"),
    )


def list_targets() -> list[Target]:
    hosts = _load_raw()["all"]["hosts"]
    return [_to_target(name, vars_ or {}) for name, vars_ in sorted(hosts.items())]


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

    if looks_like_option(host):
        raise InventoryError(f"'{host}' is not a valid address.\n" + "\n".join(HOST_OPTION_HINT))

    data = _load_raw()
    if name in data["all"]["hosts"]:
        raise InventoryError(
            f"'{name}' is already registered.\n"
            f"Remove it first:  ssherpa target remove {name}"
        )

    # 지정한 값만 기록한다. 안 적은 항목은 접속할 때 ~/.ssh/config 가 정한다.
    vars_: dict[str, object] = {"ansible_host": host}
    if user:
        vars_["ansible_user"] = user
    if port:
        vars_["ansible_port"] = port
    if key:
        vars_["ansible_ssh_private_key_file"] = key

    data["all"]["hosts"][name] = vars_
    _save_raw(data)
    return _to_target(name, vars_)


def remove_target(name: str) -> None:
    data = _load_raw()
    if name not in data["all"]["hosts"]:
        known = ", ".join(sorted(data["all"]["hosts"])) or "(none)"
        raise InventoryError(
            f"Target '{name}' not found.\nRegistered targets: {known}"
        )
    del data["all"]["hosts"][name]
    _save_raw(data)
