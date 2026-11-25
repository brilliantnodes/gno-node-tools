"""gno-tools run: start the right gnoland binary for the next block and switch at every halt.

Meant to be the ExecStart of a systemd unit. It:
  * reads the official ledger (plus a local pending file) to know which version serves which blocks;
  * starts that version, checking its SHA-256 first, with GNOROOT at its own release checkout;
  * follows the node's height over RPC and in the node's own log lines;
  * when the node reaches a halt height, stops it if it is still running (at the chain tip the
    process stays up after a halt) and starts the version for the next block. The halt is seen
    either in the node's log ("halt height N reached") or, if no such line arrives, from the
    RPC height sitting on a known halt height for `halt_stall_seconds`;
  * starts a binary recorded only in the local pending file only once the chain itself confirms
    that halt height (the chain's halt param, or a halt this runner saw happen);
  * refuses to start while another node already answers on the RPC port, and holds locks in the
    state directory and the node home so two runners cannot start two copies of one validator.

It never edits config.toml and never sets a halt height itself: halts come from governance.

Exit codes: 0 after a stop signal; 3 (EXIT_STOP) for problems a restart cannot fix, so systemd
can be told not to restart (RestartPreventExitStatus=3); otherwise the node's own exit code or 1.
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
from .config import EXIT_STOP, ConfigError
from .install import sha256_file, installed_versions
from .ledger import LedgerError, choose_version, parse_plan

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
            st = json.load(f)
        return st if isinstance(st, dict) else {}
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


def _lock(path):
    """Take an exclusive lock on `path`; return the open file (keep it open) or None if taken."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


