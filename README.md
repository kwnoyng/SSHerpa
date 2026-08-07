# SSHerpa

[![test](https://github.com/kwnoyng/SSHerpa/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/kwnoyng/SSHerpa/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/ssherpa)](https://pypi.org/project/ssherpa/)

Build Kubernetes labs on a single on-premises server, over nothing but SSH.

> **v0.5** — register a host, check that it is ready, and stand up a
> Kubernetes cluster on it: directly on the host (k3s or RKE2), or spread
> over VMs on it (`up --vm --nodes 3`) — wired into your `~/.kube/config`.
> One server, a real multi-node cluster.

## Requirements

| | |
|---|---|
| Your machine | Python 3.9+, an OpenSSH client |
| Target host | Ubuntu 22.04 / 24.04, Rocky 9, or AlmaLinux 9 |
| Access | SSH key-based login and passwordless `sudo` |
| Memory | ~1 GB for k3s, ~4 GB for RKE2 |
| VM mode | hardware virtualization on the host — `ssherpa doctor` tells you |

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

A cloud host usually gets a new address every time it is restarted. Change
the one thing that moved, rather than removing and re-adding the target —
that would also drop the key and port you had set:

```bash
ssherpa target update lab-01 --host 10.0.0.11
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

Everything except `--host` is optional, and **whatever you leave out is
resolved by `ssh` itself** — default keys (`~/.ssh/id_ed25519` …), and any
`User`, `Port`, `IdentityFile`, or `ProxyJump` from your `~/.ssh/config`.
SSHerpa never invents values it wasn't given, so a config entry like this
works as-is, bastion hop included:

```bash
# with a `Host lab` block in ~/.ssh/config:
ssherpa target add lab-01 --host lab
ssherpa check lab-01
```

Planning to run the cluster inside a VM? `doctor` diagnoses whether the
host can create VMs at all — CPU virtualization, `/dev/kvm`, memory, disk.
The verdict rests only on those capabilities; environment detection is used
to word the fix, since the same failure is cured in the BIOS on a physical
machine, by a machine-series change on GCP, and by an admin setting on
vSphere or Hyper-V:

```
  Host type:  virtual machine (google — Google Compute Engine)

  CPU virtualization  ✓  vmx (Intel VT-x)
  /dev/kvm            ✓  present
  libvirt             —  not installed (vm setup will install it)
  Memory              ✓  15.6 GB — fits ~7 × 2 GB VMs
  Disk                ✓  34 GB free

  lab-01 can run VM-backed clusters
```

A host that fails here can still run host-mode clusters — the verdict says
which way to go.

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
    ✓ update ~/.kube/config    0.0s
    ✓ verify api access        5.0s

  Cluster ready
  kubeconfig: ~/.ssherpa/kubeconfig/lab-01.yaml

  Added to ~/.kube/config as context ssherpa-lab-01 (now the default).
```

Details worth knowing:

- **One distribution per host.** They all bind port 6443 and share
  `/etc/rancher`, so they cannot coexist — SSHerpa refuses to install a second
  one until the first is removed.
- **Re-running is safe.** If the host already has a cluster, `up` asks for
  confirmation, then skips the install and just refreshes the kubeconfig.
  Useful when a previous run was interrupted.
- **Scripts pass `--distro`.** Without a terminal there is no prompt: an
  explicit `--distro k3s|rke2` is used as given, and an unspecified run
  defaults to k3s — saying so in the output, since silent decisions make
  logs a mystery.
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

#### Or run it inside a VM: `--vm`

```bash
ssherpa up lab-01 --vm
```

The cluster lands in a VM on the host instead of on the host itself: the
host stays clean, and removing the cluster is just deleting the VM. One
command covers the whole distance — QEMU/libvirt are installed if missing,
an Ubuntu cloud image is fetched (once per host) and booted with a
cloud-init identity, the API port is forwarded host → VM, and k3s goes in:

```
  Installing k3s on a VM on lab-01

    ✓ preflight                0.7s      ← can this host run VMs at all?
    ✓ enable libvirtd          0.9s
    ✓ start default network    0.7s
    ✓ verify                   0.8s
    ✓ fetch base image        14.0s      ← cached after the first run
    ✓ create disk              0.7s
    ✓ write cloud-init seed    0.8s
    ✓ boot vm                  2.2s
    ✓ wait for ip             19.9s
    ✓ expose api port          0.9s
    ✓ configure tls-san        1.1s
    ✓ install k3s             16.6s
    ✓ wait for node            1.7s
    ✓ verify certificate       1.7s
    ✓ fetch kubeconfig         1.4s
    ✓ update ~/.kube/config    0.0s
    ✓ verify api access        0.5s

  Cluster ready
```

Worth knowing:

- **The VM's specs are fixed on purpose** — Ubuntu 24.04, 2 CPUs, 2 GB,
  a 10 GB thin disk, k3s inside. The host's OS is whatever you were given;
  the VM's OS is a part SSHerpa manufactures, so there is exactly one
  combination to support and it is actually tested. Remaining knobs (memory,
  disk) will open as needs prove themselves.
- **kubectl connects through the host's address.** The VM lives on a NAT
  network only the host can see, so the host forwards port 6443 inward —
  the same trick as port-forwarding on a home router.
- **A host reboot is survivable, unattended.** Forwarding rules live in
  kernel memory, so a restart would otherwise leave the VM running and
  healthy with no way in from outside — the worst kind of failure, since
  everything looks fine from the host. SSHerpa installs a small systemd
  unit that re-applies its own rules at boot (not the distro's
  save-everything mechanism, which would also resurrect rules an
  administrator deliberately removed), and pins the VM's address in
  libvirt's DHCP so those rules still point at the right place.
- **Host mode and VM mode cannot share a host** — both need port 6443.
  `up` refuses either direction and says what to remove.
- **Re-running is safe**, as always: the VM and the cluster inside it are
  detected and reused.
- `down` needs no flag or memory of what you did — it asks the host,
  finds the VM, and removes it, cluster and all, plus the forwarding rule
  and your local kubeconfig entries.
- VMs are standard libvirt guests named `ssherpa-*`. SSHerpa never touches
  a VM it did not create, and `virsh` can inspect everything it made.

#### More than one node: `--nodes`

```bash
ssherpa up lab-01 --vm --nodes 3
```

A node is a machine, and a cluster is a group of them. One server therefore
gives you a one-node cluster and nothing else — unless you make more machines
on it, which is what this does: three VMs, one cluster, on hardware you have.

The first VM becomes the control plane, the rest join it as workers, and the
scheduler spreads work across all three for real — separate kernels, separate
networks, not containers pretending to be nodes:

```
NAME             STATUS   ROLES           AGE   VERSION
ssherpa-node-1   Ready    control-plane   63s   v1.36.3+k3s1
ssherpa-node-2   Ready    <none>          45s   v1.36.3+k3s1
ssherpa-node-3   Ready    <none>          26s   v1.36.3+k3s1
```

`--nodes` is refused before anything is built if the host cannot hold that
many — half-built clusters leave you with VMs to clean up and no explanation.
The limit is the same estimate `doctor` prints, so the advice and the outcome
agree:

```
  this host fits about 7 VM(s), but 20 were asked for

    Each node takes 2 GB, and the host keeps some for itself.
    Try --nodes 7, or add memory to the host.
    See the estimate:  ssherpa doctor lab-01
```

Re-running is safe here too: existing nodes are detected and left alone, and
only missing ones are built and joined. `down` removes the whole cluster —
every VM, the forwarding rule, and your local kubeconfig entries — without
being told how many there were.

### 3. Use the cluster

`up` merges the cluster's credentials into `~/.kube/config` — the file
kubectl reads by default — so **kubectl works from any terminal with no
setup**:

```bash
kubectl get nodes
```

The entry is a context named `ssherpa-<target>`. If your kubeconfig already
had clusters in it, your current context is left untouched (stealing the
default from someone working against a production cluster is not our place) —
address ours by name instead:

```bash
kubectl --context ssherpa-lab-01 get nodes
```

`down` removes exactly these entries again. A standalone copy also lives at
`~/.ssherpa/kubeconfig/<target>.yaml` for anything that wants an isolated
file. Both it and the backup SSHerpa keeps of your original
(`~/.kube/config.ssherpa-backup`, written once, before the first merge) are
created readable only by you — they hold cluster credentials.

If port 6443 is not reachable (a cloud firewall, for instance), `up` says so
and shows the safe alternative — an SSH tunnel:

```bash
ssh -L 6443:127.0.0.1:6443 admin@10.0.0.10
kubectl --server https://127.0.0.1:6443 get nodes
```

Opening 6443 to the internet would expose the cluster API; if you do open it,
restrict it to your own address.

In VM mode, note which firewall governs that port. The rule forwarding
6443 into the VM has to sit ahead of libvirt's own reject rule, which puts
it ahead of `ufw` and `firewalld` too — and forwarded packets never traverse
`INPUT`, so a rule blocking 6443 there does not close this path. Firewalls
upstream of the host — cloud or network — are unaffected and still apply.
`ssherpa down` removes the forwarding rule along with the VMs.

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

It asks the host rather than remembering, so a VM cluster is reported as one —
saying "not installed" while three nodes hum along inside would be a lie the
host itself could have corrected:

```
  Status of lab-01

  k3s   —  not installed on the host
  rke2  —  not installed on the host

  vm:  ssherpa-node-1  running
       ssherpa-node-2  running
       ssherpa-node-3  running

  cluster:  k3s in the VMs  3/3 nodes Ready
```

```bash
ssherpa down lab-01
```

`down` detects whatever is installed — there is nothing to choose, since a
host can only run one distribution. It asks for confirmation (destroying a
cluster is not undoable), stops everything, runs the uninstaller, verifies
nothing was left behind, and cleans up locally too: the standalone kubeconfig
is deleted and the `ssherpa-<target>` entries are removed from
`~/.kube/config`.

**Scripts must pass `--yes`.** Where there is no terminal to ask on — CI, a
pipe, a redirect — SSHerpa refuses to destroy anything rather than read
"could not ask" as consent, and exits 1 saying so. The same holds when a
terminal exists but the prompt cannot be drawn. `up` is the opposite:
re-running it changes nothing that cannot be changed back, so it goes ahead
unattended. A host with nothing on it needs no `--yes` either; `down` says so
and exits 0, which keeps clean-up scripts simple.

`down` also sweeps a forwarding rule left behind by VMs that were removed by
hand, so a host cleaned up with `virsh` does not keep re-installing a route to
an address that no longer exists.

### 5. Jump onto the host — or into the VM

```bash
ssherpa ssh lab-01          # the host
ssherpa ssh lab-01 --vm     # the VM on it
```

Opens a plain interactive SSH session using the target's saved connection
details. With `--vm` the session lands inside the SSHerpa VM: its current
address is looked up on the spot (so it is right even after the VM got a
new one), and the dedicated key plus the hop through the host are filled
in — things you would otherwise have to dig out of `virsh` by hand.
Extra arguments are passed straight to `ssh`.

### Commands

| Command | Connects over SSH |
|---|---|
| `ssherpa target add <NAME> --host <IP\|alias> [--user <USER>] [--key <PATH>] [--port <N>]` | no |
| `ssherpa target update <NAME> [--host ...] [--user ...] [--key ...] [--port ...] [--unset <FIELD>]` | no |
| `ssherpa target list` | no |
| `ssherpa target remove <NAME>` | no |
| `ssherpa check <NAME>` (or `--host` for one-off) | yes |
| `ssherpa doctor <NAME>` (or `--host` for one-off) | yes |
| `ssherpa up <NAME> [--distro k3s\|rke2] [--vm] [--nodes N] [--yes]` | yes |
| `ssherpa status <NAME>` | yes |
| `ssherpa down <NAME> [--yes]` | yes |
| `ssherpa ssh <NAME> [--vm] [ssh args...]` | yes |

## The inventory

Targets live in `~/.ssherpa/inventory.yml`, written as a plain Ansible
inventory:

```yaml
all:
  hosts:
    lab-01:
      ansible_host: 10.0.0.10
      ansible_user: admin
      ansible_ssh_private_key_file: ~/.ssh/id_ed25519
```

Three decisions worth knowing:

- **The format is Ansible's on purpose.** Later stages drive Ansible roles, and
  this file will be reused as-is rather than converted.
- **Only what you gave is stored.** Fields you omitted are not filled with
  defaults — at connect time they fall through to `~/.ssh/config`, exactly
  like plain `ssh` would.
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
