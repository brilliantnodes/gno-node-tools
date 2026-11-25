# gno-node-tools

Keep a gno.land validator running through every coordinated upgrade, and get an alert only when something is really wrong.

gno.land mainnet stopped for a coordinated upgrade **5 times** between 14 September and 18 November 2026, and its testnet Onyx **3 times** since it launched on 28 September 2026 (counted from the official upgrade ledgers). At each stop, every validator has to switch to a new program at exactly the right block. Switch too late and your node falls behind; switch too early and the node refuses to start. This tool makes the switch for you, checks the node before an upgrade, and watches it afterwards.

Standard library only. Python 3.8 or newer. MIT licence.

## What the words mean

| Word | In plain terms |
|---|---|
| Validator | A server that helps produce the chain's blocks and signs them with its own key. |
| Coordinated upgrade, halt height | A block number agreed by gno.land's governance (GovDAO). Every node stops right after that block, then restarts on the new release. |
| Binary | The `gnoland` program itself. Each release is a new binary with its own version tag, such as `v1.8.0-rc.1`. |
| Ledger | The official list of every release a chain has run and the block numbers where each one takes over: `misc/deployments/<chain>/upgrades.json` in the gno repository. It also holds each binary's checksum (SHA-256). |
| Pending upgrade | An upgrade GovDAO has voted for but the official ledger does not list yet. You record it once; the tool switches to it at its halt height. |
| GNOROOT | A copy of the gno source code at the release's exact commit. The node reads its standard libraries from it. |
| RPC | The node's local web interface (port 26657), used to read its block height and status. |

## What it does

| Command | What it does |
|---|---|
| `run` | Starts the right binary for the next block, and switches binaries at every halt height. Meant to run under systemd. |
| `install` | Downloads a release's binary, checks it against the ledger's checksum, and checks out GNOROOT at the release commit. With `--pending`, first records an upcoming upgrade. |
| `preflight` | Read-only checks before an upgrade: right chain, binary for the next block ready, scheduled halt covered, no leftover `halt_height` in `config.toml`, genesis checksum, disk, peers, lag. |
| `watch` | One check, run every 5 minutes by a systemd timer. Sends an alert (Telegram, Discord or a webhook) only when a problem is seen 3 checks in a row, and repeats it every 12 hours while it lasts. |
| `plan`, `state`, `genesis` | Show which release serves which blocks; show or set the block height the runner starts from; download and check `genesis.json`. |

## Status: read this before you use it

Version 0.2, 25 November 2026.

- **Tested:** 58 automated tests, on Python 3.8 and 3.13. They run the tool against a simulator of `gnoland` whose log lines, exit codes and start-up checks were copied from gno's source (`chain/mainnet` branch). Each log pattern the tool relies on was checked against the line in gno's source that prints it.
- **Run against the live network:** the read-only commands (`plan`, `preflight`) against the public Onyx node on 25 November 2026.
- **Not yet done:** managing a live node through a real halt. Our first live run is on our own Onyx node, before Onyx's next upgrade. Until then, treat `run` as tested in simulation only, and watch your node at each upgrade.
- **Reviewed:** an independent review of version 0.1 found 2 serious problems and 7 important ones. All are fixed in 0.2, and each fix has a test that fails on 0.1. See [CHANGELOG.md](CHANGELOG.md).

## Quick start (Onyx)

The full procedure, including how to move an existing node over, is in [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md).

A new node also needs its `config.toml` and keys first (`gnoland config init`, `gnoland secrets init`, and the settings in Onyx's [VALIDATOR.md](https://github.com/gnolang/gno/blob/chain/mainnet/misc/deployments/onyx.gno.land/VALIDATOR.md), section 3).

```shell
# 1. A service user that owns the node's folders
sudo /usr/sbin/useradd --system --create-home --home-dir /var/lib/gnoland --shell /usr/sbin/nologin gnoland
sudo /usr/bin/install -d -o gnoland -g gnoland /opt/gno

# 2. Get the code and the example config (readable by the service user only)
sudo /usr/bin/git clone https://github.com/brilliantnodes/gno-node-tools /opt/gno-node-tools
sudo /usr/bin/install -d /etc/gno-node-tools
sudo /usr/bin/install -m 640 -g gnoland /opt/gno-node-tools/examples/onyx.ini /etc/gno-node-tools/onyx.ini

# 3. See which release serves which blocks
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini plan

# 4. Install the releases the plan needs (checksums checked; GNOROOT checked out at each release commit)
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini install --version v1.8.0-rc.1

# 5. Check everything before starting
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini preflight

# 6. Run it under systemd, plus the 5-minute watch
sudo cp /opt/gno-node-tools/examples/systemd/gno-node.service /opt/gno-node-tools/examples/systemd/gno-watch.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gno-node.service gno-watch.timer
```

## Safety rules it follows

- **Never two signers.** Two copies of one validator signing at once is double-signing, which gno.land says it will punish. The runner will not start while any node already answers on the RPC port, and it holds locks in its state folder and in the node's folder, so a second runner cannot start.
- **Only checked binaries.** A binary is used only if its SHA-256 matches the ledger, and it is checked again just before each start. The ledger is loaded only over https or from a local file.
- **Governance decides the halts.** The tool never edits `config.toml` and never sets a halt height. An upgrade recorded only in the local pending file is started only once the chain confirms that halt height.
- **No upgrade depends on GitHub being up.** A good copy of the ledger is kept locally and used if the ledger cannot be downloaded.
- **No restart loops on problems a restart cannot fix.** A missing or wrong binary, an unreadable ledger, or a second node makes the runner exit with code 3. The example systemd unit does not restart on that code. The log says what to do.

## What the watch alerts on, and what it does not

It alerts on: the node's RPC not answering, the service down, the block height stuck for 15 minutes while the network moves on, the node more than 30 blocks behind a reference node, fewer than 3 peers, disk at 90 per cent, a scheduled halt within 20,000 blocks without its binary installed and recorded, a leftover `halt_height` in `config.toml`, and a binary whose checksum no longer matches.

It does not alert on: problems seen fewer than 3 checks in a row; the whole network stopping together (for example at a coordinated halt, when the reference node is stopped at the same height); the reference node being down; a problem clearing (unless you turn that on).

## Requirements

Linux (amd64 or arm64), Python 3.8 or newer, git, and systemd for the service files. `gnoland_linux_amd64` from release `v1.8.0-rc.1` needs glibc 2.34 or newer (gno.land's VALIDATOR.md).

## Sources

- How coordinated upgrades work: [gno.land/cmd/gnoland/UPGRADES.md](https://github.com/gnolang/gno/blob/chain/mainnet/gno.land/cmd/gnoland/UPGRADES.md)
- Onyx ledger and joining notes: [misc/deployments/onyx.gno.land](https://github.com/gnolang/gno/tree/chain/mainnet/misc/deployments/onyx.gno.land)
- Mainnet ledger: [misc/deployments/mainnet.gno.land](https://github.com/gnolang/gno/tree/chain/mainnet/misc/deployments/mainnet.gno.land)

Maintained by Brilliant Nodes. Issues and pull requests are welcome.
