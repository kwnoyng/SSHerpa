"""SSHerpa CLI 진입점."""

import contextlib
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

import questionary
import typer
from rich.console import Console
from rich.padding import Padding
from rich.table import Table

from . import __version__, cluster, facts, probe, support, virt
from . import distro as distro_mod
from . import doctor as doctor_mod
from . import kubeconfig as kubeconf
from . import ssh as ssh_mod
from . import vm as vm_mod
from .cluster import ClusterError
from .inventory import (
    HOST_OPTION_HINT,
    InventoryError,
    add_target,
    get_target,
    inventory_path,
    list_targets,
    remove_target,
    update_target,
)
from .ssh import SSHError, Target, looks_like_option, run
from .virt import VirtError
from .vm import VmError


def _force_utf8_output() -> None:
    """출력 스트림을 UTF-8 로 고정한다.

    파이프나 파일로 넘길 때 Python 은 stdout 인코딩을 시스템 로케일로 잡는다.
    한글 Windows(cp949) 등에서는 체크 기호(✓)를 인코딩하지 못해
    UnicodeEncodeError 로 죽어버리므로, 출력 전에 미리 막아둔다.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # 리다이렉트된 스트림 등 재설정이 불가능한 경우는 그냥 넘어간다.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8_output()

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="SSHerpa - build lab environments on on-premises servers.",
    no_args_is_help=True,
    add_completion=False,
)
target_app = typer.Typer(help="Manage targets (server address book).", no_args_is_help=True)
app.add_typer(target_app, name="target")

def _usage_hints(verb: str) -> list[str]:
    """인자 사용법 안내. 명령 이름은 지금 실행한 그 명령으로 적는다 —
    doctor 의 오류가 check 를 시키면 사용자는 엉뚱한 명령을 배운다."""
    return [
        f"Registered target:   ssherpa {verb} lab-01",
        f"One-off host:        ssherpa {verb} --host 10.0.0.10 --user admin",
    ]


# --------------------------------------------------------------------------
# 출력 헬퍼
# --------------------------------------------------------------------------

def _print_checks(rows: list[tuple[str, bool, str]]) -> None:
    """검사 결과를 정렬해서 출력한다."""
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, justify="center")
    table.add_column(overflow="fold")
    for label, ok, detail in rows:
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        table.add_row(label, mark, detail)
    console.print()
    console.print(Padding(table, (0, 0, 0, 2)))


def _say(text: str = "", indent: int = 2) -> None:
    """들여쓰기 붙여 한 줄 출력. 인자 없이 부르면 빈 줄."""
    if text:
        console.print(Padding(text, (0, 0, 0, indent)))
    else:
        console.print()


def _fail(message: str, hints: Optional[list[str]] = None) -> None:
    """실패 사유와 해결 방법을 출력하고 종료 코드 1 로 끝낸다."""
    err_console.print()
    err_console.print(Padding(f"[bold red]{message}[/bold red]", (0, 0, 0, 2)))
    if hints:
        err_console.print()
        for hint in hints:
            err_console.print(Padding(hint, (0, 0, 0, 4)))
    err_console.print()
    raise typer.Exit(code=1)


@contextlib.contextmanager
def _surface_errors():
    """도메인 오류를 사람 말로 출력하고 종료 코드 1 로 끝낸다.

    SSHError/ClusterError 는 (message, hints) 쌍을, InventoryError 는
    문자열 하나를 갖는다 — 어느 쪽이든 _fail 로 모은다.
    """
    try:
        yield
    except (SSHError, ClusterError, VirtError, VmError) as exc:
        _fail(exc.message, exc.hints)
    except InventoryError as exc:
        _fail(str(exc))


def _resolve_target(
    name: Optional[str],
    host: Optional[str],
    user: Optional[str],
    port: Optional[int],
    key: Optional[str],
    verb: str = "check",
) -> Target:
    """등록된 타겟 이름이든 일회성 --host 든 하나의 Target 으로 만든다.

    check 와 doctor 가 같은 규칙을 쓴다 — 인자 해석이 두 벌로 갈라지면
    한쪽만 고쳐지는 일이 생긴다.

    등록된 이름을 줬으면 접속 정보는 전부 인벤토리에서 온다. --host/--user
    만 막고 --port/--key 는 조용히 버리면, 같은 함수 안에 규칙이 두 개가
    된다 — 사용자는 `check lab-01 --key ~/.ssh/other` 가 그 키로 붙었다고
    믿게 되고, 실제로는 인벤토리의 키로 붙는다.
    """
    if name:
        conflicting = [
            flag
            for flag, value in (
                ("--host", host),
                ("--user", user),
                ("--port", port),
                ("--key", key),
            )
            if value is not None
        ]
        if conflicting:
            _fail(
                f"A target name cannot be combined with {'/'.join(conflicting)}.",
                [
                    "Connection details come from the registered target.",
                    "",
                    *_usage_hints(verb),
                    "",
                    # add 를 시키면 '이미 등록됨' 거절이 다시 update 를
                    # 가리킨다 — 안내가 원을 그리면 두 번 실패하고 도착한다.
                    f"Change what is registered:  ssherpa target update {name} ...",
                ],
            )
        with _surface_errors():
            return get_target(name)

    if not host:
        _fail("A target name or --host is required.", _usage_hints(verb))
    if looks_like_option(host):
        _fail(f"'{host}' is not a valid address.", HOST_OPTION_HINT)
    # 일회성 --host 는 등록보다 관대하다 — IPv6 도 여기서는 통과한다.
    # 의도다: check/doctor 는 IPv6 가 부러지는 자리(kubeconfig URL,
    # -J 표기, v4 전용 포워딩, SAN 비교) 중 어느 것도 건드리지 않으므로
    # 진단은 실제로 된다. 문은 등록에 걸려 있다 — up 까지 가는 건
    # 등록된 타겟뿐이니까.
    return Target(name=None, host=host, user=user, port=port, key=key)


class _NodeReporter:
    """단계 이름 앞에 어느 노드의 일인지 붙인다.

    여러 노드를 다룰 때 'preflight / wait for ip' 같은 단계가 이름 없이
    반복되면, 이미 있는 노드까지 새로 만드는 것처럼 읽힌다 (실사용 오해).
    """

    def __init__(self, inner, prefix: str):
        self.inner = inner
        self.prefix = prefix

    def step(self, label: str):
        return self.inner.step(f"{self.prefix}: {label}")


class StepReporter:
    """단계별 진행 표시.

    설치는 수십 초가 걸린다. 그동안 화면이 멈춰 있으면 사용자는 진행 중인지
    죽은 건지 알 수 없으므로, 실행 중에는 스피너를 돌리고 끝나면 걸린 시간을
    남긴다.
    """

    def __init__(self, console: Console):
        self.console = console

    @contextlib.contextmanager
    def step(self, label: str):
        started = time.monotonic()
        try:
            with self.console.status(f"[dim]{label}[/dim]", spinner="dots"):
                yield
        except BaseException:
            self.console.print(
                Padding(f"[red]✗[/red] {label}", (0, 0, 0, 4))
            )
            raise
        elapsed = time.monotonic() - started
        self.console.print(
            # 멀티노드에서는 'node-3: write cloud-init seed' 처럼 길어진다
            Padding(f"[green]✓[/green] {label:<30}[dim]{elapsed:6.1f}s[/dim]", (0, 0, 0, 4))
        )


# --------------------------------------------------------------------------
# target 명령
# --------------------------------------------------------------------------

@target_app.command("add")
def target_add(
    name: str = typer.Argument(..., help="Target name (e.g. lab-01)"),
    host: str = typer.Option(
        ..., "--host", help="IP, hostname, or a Host alias from ~/.ssh/config"
    ),
    user: Optional[str] = typer.Option(
        None, "--user", help="SSH username (omit to let ~/.ssh/config decide)"
    ),
    key: Optional[str] = typer.Option(None, "--key", help="Path to SSH private key"),
    port: Optional[int] = typer.Option(
        None, "--port", help="SSH port (omit to let ~/.ssh/config decide; ssh defaults to 22)"
    ),
) -> None:
    """Register a target in the inventory. Does not connect."""
    with _surface_errors():
        target = add_target(name, host=host, user=user, port=port, key=key)

    console.print()
    console.print(
        Padding(
            f"[green]✓[/green] [bold]{target.name}[/bold] registered  —  {target.endpoint()}",
            (0, 0, 0, 2),
        )
    )
    console.print(Padding(f"[dim]{inventory_path()}[/dim]", (0, 0, 0, 4)))
    console.print()
    console.print(Padding(f"[dim]Next:  ssherpa check {target.name}[/dim]", (0, 0, 0, 2)))
    console.print()


@target_app.command("update")
def target_update(
    name: str = typer.Argument(..., help="Registered target name"),
    host: Optional[str] = typer.Option(
        None, "--host", help="New IP, hostname, or ~/.ssh/config alias"
    ),
    user: Optional[str] = typer.Option(None, "--user", help="New SSH username"),
    key: Optional[str] = typer.Option(None, "--key", help="New path to SSH private key"),
    port: Optional[int] = typer.Option(None, "--port", help="New SSH port"),
    unset: Optional[list[str]] = typer.Option(
        None, "--unset", help="Clear a setting (user, port, key). Repeatable."
    ),
) -> None:
    """Change a registered target's connection details. Does not connect.

    Only what you pass is changed; everything else is left as it is. A
    cloud host that gets a new address on every restart needs one flag,
    not a remove and a re-add that would also drop the key and port.

    Clearing is a separate flag on purpose: --unset port. Overloading a
    value to also mean "erase this" is how things get erased by accident.
    """
    with _surface_errors():
        target, changes = update_target(
            name, host=host, user=user, port=port, key=key, unset=unset
        )

    console.print()
    if not changes:
        # 아무것도 안 바뀐 것을 '✓ 변경됨' 이라고 하면 거짓말이 된다.
        console.print(
            Padding(
                f"[dim]{target.name} already had those values — nothing changed.[/dim]",
                (0, 0, 0, 2),
            )
        )
        console.print()
        return

    console.print(
        Padding(
            f"[green]✓[/green] [bold]{target.name}[/bold] updated  —  "
            f"{target.endpoint()}",
            (0, 0, 0, 2),
        )
    )
    for change in changes:
        before = "(unset)" if change.before is None else change.before
        after = "(unset)" if change.after is None else change.after
        console.print(
            Padding(f"[dim]{change.field}:  {before}  →  {after}[/dim]", (0, 0, 0, 4))
        )
    console.print(Padding(f"[dim]{inventory_path()}[/dim]", (0, 0, 0, 4)))
    console.print()
    console.print(Padding(f"[dim]Next:  ssherpa check {target.name}[/dim]", (0, 0, 0, 2)))
    console.print()


@target_app.command("list")
def target_list() -> None:
    """List registered targets. Does not connect."""
    with _surface_errors():
        targets, broken = list_targets()

    if not targets and not broken:
        console.print()
        console.print(Padding("[dim]No targets registered.[/dim]", (0, 0, 0, 2)))
        console.print()
        console.print(
            Padding(
                "[dim]ssherpa target add lab-01 --host 10.0.0.10 --user admin[/dim]",
                (0, 0, 0, 2),
            )
        )
        console.print()
        return

    console.print()
    if targets:
        table = Table(box=None, pad_edge=False, padding=(0, 3, 0, 0))
        table.add_column("NAME", style="bold")
        table.add_column("HOST")
        table.add_column("USER")
        table.add_column("PORT", justify="right")

        for target in targets:
            # 비워둔 항목은 접속 시 ~/.ssh/config 가 정한다
            table.add_row(
                target.name,
                target.host,
                target.user or "[dim]—[/dim]",
                str(target.port) if target.port else "[dim]—[/dim]",
            )
        console.print(Padding(table, (0, 0, 0, 2)))
        console.print()

    # 깨진 항목도 이름은 보인다 — 숨기면 사용자는 자기가 뭘 고쳐야 하는지
    # 목록에서조차 알 수 없다.
    for name, reason in broken:
        err_console.print(
            Padding(f"[yellow]![/yellow] [bold]{name}[/bold]  {reason}", (0, 0, 0, 2))
        )
    if broken:
        err_console.print(
            Padding(f"[dim]Fix them in {inventory_path()}[/dim]", (0, 0, 0, 2))
        )
        err_console.print()


@target_app.command("remove")
def target_remove(
    name: str = typer.Argument(..., help="Target name to remove"),
) -> None:
    """Remove a target from the inventory. Does not touch the remote host."""
    with _surface_errors():
        remove_target(name)

    console.print()
    console.print(Padding(f"[green]✓[/green] [bold]{name}[/bold] removed", (0, 0, 0, 2)))
    console.print()


# --------------------------------------------------------------------------
# doctor 명령
# --------------------------------------------------------------------------

_DOCTOR_MARKS = {"ok": "[green]✓[/green]", "fail": "[red]✗[/red]", "info": "[dim]—[/dim]"}


@app.command("doctor")
def doctor(
    name: Optional[str] = typer.Argument(None, help="Registered target name"),
    host: Optional[str] = typer.Option(
        None, "--host", help="IP, hostname, or ~/.ssh/config alias (one-off)"
    ),
    user: Optional[str] = typer.Option(
        None, "--user", help="SSH username (omit to let ~/.ssh/config decide)"
    ),
    key: Optional[str] = typer.Option(None, "--key", help="Path to SSH private key"),
    port: Optional[int] = typer.Option(
        None, "--port", help="SSH port (omit to let ~/.ssh/config decide)"
    ),
) -> None:
    """Diagnose whether a host can run VM-backed clusters.

    `check` asks "can SSHerpa work with this host at all"; doctor asks
    "can this host create VMs". A host that fails here can still run
    host-mode clusters — the verdict says which way to go.
    """
    target = _resolve_target(name, host, user, port, key, verb="doctor")

    with _surface_errors():
        result = run(target, doctor_mod.DOCTOR_PROBE, timeout=60)

    facts_ = doctor_mod.parse_probe(result.stdout)
    diagnosis = doctor_mod.diagnose(facts_)

    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, justify="center")
    table.add_column(overflow="fold")
    for label, state, detail in diagnosis.rows:
        table.add_row(label, _DOCTOR_MARKS[state], detail)

    _say()
    _say(f"[dim]Host type:[/dim]  {diagnosis.host_type}")
    _say()
    console.print(Padding(table, (0, 0, 0, 2)))
    _say()

    label = target.name or target.host
    if diagnosis.capable:
        _say(f"[bold green]{label} can run VM-backed clusters[/bold green]")
        _say()
        # 판정만 하고 다음 걸음을 안 알려주면 발견 경로가 끊긴다 —
        # target add 가 check 를 안내하는 것과 같은 원칙.
        if target.name:
            _say(f"[dim]Next:  ssherpa up {target.name} --vm[/dim]")
            _say()
        return

    # 처방 속 자리표시자를 실제 타겟 이름으로 채운다. 일회성 --host 로
    # 부른 경우엔 채울 이름이 없다 — 주소를 넣으면 up 이 받지 않는
    # (등록되지 않은) 이름을 시키게 되므로, 등록부터 안내한다.
    if target.name:
        hints = [hint.replace("<target>", target.name) for hint in diagnosis.hints]
    else:
        hints = [hint.replace("<target>", "<name>") for hint in diagnosis.hints]
        if any("ssherpa up" in hint for hint in hints):
            hints += [
                "",
                "up works on registered targets, so register it first:",
                f"    ssherpa target add <name> --host {target.host}",
            ]
    _fail(diagnosis.failure, hints)


# --------------------------------------------------------------------------
# check 명령
# --------------------------------------------------------------------------

@app.command("check")
def check(
    name: Optional[str] = typer.Argument(None, help="Registered target name"),
    host: Optional[str] = typer.Option(
        None, "--host", help="IP, hostname, or ~/.ssh/config alias (one-off)"
    ),
    user: Optional[str] = typer.Option(
        None, "--user", help="SSH username (omit to let ~/.ssh/config decide)"
    ),
    key: Optional[str] = typer.Option(None, "--key", help="Path to SSH private key"),
    port: Optional[int] = typer.Option(
        None, "--port", help="SSH port (omit to let ~/.ssh/config decide)"
    ),
) -> None:
    """Check whether a host is ready for SSHerpa.

    Opens a short-lived SSH connection, runs a few probes, and disconnects.
    It does not leave a session open.
    """
    # 인벤토리에 등록된 타겟이든, 일회성 --host 든 둘 다 받는다.
    target = _resolve_target(name, host, user, port, key, verb="check")

    label = target.name or target.host

    # --- 1. SSH 연결 -------------------------------------------------------
    try:
        result = run(target, probe.PROBE)
    except SSHError as exc:
        _print_checks([("SSH connection", False, exc.message)])
        _fail("Check the following:" if exc.hints else exc.message, exc.hints)

    uid, remote_user, sudo_block, osrelease_text = probe.split_probe(result.stdout)

    # --user 없이 config 의 User 로 접속한 경우, 실제 계정명은 서버가 알려준다
    shown = target.endpoint()
    if not target.user and remote_user:
        shown = f"{remote_user}@{target.host}" + (f":{target.port}" if target.port else "")
    rows: list[tuple[str, bool, str]] = [("SSH connection", True, shown)]

    # --- 2. sudo 권한 ------------------------------------------------------
    sudo_ok, sudo_detail, sudo_hints = probe.judge_sudo(uid, sudo_block)
    rows.append(("sudo access", sudo_ok, sudo_detail))
    if not sudo_ok:
        _print_checks(rows)
        fix_user = remote_user or target.user or "<user>"
        _fail("Passwordless sudo is required", sudo_hints or probe.sudo_fix_hint(fix_user))

    # --- 3. OS 감지 --------------------------------------------------------
    if not osrelease_text.strip():
        rows.append(("OS detection", False, "could not read /etc/os-release"))
        _print_checks(rows)
        _fail(
            "Could not identify the operating system",
            [
                "Systems without /etc/os-release are not supported",
                f"Supported: {support.supported_summary()}",
            ],
        )

    os_info = facts.detect(osrelease_text)
    if not os_info.family:
        rows.append(("OS detection", False, os_info.pretty_name or "unknown"))
        _print_checks(rows)
        _fail(
            f"Unknown distribution family ({os_info.id or '?'})",
            [f"Supported: {support.supported_summary()}"],
        )

    rows.append(("OS detection", True, os_info.describe()))

    # --- 4. 지원 여부 ------------------------------------------------------
    supported, reason = support.is_supported(os_info.id, os_info.family, os_info.version_id)
    rows.append(("support status", supported, reason))
    _print_checks(rows)

    if not supported:
        _fail(
            f"This host is not supported: {reason}",
            [f"Supported: {support.supported_summary()}"],
        )

    console.print()
    console.print(Padding(f"[bold green]{label} is ready[/bold green]", (0, 0, 0, 2)))
    console.print()


# --------------------------------------------------------------------------
# up / down / ssh 명령
# --------------------------------------------------------------------------

def _resolve_distro(name: str):
    chosen = distro_mod.get(name)
    if chosen is None:
        _fail(
            f"Unknown distribution: {name}",
            [f"Available: {distro_mod.names()}"],
        )
    return chosen


def _interactive() -> bool:
    """사람이 앉아 있는 터미널인지.

    stdin 만 보면 안 된다. 출력을 파이프로 넘기면 stdin 은 여전히 TTY 지만
    화면 제어가 불가능해서 프롬프트가 터진다.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _distro_choices(vm: bool = False) -> list[tuple[str, str]]:
    """선택 프롬프트의 (표시줄, 값) 목록.

    메모리 숫자는 모드를 따른다 — 호스트 모드는 바닥, vm 모드는 VM 크기.
    doctor 가 보여주는 수와 같은 수가 나와야 두 화면이 서로 다른 말을
    하지 않는다.
    """
    return [
        (f"{o.name:<6} {o.summary} — {o.memory_note(vm)}", o.name)
        for o in distro_mod.DISTROS.values()
    ]


