"""SSHerpa CLI 진입점."""

import contextlib
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.padding import Padding
from rich.table import Table

from . import __version__, facts, support
from .inventory import (
    InventoryError,
    add_target,
    get_target,
    inventory_path,
    list_targets,
    remove_target,
)
from .ssh import SSHError, Target, run


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

USAGE_HINTS = [
    "Registered target:   ssherpa check lab-01",
    "One-off check:       ssherpa check --host 10.0.0.10 --user admin",
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


# --------------------------------------------------------------------------
# 원격 검사
# --------------------------------------------------------------------------

# 한 번의 접속으로 uid / sudo / os-release 를 모두 가져온다.
PROBE = (
    'echo "SSHERPA_UID=$(id -u)"; '
    'echo "SSHERPA_SUDO"; '
    "sudo -n true 2>&1; "
    'echo "SSHERPA_SUDO_RC=$?"; '
    'echo "SSHERPA_OSRELEASE"; '
    "cat /etc/os-release 2>/dev/null"
)


def _split_probe(stdout: str) -> tuple[str, str, str]:
    """PROBE 출력을 (uid, sudo구간, os-release) 로 나눈다."""
    uid = ""
    sudo_block: list[str] = []
    osrelease: list[str] = []
    section = "head"

    for line in stdout.splitlines():
        if line.startswith("SSHERPA_UID="):
            uid = line.split("=", 1)[1].strip()
        elif line == "SSHERPA_SUDO":
            section = "sudo"
        elif line == "SSHERPA_OSRELEASE":
            section = "os"
        elif section == "sudo":
            sudo_block.append(line)
        elif section == "os":
            osrelease.append(line)

    return uid, "\n".join(sudo_block), "\n".join(osrelease)


def _judge_sudo(uid: str, sudo_block: str) -> tuple[bool, str, list[str]]:
    """(통과여부, 표시문구, 힌트) 를 돌려준다."""
    if uid == "0":
        return True, "root account", []

    rc = None
    message_lines = []
    for line in sudo_block.splitlines():
        if line.startswith("SSHERPA_SUDO_RC="):
            rc = line.split("=", 1)[1].strip()
        else:
            message_lines.append(line)
    message = "\n".join(message_lines).lower()

    if rc == "0":
        return True, "NOPASSWD", []

    if "command not found" in message or rc == "127":
        return False, "sudo is not installed", [
            "Install sudo on the target host, or use the root account",
        ]

    if "password is required" in message:
        return False, "password required", []

    if "not allowed" in message or "not in the sudoers" in message:
        return False, "not in sudoers", []

    return False, "sudo is unavailable", (
        [line for line in message_lines if line.strip()][:2]
    )


def _sudo_fix_hint(user: str) -> list[str]:
    return [
        f"SSHerpa needs passwordless sudo for '{user}'.",
        "Run this on the target host:",
        "",
        f"    echo '{user} ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ssherpa",
    ]


# --------------------------------------------------------------------------
# target 명령
# --------------------------------------------------------------------------

@target_app.command("add")
def target_add(
    name: str = typer.Argument(..., help="Target name (e.g. lab-01)"),
    host: str = typer.Option(..., "--host", help="IP address or hostname"),
    user: str = typer.Option(..., "--user", help="SSH username"),
    key: Optional[str] = typer.Option(None, "--key", help="Path to SSH private key"),
    port: int = typer.Option(22, "--port", help="SSH port"),
) -> None:
    """Register a target in the inventory. Does not connect."""
    try:
        target = add_target(name, host=host, user=user, port=port, key=key)
    except InventoryError as exc:
        _fail(str(exc))

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


@target_app.command("list")
def target_list() -> None:
    """List registered targets. Does not connect."""
    try:
        targets = list_targets()
    except InventoryError as exc:
        _fail(str(exc))

    if not targets:
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

    table = Table(box=None, pad_edge=False, padding=(0, 3, 0, 0))
    table.add_column("NAME", style="bold")
    table.add_column("HOST")
    table.add_column("USER")
    table.add_column("PORT", justify="right")

    for target in targets:
        table.add_row(target.name, target.host, target.user, str(target.port))

    console.print()
    console.print(Padding(table, (0, 0, 0, 2)))
    console.print()


@target_app.command("remove")
def target_remove(
    name: str = typer.Argument(..., help="Target name to remove"),
) -> None:
    """Remove a target from the inventory. Does not touch the remote host."""
    try:
        remove_target(name)
    except InventoryError as exc:
        _fail(str(exc))

    console.print()
    console.print(Padding(f"[green]✓[/green] [bold]{name}[/bold] removed", (0, 0, 0, 2)))
    console.print()


# --------------------------------------------------------------------------
# check 명령
# --------------------------------------------------------------------------

@app.command("check")
def check(
    name: Optional[str] = typer.Argument(None, help="Registered target name"),
    host: Optional[str] = typer.Option(None, "--host", help="IP or hostname (one-off)"),
    user: Optional[str] = typer.Option(None, "--user", help="SSH username"),
    key: Optional[str] = typer.Option(None, "--key", help="Path to SSH private key"),
    port: int = typer.Option(22, "--port", help="SSH port"),
) -> None:
    """Check whether a host is ready for SSHerpa.

    Opens a short-lived SSH connection, runs a few probes, and disconnects.
    It does not leave a session open.
    """
    # 인벤토리에 등록된 타겟이든, 일회성 --host 든 둘 다 받는다.
    if name:
        if host or user:
            _fail("A target name cannot be combined with --host/--user.", USAGE_HINTS)
        try:
            target = get_target(name)
        except InventoryError as exc:
            _fail(str(exc))
    else:
        if not host or not user:
            _fail("A target name, or both --host and --user, are required.", USAGE_HINTS)
        target = Target(name=None, host=host, user=user, port=port, key=key)

    label = target.name or target.host

    # --- 1. SSH 연결 -------------------------------------------------------
    try:
        result = run(target, PROBE)
    except SSHError as exc:
        _print_checks([("SSH connection", False, exc.message)])
        _fail("Check the following:" if exc.hints else exc.message, exc.hints)

    rows: list[tuple[str, bool, str]] = [("SSH connection", True, target.endpoint())]
    uid, sudo_block, osrelease_text = _split_probe(result.stdout)

    # --- 2. sudo 권한 ------------------------------------------------------
    sudo_ok, sudo_detail, sudo_hints = _judge_sudo(uid, sudo_block)
    rows.append(("sudo access", sudo_ok, sudo_detail))
    if not sudo_ok:
        _print_checks(rows)
        _fail("Passwordless sudo is required", sudo_hints or _sudo_fix_hint(target.user))

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
