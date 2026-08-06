"""~/.kube/config 병합.

kubectl 은 환경변수가 없으면 ~/.kube/config 를 읽는다. up 이 가져온
접속 정보를 여기에 넣어두면 어느 터미널에서든 설정 없이 kubectl 이 된다.

이 파일은 우리 것이 아니다 — 사용자의 다른 클러스터(회사 EKS 등)가 이미
들어있을 수 있다. 그래서 규칙이 엄격하다:

  - 쓰기 전에 원본을 백업한다
  - 'ssherpa-<타겟>' 이름이 붙은 우리 항목만 추가·교체·삭제한다
  - current-context 는 원래 비어 있을 때만 잡는다. 다른 클러스터를 쓰던
    사용자의 기본값을 몰래 바꾸면 그쪽 운영 사고로 이어질 수 있다.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

SECTIONS = ("clusters", "users", "contexts")


class KubeconfigError(Exception):
    """~/.kube/config 를 읽거나 쓸 수 없을 때."""


def default_path() -> Path:
    return Path.home() / ".kube" / "config"


def entry_name(target_name: str) -> str:
    return f"ssherpa-{target_name}"


@dataclass
class MergeResult:
    context: str
    became_current: bool  # current-context 를 우리가 잡았나 (원래 비어 있었나)
    backup: Optional[Path]  # 기존 파일이 있었으면 백업 위치


def _load(path: Path) -> tuple[dict, Optional[str]]:
    """(파싱된 내용, 원본 텍스트) 를 돌려준다. 파일이 없으면 빈 뼈대."""
    if not path.exists():
        return {"apiVersion": "v1", "kind": "Config"}, None

    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise KubeconfigError(f"could not parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise KubeconfigError(f"{path} is not a kubeconfig mapping")
    return data, text


def _upsert(section: list, item: dict) -> None:
    """같은 이름의 항목이 있으면 교체, 없으면 추가한다."""
    for index, existing in enumerate(section):
        if isinstance(existing, dict) and existing.get("name") == item["name"]:
            section[index] = item
            return
    section.append(item)


def merge(
    fetched_text: str,
    target_name: str,
    path: Optional[Path] = None,
) -> MergeResult:
    """가져온 kubeconfig 의 접속 정보를 ~/.kube/config 에 병합한다.

    fetched_text 는 fetch_kubeconfig 가 저장한 내용(주소는 이미 실제 IP)이다.
    k3s/RKE2 는 cluster/user/context 를 하나씩 'default' 이름으로 만드는데,
    그대로 병합하면 다른 클러스터의 'default' 와 충돌하므로 이름을 바꾼다.
    """
    path = path or default_path()
    name = entry_name(target_name)

    try:
        source = yaml.safe_load(fetched_text) or {}
        cluster = source["clusters"][0]["cluster"]
        user = source["users"][0]["user"]
    except (yaml.YAMLError, KeyError, IndexError, TypeError) as exc:
        raise KubeconfigError(f"fetched kubeconfig is malformed: {exc}") from exc

    data, original = _load(path)

    backup = None
    if original is not None:
        backup = path.with_name(path.name + ".ssherpa-backup")
        backup.write_text(original, encoding="utf-8")

    # 병합하기 전에 이 파일에 컨텍스트가 있었는지 봐 둔다 — 뒤에서
    # current-context 를 건드려도 되는지 판단하는 근거다.
    had_contexts = bool(data.get("contexts"))

    for section_name, item in (
        ("clusters", {"name": name, "cluster": cluster}),
        ("users", {"name": name, "user": user}),
        ("contexts", {"name": name, "context": {"cluster": name, "user": name}}),
    ):
        section = data.get(section_name)
        if section is None:
            # kubectl 은 마지막 항목을 지우면 키를 남기고 값만 null 로 쓴다.
            # 그 파일을 '망가진 것' 으로 취급하면, 방금 정리한 사용자가
            # 병합에 실패한다 (실측: kubectl config delete-cluster 직후).
            section = []
        if not isinstance(section, list):
            raise KubeconfigError(f"{path}: '{section_name}' is not a list")
        _upsert(section, item)
        data[section_name] = section

    # 남의 current-context 는 뺏지 않는다. 다만 그 이름이 가리키는 컨텍스트가
    # 실제로 없으면(지워졌거나 null) 가리키는 곳이 없는 것이므로 비어 있는
    # 것으로 본다 — 그대로 두면 kubectl 이 없는 컨텍스트를 계속 찾는다.
    # 남의 current-context 는 뺏지 않는다. 여기 없는 이름을 가리킨다고 해서
    # 고아라고 단정할 수도 없다 — KUBECONFIG 로 파일 여러 개를 엮어 쓰면
    # 컨텍스트 정의는 다른 파일에 있고 current-context 만 이 파일에 남는다.
    # 그걸 고아로 오판해 덮어쓰면 운영 클러스터를 쓰던 사람의 기본값이
    # 조용히 랩으로 바뀐다. 그래서 '이 파일에 컨텍스트가 하나도 없었을 때'
    # 로만 한정한다 — 그때는 빼앗을 남의 것 자체가 없다.
    if not data.get("current-context") or not had_contexts:
        data["current-context"] = name

    # '우리가 기본값이 됐나' 가 아니라 '결과적으로 우리가 기본값인가' 를
    # 답한다. 재실행처럼 이미 우리 것이 기본값이던 경우에도 CLI 는
    # kubectl 사용법을 정확히 안내해야 한다.
    became_current = data.get("current-context") == name

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return MergeResult(context=name, became_current=became_current, backup=backup)


def remove(target_name: str, path: Optional[Path] = None) -> bool:
    """우리 항목을 걷어낸다. 남의 항목은 절대 건드리지 않는다."""
    path = path or default_path()
    if not path.exists():
        return False

    name = entry_name(target_name)
    data, _ = _load(path)

    changed = False
    for section_name in SECTIONS:
        section = data.get(section_name)
        if not isinstance(section, list):
            continue
        kept = [
            item
            for item in section
            if not (isinstance(item, dict) and item.get("name") == name)
        ]
        if len(kept) != len(section):
            data[section_name] = kept
            changed = True

    # 죽은 클러스터를 기본값으로 남겨두면 다음 kubectl 이 영문 모를 실패를 한다
    if data.get("current-context") == name:
        data["current-context"] = ""
        changed = True

    if changed:
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
    return changed