def _prompt_for_distro(vm: bool = False):
    """설치할 배포판을 화살표로 고르게 한다. 대화형 터미널에서만 호출된다."""
    options = list(distro_mod.DISTROS.values())
    try:
        answer = questionary.select(
            "Which Kubernetes distribution?",
            choices=[
                questionary.Choice(title=title, value=value)
                for title, value in _distro_choices(vm)
            ],
        ).ask()
    except Exception:  # 프롬프트를 띄울 수 없는 터미널
        return options[0]

    if answer is None:  # Ctrl+C
        raise typer.Exit(code=1)
    return _resolve_distro(answer)


def _confirm(question: str, *, assume_yes: bool, fallback: bool = True) -> bool:
    """파괴적이거나 놀랄 수 있는 동작 전에 한 번 묻는다.

    fallback 은 '프롬프트를 띄울 수 없을 때 어떻게 할 것인가'다. 되돌릴 수
    있는 동작은 막지 않는 편이 낫지만(True), 파괴는 아니다 — 물어보지
    못한 것은 승낙이 아니므로 그쪽은 False 로 부른다.
    """
    if assume_yes or not _interactive():
        return True
    try:
        return bool(questionary.confirm(question, default=False).ask())
    except Exception:
        return fallback


def _installed_on(node) -> list[str]:
    """호스트에 설치된 배포판 이름 목록."""
    with _surface_errors():
        host = cluster.status(node, list(distro_mod.DISTROS.values()))
    return [d.name for d in host.installed]


