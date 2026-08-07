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
    HOST_OPTION_HINT,
    InventoryError,
    add_target,
    get_target,
    inventory_path,
    list_targets,
    remove_target,
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


def _resolve_target(
    name: Optional[str],
    host: Optional[str],
    user: Optional[str],
    port: Optional[int],
    key: Optional[str],
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
                    *USAGE_HINTS,
                    "",
                    f"Change what is registered:  ssherpa target add {name} ...",
                ],
            )
        with _surface_errors():
            return get_target(name)

    if not host:
        _fail("A target name or --host is required.", USAGE_HINTS)
    if looks_like_option(host):
        _fail(f"'{host}' is not a valid address.", HOST_OPTION_HINT)
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
    target = _resolve_target(name, host, user, port, key)

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
    target = _resolve_target(name, host, user, port, key)

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

    console.print()
    where = "a VM" if nodes == 1 else f"{nodes} VMs"
    console.print(
        Padding(
            f"Installing [bold]{chosen.name}[/bold] on {where} on [bold]{name}[/bold]",
            (0, 0, 0, 2),
        )
    )
    console.print()

    reporter = StepReporter(console)
    with _surface_errors():
        foundation = virt.setup(target, reporter)

        # 만들기 전에 거절한다. 절반쯤 만들다 메모리가 떨어지면 사용자에게는
        # 지워야 할 VM 몇 대와 이유 모를 실패만 남는다. doctor 가 보여주는
        # 것과 같은 계산이라 안내와 결과가 어긋나지 않는다.
        capacity = foundation.vm_capacity
        if capacity is not None and nodes > capacity:
            _fail(
                f"this host fits about {capacity} VM(s), but {nodes} were asked for",
                [
                    f"Each node takes {vm_mod.PER_VM_MB // 1024} GB, and the host "
                    "keeps some for itself.",
                    f"Try --nodes {capacity}, or add memory to the host.",
                    f"See the estimate:  ssherpa doctor {name}",
                ],
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

        specs = vm_mod.specs_for(nodes)
        server_info, server = build(specs[0], "server")
        agents = [build(spec, "agent")[1] for spec in specs[1:]]

        # 바깥에서 들어오는 길은 server 로만 낸다 — kubectl 이 말을 거는
        # API 서버가 거기에만 있다.
        vm_mod.expose_api(target, server_info.ip, reporter)

        # 인증서와 kubeconfig 에는 밖에서 닿는 주소(호스트)를 넣는다 —
        # VM 의 NAT 주소는 내 PC 의 kubectl 이 갈 수 없는 주소다.
        result = cluster.up(
            server, chosen, reporter, api_address=target.host, agents=agents
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

    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column(no_wrap=True, style="bold")
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    where = " on the host" if vms else ""
    for entry in host.distros:
        if not entry.installed:
            table.add_row(entry.name, "[dim]—[/dim]", f"[dim]not installed{where}[/dim]")
            continue
        colour = "green" if entry.running else "yellow"
        state = entry.service_state or "unknown"
        table.add_row(entry.name, "[green]✓[/green]", f"installed  [{colour}]{state}[/{colour}]")

    console.print()
    console.print(Padding(f"Status of [bold]{name}[/bold]", (0, 0, 0, 2)))
    console.print()
    console.print(Padding(table, (0, 0, 0, 2)))
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

    # VM 안에서 도는 클러스터를 한 줄로 요약한다.
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
        # VM 을 손으로 지운 뒤 down 하면 VM 목록은 비어 있지만 포워딩은
        # 남는다. 그걸 안 걷으면 부팅마다 없는 주소로 6443 을 넘기는
        # 규칙이 되살아나, 그 포트가 영영 막힌 호스트가 된다.
        forwarding = vm_mod.forwarding_installed(target)

    if not installed and not vms and not forwarding:
        console.print()
        console.print(Padding("[dim]Nothing is installed on this host.[/dim]", (0, 0, 0, 2)))
        console.print()
        return

    # 클러스터를 통째로 없애는 동작이라 되돌릴 수 없다.
    removing = installed + vms or ["the leftover API forwarding"]
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

    # 물어볼 자리가 없다고 해서 승낙은 아니다. up 은 되돌릴 수 있으니 조용히
    # 진행해도 되지만, 여기서 잘못 진행하면 클러스터가 돌아오지 않는다 —
    # 파괴는 명시적으로 요청받았을 때만 한다.
    if not assume_yes and not _interactive():
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
