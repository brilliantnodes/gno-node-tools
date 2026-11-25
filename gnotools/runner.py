"""gno-tools run: start the right gnoland binary for the next block and switch at every halt.

Meant to be the ExecStart of a systemd unit. It:
  * reads the official ledger (plus a local pending file) to know which version serves which blocks;
  * starts that version, checking its SHA-256 first, with GNOROOT at its own release checkout;
  * follows the node's height over RPC and in the node's own log lines;
  * when the node reaches a halt height, stops it if it is still running (at the chain tip the
    process stays up after a halt) and starts the version for the next block;
  * refuses to start while another node already answers on the RPC port, and holds a lock so
    two runners cannot start two copies of the same validator.

It never edits config.toml and never sets a halt height itself: halts come from governance.
"""

import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

from . import rpc
from .install import sha256_file, installed_versions
from .ledger import Ledger, choose_version, parse_plan

HALT_RE = re.compile(r"halt height (\d+) reached")
GOV_HALT_RE = re.compile(r"GovDAO halt height reached.*?halt_height[=\":\s]+(\d+)")
TOO_NEW_RE = re.compile(r"is an upgrade intended for halt height (\d+), but the chain is at height (\d+)")
TOO_OLD_RE = re.compile(r"does not meet the minimum version \\?\"?([^\"\\\s;]+)")
APPHASH_RE = re.compile(r"wrong Block\.Header\.AppHash")


def state_path(cfg):
    return os.path.join(cfg.state_dir, "runner.json")


def load_state(cfg):
    try:
        with open(state_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(cfg, st):
    os.makedirs(cfg.state_dir, exist_ok=True)
    tmp = state_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, state_path(cfg))


def data_present(cfg):
    db = os.path.join(cfg.home, "gnoland-data", "db")
    return os.path.isdir(db) and bool(os.listdir(db))