class Runner:
    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda m: print("gno-runner: " + m, flush=True))
        self.stopping = False
        self.child = None
        self.lock = threading.Lock()
        self.st = load_state(cfg)
        self.height = self.st.get("height")
        ch = self.st.get("chain_halt")
        self.chain_halt = tuple(ch) if isinstance(ch, list) and len(ch) == 2 else None
        seen = self.st.get("halts_seen")
        self.halts_seen = {h for h in seen if isinstance(h, int)} if isinstance(seen, list) else set()
        self._save_failed = False
        self._reset_run()

    def _reset_run(self):
        self.halt = None          # (height, time seen), set in one assignment
        self.too_new = None       # (halt_height, chain_height)
        self.too_old = None       # floor version
        self.apphash = False
        self.progress = False
        self.rpc_h = None         # last RPC height and when it last changed
        self.rpc_h_t = None
        self.rpc_catching_up = True
        self.start_height = None  # height of the chain data when this child was started

    # ---- state ---------------------------------------------------------------------------
    def _save(self):
        try:
            save_state(self.cfg, self.st)
            self._save_failed = False
        except OSError as e:
            if not self._save_failed:
                self.log("cannot write the state file (%s); carrying on" % e)
            self._save_failed = True

    def _remember_halt(self, h):
        if h in self.halts_seen:
            return
        with self.lock:
            self.halts_seen.add(h)
            self.st["halts_seen"] = sorted(self.halts_seen)[-50:]
            self._save()

    def _note_height(self, h, source, authoritative=False):
        with self.lock:
            if h is None:
                return
            if authoritative or self.height is None or h > self.height:
                if self.height is not None and h > self.height:
                    self.progress = True
                self.height = h
                self.st.update({"height": h, "height_source": source, "updated": int(time.time())})
                self._save()

    # ---- child output and polling --------------------------------------------------------
    def _parse(self, line):
        m = HALT_RE.search(line)
        if m:
            h = int(m.group(1))
            self.halt = (h, time.time())
            self._note_height(h, "halt line")
            return
        m = GOV_HALT_RE.search(line)
        if m:
            self.log("node announced the governance halt at %s" % m.group(1))
            return
        m = TOO_NEW_RE.search(line)
        if m:
            self.too_new = (int(m.group(1)), int(m.group(2)))
            self._note_height(int(m.group(2)), "refusal line", authoritative=True)
            return
        m = TOO_OLD_RE.search(line)
        if m:
            self.too_old = m.group(1)
            return
        if APPHASH_RE.search(line):
            self.apphash = True

    def _reader(self, stream):
        """Copy the node's output to our stdout and watch it for known lines.

        The node writes into a pipe; if nothing drains it, the node blocks on its next log write.
        So this loop must never die: bytes are decoded leniently and every per-line failure is
        caught. Writing to our own stdout is best effort."""
        out = getattr(sys.stdout, "buffer", None)
        complained = False
        try:
            for raw in iter(stream.readline, b""):
                try:
                    if out is not None:
                        out.write(raw)
                        out.flush()
                    else:
                        sys.stdout.write(raw.decode("utf-8", "replace"))
                        sys.stdout.flush()
                except Exception:
                    pass
                try:
                    self._parse(raw.decode("utf-8", "replace"))
                except Exception as e:
                    if not complained:
                        self.log("could not parse a node log line (%s); still reading" % e)
                        complained = True
        except Exception as e:
            self.log("reading the node's output failed (%s)" % e)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _poller(self, child):
        every = max(1, self.cfg.getint("runner", "poll_seconds"))
        base = self.cfg.get("node", "rpc")
        while child.poll() is None and not self.stopping:
            try:
                s = rpc.status(base, timeout=4)
                now = time.time()
                if s["height"] != self.rpc_h:
                    self.rpc_h, self.rpc_h_t = s["height"], now
                self.rpc_catching_up = s["catching_up"]
                self._note_height(s["height"], "rpc")
                hh, floor = rpc.scheduled_halt(base, timeout=4)
                # (0, "") is stored too: GovDAO cancels a scheduled halt by setting its height to 0.
                if (hh, floor) != self.chain_halt:
                    self.chain_halt = (hh, floor)
                    with self.lock:
                        self.st["chain_halt"] = [hh, floor]
                        self._save()
                # The params are never cleared and arm only on the exact height, so a halt param
                # below the chain's height is a halt that has happened.
                if hh and s["height"] > hh:
                    self._remember_halt(hh)
            except Exception:
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
        self._save()
        return subprocess.Popen(cmd, cwd=self.cfg.home, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, bufsize=-1)

    def _reference_halt(self):
        ref = self.cfg.get("watch", "reference_rpc")
        if not ref:
            return None
        try:
            return rpc.scheduled_halt(ref)
        except rpc.RPCError:
            return None

    def _pending_confirmed(self, entry):
        """A binary known only from the local pending file starts only if the chain agrees on its halt."""
        h = entry.get("halt_height")
        if self.chain_halt and self.chain_halt[0] == h:
            return True, "the chain's halt param is %d" % h
        if h in self.halts_seen:
            return True, "the chain halted at %d (recorded in the runner state)" % h
        ref = self._reference_halt()
        if ref and ref[0] == h:
            return True, "the reference RPC's halt param is %d" % h
        seen = self.chain_halt[0] if self.chain_halt else (ref[0] if ref else "unknown")
        return False, ("the chain does not confirm halt %d (last halt param seen: %s). If gno.land cancelled that "
                       "halt, delete the entry from the pending file; if the halt did happen and a later one is "
                       "already scheduled, record it with `install --pending --past-halt`" % (h, seen))

    # ---- main loop -----------------------------------------------------------------------
    def run(self):
        try:
            return self._run()
        except (ConfigError, LedgerError, ValueError) as e:
            self.log("configuration problem: %s" % e)
            self._terminate("configuration problem")
            return EXIT_STOP

    def _run(self):
        locks = []
        for path in (os.path.join(self.cfg.state_dir, "runner.lock"),
                     os.path.join(self.cfg.home, ".gno-runner.lock")):
            f = _lock(path)
            if f is None:
                self.log("another runner holds the lock %s; not starting a second node" % path)
                return EXIT_STOP
            locks.append(f)
        try:
            rpc.status(self.cfg.get("node", "rpc"), timeout=3)
            self.log("a node already answers on %s; stop it before starting the runner" % self.cfg.get("node", "rpc"))
            return EXIT_STOP
        except rpc.RPCError:
            pass
        self.cfg.start_args()      # fail early on a bad start_args

        if self.height is None:
            if data_present(self.cfg):
                self.log("the node already has chain data but the runner has no record of its height. "
                         "Run `python3 -m gnotools state --set-height N` (or `--from-rpc` while the old node "
                         "still runs).")
                return EXIT_STOP
            self._note_height(0, "empty data directory", authoritative=True)

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        plan = parse_plan(self.cfg.get("node", "plan"))
        grace = self.cfg.getint("runner", "halt_grace_seconds")
        stall = self.cfg.getint("runner", "halt_stall_seconds")
        min_run = self.cfg.getint("runner", "min_run_seconds")
        last_version, last_outcome = None, None

        for _ in range(self.cfg.getint("runner", "max_switches")):
            if self.stopping:
                return 0
            try:
                ledger = self.cfg.load_ledger(log=self.log)
            except (OSError, ValueError, LedgerError) as e:
                self.log("cannot read the ledger (%s); not starting a node" % e)
                return EXIT_STOP
            installed = {v for v, i in installed_versions(self.cfg).items() if not i.get("broken")}
            version, reason = choose_version(ledger, self.height + 1, installed, plan)
            if version is None:
                self.log(reason)
                return EXIT_STOP
            self.log(reason)
            entry = ledger.entry(version) or {}
            if entry.get("pending") and entry.get("kind") == "upgrade":
                ok, why = self._pending_confirmed(entry)
                if not ok:
                    self.log("%s comes only from the pending file and %s; not starting it. Check the halt height "
                             "you recorded." % (version, why))
                    return EXIT_STOP
                self.log("%s (pending) confirmed: %s" % (version, why))
            if version == last_version and last_outcome == "too_new":
                self.log("%s was refused before its halt and is still the version picked for block %d. Install "
                         "the version for that block; if your replay plan forces %s here, set "
                         "skip_upgrade_height to that halt height in config.toml for one restart and back to 0 "
                         "afterwards, as gno.land's VALIDATOR.md describes" % (version, self.height + 1, version))
                return EXIT_STOP

            self._reset_run()
            self.start_height = self.height
            known_halts = ledger.halt_heights() | ({self.chain_halt[0]} if self.chain_halt and self.chain_halt[0] else set())
            started = time.time()
            try:
                self.child = self._start(version)
            except (OSError, RuntimeError, ConfigError) as e:
                self.log(str(e))
                return EXIT_STOP
            if self.stopping:              # a stop signal arrived while we were starting it
                self._terminate("stop requested during start")
                return 0
            reader = threading.Thread(target=self._reader, args=(self.child.stdout,), daemon=True)
            poller = threading.Thread(target=self._poller, args=(self.child,), daemon=True)
            reader.start()
            poller.start()

            reader_gone_t = None
            while self.child.poll() is None:
                now = time.time()
                halt = self.halt
                if halt is not None and now - halt[1] >= grace:
                    # At the chain tip the node keeps running after a halt; stop it ourselves.
                    self._terminate("halted at block %d" % halt[0])
                    break
                # Only a binary that was started below the halt can have halted there. A binary started
                # at the halt height is the new release waiting for the other validators to restart:
                # the height sits on the halt until two thirds of the voting power is back.
                if (halt is None and self.rpc_h in known_halts and self.rpc_h_t is not None
                        and self.start_height is not None and self.rpc_h > self.start_height
                        and not self.rpc_catching_up and now - self.rpc_h_t >= stall):
                    self.log("no halt line seen, but the height has stayed at the known halt height %d for %ds"
                             % (self.rpc_h, now - self.rpc_h_t))
                    self.halt = (self.rpc_h, now)
                    self._note_height(self.rpc_h, "rpc at halt")
                    self._terminate("halted at block %d" % self.rpc_h)
                    break
                if not reader.is_alive():
                    reader_gone_t = reader_gone_t or now
                    if now - reader_gone_t >= 5:
                        self.log("the node's output is no longer being read; stopping it so it cannot block")
                        self._terminate("output reader stopped")
                        break
                time.sleep(0.2)
            rc = self.child.wait()
            reader.join(timeout=5)
            ran = time.time() - started
            self.log("%s exited with code %s after %ds" % (version, rc, ran))

            if self.stopping:
                return 0
            if self.halt is not None:
                self.st["last_halt"] = self.halt[0]
                self._remember_halt(self.halt[0])
                self._save()
                last_version, last_outcome = version, "halt"
                continue
            if self.too_new is not None:
                self.log("%s is meant for after the halt at %d; the chain is at %d" % (
                    version, self.too_new[0], self.too_new[1]))
                last_version, last_outcome = version, "too_new"
                continue
            if self.too_old is not None:
                self.log("%s is below the governance minimum %s: the chain is past a halt this runner does not "
                         "know. Install that version and record it (`python3 -m gnotools install --pending`)." % (
                             version, self.too_old))
                return EXIT_STOP
            if self.apphash:
                self.log("%s cannot process block %d (wrong AppHash): it is the wrong binary for this range. "
                         "Check the ledger or the replay plan." % (version, self.height + 1))
                return EXIT_STOP
            if ran < min_run and not self.progress:
                self.log("the node stopped quickly without making progress; leaving the restart to systemd")
            return rc if rc else 1
        self.log("too many binary switches in one run; stopping")
        return EXIT_STOP


def set_height(cfg, height=None, from_rpc=False, log=print):
    st = load_state(cfg)
    if from_rpc:
        height = rpc.status(cfg.get("node", "rpc"))["height"]
    if height is None:
        raise ValueError("give a height or --from-rpc")
    st.update({"height": int(height), "height_source": "rpc" if from_rpc else "operator", "updated": int(time.time())})
    save_state(cfg, st)
    log("runner height set to %d" % int(height))
