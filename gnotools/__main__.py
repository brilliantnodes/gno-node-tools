"""Command line: python3 -m gnotools --config node.ini <command>   (or `gno-tools` once installed)

  plan       show which version serves which blocks, and what is installed
  install    download and verify binaries (and GNOROOT) from the ledger;
             with --pending, first record an upgrade the official ledger does not list yet
  genesis    download genesis.json and check it against the ledger hash
  state      show or set the height the runner starts from
  preflight  read-only checks before an upgrade (exit code 1 if anything fails)
  watch      one watch check; alerts only on confirmed problems
  run        start the node and switch binaries at every halt (systemd ExecStart)
"""

import argparse
import json
import sys

from . import __version__, rpc
from .checks import run_checks
from .config import Config, ConfigError
from .install import (InstallError, add_pending, install_genesis, install_version,
                      installed_versions)
from .ledger import LedgerError, choose_version, parse_plan
from .runner import Runner, load_state, save_state, set_height
from .watch import check_once


def _ledger(cfg):
    return cfg.load_ledger(log=print)


def cmd_plan(cfg, a):
    ledger = _ledger(cfg)
    inst = installed_versions(cfg)
    print("chain %s, platform %s" % (ledger.chain_id, cfg.platform))
    for era in ledger.eras:
        end = era.end if era.end is not None else "tip"
        vs = []
        for v in era.versions:
            mark = "installed" if v in inst and not inst[v].get("broken") else ("BROKEN" if v in inst else "missing")
            pend = " (pending)" if (ledger.entry(v) or {}).get("pending") else ""
            vs.append("%s [%s]%s" % (v, mark, pend))
        print("  blocks %s..%s: %s" % (era.start, end, ", ".join(vs)))
    h = load_state(cfg).get("height")
    if h is not None:
        good = {v for v, i in inst.items() if not i.get("broken")}
        v, why = choose_version(ledger, h + 1, good, parse_plan(cfg.get("node", "plan")))
        print("runner height %d -> %s" % (h, why))
    return 0


def cmd_install(cfg, a):
    ledger = _ledger(cfg)
    if a.pending:
        if not (a.version and a.url and a.sha256 and a.commit):
            print("--pending needs --version, --url, --sha256 and --commit")
            return 2
        src = a.rpc or cfg.get("node", "rpc")
        hh, floor = a.halt_height, a.min_version
        chain_height = None
        try:
            chain_height = rpc.status(src)["height"]
            if hh is None and not a.rolling:
                hh, floor2 = rpc.scheduled_halt(src)
                floor = floor or floor2
                print("read from %s: chain height %d, halt height %s, floor %s" % (
                    src, chain_height, hh or "none", floor or "none"))
        except rpc.RPCError as e:
            if hh is None and not a.rolling:
                print("error: cannot read the scheduled halt from %s (%s). Pass --rpc with a node that answers "
                      "(for example the network's public RPC), or give --halt-height and --min-version." % (src, e))
                return 1
            print("note: could not read the chain height from %s; checking against the ledger only" % src)
        if a.past_halt and a.halt_height is None:
            print("--past-halt needs --halt-height (and --min-version): the chain's params now show a later halt")
            return 2
        add_pending(cfg, ledger, a.version, hh, floor, a.commit, a.url, a.sha256,
                    chain_height=chain_height, rolling=a.rolling, past_halt=a.past_halt)
        if a.past_halt:
            # The operator states that the chain halted there: let the runner start this version after it.
            st = load_state(cfg)
            seen = set(st.get("halts_seen") or [])
            seen.add(int(hh))
            st["halts_seen"] = sorted(seen)[-50:]
            save_state(cfg, st)
            print("recorded in the runner state that the chain halted at %d" % hh)
        ledger = _ledger(cfg)
    versions = a.version and [a.version] or []
    if a.all:
        versions = [e["version"] for e in ledger.entries]
    if a.for_height is not None:
        era = ledger.era_for(a.for_height + 1)
        versions = [era.versions[-1]] if era else []
    if not versions:
        print("nothing to install: give --version, --all or --for-height")
        return 2
    for v in versions:
        install_version(cfg, ledger, v, url=a.url if a.version == v else None,
                        sha256=a.sha256 if a.version == v else None,
                        commit=a.commit if a.version == v else None,
                        check_version=not a.skip_version_check)
    return 0


def cmd_genesis(cfg, a):
    install_genesis(cfg, _ledger(cfg), url=a.url)
    return 0


def cmd_state(cfg, a):
    if a.set_height is not None or a.from_rpc:
        set_height(cfg, a.set_height, a.from_rpc)
    print(json.dumps(load_state(cfg), indent=2, sort_keys=True))
    return 0


def cmd_preflight(cfg, a):
    res = run_checks(cfg)
    for r in res:
        print("%-4s %-15s %s" % (r.level.upper(), r.key, r.text))
    return 1 if any(r.level == "fail" for r in res) else 0


def cmd_watch(cfg, a):
    msgs, found = check_once(cfg, send=(lambda t: None) if a.dry_run else None)
    if a.dry_run:
        print("open problems: %s" % (json.dumps(found) if found else "none"))
    return 0


def cmd_run(cfg, a):
    return Runner(cfg).run()


def main(argv=None):
    p = argparse.ArgumentParser(prog="gno-tools", description="gno.land node tools %s" % __version__)
    p.add_argument("--config", required=True, help="path to the node's INI file")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    i = sub.add_parser("install")
    i.add_argument("--version")
    i.add_argument("--all", action="store_true")
    i.add_argument("--for-height", type=int, help="install the version for the block after this height")
    i.add_argument("--pending", action="store_true", help="record --version as an upcoming upgrade")
    i.add_argument("--url")
    i.add_argument("--sha256")
    i.add_argument("--commit")
    i.add_argument("--halt-height", type=int)
    i.add_argument("--min-version")
    i.add_argument("--rpc", help="RPC to read the scheduled halt from (default: the local node)")
    i.add_argument("--rolling", action="store_true", help="with --pending: a rolling patch with no halt height")
    i.add_argument("--past-halt", action="store_true",
                   help="with --pending: this halt has already happened (give --halt-height and --min-version)")
    i.add_argument("--skip-version-check", action="store_true")
    g = sub.add_parser("genesis")
    g.add_argument("--url")
    s = sub.add_parser("state")
    s.add_argument("--set-height", type=int)
    s.add_argument("--from-rpc", action="store_true")
    sub.add_parser("preflight")
    w = sub.add_parser("watch")
    w.add_argument("--dry-run", action="store_true", help="check and print, send nothing")
    sub.add_parser("run")
    a = p.parse_args(argv)
    try:
        cfg = Config(a.config)
    except Exception as e:
        print("error: cannot read the config %s: %s" % (a.config, e))
        return 1
    try:
        return {"plan": cmd_plan, "install": cmd_install, "genesis": cmd_genesis, "state": cmd_state,
                "preflight": cmd_preflight, "watch": cmd_watch, "run": cmd_run}[a.cmd](cfg, a)
    except (InstallError, LedgerError, ConfigError, rpc.RPCError, ValueError, OSError) as e:
        print("error: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