def _distro_to_install(
    node,
    requested: Optional[str] = None,
    installed: Optional[list] = None,
    where: str = "on this host",
    vm: bool = False,
):
    """설치 대상을 정한다.

    한 호스트에는 배포판 하나만 존재할 수 있다(모두 6443 과 /etc/rancher 를
    공유한다). 고를 일이 생기는 건 '아무것도 없을 때' 뿐이다:
    사람이 있으면 화살표로 묻고, 스크립트는 --distro 로 지정하며,
    지정 없는 스크립트는 k3s 를 기본으로 쓰되 그 사실을 로그에 남긴다 —
    조용히 내린 결정은 나중에 읽는 사람에게 미스터리가 된다.

    vm 모드도 같은 규칙을 쓴다 — where 만 바뀐다. 규칙이 두 벌이 되면
    한쪽만 고쳐지는 날이 온다.
    """
    if installed is None:
        installed = _installed_on(node)

    if len(installed) > 1:
        _fail(
            f"More than one distribution is installed {where}",
            [
                "They cannot coexist — remove them and start clean:",
                f"    ssherpa down {node.cli_name or node.target.name}",
            ],
        )

    if installed:
        existing = installed[0]
        if requested and requested != existing:
            _fail(
                f"{existing} is already installed {where}",
                [
                    "Kubernetes distributions cannot share a cluster — "
                    "they all bind port 6443.",
                    "Remove the existing one first:",
                    "",
                    f"    ssherpa down {node.cli_name or node.target.name}",
                ],
            )
        return _resolve_distro(existing)

    if requested:
        return _resolve_distro(requested)

    if not _interactive():
        console.print()
        console.print(
            Padding(
                "[dim]No terminal to ask which distribution — defaulting to k3s. "
                "Pass --distro to choose.[/dim]",
                (0, 0, 0, 2),
            )
        )
        return _resolve_distro("k3s")

    return _prompt_for_distro(vm)


