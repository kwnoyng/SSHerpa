# SSHerpa

Build Kubernetes labs on a single on-premises server, over nothing but SSH.

> **v0.1** — only the connection layer exists today. `ssherpa check` tells you
> whether a host is ready. VM provisioning and Kubernetes come later.

## Requirements

| | |
|---|---|
| Your machine | Python 3.9+, an OpenSSH client |
| Target host | Ubuntu 22.04 / 24.04, Rocky 9, or AlmaLinux 9 |
| Access | SSH key-based login and passwordless `sudo` |

Windows 10/11, macOS, and most Linux distributions ship an OpenSSH client
already.

**Password authentication is not supported by design.** SSHerpa runs
non-interactively, so it uses public-key authentication only.

## Install

```bash
pip install git+https://github.com/kwnoyng/SSHerpa.git
```

Or, for an isolated install:

```bash
pipx install git+https://github.com/kwnoyng/SSHerpa.git
```

## Usage

Replace the address and username below with your own host.

```bash
ssherpa check --host 10.0.0.10 --user admin
```

```
  SSH connection  ✓  admin@10.0.0.10:22
  sudo access     ✓  NOPASSWD
  OS detection    ✓  Rocky Linux 9.8 (Blue Onyx)  (family: RedHat)
  support status  ✓  supported

  10.0.0.10 is ready
```

`check` opens a short-lived SSH connection, runs a few probes, and disconnects.
It does not leave a session open. It exits `0` when every check passes and `1`
otherwise, so it composes with scripts.

When something is wrong, it says what to do about it:

```
  SSH connection  ✓  deploy@10.0.0.10:22
  sudo access     ✗  password required

  Passwordless sudo is required

    SSHerpa needs passwordless sudo for 'deploy'.
    Run this on the target host:

        echo 'deploy ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ssherpa
```

### Saving hosts you use often

```bash
ssherpa target add lab-01 --host 10.0.0.10 --user admin --key ~/.ssh/id_ed25519
ssherpa target list
ssherpa check lab-01
```

```
  NAME     HOST        USER    PORT
  lab-01   10.0.0.10   admin     22
```

`ssherpa target remove lab-01` deletes the entry. It never touches the remote
host — the inventory is an address book, nothing more.

### Commands

| Command | Connects over SSH |
|---|---|
| `ssherpa target add <NAME> --host <IP> --user <USER> [--key <PATH>] [--port <N>]` | no |
| `ssherpa target list` | no |
| `ssherpa target remove <NAME>` | no |
| `ssherpa check <NAME>` | **yes** |
| `ssherpa check --host <IP> --user <USER> [--key <PATH>] [--port <N>]` | **yes** |

`--port` defaults to `22`. If `--key` is omitted, `ssh` looks for its usual
default keys (`~/.ssh/id_ed25519`, `id_ecdsa`, `id_rsa`).

## The inventory

Targets live in `~/.ssherpa/inventory.yml`, written as a plain Ansible
inventory:

```yaml
all:
  hosts:
    lab-01:
      ansible_host: 10.0.0.10
      ansible_user: admin
      ansible_port: 22
      ansible_ssh_private_key_file: ~/.ssh/id_ed25519
```

Two decisions worth knowing:

- **The format is Ansible's on purpose.** Later stages drive Ansible roles, and
  this file will be reused as-is rather than converted.
- **Only connection details are stored.** The detected OS — and later the VM and
  snapshot lists — are never written here. They are read from the host every
  time, so the file cannot drift out of sync with reality.

Set `SSHERPA_INVENTORY` to use a different path.

## Development

```bash
git clone https://github.com/kwnoyng/SSHerpa.git
cd SSHerpa
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
```

Unit tests need nothing but Python, and cover failure modes that are
impractical to reproduce against a live host — changed host keys, unroutable
networks, over-permissive key files:

```bash
pytest
```

Integration tests need Docker. Six containers stand in for real hosts — one per
supported release, plus one without `sudo` and one running an unsupported
release. Every version listed as supported is actually connected to here. Both
scripts generate their own throwaway SSH key, so they never depend on your
personal keys.

```bash
./tests/verify.sh            # macOS / Linux
.\tests\verify.ps1           # Windows
./tests/verify.sh down       # tear down
```
