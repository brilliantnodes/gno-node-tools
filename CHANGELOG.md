# Changelog

## 0.2.0 (25 November 2026)

An independent review of 0.1 checked the code against gno's own source (`chain/mainnet` branch). It confirmed that the log patterns and the block-range rules match gno, and found the problems below. Each one is fixed, and each runner fix has a test that fails on 0.1.

### Serious
- **The node could freeze silently.** The runner reads the node's output through a pipe. In 0.1, one byte that is not valid UTF-8, a full disk, or a broken stdout ended that reading, the pipe filled up, and the node stopped producing blocks while its process stayed alive. The reader now decodes leniently, survives errors on any single line, and if it ever stops, the runner stops the node cleanly instead of leaving it blocked. Test: 3,000 lines of invalid UTF-8 plus 50 lines per block.
- **Recording an upgrade could record the past halt.** The chain never clears its halt parameters, so between upgrades they still show the previous halt. In 0.1, `install --pending` copied whatever was there, and every later command failed. It now refuses a halt height that is not after both the chain's height and the ledger's last halt, refuses a missing one, and writes nothing unless the result still validates.

### Important
- An upgrade known only from the local pending file now starts only once the chain confirms its halt height (the chain's halt parameter, or a halt the runner saw happen). This catches a wrong halt height typed by hand, which the node's own start-up check does not catch between halts.
- A halt is also detected from the block height staying on a known halt height when no halt line appears in the log (gno's UPGRADES.md: "watch the height, not the log").
- An unreadable ledger at an upgrade no longer crashes the runner into a restart loop; a good copy of the ledger is cached and used if GitHub cannot be reached.
- A scheduled halt with its binary installed but not recorded is now a failure when the halt is within 20,000 blocks (0.1 only warned, and the watch never alerted on it).
- The watch no longer alerts when the reference node is down, or when the whole network has stopped at the same height.
- Files written by the tool are readable by the service user (0.1 left some at mode 0600).
- The ledger is accepted only over https or from a local file; commit hashes, version tags and checksums are checked before use.

### Smaller
- A stop signal during a binary switch no longer leaves a node running.
- Problems a restart cannot fix exit with code 3; the example systemd unit does not restart on it.
- Locks in both the state folder and the node folder.
- A failed GNOROOT checkout is cleaned up, so a retry works.
- `gnoland version` must report exactly the expected tag.
- The default start arguments include `--skip-genesis-sig-verification`, which Onyx and mainnet require.
- The example configs use the replay path gno.land documents for joining from genesis.
- Added: README, operator guide, example configs, systemd units, packaging with a `gno-tools` command, licence.

## 0.1.0 (25 November 2026)

First version, not published.