def _load_target(name: str) -> Target:
    with _surface_errors():
        return get_target(name)


def _api_address(target: Target) -> str:
    """kubectl 이 접속할 주소. 인증서 SAN 과 kubeconfig 에 그대로 들어간다.

    타겟 주소가 ~/.ssh/config 의 별칭일 수 있다 — SSHerpa 는 그걸 허용하고,
    ssh 는 알아서 푼다. 하지만 kubectl 은 그 파일을 읽지 않으므로, 별칭이
    그대로 들어가면 '설치는 성공했는데 kubectl 은 전부 실패' 가 된다.
    ssh 에게 물어 실제 이름으로 바꾸고, 바뀌었으면 그 사실을 말한다.
    """
    resolved = ssh_mod.resolve_hostname(target)
    if resolved != target.host:
        _say()
        _say(
            f"[dim]{target.host} resolves to {resolved} (~/.ssh/config) — "
            "using that for kubectl.[/dim]"
        )
    return resolved


def _kubectl_with_config(path) -> str:
    """kubeconfig 를 환경변수로 얹어 kubectl 을 부르는 한 줄.

    이 명령을 붙여넣는 곳은 원격 호스트가 아니라 사용자의 터미널이다.
    문법은 그쪽 셸이 정하므로, 원격 명령과 달리 여기서만 OS 를 본다.
    """
    if os.name == "nt":
        return f'$env:KUBECONFIG="{path}"; kubectl'
    return f"KUBECONFIG={path} kubectl"


def _print_forwarding_exposure(target: Target) -> None:
    """VM 모드가 열어둔 길이 무엇을 지나가는지 밝힌다.

    호스트의 6443 을 VM 으로 넘기는 규칙은 FORWARD 체인 맨 앞에 들어간다.
    libvirt 의 거부 규칙보다 앞서야 해서 그런데, 그 자리는 ufw/firewalld 의
    규칙보다도 앞이다. 게다가 넘겨지는 패킷에는 INPUT 규칙이 적용되지
    않는다 — 호스트가 6443 을 INPUT 에서 막고 있어도 VM 의 API 는 열린다.

    막힌 것을 알려주는 안내는 이미 있는데, 정작 위험한 쪽은 열린 쪽이다.
    사용자가 호스트 방화벽을 믿고 있다면 그 믿음이 여기서 틀린다.

    과장하지는 않는다: 클라우드나 네트워크 방화벽은 호스트 바깥이라
    그대로 유효하다. 우회되는 것은 호스트의 iptables 층뿐이다.
    """
    where = target.name or target.host
    _say()
    _say(
        f"[yellow]Note: port {cluster.API_PORT} on the host now reaches the "
        "VM's API server.[/yellow]"
    )
    for line in (
        "The rule sits ahead of the host's own firewall (ufw, firewalld),",
        "and forwarded traffic never passes INPUT, so blocking 6443 there",
        "will not close this. Firewalls upstream of the host still apply.",
        f"Close it with:  ssherpa down {where}",
    ):
        _say(f"[dim]{line}[/dim]", indent=4)


def _print_up_result(
    result, distro_name: str, target: Target, *, via_vm: bool = False
) -> None:
    """up 의 결과 요약과 다음 할 일 안내.

    ~/.kube/config 병합 결과에 따라 kubectl 사용법이 셋으로 갈린다:
    기본값이 됐으면 그냥 kubectl, 기존 기본값을 존중했으면 --context,
    병합이 실패했으면 단독 파일 + 환경변수 폴백.
    """
    _say()
    if result.already_installed:
        _say(f"[dim]{distro_name} was already installed — left as is.[/dim]")
    if result.certificate_refreshed:
        _say(
            "[yellow]The host address changed since install — the certificate "
            "was refreshed to match.[/yellow]"
        )
    if result.node_count > 1:
        _say(f"[bold green]Cluster ready — {result.node_count} nodes[/bold green]")
    else:
        _say("[bold green]Cluster ready[/bold green]")
    _say(f"[dim]kubeconfig: {result.kubeconfig}[/dim]")
    _say()

    if result.merge_error:
        _say(f"[yellow]Could not update ~/.kube/config: {result.merge_error}[/yellow]")
        _say("Use the standalone file instead:", indent=4)
        kubectl = _kubectl_with_config(result.kubeconfig)
    elif result.context_is_current:
        _say(
            f"[dim]Added to ~/.kube/config as context "
            f"[/dim][bold]{result.context}[/bold][dim] (now the default).[/dim]"
        )
        kubectl = "kubectl"
    else:
        _say(
            f"[dim]Added to ~/.kube/config as context "
            f"[/dim][bold]{result.context}[/bold][dim] — your current context "
            "was left untouched.[/dim]"
        )
        kubectl = f"kubectl --context {result.context}"

    _say()
    if result.api_reachable:
        _say("Use it from any terminal:")
        _say(f"[dim]{kubectl} get nodes[/dim]", indent=4)
        if via_vm:
            _print_forwarding_exposure(target)
    else:
        # 포트가 막힌 것은 흔한 정상 상황이다. 실패로 처리하지 않고 방법을 안내한다.
        _say(f"[yellow]Port {cluster.API_PORT} is not reachable from here.[/yellow]")
        _say()
        _say("Open an SSH tunnel in another terminal:", indent=4)
        _say(
            f"[dim]ssh -L {cluster.API_PORT}:127.0.0.1:{cluster.API_PORT} "
            f"{target.destination()}[/dim]",
            indent=6,
        )
        _say()
        _say("then point kubectl at the tunnel:", indent=4)
        _say(
            f"[dim]{kubectl} --server https://127.0.0.1:{cluster.API_PORT} get nodes[/dim]",
            indent=6,
        )
        _say()
        _say(
            "[dim]Opening the port to the internet instead would expose the "
            "cluster API — restrict it to your own address if you do.[/dim]",
            indent=4,
        )
    _say()


