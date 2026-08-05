# SSHerpa

Build Kubernetes labs on a single on-premises server, over nothing but SSH.

> **v0.2** — register a host, check that it is ready, and stand up a
> single-node Kubernetes cluster (k3s or RKE2) directly on it. VM-based
> multi-node labs with snapshot/rewind come later.

## Requirements

| | |
|---|---|
| Your machine | Python 3.9+, an OpenSSH client |
| Target host | Ubuntu 22.04 / 24.04, Rocky 9, or AlmaLinux 9 |
| Access | SSH key-based login and passwordless `sudo` |
| Memory | ~1 GB for k3s, ~4 GB for RKE2 |

Windows 10/11, macOS, and most Linux distributions ship an OpenSSH client
already.

**Password authentication is not supported by design.** SSHerpa runs
non-interactively, so it uses public-key authentication only.

## Install

```bash
pip install ssherpa
```

Or, for an isolated install (recommended for CLI tools):

```bash
pipx install ssherpa
```

To try the latest unreleased changes instead:

```bash
pip install git+https://github.com/kwnoyng/SSHerpa.git@develop
```

## Usage

### 1. Register a host and check it

Replace the address and username below with your own host.

```bash
ssherpa target add lab-01 --host 10.0.0.10 --user admin --key ~/.ssh/id_ed25519
ssherpa check lab-01
```

```
  SSH connection  ✓  admin@10.0.0.10:22
  sudo access     ✓  NOPASSWD
  OS detection    ✓  Ubuntu 24.04.4 LTS  (family: Debian)
  support status  ✓  supported

  lab-01 is ready
```

`check` opens a short-lived SSH connection, runs a few probes, and
disconnects. It exits `0` when every check passes and `1` otherwise, so it
composes with scripts. When something is wrong, it says what to do about it:

```
  sudo access     ✗  password required

  Passwordless sudo is required

    SSHerpa needs passwordless sudo for 'admin'.
    Run this on the target host:

        echo 'admin ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ssherpa
```

`--key` and `--port` are optional: the port defaults to `22`, and without a
key `ssh` looks for its usual defaults (`~/.ssh/id_ed25519`, `id_ecdsa`,
`id_rsa`).

### 2. Bring up a cluster

```bash
ssherpa up lab-01
```

On an empty host you pick a distribution with the arrow keys:

```
? Which Kubernetes distribution?
❯ k3s    lightweight, installs in about 20s, runs in 1 GB
  rke2   security-hardened, closer to production, needs 4 GB
```

then each step reports as it completes:

```
  Installing k3s on lab-01

    ✓ preflight                1.4s
    ✓ configure tls-san        0.7s
    ✓ install k3s             17.5s
    ✓ wait for node            1.2s
    ✓ verify certificate       0.8s
    ✓ fetch kubeconfig         0.7s
    ✓ verify api access        5.0s

  Cluster ready
  kubeconfig: ~/.ssherpa/kubeconfig/lab-01.yaml
```

Details worth knowing:

- **One distribution per host.** They all bind port 6443 and share
  `/etc/rancher`, so they cannot coexist — SSHerpa refuses to install a second
  one until the first is removed.
- **Re-running is safe.** If the host already has a cluster, `up` asks for
  confirmation, then skips the install and just refreshes the kubeconfig.
  Useful when a previous run was interrupted.
- **Preflight catches real blockers early.** For example, installing RKE2 on a
  2 GB host fails in seconds with the actual reason (not enough memory) and a
  suggestion, instead of timing out after five minutes with the control plane
  half-up.
- The connect address is written into the distro's config (`tls-san`) before
  install, so the API server certificate is valid for the address you connect
  to — not just the host's internal IP.
- **An address change heals itself.** Cloud hosts lose their external IP on
  stop/start unless one is reserved. Re-run `up` after updating the target:
  it detects that the certificate no longer covers the new address, refreshes
  it in place, and re-fetches the kubeconfig. No reinstall, cluster data kept.

### 3. Use the cluster

The fetched kubeconfig already points at your host's address:

```bash
export KUBECONFIG=~/.ssherpa/kubeconfig/lab-01.yaml    # PowerShell: $env:KUBECONFIG="..."
kubectl get nodes
```

If port 6443 is not reachable (a cloud firewall, for instance), `up` says so
and shows the safe alternative — an SSH tunnel:

```bash
ssh -L 6443:127.0.0.1:6443 admin@10.0.0.10
kubectl --server https://127.0.0.1:6443 get nodes
```

Opening 6443 to the internet would expose the cluster API; if you do open it,
restrict it to your own address.

### 4. Inspect and tear down

```bash
ssherpa status lab-01
```

```
  Status of lab-01

  k3s   ✓  installed  active
  rke2  —  not installed

  node:  lab-01   Ready   control-plane   5m   v1.36.2+k3s1
```

```bash
ssherpa down lab-01
```

`down` detects whatever is installed — there is nothing to choose, since a
host can only run one distribution. It asks for confirmation (destroying a
cluster is not undoable), stops everything, runs the uninstaller, verifies
nothing was left behind, and deletes the local kubeconfig. `--yes` skips the
prompt for scripts.

### 5. Jump onto the host

```bash
ssherpa ssh lab-01
```

Opens a plain interactive SSH session using the target's saved connection
details. Extra arguments are passed straight to `ssh`.

### Commands

| Command | Connects over SSH |
|---|---|
| `ssherpa target add <NAME> --host <IP> --user <USER> [--key <PATH>] [--port <N>]` | no |
| `ssherpa target list` | no |
| `ssherpa target remove <NAME>` | no |
| `ssherpa check <NAME>` (or `--host/--user` for one-off) | yes |
| `ssherpa up <NAME> [--yes]` | yes |
| `ssherpa status <NAME>` | yes |
| `ssherpa down <NAME> [--yes]` | yes |
| `ssherpa ssh <NAME> [ssh args...]` | yes |

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
- **Only connection details are stored.** What is installed and running is
  never written here — it is read from the host every time (`status` does
  exactly this), so the file cannot drift out of sync with reality.

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
networks, over-permissive key files, out-of-memory preflights:

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

The cluster layer (`up`/`down`) is exercised against a real cloud host rather
than containers — k3s and RKE2 need systemd and their own kernel namespaces,
which containers do not provide faithfully.
