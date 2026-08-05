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
from . import vm as vm_mod
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


@target_app.command("list")
def target_list() -> None:
    """List registered targets. Does not connect."""
    with _surface_errors():
        targets = list_targets()

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
        # 비워둔 항목은 접속 시 ~/.ssh/config 가 정한다
        table.add_row(
            target.name,
            target.host,
            target.user or "[dim]—[/dim]",
            str(target.port) if target.port else "[dim]—[/dim]",
        )

    console.print()
    console.print(Padding(table, (0, 0, 0, 2)))
    console.print()


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
    if name:
        if host or user:
            _fail("A target name cannot be combined with --host/--user.", USAGE_HINTS)
        with _surface_errors():
            target = get_target(name)
    else:
        if not host:
            _fail("A target name or --host is required.", USAGE_HINTS)
        target = Target(name=None, host=host, user=user, port=port, key=key)

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

    # 처방 속 자리표시자를 실제 타겟 이름으로 채운다
    hints = [hint.replace("<target>", label) for hint in diagnosis.hints]
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
    if name:
        if host or user:
            _fail("A target name cannot be combined with --host/--user.", USAGE_HINTS)
        with _surface_errors():
            target = get_target(name)
    else:
        if not host:
            _fail("A target name or --host is required.", USAGE_HINTS)
        target = Target(name=None, host=host, user=user, port=port, key=key)

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
    with _surface_errors():
        host = cluster.status(node, list(distro_mod.DISTROS.values()))
    return [d.name for d in host.installed]


def _distro_to_install(
    node, requested: Optional[str] = None, installed: Optional[list] = None
):
    """설치 대상을 정한다.

    한 호스트에는 배포판 하나만 존재할 수 있다(모두 6443 과 /etc/rancher 를
    공유한다). 고를 일이 생기는 건 '아무것도 없을 때' 뿐이다:
    사람이 있으면 화살표로 묻고, 스크립트는 --distro 로 지정하며,
    지정 없는 스크립트는 k3s 를 기본으로 쓰되 그 사실을 로그에 남긴다 —
    조용히 내린 결정은 나중에 읽는 사람에게 미스터리가 된다.
    """
    if installed is None:
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
        existing = installed[0]
        if requested and requested != existing:
            _fail(
                f"{existing} is already installed on this host",
                [
                    "Kubernetes distributions cannot share a host — "
                    "they all bind port 6443.",
                    "Remove the existing one first:",
                    "",
                    f"    ssherpa down {node.target.name}",
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

    return _prompt_for_distro()


def _load_target(name: str) -> Target:
    with _surface_errors():
        return get_target(name)


def _print_up_result(result, distro_name: str, target: Target) -> None:
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
    _say("[bold green]Cluster ready[/bold green]")
    _say(f"[dim]kubeconfig: {result.kubeconfig}[/dim]")
    _say()

    if result.merge_error:
        _say(f"[yellow]Could not update ~/.kube/config: {result.merge_error}[/yellow]")
        _say("Use the standalone file instead:", indent=4)
        kubectl = f'$env:KUBECONFIG="{result.kubeconfig}"; kubectl'
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


def _up_vm(name: str, target: Target, requested: Optional[str]) -> None:
    """vm 모드: 호스트 위에 VM 을 만들고 그 안에 쿠버네티스를 올린다.

    사용자는 libvirt/cloud-init/포트포워딩의 존재를 몰라도 된다 —
    기반 준비부터 kubectl 연결까지가 이 한 번의 호출이다.
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

    # VM 사양이 2 GB 로 고정돼 있어 지금은 k3s 만 들어간다.
    if requested and requested != "k3s":
        _fail(
            "VM mode currently installs k3s only",
            [
                f"The VM has 2 GB of memory and {requested} needs more.",
                "Omit --distro (or pass --distro k3s).",
            ],
        )
    chosen = _resolve_distro("k3s")

    console.print()
    console.print(
        Padding(
            f"Installing [bold]{chosen.name}[/bold] on a VM on [bold]{name}[/bold]",
            (0, 0, 0, 2),
        )
    )
    console.print()

    reporter = StepReporter(console)
    with _surface_errors():
        virt.setup(target, reporter)
        info = vm_mod.create(target, reporter=reporter)
        vm_mod.expose_api(target, info.ip, reporter)
        vm_node = cluster.Node(
            name=f"{name}-vm", target=vm_mod.vm_target(target, info)
        )
        # 인증서와 kubeconfig 에는 밖에서 닿는 주소(호스트)를 넣는다 —
        # VM 의 NAT 주소는 내 PC 의 kubectl 이 갈 수 없는 주소다.
        result = cluster.up(vm_node, chosen, reporter, api_address=target.host)

    _print_up_result(result, chosen.name, target)


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
    assume_yes: bool = typer.Option(
        False, "--yes", "-y", help="Do not ask for confirmation"
    ),
) -> None:
    """Install Kubernetes on a target host.

    Asks which distribution to install when the host is empty (pass --distro
    to skip the question, e.g. in scripts). If one is already installed it is
    detected and left alone, so re-running is safe. With --vm the cluster
    runs inside a VM on the host instead of on the host itself.
    """
    target = _load_target(name)

    if vm_mode:
        _up_vm(name, target, distro)
        return

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
        result = cluster.up(node, chosen, StepReporter(console))

    _print_up_result(result, chosen.name, target)


@app.command("status")
def status(
    name: str = typer.Argument(..., help="Registered target name"),
) -> None:
    """Show what Kubernetes is installed and running on a target."""
    target = _load_target(name)
    node = cluster.nodes_for_host_mode(target)[0]
    known = list(distro_mod.DISTROS.values())

    with _surface_errors():
        host = cluster.status(node, known)
        vms = vm_mod.list_vms(target)

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

    for vm_name in vms:
        state = vm_mod.vm_state(target, vm_name)
        colour = "green" if state == "running" else "yellow"
        console.print(
            Padding(
                f"[dim]vm:[/dim]  {vm_name}  [{colour}]{state or 'unknown'}[/{colour}]",
                (0, 0, 0, 2),
            )
        )
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

    if not installed and not vms:
        console.print()
        console.print(Padding("[dim]Nothing is installed on this host.[/dim]", (0, 0, 0, 2)))
        console.print()
        return

    # 클러스터를 통째로 없애는 동작이라 되돌릴 수 없다.
    removing = installed + vms
    console.print()
    console.print(
        Padding(
            f"This removes [bold]{', '.join(removing)}[/bold] from [bold]{name}[/bold] "
            "and destroys the cluster.",
            (0, 0, 0, 2),
        )
    )
    console.print(Padding("[dim]Workloads and cluster state are lost.[/dim]", (0, 0, 0, 2)))
    console.print()
    if not _confirm("Continue?", assume_yes=assume_yes):
        raise typer.Exit(code=1)

    console.print()
    reporter = StepReporter(console)

    # VM 클러스터는 VM 삭제가 곧 제거다 — 안에서 k3s 를 지울 필요가 없다.
    if vms:
        with _surface_errors():
            for vm_name in vms:
                vm_mod.destroy(target, vm_name, reporter)
            vm_mod.unexpose_api(target, reporter)

        # 죽은 클러스터를 가리키는 로컬 흔적도 함께 걷는다 (host 모드와 동일)
        entry = f"{name}-vm"
        path = cluster.kubeconfig_path(entry)
        if path.exists():
            path.unlink()
        with contextlib.suppress(kubeconf.KubeconfigError):
            kubeconf.remove(entry)

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
