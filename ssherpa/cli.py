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

from . import __version__, cluster, facts, probe, support
from . import distro as distro_mod
from .cluster import ClusterError
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
            Padding(f"[green]✓[/green] {label:<22}[dim]{elapsed:6.1f}s[/dim]", (0, 0, 0, 4))
        )


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
        result = run(target, probe.PROBE)
    except SSHError as exc:
        _print_checks([("SSH connection", False, exc.message)])
        _fail("Check the following:" if exc.hints else exc.message, exc.hints)

    rows: list[tuple[str, bool, str]] = [("SSH connection", True, target.endpoint())]
    uid, sudo_block, osrelease_text = probe.split_probe(result.stdout)

    # --- 2. sudo 권한 ------------------------------------------------------
    sudo_ok, sudo_detail, sudo_hints = probe.judge_sudo(uid, sudo_block)
    rows.append(("sudo access", sudo_ok, sudo_detail))
    if not sudo_ok:
        _print_checks(rows)
        _fail("Passwordless sudo is required", sudo_hints or probe.sudo_fix_hint(target.user))

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


def _prompt_for_distro():
    """설치할 배포판을 화살표로 고르게 한다. 대화형 터미널에서만 호출된다."""
    options = list(distro_mod.DISTROS.values())
    try:
        answer = questionary.select(
            "Which Kubernetes distribution?",
            choices=[
                questionary.Choice(title=f"{o.name:<6} {o.summary}", value=o.name)
                for o in options
            ],
        ).ask()
    except Exception:  # 프롬프트를 띄울 수 없는 터미널
        return options[0]

    if answer is None:  # Ctrl+C
        raise typer.Exit(code=1)
    return _resolve_distro(answer)


def _confirm(question: str, *, assume_yes: bool) -> bool:
    """파괴적이거나 놀랄 수 있는 동작 전에 한 번 묻는다."""
    if assume_yes or not _interactive():
        return True
    try:
        return bool(questionary.confirm(question, default=False).ask())
    except Exception:  # 프롬프트를 띄울 수 없으면 막지 않는다
        return True


def _installed_on(node) -> list[str]:
    """호스트에 설치된 배포판 이름 목록."""
    try:
        host = cluster.status(node, list(distro_mod.DISTROS.values()))
    except SSHError as exc:
        _fail(exc.message, exc.hints)
    return [d.name for d in host.installed]


def _distro_to_install(node):
    """설치 대상을 정한다.

    한 호스트에는 배포판 하나만 존재할 수 있다(모두 6443 과 /etc/rancher 를
    공유한다). 그래서 고를 일이 생기는 건 '아무것도 없을 때' 뿐이고, 그때만
    물어본다. 이미 있으면 그것을 그대로 따르므로 충돌이 발생할 수 없다.
    """
    installed = _installed_on(node)

    if len(installed) > 1:
        _fail(
            "More than one distribution is installed on this host",
            [
                "They cannot coexist — remove them and start clean:",
                f"    ssherpa down {node.target.name}",
            ],
        )

    if installed:
        return _resolve_distro(installed[0])

    # 스크립트로 실행되면 물어볼 수 없으므로 가벼운 쪽을 쓴다.
    if not _interactive():
        return _resolve_distro("k3s")

    return _prompt_for_distro()


def _load_target(name: str) -> Target:
    try:
        return get_target(name)
    except InventoryError as exc:
        _fail(str(exc))


@app.command("up")
def up(
    name: str = typer.Argument(..., help="Registered target name"),
    assume_yes: bool = typer.Option(
        False, "--yes", "-y", help="Do not ask for confirmation"
    ),
) -> None:
    """Install Kubernetes on a target host.

    Asks which distribution to install when the host is empty. If one is
    already installed it is detected and left alone, so re-running is safe.
    """
    target = _load_target(name)
    node = cluster.nodes_for_host_mode(target)[0]
    already_installed = _installed_on(node)

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

    chosen = _distro_to_install(node)

    console.print()
    console.print(
        Padding(f"Installing [bold]{chosen.name}[/bold] on [bold]{name}[/bold]", (0, 0, 0, 2))
    )
    console.print()

    try:
        result = cluster.up(node, chosen, StepReporter(console))
    except SSHError as exc:
        _fail(exc.message, exc.hints)
    except ClusterError as exc:
        _fail(exc.message, exc.hints)

    console.print()
    if result.already_installed:
        console.print(
            Padding(f"[dim]{chosen.name} was already installed — left as is.[/dim]", (0, 0, 0, 2))
        )
    if result.certificate_refreshed:
        console.print(
            Padding(
                "[yellow]The host address changed since install — the certificate "
                "was refreshed to match.[/yellow]",
                (0, 0, 0, 2),
            )
        )
    console.print(Padding("[bold green]Cluster ready[/bold green]", (0, 0, 0, 2)))
    console.print(Padding(f"[dim]kubeconfig: {result.kubeconfig}[/dim]", (0, 0, 0, 2)))
    console.print()

    # ~/.kube/config 병합 결과에 따라 kubectl 사용법이 달라진다.
    if result.merge_error:
        console.print(
            Padding(
                f"[yellow]Could not update ~/.kube/config: {result.merge_error}[/yellow]",
                (0, 0, 0, 2),
            )
        )
        console.print(Padding("Use the standalone file instead:", (0, 0, 0, 4)))
        kubectl_cmd = f'$env:KUBECONFIG="{result.kubeconfig}"; kubectl'
    elif result.context_is_current:
        console.print(
            Padding(
                f"[dim]Added to ~/.kube/config as context "
                f"[/dim][bold]{result.context}[/bold][dim] (now the default).[/dim]",
                (0, 0, 0, 2),
            )
        )
        kubectl_cmd = "kubectl"
    else:
        console.print(
            Padding(
                f"[dim]Added to ~/.kube/config as context "
                f"[/dim][bold]{result.context}[/bold][dim] — your current context "
                "was left untouched.[/dim]",
                (0, 0, 0, 2),
            )
        )
        kubectl_cmd = f"kubectl --context {result.context}"

    console.print()
    if result.api_reachable:
        console.print(Padding("Use it from any terminal:", (0, 0, 0, 2)))
        console.print(Padding(f"[dim]{kubectl_cmd} get nodes[/dim]", (0, 0, 0, 4)))
    else:
        # 포트가 막힌 것은 흔한 정상 상황이다. 실패로 처리하지 않고 방법을 안내한다.
        console.print(
            Padding(
                f"[yellow]Port {cluster.API_PORT} is not reachable from here.[/yellow]",
                (0, 0, 0, 2),
            )
        )
        console.print()
        console.print(Padding("Open an SSH tunnel in another terminal:", (0, 0, 0, 4)))
        console.print(
            Padding(
                f"[dim]ssh -L {cluster.API_PORT}:127.0.0.1:{cluster.API_PORT} "
                f"{target.user}@{target.host}[/dim]",
                (0, 0, 0, 6),
            )
        )
        console.print()
        console.print(Padding("then point kubectl at the tunnel:", (0, 0, 0, 4)))
        console.print(
            Padding(
                f"[dim]{kubectl_cmd} --server https://127.0.0.1:{cluster.API_PORT} "
                "get nodes[/dim]",
                (0, 0, 0, 6),
            )
        )
        console.print()
        console.print(
            Padding(
                "[dim]Opening the port to the internet instead would expose the "
                "cluster API — restrict it to your own address if you do.[/dim]",
                (0, 0, 0, 4),
            )
        )
    console.print()


