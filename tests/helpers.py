import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAKE = os.path.join(HERE, "fakegnoland.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port_closed(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            return True
        finally:
            s.close()
        time.sleep(0.2)
    return False


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestNode:
    """A throwaway node directory with fake binaries, a ledger, a chain and a config file."""

    def __init__(self, ledger_entries, pending=None, chain=None, versions=None, plan="", runner_extra=None):
        self.dir = tempfile.mkdtemp(prefix="gnotools-")
        self.root = os.path.join(self.dir, "root")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.root, "versions"))
        os.makedirs(self.home)
        self.port = free_port()
        self.ledger = os.path.join(self.dir, "ledger.json")
        with open(self.ledger, "w") as f:
            json.dump({"chain_id": "test-1", "genesis_sha256": "0" * 64, "upgrades": ledger_entries}, f)
        self.pending = os.path.join(self.dir, "pending.json")
        with open(self.pending, "w") as f:
            json.dump({"pending": pending or []}, f)
        self.chain = os.path.join(self.dir, "chain.json")
        ch = {"chain_id": "test-1", "rpc_port": self.port, "sync_interval": 0.01, "tip_interval": 0.05,
              "tip": 35, "halts": []}
        ch.update(chain or {})
        with open(self.chain, "w") as f:
            json.dump(ch, f)
        self.data = os.path.join(self.dir, "data.json")
        for v in versions or []:
            self.add_version(v)
        self.config = os.path.join(self.dir, "node.ini")
        extra = runner_extra or {}
        with open(self.config, "w") as f:
            f.write("[node]\nchain_id = test-1\nledger = %s\npending = %s\nroot = %s\nhome = %s\n"
                    "genesis = %s\nrpc = http://127.0.0.1:%d\nstart_args = --chainid test-1\nplan = %s\n"
                    % (self.ledger, self.pending, self.root, self.home, os.path.join(self.dir, "genesis.json"),
                       self.port, plan))
            f.write("[runner]\npoll_seconds = 1\nhalt_grace_seconds = 1\nstop_timeout_seconds = 5\n"
                    "min_run_seconds = 1\n")
            for k, v in extra.items():
                f.write("%s = %s\n" % (k, v))

    def add_version(self, v):
        d = os.path.join(self.root, "versions", v)
        os.makedirs(os.path.join(d, "gno"))
        shutil.copy(FAKE, os.path.join(d, "gnoland"))
        os.chmod(os.path.join(d, "gnoland"), 0o755)
        with open(os.path.join(d, "VERSION"), "w") as f:
            f.write(v)
        with open(os.path.join(d, "INSTALLED.json"), "w") as f:
            json.dump({"version": v, "sha256": sha(os.path.join(d, "gnoland"))}, f)

    def env(self):
        e = dict(os.environ, FAKE_CHAIN=self.chain, FAKE_DATA=self.data, PYTHONPATH=REPO)
        e.pop("HTTPS_PROXY", None)
        e.pop("HTTP_PROXY", None)
        e.pop("https_proxy", None)
        e.pop("http_proxy", None)
        e["NO_PROXY"] = "127.0.0.1,localhost"
        return e

    def height(self):
        try:
            with open(self.data) as f:
                return json.load(f)["height"]
        except (OSError, ValueError):
            return 0

    def set_data_height(self, h):
        with open(self.data, "w") as f:
            json.dump({"height": h}, f)

    def start_runner(self):
        log = open(os.path.join(self.dir, "runner.log"), "w")
        self._logs = getattr(self, "_logs", []) + [log]
        return subprocess.Popen([sys.executable, "-m", "gnotools", "--config", self.config, "run"],
                                cwd=REPO, env=self.env(), stdout=log, stderr=subprocess.STDOUT)

    def log_text(self):
        with open(os.path.join(self.dir, "runner.log")) as f:
            return f.read()

    def wait_for(self, cond, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def stop(self, proc, timeout=20):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()

    def cleanup(self):
        for f in getattr(self, "_logs", []):
            f.close()
        shutil.rmtree(self.dir, ignore_errors=True)