def _up_vm(
    name: str, target: Target, requested: Optional[str], nodes: Optional[int]
) -> None:
    """vm 모드: 호스트 위에 VM 을 만들고 그 안에 쿠버네티스를 올린다.

    사용자는 libvirt/cloud-init/포트포워딩의 존재를 몰라도 된다 —
    기반 준비부터 kubectl 연결까지가 이 한 번의 호출이다.

    nodes 가 2 이상이면 VM 을 그만큼 만들어 하나의 클러스터로 묶는다.
    첫 번째가 server, 나머지는 거기에 합류하는 agent 다.

    nodes 가 None 이면 '몇 대인지 말하지 않았다'는 뜻이다 — 이미 있는
    클러스터의 크기를 그대로 따른다. 1 로 단정하면 3노드가 도는 호스트에서
    '1노드 준비됨' 이라고 보고하게 된다 (실측).
    """
    # 호스트에 직접 설치된 클러스터와는 공존할 수 없다. kubectl 이 쓸 6443 을
    # 호스트 모드는 자신이 듣고, vm 모드는 VM 으로 넘겨야 하기 때문이다.
    node_host = cluster.nodes_for_host_mode(target)[0]
    installed = _installed_on(node_host)
    if installed:
        _fail(
            f"{installed[0]} is installed directly on this host",
            [
                "A host-mode cluster and a VM cluster would fight over "
                f"port {cluster.API_PORT}.",
                "Remove the existing one first:",
                f"    ssherpa down {name}",
            ],
        )

    # 만들기 전에 정해둔다. 인증서에 한 번 박히면 바꾸는 데 재발급이 든다.
    api_address = _api_address(target)

    # 이미 몇 대가 있는지는 호스트에게 묻는다. 저장해두지 않는 이유는
    # 늘 같다 — 파일과 현실이 어긋날 수 있으니까.
    with _surface_errors():
        existing = vm_mod.list_vms(target)

    if nodes is None:
        # 말하지 않았으면 있는 그대로 — 없으면 한 대.
        nodes = max(1, len(existing))
    elif len(existing) > nodes:
        # 줄이는 건 VM 을 지우는 일이라 조용히 하면 안 된다. 그렇다고
        # 요청보다 많은 채로 '준비됨' 이라고 할 수도 없다.
        _fail(
            f"{len(existing)} nodes already exist here, but "
            f"{nodes} {'was' if nodes == 1 else 'were'} asked for",
            [
                "SSHerpa does not shrink a cluster — removing nodes destroys them.",
                "Take it down and build the size you want:",
                f"    ssherpa down {name}",
                f"    ssherpa up {name} --vm --nodes {nodes}",
                "",
                f"Or omit --nodes to keep the {len(existing)} that are there.",
            ],
        )

    # 이름이 1..N 로 연속일 때만 개수 기반 계획이 성립한다. VM 을 손으로
    # (virsh) 지우는 것은 이 프로젝트가 인정한 사용법이고(고아 예약 청소가
    # 그 실측에서 나왔다), 그렇게 생긴 구멍을 모르는 채 세면:
    #   - node-1 이 없으면 '기존 server 재사용' 전제가 무너져, 새 server 를
    #     기본 크기(2GB)로 짓고 빈 VM 에게 배포판을 묻는다 (실측)
    #   - 중간이 비면 그 이름을 새로 만들고 구멍 뒤 노드는 계획 밖에 남는다
    expected = [vm_mod.node_name(i) for i in range(1, len(existing) + 1)]
    if existing and existing != expected:
        if vm_mod.node_name(1) not in existing:
            headline = f"this cluster has no server VM ({vm_mod.node_name(1)})"
            detail = [
                f"Found {', '.join(existing)} — agents whose server is gone.",
                "A new server would issue a new join token these agents do not",
                "know; they would retry against the old one forever.",
            ]
        else:
            headline = "this cluster's node names have a gap"
            detail = [
                f"Found {', '.join(existing)}; expected {', '.join(expected)}.",
                "Counting by size would rebuild the missing name as a fresh VM",
                "and leave everything after the gap out of the plan.",
            ]
        # 원래 크기는 남은 이름의 최대 번호다 — node-2,3 이 남았으면 3노드
        # 클러스터였다. len(existing) 을 쓰면 원래보다 작게 복구하라고
        # 권하게 된다.
        tails = [n.rsplit("-", 1)[-1] for n in existing]
        size = max((int(t) for t in tails if t.isdigit()), default=len(existing))
        _fail(
            headline,
            [
                *detail,
                "This cannot be healed in place. Take it down and rebuild:",
                f"    ssherpa down {name}",
                f"    ssherpa up {name} --vm --nodes {size}",
            ],
        )

    # 빈 호스트에서만 고를 일이 생긴다. VM 이 이미 있으면 답은 그 안에
    # 있다 — 꺼진 VM 안은 볼 수 없으니, server 를 먼저 살린 뒤에 정한다.
    chosen = None
    if not existing:
        chosen = _distro_to_install(node_host, requested, installed=[], vm=True)

    console.print()
    what = "a VM" if nodes == 1 else f"{nodes} VMs"
    if chosen is not None:
        banner = f"Installing [bold]{chosen.name}[/bold] on {what} on [bold]{name}[/bold]"
    else:
        what = "the VM" if nodes == 1 else f"{nodes} VMs"
        banner = f"Bringing up {what} on [bold]{name}[/bold]"
    console.print(Padding(banner, (0, 0, 0, 2)))
    console.print()

    reporter = StepReporter(console)
    with _surface_errors():
        foundation = virt.setup(target, reporter)

        def check_fit(distro):
            # 만들기 전에 거절한다. 절반쯤 만들다 메모리가 떨어지면
            # 사용자에게는 지워야 할 VM 몇 대와 이유 모를 실패만 남는다.
            # VM 크기는 배포판이 정하므로, 배포판이 정해진 뒤에야 셀 수 있다.
            usable = foundation.usable_memory_mb
            size_gb = distro.vm_memory_mb // 1024
            if usable is not None:
                capacity = usable // distro.vm_memory_mb
                if nodes > capacity:
                    _fail(
                        f"this host fits about {capacity} {distro.name} VM(s) "
                        f"({size_gb} GB each), but {nodes} were asked for",
                        [
                            f"Each {distro.name} node takes {size_gb} GB, and "
                            "the host keeps some for itself.",
                            f"Try --nodes {capacity}, or add memory to the host.",
                            f"See the estimate:  ssherpa doctor {name}",
                        ],
                    )
            # 디스크는 얇은 파일이라 당장 다 차지하지는 않지만, 자라면
            # 이 크기까지 간다. 막지 않고 말해둔다 — 가득참은 스냅샷이
            # 반쯤 쓰이다 깨지는 최악의 실패 양상이다.
            new_count = nodes - len(existing)
            free = foundation.disk_free_gb
            if free is not None and new_count > 0:
                growth = new_count * distro.vm_disk_gb
                if growth > free:
                    console.print(
                        Padding(
                            f"[yellow]Note: these VMs can grow to {growth} GB of "
                            f"disk; the host has {free:.0f} GB free.[/yellow]",
                            (0, 0, 0, 2),
                        )
                    )

        def build(spec, role):
            short = spec.name.removeprefix(vm_mod.VM_PREFIX)
            # 한 대뿐이면 어느 노드인지 물을 일이 없다 — 이름표는 붙이지 않는다.
            step_reporter = reporter if nodes == 1 else _NodeReporter(reporter, short)
            info = vm_mod.create(target, spec=spec, reporter=step_reporter)
            return info, cluster.Node(
                name=vm_mod.node_label(name, spec.name),
                target=vm_mod.vm_target(target, info),
                role=role,
                # 오류 힌트가 안내할 이름은 VM 의 합성 이름이 아니라 사용자가
                # 등록한 타겟이다 — 그리고 그 안으로 들어가려면 --vm 이 필요하다.
                cli_name=name,
                in_vm=True,
                short_name=short,
            )

        if chosen is not None:
            # 빈 호스트: 크기가 정해졌으니 만들기 전에 감당이 되는지 본다.
            check_fit(chosen)
            specs = vm_mod.specs_for(
                nodes, chosen.vm_memory_mb, chosen.vm_disk_gb
            )
            server_info, server = build(specs[0], "server")
        else:
            # 이미 있는 클러스터: server 를 살려 안을 들여다본 뒤에 정한다.
            # 크기 인자는 닿지 않는다 — create 는 있는 VM 을 그대로 쓴다.
            server_info, server = build(vm_mod.specs_for(nodes)[0], "server")
            inside = _installed_on(server)
            chosen = _distro_to_install(
                server,
                requested,
                installed=inside,
                where="in the VMs on this host",
                vm=True,
            )
            if inside and not requested:
                console.print(
                    Padding(
                        f"[dim]{chosen.name} runs inside these VMs — "
                        "continuing with it.[/dim]",
                        (0, 0, 0, 2),
                    )
                )
            # 늘어나는 만큼은 새 VM 이다 — 배포판이 정해졌으니 이제 셀 수 있다.
            check_fit(chosen)
            specs = vm_mod.specs_for(
                nodes, chosen.vm_memory_mb, chosen.vm_disk_gb
            )

        agents = [build(spec, "agent")[1] for spec in specs[1:]]

        # 바깥에서 들어오는 길은 server 로만 낸다 — kubectl 이 말을 거는
        # API 서버가 거기에만 있다.
        vm_mod.expose_api(target, server_info.ip, reporter)

        # 인증서와 kubeconfig 에는 밖에서 닿는 주소(호스트)를 넣는다 —
        # VM 의 NAT 주소는 내 PC 의 kubectl 이 갈 수 없는 주소다.
        result = cluster.up(
            server, chosen, reporter, api_address=api_address, agents=agents
        )

    _print_up_result(result, chosen.name, target, via_vm=True)