@app.command("status")
def status(
    name: str = typer.Argument(..., help="Registered target name"),
) -> None:
    """Show what Kubernetes is installed and running on a target."""
    target = _load_target(name)
    node = cluster.nodes_for_host_mode(target)[0]
    known = list(distro_mod.DISTROS.values())

    try:
        host = cluster.status(node, known)
    except SSHError as exc:
        _fail(exc.message, exc.hints)

    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column(no_wrap=True, style="bold")
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    for entry in host.distros:
        if not entry.installed:
            table.add_row(entry.name, "[dim]—[/dim]", "[dim]not installed[/dim]")
            continue
        colour = "green" if entry.running else "yellow"
        state = entry.service_state or "unknown"
        table.add_row(entry.name, "[green]✓[/green]", f"installed  [{colour}]{state}[/{colour}]")

    console.print()
    console.print(Padding(f"Status of [bold]{name}[/bold]", (0, 0, 0, 2)))
    console.print()
    console.print(Padding(table, (0, 0, 0, 2)))
    console.print()

    if host.node_line:
        console.print(Padding(f"[dim]node:[/dim]  {host.node_line}", (0, 0, 0, 2)))
        console.print()

    if host.conflicted:
        # 두 배포판이 함께 있으면 둘 다 6443 을 잡으려 해서 나중 것이 못 뜬다.
        extra = [d.name for d in host.installed if not d.running]
        err_console.print(
            Padding(
                "[yellow]More than one distribution is installed.[/yellow]",
                (0, 0, 0, 2),
            )
        )
        err_console.print()
        err_console.print(
            Padding(
                "They all bind port 6443, so only one can run. Remove the others:",
                (0, 0, 0, 4),
            )
        )
        for other in extra or [d.name for d in host.installed[1:]]:
            err_console.print(
                Padding(f"[dim]ssherpa down {name} --distro {other}[/dim]", (0, 0, 0, 8))
            )
        err_console.print()
    elif not host.installed:
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

    Whatever is installed is detected and removed — there is nothing to choose,
    since a host can only run one distribution.
    """
    target = _load_target(name)
    node = cluster.nodes_for_host_mode(target)[0]
    installed = _installed_on(node)

    if not installed:
        console.print()
        console.print(Padding("[dim]Nothing is installed on this host.[/dim]", (0, 0, 0, 2)))
        console.print()
        return

    # 클러스터를 통째로 없애는 동작이라 되돌릴 수 없다.
    console.print()
    console.print(
        Padding(
            f"This removes [bold]{', '.join(installed)}[/bold] from [bold]{name}[/bold] "
            "and destroys the cluster.",
            (0, 0, 0, 2),
        )
    )
    console.print(Padding("[dim]Workloads and cluster state are lost.[/dim]", (0, 0, 0, 2)))
    console.print()
    if not _confirm("Continue?", assume_yes=assume_yes):
        raise typer.Exit(code=1)

    console.print()

    for distro_name in installed:
        chosen = _resolve_distro(distro_name)
        try:
            cluster.down(node, chosen, StepReporter(console))
        except SSHError as exc:
            _fail(exc.message, exc.hints)
        except ClusterError as exc:
            _fail(exc.message, exc.hints)

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
) -> None:
    """Open an interactive SSH session to a target.

    Extra arguments are passed straight through to ssh.
    """
    target = _load_target(name)

    if shutil.which("ssh") is None:
        _fail("ssh client not found")

    argv = ["ssh", "-p", str(target.port)]
    if target.key:
        argv += ["-i", os.path.expanduser(target.key)]
    argv += [f"{target.user}@{target.host}", *ctx.args]

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
