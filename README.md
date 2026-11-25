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

- **Tested:** 70 automated tests, on Python 3.8 and 3.13. They run the tool against a simulator of `gnoland` whose log lines, exit codes and start-up checks were copied from gno's source (`chain/mainnet` branch). Each log pattern the tool relies on was checked against the line in gno's source that prints it.
- **Running on a live node:** since 25 November 2026 it runs our own Onyx node. It installed the release (checksum checked against the ledger), started the node and keeps it at the network's tip.
- **Not yet done:** taking a live node through a real coordinated halt; Onyx's next upgrade will be the first. Until then, the switch at a halt is tested in simulation only, so watch your node at each upgrade.
- **Reviewed:** three independent reviews. The first, of version 0.1, found 2 serious problems and 7 important ones; the second and third, of the fixes, found further edge cases. All are fixed in 0.2, and most fixes have a test that fails without them. See [CHANGELOG.md](CHANGELOG.md).

## Quick start (Onyx)

Moving a node that already runs? Follow [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md), section 3, instead. The steps below set up a new node that syncs from the first block.

```shell
# 1. A service user that owns the node's folders
sudo /usr/sbin/useradd --system --create-home --home-dir /var/lib/gnoland --shell /usr/sbin/nologin gnoland
sudo /usr/bin/install -d -o gnoland -g gnoland /opt/gno

# 2. The code, and the example config (owner root, readable by the gnoland group only)
sudo /usr/bin/git clone https://github.com/brilliantnodes/gno-node-tools /opt/gno-node-tools
sudo /usr/bin/install -d /etc/gno-node-tools
sudo /usr/bin/install -m 640 -o root -g gnoland /opt/gno-node-tools/examples/onyx.ini /etc/gno-node-tools/onyx.ini

# 3. See which release serves which blocks
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini plan

# 4. Install the two releases the example plan uses, and the genesis file (each checked against the ledger)
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini install --version v1.7.1-rc.1
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini install --version v1.8.0-rc.1
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini genesis

# 5. The node's own config and keys, made with the installed release, in the node's folder.
#    The settings are those of Onyx's VALIDATOR.md, section 3, as read on 25 November 2026: check them there.
#    Replace the two values in <angle brackets> with your own.
sudo -u gnoland /bin/sh -c '
  set -e
  cd /var/lib/gnoland
  export GNOROOT=/opt/gno/versions/v1.8.0-rc.1/gno
  G=/opt/gno/versions/v1.8.0-rc.1/gnoland
  $G config init
  $G secrets init
  $G config set p2p.persistent_peers "g1x5mlj5ava0dw9vkf4j6admjlzswm6f06p44krn@seed-1.onyx.testnets.gno.land:26656,g1grq5zswt0dlwwe7clr4359w70k2ewgse0gcwck@seed-2.onyx.testnets.gno.land:26656"
  $G config set application.prune_strategy syncable
  $G config set consensus.timeout_commit 3s
  $G config set consensus.peer_gossip_sleep_duration 10ms
  $G config set p2p.flush_throttle_timeout 10ms
  $G config set p2p.pex true
  $G config set moniker "<your node name>"
  $G config set p2p.external_address "<your public IP>:26656"
'

# 6. Check before the first start. Until the node runs, the "rpc" line fails (nothing answers yet):
#    that one is expected; every other line should read OK.
sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini preflight

# 7. Run it under systemd, plus the 5-minute watch
sudo /usr/bin/cp /opt/gno-node-tools/examples/systemd/gno-node.service /opt/gno-node-tools/examples/systemd/gno-watch.service /opt/gno-node-tools/examples/systemd/gno-watch.timer /etc/systemd/system/
sudo /usr/bin/systemctl daemon-reload
sudo /usr/bin/systemctl enable --now gno-node.service gno-watch.timer
```

## Safety rules it follows

- **Never two signers.** Two copies of one validator signing at once is double-signing, which gno.land says it will punish. The runner will not start while any node already answers on the RPC port, and it holds locks in its state folder and in the node's folder, so a second runner cannot start.
- **Only checked binaries.** A binary is used only if its SHA-256 matches the ledger, and it is checked again just before each start. The ledger is loaded only over https or from a local file.
- **Governance decides the halts.** The tool never edits `config.toml` and never sets a halt height. An upgrade recorded only in the local pending file is started only once the chain confirms that halt height (its halt parameter, or a halt the runner saw), or once you state with `--past-halt` that it already happened.
- **No upgrade depends on GitHub being up.** A good copy of the ledger is kept locally and used if the ledger cannot be downloaded.
- **No restart loops on problems a restart cannot fix.** A missing or wrong binary, an unreadable ledger, or a second node makes the runner exit with code 3. The example systemd unit does not restart on that code. The log says what to do.

## What the watch alerts on, and what it does not

It alerts on: the node's RPC not answering, the service down, the block height stuck for 15 minutes while the network moves on, the node more than 30 blocks behind a reference node, fewer than 3 peers, disk at 90 per cent, a scheduled halt within 20,000 blocks without its binary installed and recorded, a leftover `halt_height` in `config.toml`, and a binary whose checksum no longer matches.

It does not alert on: problems seen fewer than 3 checks in a row; the whole network stopping together, for example at a coordinated halt (this needs `watch.reference_rpc` set: the watch sees that the reference node is stopped at the same height); the reference node being down; a problem clearing (unless you turn that on). Without a reference node, a coordinated halt that lasts 15 minutes is alerted as a stuck height.

## Requirements

Linux (amd64 or arm64), Python 3.8 or newer, git, and systemd for the service files. `gnoland_linux_amd64` from release `v1.8.0-rc.1` needs glibc 2.34 or newer (gno.land's VALIDATOR.md).

## Sources

- How coordinated upgrades work: [gno.land/cmd/gnoland/UPGRADES.md](https://github.com/gnolang/gno/blob/chain/mainnet/gno.land/cmd/gnoland/UPGRADES.md)
- Onyx ledger and joining notes: [misc/deployments/onyx.gno.land](https://github.com/gnolang/gno/tree/chain/mainnet/misc/deployments/onyx.gno.land)
- Mainnet ledger: [misc/deployments/mainnet.gno.land](https://github.com/gnolang/gno/tree/chain/mainnet/misc/deployments/mainnet.gno.land)

Maintained by Brilliant Nodes. Issues and pull requests are welcome.