@app.command("up")
def up(
    name: str = typer.Argument(..., help="Registered target name"),
    distro: Optional[str] = typer.Option(
        None,
        "--distro",
        help=f"Choose without prompting ({distro_mod.names()}) — for scripts",
    ),
    vm_mode: bool = typer.Option(
        False, "--vm", help="Run the cluster inside a VM on the host"
    ),
    nodes: Optional[int] = typer.Option(
        None,
        "--nodes",
        help="How many VM nodes the cluster has (requires --vm; default 1)",
    ),
    assume_yes: bool = typer.Option(
        False, "--yes", "-y", help="Do not ask for confirmation"
    ),
) -> None:
    """Install Kubernetes on a target host.

    Asks which distribution to install when the host is empty (pass --distro
    to skip the question, e.g. in scripts). If one is already installed it is
    detected and left alone, so re-running is safe. With --vm the cluster
    runs inside a VM on the host instead of on the host itself, and --nodes
    spreads it over that many VMs.
    """
    target = _load_target(name)

    # 값이 말이 되는지를 모드보다 먼저 본다. 순서가 반대면 `--nodes 0` 이
    # 호스트 모드에서 "--vm 을 붙이라" 는 안내를 받는데, 붙여도 거절이다 —
    # 고칠 수 없는 명령을 시키는 안내가 된다.
    if nodes is not None and nodes < 1:
        _fail(
            f"--nodes must be at least 1 (got {nodes})",
            ["A cluster needs a node to run on."],
        )

    if vm_mode:
        _up_vm(name, target, distro, nodes)
        return

    # 노드를 늘리려면 기계를 늘려야 하고, 호스트 모드에는 기계가 하나뿐이다.
    if nodes is not None and nodes != 1:
        _fail(
            "--nodes needs --vm",
            [
                "A node is a machine. Host mode has exactly one, so extra nodes",
                "have nowhere to live — VMs are what makes more of them:",
                f"    ssherpa up {name} --vm --nodes {nodes}",
            ],
        )

    node = cluster.nodes_for_host_mode(target)[0]

    # 반대 방향의 6443 충돌도 막는다: VM 클러스터가 이미 있으면 호스트 모드
    # 설치는 포워딩 규칙과 포트를 두고 싸우게 된다.
    with _surface_errors():
        existing_vms = vm_mod.list_vms(target)
    if existing_vms:
        _fail(
            f"A VM cluster already runs on this host ({', '.join(existing_vms)})",
            [
                f"It forwards port {cluster.API_PORT} to the VM, which a "
                "host-mode cluster would also need.",
                "Remove it first:",
                f"    ssherpa down {name}",
            ],
        )

    already_installed = _installed_on(node)

    # 충돌(--distro 가 설치본과 다름)은 재실행 안내보다 먼저 판정해야 한다 —
    # "재설치는 안 한다"고 말해놓고 거부하면 앞뒤가 안 맞는다.
    chosen = _distro_to_install(node, distro, installed=already_installed)

    if already_installed:
        console.print()
        console.print(
            Padding(
                f"[yellow]{already_installed[0]}[/yellow] is already installed "
                f"on [bold]{name}[/bold].",
                (0, 0, 0, 2),
            )
        )
        console.print(
            Padding(
                "[dim]Re-running will wait for the node and refresh the kubeconfig. "
                "Nothing is reinstalled.[/dim]",
                (0, 0, 0, 2),
            )
        )
        console.print()
        if not _confirm("Continue?", assume_yes=assume_yes):
            raise typer.Exit(code=1)

    console.print()
    console.print(
        Padding(f"Installing [bold]{chosen.name}[/bold] on [bold]{name}[/bold]", (0, 0, 0, 2))
    )
    console.print()

    with _surface_errors():
        result = cluster.up(
            node, chosen, StepReporter(console), api_address=_api_address(target)
        )

    _print_up_result(result, chosen.name, target)


