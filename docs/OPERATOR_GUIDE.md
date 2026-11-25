# Operator guide

This guide covers setting up gno-node-tools, moving an existing node over to it, and what to do at each coordinated upgrade. Commands use full paths and assume the layout of the example files: code in `/opt/gno-node-tools`, config in `/etc/gno-node-tools/onyx.ini`, a service user `gnoland`.

To keep the commands short, the guide uses this alias. Type it once per shell session:

```shell
alias gt='sudo -u gnoland /usr/bin/env PYTHONPATH=/opt/gno-node-tools /usr/bin/python3 -m gnotools --config /etc/gno-node-tools/onyx.ini'
```

## 1. Where things live

| Path | What it is |
|---|---|
| `/opt/gno/versions/<tag>/gnoland` | One binary per release, used only after its SHA-256 matched the ledger. |
| `/opt/gno/versions/<tag>/gno/` | GNOROOT for that release: the gno source at the release commit. |
| `/opt/gno/versions/<tag>/INSTALLED.json` | What was installed: checksum, commit, download link, time. |
| `/opt/gno/state/runner.json` | The block height the runner starts from, the binary it last started, the last halt it saw, and the chain's halt parameter as last read. |
| `/opt/gno/state/watch.json` | The watch's memory of open problems. |
| `/opt/gno/state/ledger-cache.json` | The last good copy of the official ledger. |
| `/opt/gno/pending.json` | Upgrades voted on chain but not yet in the official ledger. |
| `/var/lib/gnoland/gnoland-data/` | The node's own folder: `config/config.toml`, `secrets/`, `db/`. The tool never edits it. |

## 2. Settings

Every setting has a default (`gnotools/config.py`); the example `examples/onyx.ini` sets the ones that matter.

| Section, key | Meaning | Default |
|---|---|---|
| `node.chain_id` | `onyx-1` or `gnoland-1`. Checked against the ledger and the node. | none |
| `node.ledger` | The official ledger: an https link or a local file. | none |
| `node.pending` | Local file for upcoming upgrades. | none |
| `node.root` | Where binaries and state go. | `/opt/gno` |
| `node.home` | The node's working folder (holds `gnoland-data/`). | `/var/lib/gnoland` |
| `node.genesis`, `node.genesis_url` | Path of `genesis.json`, and where to download it. Checked against the ledger's checksum. | none |
| `node.rpc` | The node's own RPC. | `http://127.0.0.1:26657` |
| `node.start_args` | Arguments after `gnoland start`. `{chain_id}` and `{genesis}` are filled in. | `--chainid {chain_id} --genesis {genesis} --skip-genesis-sig-verification` |
| `node.plan` | Which release replays which blocks when joining from genesis, if it differs from the ledger's own rule. | none |
| `runner.halt_grace_seconds` | After the node logs a halt, how long to wait before stopping it. | 20 |
| `runner.halt_stall_seconds` | With no halt line in the log, how long the height must stay on a known halt height before the runner treats it as halted. | 60 |
| `runner.stop_timeout_seconds` | How long to wait for the node to stop before killing it. | 90 |
| `watch.reference_rpc` | A node you do not run, used to tell "my node is behind" from "the whole network stopped". | none |
| `watch.service` | The systemd unit to check, e.g. `gno-node`. | none |
| `watch.confirm_checks` | How many checks in a row a problem must be seen before an alert. | 3 |
| `watch.stall_minutes` | Height unchanged this long counts as stuck. | 15 |
| `watch.max_lag_blocks`, `watch.min_peers`, `watch.disk_percent` | Limits for lag, peers and disk use. | 30, 3, 90 |
| `watch.halt_warn_blocks` | A scheduled halt this close without its binary ready is a failure. | 20000 |
| `watch.repeat_hours` | How often an open problem is repeated. | 12 |
| `notify.*` | Telegram bot token and chat id, a Discord webhook, or any webhook. If you set any, `chmod 640` the file and keep it owned by root, group `gnoland`. | none |

## 3. Moving an existing node to the runner

Do this between upgrades, not close to a halt. Your validator stops signing for a minute or two while you switch.

1. Read the node's height and version:
   ```shell
   /usr/bin/curl -s http://127.0.0.1:26657/status | /usr/bin/grep -E 'latest_block_height|build_version'
   ```
2. Point `node.home` at the folder that holds the node's `gnoland-data/`, and `node.genesis` at its `genesis.json`. If the node ran as another user, give the folder to `gnoland`: `sudo /usr/bin/chown -R gnoland:gnoland /var/lib/gnoland`.
3. Install the release that serves the next block (replace `1405000` with the height from step 1):
   ```shell
   gt install --for-height 1405000
   ```
4. While the old node is still running, record its height for the runner, then run the checks:
   ```shell
   gt state --from-rpc
   gt preflight
   ```
   Everything should read OK, except that `running-version` may warn if the old node runs a different build.
