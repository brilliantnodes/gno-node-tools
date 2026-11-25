"""Read-only checks shared by `preflight` and `watch`.

Each check returns Result(level, key, text): level is "ok", "warn" or "fail".
Nothing on the node is changed; the only file written is the ledger cache in the state directory.
"""

import datetime
import os
import re
import shutil
from collections import namedtuple

from . import rpc
from .install import installed_versions, sha256_file
from .config import ConfigError
from .ledger import LedgerError, choose_version, meets_min_version, parse_plan
from .runner import load_state

Result = namedtuple("Result", "level key text")


def _toml_top_int(path, key):
    """Read an integer key from the top (section-less) part of config.toml. None if absent."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("["):
                    return None
                m = re.match(r'^%s\s*=\s*"?(-?\d+)"?\s*(#.*)?$' % re.escape(key), s)
                if m:
                    return int(m.group(1))
    except OSError:
        return None
    return None


def _parse_time(t):
    # RFC 3339 with nanoseconds, e.g. 2026-11-25T10:35:35.219216029Z
    t = t.rstrip("Z")
    if "." in t:
        head, frac = t.split(".", 1)
        t = head + "." + frac[:6]
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    return datetime.datetime.strptime(t, fmt)


def seconds_per_block(base, height, span=1000):
    try:
        a = _parse_time(rpc.block_time(base, max(1, height - span)))
        b = _parse_time(rpc.block_time(base, height))
        n = min(span, height - 1)
        return (b - a).total_seconds() / n if n > 0 else None
    except (rpc.RPCError, ValueError):
        return None


def _eta(blocks, spb):
    if spb is None:
        return ""
    secs = blocks * spb
    if secs < 3600:
        return " (about %d min)" % max(1, round(secs / 60))
    if secs < 72 * 3600:
        return " (about %.0f h)" % (secs / 3600)
    return " (about %.0f days)" % (secs / 86400)


def run_checks(cfg, local_status=None):
    """Return a list of Result. Never changes anything."""
    out = []
    add = lambda level, key, text: out.append(Result(level, key, text))
    base = cfg.get("node", "rpc")
    ref = cfg.get("watch", "reference_rpc")

    # Ledger
    try:
        ledger = cfg.load_ledger()
        plan = parse_plan(cfg.get("node", "plan"))
    except (OSError, ValueError, LedgerError, ConfigError) as e:
        add("fail", "ledger", "cannot read the ledger: %s" % e)
        return out
    want_chain = cfg.get("node", "chain_id")
    if want_chain and ledger.chain_id != want_chain:
        add("fail", "ledger", "ledger is for %s, config says %s" % (ledger.chain_id, want_chain))
    else:
        add("warn" if ledger.from_cache else "ok", "ledger", "ledger for %s: %d entries, %d ranges%s" % (
            ledger.chain_id, len(ledger.entries), len(ledger.eras),
            " (from the local cache: the ledger URL could not be read)" if ledger.from_cache else ""))

    # Local node
    st = local_status
    if st is None:
        try:
            st = rpc.status(base)
        except rpc.RPCError as e:
            st = None
            add("fail", "rpc", "local node RPC not answering (%s)" % e)
    if st:
        if want_chain and st["network"] and st["network"] != want_chain:
            add("fail", "chain", "node is on %s, expected %s" % (st["network"], want_chain))
        add("ok", "node", "height %d, catching up %s, binary %s" % (st["height"], st["catching_up"], st["build_version"] or "?"))
        try:
            n = rpc.peers(base)
            lvl = "warn" if n is not None and n < cfg.getint("watch", "min_peers") else "ok"
            add(lvl, "peers", "%s peers" % n)
        except rpc.RPCError:
            add("warn", "peers", "net_info not answering")

    height = st["height"] if st else load_state(cfg).get("height")

    # Installed binaries
    inst = installed_versions(cfg)
    good = {v for v, i in inst.items() if not i.get("broken")}
    for v, i in sorted(inst.items()):
        if i.get("broken"):
            add("fail", "binary:" + v, "%s: %s" % (v, i["broken"]))
    add("ok" if good else "fail", "binaries", "installed and verified: %s" % (", ".join(sorted(good)) or "none"))

    if height is not None:
        v, why = choose_version(ledger, height + 1, good, plan)
        if v is None:
            add("fail", "next-binary", why)
        else:
            add("ok", "next-binary", why)
            if st and st["build_version"] and st["build_version"] != v and not st["catching_up"]:
                add("warn", "running-version", "node runs %s, the ledger rule picks %s for the next block" % (st["build_version"], v))

    # Scheduled governance halt
    src = base if st else (ref or None)
    if src:
        try:
            hh, floor = rpc.scheduled_halt(src)
            if hh and height is not None and hh > height:
                spb = seconds_per_block(src, height) if height > 1 else None
                blocks = hh - height
                after = [x for x in good if meets_min_version(x, floor)] if floor else []
                recorded = any(e.get("halt_height") == hh for e in ledger.entries)
                eta = _eta(blocks, spb)
                if floor and not after:
                    lvl = "fail" if blocks <= cfg.getint("watch", "halt_warn_blocks") else "warn"
                    add(lvl, "halt", "governance halt at %d in %d blocks%s needs %s or newer: not installed" % (hh, blocks, eta, floor))
                elif floor and not recorded:
                    # The binary is there but the runner does not know this halt: at the halt it would
                    # restart the old binary, which the node then refuses. Fail when the halt is close.
                    lvl = "fail" if blocks <= cfg.getint("watch", "halt_warn_blocks") else "warn"
                    add(lvl, "halt", "governance halt at %d in %d blocks%s: %s installed but not recorded; "
                        "run `gno-tools install --pending` so the runner switches to it" % (hh, blocks, eta, ", ".join(sorted(after))))
                else:
                    add("ok", "halt", "governance halt at %d in %d blocks%s; binary for after it is ready (%s)" % (
                        hh, blocks, eta, ", ".join(sorted(after)) or "no version floor: same binary resumes"))
            elif hh:
                add("ok", "halt", "last governance halt was at %d (floor %s); none scheduled ahead" % (hh, floor or "none"))
            else:
                add("ok", "halt", "no governance halt scheduled")
        except rpc.RPCError as e:
            add("warn", "halt", "could not read the halt params (%s)" % e)

    # config.toml traps described in gno.land/cmd/gnoland/UPGRADES.md
    cfgfile = os.path.join(cfg.home, "gnoland-data", "config", "config.toml")
    if os.path.isfile(cfgfile):
        hcfg = _toml_top_int(cfgfile, "halt_height")
        if hcfg:
            add("fail", "config-halt", "config.toml sets halt_height = %d; with governance halts it must be 0" % hcfg)
        else:
            add("ok", "config-halt", "config.toml halt_height is 0")
        skip = _toml_top_int(cfgfile, "skip_upgrade_height")
        if skip and height is not None and height > skip:
            add("warn", "config-skip", "skip_upgrade_height = %d is behind the node; set it back to 0" % skip)
    else:
        add("warn", "config", "no config.toml at %s" % cfgfile)

    # Genesis
    g = cfg.get("node", "genesis")
    if g and os.path.isfile(g):
        ok = sha256_file(g) == ledger.genesis_sha256
        add("ok" if ok else "fail", "genesis", "genesis.json %s the ledger hash" % ("matches" if ok else "does NOT match"))
    elif g:
        add("fail", "genesis", "genesis.json missing at %s" % g)

    # Disk
    path = cfg.get("watch", "disk_path") or cfg.home
    if os.path.exists(path):
        du = shutil.disk_usage(path)
        pct = round(100 * du.used / du.total)
        lvl = "fail" if pct >= cfg.getint("watch", "disk_percent") else "ok"
        add(lvl, "disk", "disk %d%% used, %.0f GB free" % (pct, du.free / 1e9))

    # Reference node
    if ref and st:
        try:
            r = rpc.status(ref)
            lag = r["height"] - st["height"]
            lvl = "warn" if lag > cfg.getint("watch", "max_lag_blocks") else "ok"
            add(lvl, "lag", "%d blocks behind the reference node (%s)" % (max(lag, 0), r["build_version"] or "?"))
        except rpc.RPCError as e:
            # Someone else's outage: reported, but under its own key so the watch never pages on it.
            add("warn", "reference", "reference RPC not answering (%s)" % e)
    return out