class Runner:
    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda m: print("gno-runner: " + m, flush=True))
        self.stopping = False
        self.child = None
        self.lock = threading.Lock()
        self.st = load_state(cfg)
        self.height = self.st.get("height")
        self._reset_run()

    def _reset_run(self):
        self.halted_at = None
        self.halted_seen_t = None
        self.too_new = None       # (halt_height, chain_height)
        self.too_old = None       # floor version
        self.apphash = False
        self.progress = False

    # ---- height bookkeeping -------------------------------------------------------------
    def _note_height(self, h, source, authoritative=False):
        with self.lock:
            if h is None:
                return
            if authoritative or self.height is None or h > self.height:
                if self.height is not None and h > self.height:
                    self.progress = True
                self.height = h
                self.st.update({"height": h, "height_source": source, "updated": int(time.time())})
                save_state(self.cfg, self.st)

    # ---- child output and polling --------------------------------------------------------
    def _reader(self, stream):
        for line in iter(stream.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
            m = HALT_RE.search(line)
            if m:
                h = int(m.group(1))
                self.halted_at = h
                self.halted_seen_t = time.time()
                self._note_height(h, "halt line")
                continue
            m = GOV_HALT_RE.search(line)
            if m:
                self.log("node announced the governance halt at %s" % m.group(1))
                continue
            m = TOO_NEW_RE.search(line)
            if m:
                self.too_new = (int(m.group(1)), int(m.group(2)))
                self._note_height(int(m.group(2)), "refusal line", authoritative=True)
                continue
            m = TOO_OLD_RE.search(line)
            if m:
                self.too_old = m.group(1)
                continue
            if APPHASH_RE.search(line):
                self.apphash = True
        stream.close()

    def _poller(self, child):
        every = self.cfg.getint("runner", "poll_seconds")
        base = self.cfg.get("node", "rpc")
        while child.poll() is None and not self.stopping:
            try:
                s = rpc.status(base, timeout=4)
                self._note_height(s["height"], "rpc")
            except rpc.RPCError:
                pass
            time.sleep(every)

    # ---- process control -----------------------------------------------------------------
    def _terminate(self, why):
        c = self.child
        if c is None or c.poll() is not None:
            return
        self.log("stopping the node (%s)" % why)
        c.send_signal(signal.SIGTERM)
        try:
            c.wait(timeout=self.cfg.getint("runner", "stop_timeout_seconds"))
        except subprocess.TimeoutExpired:
            self.log("node did not stop in time; killing it")
            c.kill()
            c.wait()

    def _on_signal(self, signum, frame):
        self.stopping = True
        self._terminate("received signal %d" % signum)

    def _start(self, version):
        vdir = os.path.join(self.cfg.versions_dir, version)
        binp = os.path.join(vdir, "gnoland")
        info = installed_versions(self.cfg, verify=False).get(version, {})
        got = sha256_file(binp)
        if got != info.get("sha256"):
            raise RuntimeError("%s binary hash %s does not match the installed record %s; refusing to start"
                               % (version, got, info.get("sha256")))
        cmd = [binp, "start"] + shlex.split(self.cfg.start_args())
        env = dict(os.environ, GNOROOT=os.path.join(vdir, "gno"))
        self.log("starting %s for block %s (hash ok): %s" % (version, (self.height or 0) + 1, " ".join(cmd[1:])))
        self.st.update({"version": version, "started": int(time.time())})
        save_state(self.cfg, self.st)
        return subprocess.Popen(cmd, cwd=self.cfg.home, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)

    # ---- main loop -----------------------------------------------------------------------
    def run(self):
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        lockf = open(os.path.join(self.cfg.state_dir, "runner.lock"), "w")
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.log("another runner holds the lock in %s; not starting a second node" % self.cfg.state_dir)
            return 1
        try:
            rpc.status(self.cfg.get("node", "rpc"), timeout=3)
            self.log("a node already answers on %s; stop it before starting the runner" % self.cfg.get("node", "rpc"))
            return 1
        except rpc.RPCError:
            pass

        if self.height is None:
            if data_present(self.cfg):
                self.log("the node already has chain data but the runner has no record of its height. "
                         "Run `gno-tools state --set-height N` (or `--from-rpc` while the old node still runs).")
                return 1
            self._note_height(0, "empty data directory", authoritative=True)

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        plan = parse_plan(self.cfg.get("node", "plan"))
        grace = self.cfg.getint("runner", "halt_grace_seconds")
        min_run = self.cfg.getint("runner", "min_run_seconds")
        last_version, last_outcome = None, None

        for _ in range(self.cfg.getint("runner", "max_switches")):
            if self.stopping:
                return 0
            ledger = Ledger.load(self.cfg.get("node", "ledger"), self.cfg.get("node", "pending") or None)
            installed = {v for v, i in installed_versions(self.cfg).items() if not i.get("broken")}
            version, reason = choose_version(ledger, self.height + 1, installed, plan)
            if version is None:
                self.log(reason)
                return 1
            self.log(reason)
            if version == last_version and last_outcome == "too_new":
                self.log("%s was refused before its halt and no older version is installed for block %d; "
                         "install the previous version" % (version, self.height + 1))
                return 1

            self._reset_run()
            started = time.time()
            try:
                self.child = self._start(version)
            except (OSError, RuntimeError) as e:
                self.log(str(e))
                return 1
            reader = threading.Thread(target=self._reader, args=(self.child.stdout,), daemon=True)
            poller = threading.Thread(target=self._poller, args=(self.child,), daemon=True)
            reader.start()
            poller.start()

            while self.child.poll() is None:
                if self.halted_at is not None and time.time() - self.halted_seen_t >= grace:
                    # At the chain tip the node keeps running after a halt; stop it ourselves.
                    self._terminate("halted at block %d" % self.halted_at)
                    break
                time.sleep(0.2)
            rc = self.child.wait()
            reader.join(timeout=5)
            ran = time.time() - started
            self.log("%s exited with code %s after %ds" % (version, rc, ran))

            if self.stopping:
                return 0
            if self.halted_at is not None:
                self.st["last_halt"] = self.halted_at
                save_state(self.cfg, self.st)
                last_version, last_outcome = version, "halt"
                continue
            if self.too_new is not None:
                self.log("%s is meant for after the halt at %d; the chain is at %d" % (
                    version, self.too_new[0], self.too_new[1]))
                last_version, last_outcome = version, "too_new"
                continue
            if self.too_old is not None:
                self.log("%s is below the governance minimum %s: the chain is past a halt this runner does not "
                         "know. Install that version and record it (`gno-tools install --pending`)." % (
                             version, self.too_old))
                return 1
            if self.apphash:
                self.log("%s cannot process block %d (wrong AppHash): it is the wrong binary for this range. "
                         "Check the ledger or the replay plan." % (version, self.height + 1))
                return 1
            if ran < min_run and not self.progress:
                self.log("the node stopped quickly without making progress; leaving the restart to systemd")
            return rc if rc else 1
        self.log("too many binary switches in one run; stopping")
        return 1


def set_height(cfg, height=None, from_rpc=False, log=print):
    st = load_state(cfg)
    if from_rpc:
        height = rpc.status(cfg.get("node", "rpc"))["height"]
    if height is None:
        raise ValueError("give a height or --from-rpc")
    st.update({"height": int(height), "height_source": "rpc" if from_rpc else "operator", "updated": int(time.time())})
    save_state(cfg, st)
    log("runner height set to %d" % int(height))