5. Stop the old service and make sure nothing answers on the RPC port any more (the runner refuses to start otherwise):
   ```shell
   sudo /usr/bin/systemctl disable --now <old-unit>
   /usr/bin/curl -s -m 3 http://127.0.0.1:26657/status || echo "port is free"
   ```
6. Start the runner and the watch, and follow the log until blocks are being made again:
   ```shell
   sudo /usr/bin/systemctl enable --now gno-node.service gno-watch.timer
   sudo /usr/bin/journalctl -u gno-node -f
   ```
7. To go back: `sudo /usr/bin/systemctl disable --now gno-node.service`, then `sudo /usr/bin/systemctl enable --now <old-unit>`.

## 4. At each coordinated upgrade

1. **The vote passes.** Once the GovDAO proposal has executed, the chain shows the new halt height and minimum version. `gt preflight` then reports it: FAIL when the halt is within 20,000 blocks and the binary is not ready, WARN when it is further away. The watch alerts on the FAIL.
2. **Record and install the new release.** Take the binary link and checksum from the release page (`CHECKSUMS.txt`) and the release commit from its tag. The halt height and minimum version are read from the chain:
   ```shell
   gt install --pending --version v1.9.0-rc.1 \
     --url https://github.com/gnolang/gno/releases/download/v1.9.0-rc.1/gnoland_linux_amd64 \
     --sha256 <64 hex characters from CHECKSUMS.txt> \
     --commit <40 hex characters: the tag's commit>
   ```
   (`v1.9.0-rc.1` is an example.) If your own node's RPC is down, add `--rpc https://rpc.onyx.testnets.gno.land`, or give `--halt-height` and `--min-version` yourself. The command refuses a halt height that is already behind the chain.
3. **Check:** `gt preflight` should now say the binary for after the halt is ready.
4. **At the halt** the node stops after the halt block. The runner stops the process if it is still up, starts the new release, and logs each step (`journalctl -u gno-node | grep gno-runner`). Nothing to do unless the watch alerts.
5. Once gno.land adds the release to the official ledger, the ledger entry replaces your pending one automatically.

A halt with no minimum version is a coordinated pause, not an upgrade: the runner restarts the same release after it.

## 5. Joining from genesis

The example configs carry the replay path gno.land documents: on Onyx, `v1.7.1-rc.1` replays everything up to block 1,203,400, then `v1.8.0-rc.1`; on mainnet, `v1.7.1` up to block 1,651,500, then `v1.8.0`. Install the releases in the plan, run `gt genesis`, and start the service on an empty data folder. During the replay the node stops by itself at each past halt (it exits with code 2), and the runner restarts it on the right release.

## 6. Exit codes and log messages

The runner exits with code **3** for problems a restart cannot fix; the example unit does not restart on it. Other exit codes are left to systemd's `Restart=on-failure`.

| Runner log says | Meaning | What to do |
|---|---|---|
| `block N needs one of ... none is installed` | The release for the next block is missing or its checksum no longer matches. | `gt install --for-height <N-1>` |
| `... comes only from the pending file and the chain does not confirm halt H` | The halt height you recorded does not match the chain. | `gt preflight` shows the chain's halt; record it again with `gt install --pending`. |
| `is below the governance minimum` | The chain passed a halt this runner does not know about. | Record and install that release (section 4, step 2). |
| `wrong AppHash` | That release cannot process these blocks. | Check `node.plan` and `gt plan`. |
| `a node already answers on ...` | Another node is running. | Stop it first. Never run two copies of one validator. |
| `another runner holds the lock` | A second runner was started. | Leave the first one running. |
| `the node already has chain data but the runner has no record of its height` | First start on an existing node. | `gt state --from-rpc` while the old node runs, or `gt state --set-height N`. |
| `cannot read the ledger` | Neither the ledger nor its cached copy could be read. | Check `node.ledger` and the network. |
| `no halt line seen, but the height has stayed at the known halt height` | The halt was detected from the block height instead of the log. | Nothing; it is logged so you know. |

## 7. Keys, backups and double-signing

- Back up `gnoland-data/secrets/priv_validator_key.json` and `node_key.json` offline. Never start a second node with the same validator key while the first could still be running: that is double-signing.
- `secrets/priv_validator_state.json` records what the validator last signed. Keep it with the key, on persistent storage. A node started without it has lost that record.
- To move a validator to another server: stop it on the old one and confirm it is stopped, then start it on the new one with the same `priv_validator_state.json`.

## 8. What the tool does not do

- **Upgrades that rebuild the state from a new genesis file** (`gnogenesis fork`): the runner only switches binaries on the same data folder. For those, follow gno.land's instructions for that upgrade.
- **Hotfixes** outside governance, sentry-node layouts, and remote signing (TMKMS): out of scope; see gno.land's `gnoland` README.
- **Editing `config.toml`**, or setting or clearing a halt height: never.
