"""Install, pending and ledger-loading safety checks (no network: fake RPCs and local paths only)."""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
    os.environ.pop(k, None)

from gnotools.__main__ import main                                     # noqa: E402
from gnotools.config import Config                                     # noqa: E402
from gnotools.install import (InstallError, add_pending, checkout_gnoroot,  # noqa: E402
                              reported_version, version_dir)
from gnotools.ledger import Ledger, LedgerError, load_json             # noqa: E402
from helpers import TestNode, free_port                                # noqa: E402
from test_watch import FakeRPC                                         # noqa: E402

C = "c" * 40
SHA = "ab" * 32
LEDGER = [
    {"kind": "genesis", "version": "v1.5.0", "commit": C, "halt_height": None},
    {"kind": "upgrade", "version": "v1.8.0", "commit": C, "halt_height": 30, "halt_min_version": "v1.8.0"},
]


class Pending(unittest.TestCase):
    def setUp(self):
        self.node = TestNode(LEDGER, [], {}, ["v1.5.0", "v1.8.0"])
        self.cfg = Config(self.node.config)
        self.rpcs = []

    def tearDown(self):
        for r in self.rpcs:
            r.close()
        self.node.cleanup()

    def rpc(self, **kw):
        r = FakeRPC(**kw)
        self.rpcs.append(r)
        return r.url()

    def pending_text(self):
        with open(self.node.pending) as f:
            return f.read()

    def cli(self, *extra):
        args = ["--config", self.node.config, "install", "--pending", "--version", "v1.9.0",
                "--url", "https://example.invalid/gnoland", "--sha256", SHA, "--commit", "d" * 40,
                "--skip-version-check"] + list(extra)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(args)
        return rc, buf.getvalue()

    def test_past_halt_from_chain_is_refused(self):
        # On a live chain the params still show the last halt (here 30) until a new one is voted.
        before = self.pending_text()
        rc, out = self.cli("--rpc", self.rpc(height=1000, halt=(30, "v1.8.0")))
        self.assertEqual(rc, 1, out)
        self.assertIn("not after the ledger's last halt 30", out)
        self.assertEqual(self.pending_text(), before)

    def test_no_halt_scheduled_is_refused(self):
        before = self.pending_text()
        rc, out = self.cli("--rpc", self.rpc(height=1000, halt=(0, "")))
        self.assertEqual(rc, 1, out)
        self.assertIn("no halt height", out)
        self.assertEqual(self.pending_text(), before)

    def test_rpc_down_gives_a_clear_error(self):
        before = self.pending_text()
        rc, out = self.cli("--rpc", "http://127.0.0.1:%d" % free_port())
        self.assertEqual(rc, 1, out)
        self.assertIn("cannot read the scheduled halt", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.pending_text(), before)

    def test_halt_behind_the_chain_is_refused(self):
        led = self.cfg.load_ledger()
        with self.assertRaises(InstallError):
            add_pending(self.cfg, led, "v1.9.0", 900, "v1.9.0", "d" * 40, "https://x/g", SHA,
                        chain_height=1000, log=lambda m: None)

    def test_good_pending_is_written_readable_and_validates(self):
        led = self.cfg.load_ledger()
        add_pending(self.cfg, led, "v1.9.0", 5000, "v1.9.0", "d" * 40, "https://x/g", SHA,
                    chain_height=1000, log=lambda m: None)
        mode = stat.S_IMODE(os.stat(self.node.pending).st_mode)
        self.assertEqual(mode, 0o644)
        led2 = self.cfg.load_ledger()
        self.assertTrue(led2.entry("v1.9.0")["pending"])
        self.assertEqual(led2.eras[-1].start, 5001)

    def test_rolling_needs_the_flag_and_no_halt(self):
        led = self.cfg.load_ledger()
        with self.assertRaises(InstallError):
            add_pending(self.cfg, led, "v1.8.1", None, None, "d" * 40, "https://x/g", SHA, log=lambda m: None)
        add_pending(self.cfg, led, "v1.8.1", None, None, "d" * 40, "https://x/g", SHA, rolling=True,
                    log=lambda m: None)
        self.assertEqual(self.cfg.load_ledger().entry("v1.8.1")["kind"], "rolling")

    def test_bad_commit_and_url_are_refused(self):
        led = self.cfg.load_ledger()
        for commit, url in (("--upload-pack=x", "https://x/g"), ("d" * 40, "http://x/g")):
            with self.assertRaises(InstallError):
                add_pending(self.cfg, led, "v1.9.0", 5000, "v1.9.0", commit, url, SHA, log=lambda m: None)


class Safety(unittest.TestCase):
    def setUp(self):
        self.node = TestNode(LEDGER, [], {}, [])
        self.cfg = Config(self.node.config)

    def tearDown(self):
        self.node.cleanup()

    def test_plain_http_ledger_is_refused(self):
        with self.assertRaises(LedgerError):
            load_json("http://example.invalid/upgrades.json")

    def test_version_cannot_escape_the_versions_directory(self):
        with self.assertRaises(InstallError):
            version_dir(self.cfg, "../../etc")

    def test_reported_version_is_exact(self):
        self.assertEqual(reported_version("gnoland version: v1.8.0-rc.1\n"), "v1.8.0-rc.1")
        self.assertNotEqual(reported_version("gnoland version: v1.8.0-rc.1\n"), "v1.8.0")

    def test_ledger_commit_must_be_a_hash(self):
        with self.assertRaises(LedgerError):
            Ledger({"chain_id": "x", "upgrades": [{"kind": "genesis", "version": "v1.0.0", "commit": "-x"}]})

    def test_cached_ledger_is_used_when_the_url_fails(self):
        cache = os.path.join(self.node.dir, "cache.json")
        with open(self.node.ledger) as f, open(cache, "w") as g:
            g.write(f.read())
        logs = []
        led = Ledger.load("https://127.0.0.1:%d/upgrades.json" % free_port(), cache=cache, log=logs.append)
        self.assertTrue(led.from_cache)
        self.assertEqual(led.chain_id, "test-1")
        self.assertIn("cached copy", logs[0])

    def test_failed_gnoroot_checkout_leaves_nothing_behind(self):
        self.cfg.cp.set("node", "gno_repo", os.path.join(self.node.dir, "no-such-repo"))
        dest = os.path.join(self.node.dir, "gnoroot")
        for _ in range(2):           # a retry must fail the same clear way, not on a half-made .git
            with self.assertRaises(InstallError) as ctx:
                checkout_gnoroot(self.cfg, "e" * 40, dest)
            self.assertIn("fetch", str(ctx.exception))
            self.assertFalse(os.path.exists(dest))


if __name__ == "__main__":
    unittest.main()