@app.command("status")
def status(
    name: str = typer.Argument(..., help="Registered target name"),
) -> None:
    """Show what Kubernetes is installed and running on a target."""
    target = _load_target(name)
    node = cluster.nodes_for_host_mode(target)[0]
    known = list(distro_mod.DISTROS.values())

    # 호스트에 묻는 일은 전부 여기서 끝낸다. 출력 도중에 한 번 더 물으면
    # 그때의 연결 실패는 이 블록 밖이라, 사람 말 대신 스택트레이스가 나간다.
    with _surface_errors():
        host = cluster.status(node, known)
        vms = [(vm, vm_mod.vm_state(target, vm)) for vm in vm_mod.list_vms(target)]

        # VM 안의 클러스터도 물어본다. 호스트만 보고 'not installed' 라고
        # 하면, 3노드가 멀쩡히 도는 중에도 아무것도 없다고 답하게 된다.
        #
        # 안을 못 들여다보는 것(꺼진 VM, 안 뜬 k3s)은 보고를 멈출 이유가
        # 아니다 — status 는 아픈 호스트 앞에서도 아는 만큼은 말해야 한다.
        # 호스트 자체의 접속은 위에서 이미 성공했으므로 여기서 삼키는 것은
        # VM 안쪽 사정뿐이다.
        inside = None
        unreachable = None
        if vms:
            try:
                # 기다리지 않는다. status 는 지금 상태를 말하는 명령이지
                # 상태가 좋아지기를 기다리는 명령이 아니다.
                info = vm_mod.find(target, timeout=0)
                if info is not None:
                    inside = cluster.status(
                        cluster.Node(
                            name=info.name,
                            target=vm_mod.vm_target(target, info),
                            cli_name=name,
                            in_vm=True,
                        ),
                        known,
                    )
            except (VmError, SSHError) as exc:
                # 못 물어봤다는 사실 자체가 보고할 내용이다. 조용히 넘기면
                # 침묵이 '클러스터가 없다' 로 읽힌다 (실측: server 노드가
                # 꺼져 있을 때 아무 줄도 나오지 않았다).
                unreachable = exc.message

    console.print()
    console.print(Padding(f"Status of [bold]{name}[/bold]", (0, 0, 0, 2)))
    console.print()

    # 답부터 말한다. VM 클러스터가 있는 호스트에서 'k3s — not installed'
    # 두 줄을 먼저 내밀면, 멀쩡히 도는 클러스터 앞에서 아무것도 없다는
    # 인상을 준다 (실사용 지적). 배포판 표는 호스트에 직접 깔렸거나
    # VM 이 아예 없을 때만 답이다.
    if host.installed or not vms:
        table = Table(
            box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0)
        )
        table.add_column(no_wrap=True, style="bold")
        table.add_column(no_wrap=True)
        table.add_column(overflow="fold")
        where = " on the host" if vms else ""
        for entry in host.distros:
            if not entry.installed:
                table.add_row(
                    entry.name, "[dim]—[/dim]", f"[dim]not installed{where}[/dim]"
                )
                continue
            colour = "green" if entry.running else "yellow"
            state = entry.service_state or "unknown"
            table.add_row(
                entry.name, "[green]✓[/green]", f"installed  [{colour}]{state}[/{colour}]"
            )
        console.print(Padding(table, (0, 0, 0, 2)))
        console.print()

    # VM 안에서 도는 클러스터를 한 줄로 요약한다 — VM 이 있으면 이게 답이다.
    if inside is not None and inside.running:
        running = inside.running[0].name
        ready = inside.ready_count
        total = len(inside.node_lines)
        shape = f"{ready}/{total} nodes Ready" if total else "no nodes yet"
        colour = "green" if total and ready == total else "yellow"
        console.print(
            Padding(
                f"[dim]cluster:[/dim]  {running} in the VMs  "
                f"[{colour}]{shape}[/{colour}]",
                (0, 0, 0, 2),
            )
        )
        console.print()
    elif inside is not None and inside.installed:
        # 깔려는 있는데 안 돈다 — '없다' 도 '준비됨' 도 아닌 상태를
        # 그대로 말해야 사용자가 맞는 다음 걸음을 찾는다.
        entry = inside.installed[0]
        state = entry.service_state or "not running"
        console.print(
            Padding(
                f"[dim]cluster:[/dim]  {entry.name} in the VMs  "
                f"[yellow]{state}[/yellow]",
                (0, 0, 0, 2),
            )
        )
        console.print()
    elif inside is not None:
        # VM 은 있는데 안이 비었다 — 만들다 만 상태. 침묵하면
        # '클러스터가 없다' 로 읽힌다.
        console.print(
            Padding(
                f"[dim]cluster:[/dim]  [yellow]nothing installed in the VMs "
                f"yet[/yellow]  [dim]ssherpa up {name} --vm finishes the "
                "job[/dim]",
                (0, 0, 0, 2),
            )
        )
        console.print()
    elif unreachable:
        console.print(
            Padding(
                f"[dim]cluster:[/dim]  [yellow]could not ask the VMs — "
                f"{unreachable}[/yellow]",
                (0, 0, 0, 2),
            )
        )
        console.print()
    elif host.node_lines:
        console.print(
            Padding(f"[dim]node:[/dim]  {host.node_lines[0]}", (0, 0, 0, 2))
        )
        console.print()

    if vms:
        for index, (vm_name, state) in enumerate(vms):
            colour = "green" if state == "running" else "yellow"
            label = "[dim]vm:[/dim]" if index == 0 else "   "
            console.print(
                Padding(
                    f"{label}  {vm_name}  [{colour}]{state or 'unknown'}[/{colour}]",
                    (0, 0, 0, 2),
                )
            )
        console.print()
        if not host.installed:
            # 표를 걷어낸 자리의 정보는 남긴다 — 호스트 직접 설치가
            # 없다는 사실은 down/up 의 동작을 예측하는 데 쓰인다.
            console.print(
                Padding(
                    "[dim]The host itself has nothing installed — "
                    "the cluster lives in the VMs.[/dim]",
                    (0, 0, 0, 2),
                )
            )
            console.print()

    if host.installed and vms:
        # SSHerpa 는 이 상태를 만들지 않는다 — up 은 양쪽 문에서 거절한다.
        # 그러니 여기 왔다는 건 누가 손으로 깔았다는 뜻이고, 둘 다 초록불로
        # 보여주면 6443 을 두고 싸우는 중이라는 사실이 숨는다: 포워딩이
        # 바깥 트래픽을 VM 으로 보내므로 호스트 쪽 설치는 밖에서 조용히
        # 끊긴다.
        err_console.print(
            Padding(
                f"[yellow]{host.installed[0].name} on the host and the VM "
                "cluster both need port 6443.[/yellow]",
                (0, 0, 0, 2),
            )
        )
        err_console.print()
        err_console.print(
            Padding(
                "The host forwards that port into the VMs, so from outside "
                "kubectl reaches",
                (0, 0, 0, 4),
            )
        )
        err_console.print(
            Padding(
                f"the VM cluster — the host's {host.installed[0].name} is cut "
                "off. Start clean:",
                (0, 0, 0, 4),
            )
        )
        err_console.print(Padding(f"[dim]ssherpa down {name}[/dim]", (0, 0, 0, 8)))
        err_console.print()

    if host.conflicted:
        # 두 배포판이 함께 있으면 둘 다 6443 을 잡으려 해서 나중 것이 못 뜬다.
        err_console.print(
            Padding(
                "[yellow]More than one distribution is installed.[/yellow]",
                (0, 0, 0, 2),
            )
        )
        err_console.print()
        err_console.print(
            Padding(
                # down 은 골라서 지우지 않는다 — 찾은 것을 전부 걷는다.
                # 배포판을 골라 지우라는 안내는 down 에 없는 옵션을 시키는
                # 것이었다 (--distro 는 up 의 옵션이다).
                "They all bind port 6443, so only one can run. down removes "
                "everything",
                (0, 0, 0, 4),
            )
        )
        err_console.print(
            Padding(
                "it finds — start clean, then put back the one you want:",
                (0, 0, 0, 4),
            )
        )
        err_console.print(Padding(f"[dim]ssherpa down {name}[/dim]", (0, 0, 0, 8)))
        err_console.print(
            Padding(
                # <> 는 자리표시자 관례다 — 따옴표 없는 | 를 그대로 내면
                # 붙여넣는 순간 셸이 파이프로 읽는다.
                f"[dim]ssherpa up {name} --distro <{distro_mod.names().replace(', ', '|')}>[/dim]",
                (0, 0, 0, 8),
            )
        )
        err_console.print()
    elif not host.installed and not vms:
        console.print(Padding(f"[dim]Nothing installed.  ssherpa up {name}[/dim]", (0, 0, 0, 2)))
        console.print()


@app.command("down")
def down(
    name: str = typer.Argument(..., help="Registered target name"),
    assume_yes: bool = typer.Option(
        False, "--yes", "-y", help="Do not ask for confirmation"
    ),
) -> None:
    """Remove Kubernetes from a target host.

    Whatever is there is detected and removed — a host-mode install, a VM
    cluster, or nothing. There is no mode to remember or pass.
    """
    target = _load_target(name)
    node = cluster.nodes_for_host_mode(target)[0]
    installed = _installed_on(node)
    with _surface_errors():
        vms = vm_mod.list_vms(target)
        # VM 을 손으로 지운 뒤 down 하면 VM 목록은 비어 있지만 포워딩은
        # 남는다. 그걸 안 걷으면 부팅마다 없는 주소로 6443 을 넘기는
        # 규칙이 되살아나, 그 포트가 영영 막힌 호스트가 된다.
        forwarding = vm_mod.forwarding_installed(target)
        # 주소 예약도 같은 이유로 남는다. 포워딩만 걷고 이쪽을 두면
        # 없는 VM 이 주소를 붙든 채 쌓인다 (실측: node-3/4/5).
        stale = vm_mod.stale_reservations(target, vms)

    if not installed and not vms and not forwarding and not stale:
        console.print()
        console.print(Padding("[dim]Nothing is installed on this host.[/dim]", (0, 0, 0, 2)))
        console.print()
        return

    # 무엇을 지우는지 그대로 적는다. 남은 자국만 걷는 자리에서 "클러스터를
    # 파괴한다" 고 하면 없는 일을 경고하는 것이고, 그런 경고는 다음부터
    # 읽히지 않는다 — 진짜로 클러스터가 걸린 순간에도.
    # 포워딩은 VM 이 있으면 잔재가 아니라 그 클러스터로 가는 길이므로 따로
    # 세지 않는다. 예약은 이미 '고아인 것' 만 골라낸 목록이라 언제나 따로 센다.
    leftovers = []
    if forwarding and not vms:
        leftovers.append("the leftover API forwarding")
    if stale:
        leftovers.append(
            f"{len(stale)} stale address reservation{'s' if len(stale) > 1 else ''}"
        )

    destroys_cluster = bool(installed or vms)
    removing = installed + vms if destroys_cluster else leftovers

    console.print()
    console.print(
        Padding(
            f"This removes [bold]{', '.join(removing)}[/bold] from [bold]{name}[/bold]"
            + (" and destroys the cluster." if destroys_cluster else "."),
            (0, 0, 0, 2),
        )
    )
    if destroys_cluster:
        console.print(
            Padding("[dim]Workloads and cluster state are lost.[/dim]", (0, 0, 0, 2))
        )
        # 클러스터 말고 더 걷는 것이 있으면 그것도 미리 말한다. 진행 표시에서
        # 처음 보게 하면, 예고하지 않은 일을 한 것이 된다.
        if leftovers:
            console.print(
                Padding(
                    f"[dim]Also sweeping: {', '.join(leftovers)}.[/dim]", (0, 0, 0, 2)
                )
            )
    else:
        console.print(
            Padding(
                "[dim]No cluster is running here — these are leftovers.[/dim]",
                (0, 0, 0, 2),
            )
        )
    console.print()

    # 물어볼 자리가 없다고 해서 승낙은 아니다. up 은 되돌릴 수 있으니 조용히
    # 진행해도 되지만, 여기서 잘못 진행하면 클러스터가 돌아오지 않는다 —
    # 파괴는 명시적으로 요청받았을 때만 한다.
    #
    # 다만 되돌릴 것이 없는 자리까지 막지는 않는다. 없는 VM 을 가리키는
    # 규칙과 예약을 걷는 데에는 잃을 것이 없고, 여기서 --yes 를 요구하면
    # 스크립트로 뒷정리를 할 수 없다.
    if destroys_cluster and not assume_yes and not _interactive():
        _fail(
            "Refusing to destroy a cluster without confirmation",
            [
                "There is no terminal to ask on, and this cannot be undone.",
                "Say so explicitly if that is what you want:",
                "",
                f"    ssherpa down {name} --yes",
            ],
        )

    # 프롬프트를 띄우지 못한 것도 승낙이 아니다 — 되돌릴 수 없는 쪽에서는
    # 모르면 멈춘다.
    if not _confirm("Continue?", assume_yes=assume_yes, fallback=False):
        raise typer.Exit(code=1)

    console.print()
    reporter = StepReporter(console)

    # VM 클러스터는 VM 삭제가 곧 제거다 — 안에서 k3s 를 지울 필요가 없다.
    if vms or forwarding:
        with _surface_errors():
            for vm_name in vms:
                vm_mod.destroy(target, vm_name, reporter)
            vm_mod.unexpose_api(target, reporter)

        # 죽은 클러스터를 가리키는 로컬 흔적도 함께 걷는다 (host 모드와 동일).
        # 이름은 up 이 쓴 것과 같은 함수로 짓는다 — 각자 지으면 한쪽만 바뀌었을
        # 때 지워지지 않고 남는다 (실측).
        for vm_name in vms:
            entry = vm_mod.node_label(name, vm_name)
            path = cluster.kubeconfig_path(entry)
            if path.exists():
                path.unlink()
            with contextlib.suppress(kubeconf.KubeconfigError):
                kubeconf.remove(entry)

    # VM 을 지운 뒤에도 남는 예약이 있다 — 손으로 지워진 옛 노드의 것이다.
    # destroy 는 자기가 지우는 VM 의 예약만 걷으므로, 그 밖은 여기서 쓴다.
    if stale:
        with _surface_errors():
            vm_mod.release_reservations(target, stale, reporter)

    for distro_name in installed:
        chosen = _resolve_distro(distro_name)
        with _surface_errors():
            cluster.down(node, chosen, reporter)

    console.print()
    console.print(Padding("[bold green]Removed[/bold green]", (0, 0, 0, 2)))
    console.print()


@app.command(
    "ssh",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ssh_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Registered target name"),
    vm_flag: bool = typer.Option(
        False, "--vm", help="Connect to the VM on the host instead of the host"
    ),
) -> None:
    """Open an interactive SSH session to a target.

    With --vm the session lands inside the SSHerpa VM on the host — the
    VM's address, dedicated key, and the hop through the host are filled
    in automatically. Extra arguments are passed straight through to ssh.
    """
    target = _load_target(name)

    if vm_flag:
        with _surface_errors():
            info = vm_mod.find(target)
        if info is None:
            _fail(
                "There is no SSHerpa VM on this host",
                [f"Create one (with a cluster inside):  ssherpa up {name} --vm"],
            )
        target = vm_mod.vm_target(target, info)

    if shutil.which("ssh") is None:
        _fail("ssh client not found")

    # 명시된 것만 넘긴다 — 나머지는 ~/.ssh/config 가 정한다 (run() 과 동일 원칙)
    argv = ["ssh"]
    if target.port:
        argv += ["-p", str(target.port)]
    if target.key:
        argv += ["-i", os.path.expanduser(target.key)]
    if target.jump:
        argv += ["-J", target.jump]
    if target.known_hosts:
        argv += ["-o", f"UserKnownHostsFile={os.path.expanduser(target.known_hosts)}"]
    argv += [target.destination(), *ctx.args]

    # 대화형 세션이므로 출력을 가로채지 않고 그대로 넘긴다.
    raise typer.Exit(code=subprocess.call(argv))


# --------------------------------------------------------------------------

def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ssherpa {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version"
    ),
) -> None:
    pass


if __name__ == "__main__":
    app()
