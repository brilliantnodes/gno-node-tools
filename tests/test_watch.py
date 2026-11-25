"""Watch and preflight checks against a fake RPC (status, net_info, halt params, block times)."""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
    os.environ.pop(k, None)

from gnotools.checks import run_checks          # noqa: E402
from gnotools.config import Config               # noqa: E402
from gnotools.watch import check_once            # noqa: E402
from helpers import TestNode, free_port          # noqa: E402

C = "c" * 40
LEDGER = [
    {"kind": "genesis", "version": "v1.5.0", "commit": C, "halt_height": None},
    {"kind": "upgrade", "version": "v1.8.0", "commit": C, "halt_height": 30, "halt_min_version": "v1.8.0"},
]


class FakeRPC:
    def __init__(self, height=1000, peers=8, halt=(0, ""), version="v1.8.0", catching_up=False, network="test-1"):
        self.s = dict(height=height, peers=peers, halt=halt, version=version, catching_up=catching_up,
                      network=network)
        self.port = free_port()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlparse(self.path)
                s = outer.s
                if u.path == "/status":
                    res = {"node_info": {"network": s["network"]}, "build_version": s["version"],
                           "sync_info": {"latest_block_height": str(s["height"]), "catching_up": s["catching_up"],
                                         "latest_block_time": "2026-11-25T10:00:00Z"}}
                elif u.path == "/net_info":
                    res = {"n_peers": str(s["peers"])}
                elif u.path == "/block":
                    h = int(parse_qs(u.query)["height"][0])
                    sec = 3 * h                       # 3 s per block
                    t = "2026-11-25T%02d:%02d:%02d.123456789Z" % ((sec // 3600) % 24, (sec // 60) % 60, sec % 60)
                    if sec >= 86400:
                        t = "2026-11-%02dT%02d:%02d:%02dZ" % (25 + sec // 86400, (sec // 3600) % 24, (sec // 60) % 60, sec % 60)
                    res = {"block": {"header": {"time": t}}}
                elif u.path == "/abci_query":
                    path = unquote(parse_qs(u.query)["path"][0]).strip('"')
                    val = None
                    if path == "params/node:p:halt_height" and s["halt"][0]:
                        val = json.dumps(str(s["halt"][0]))
                    if path == "params/node:p:halt_min_version" and s["halt"][1]:
                        val = json.dumps(s["halt"][1])
                    data = base64.b64encode(val.encode()).decode() if val else None
                    res = {"response": {"ResponseBase": {"Error": None, "Data": data}}}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"jsonrpc": "2.0", "id": "", "result": res}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.node = TestNode(LEDGER, [], {}, ["v1.5.0", "v1.8.0"])
        self.local = FakeRPC()
        self.ref = FakeRPC()
        self.cfg_path = self.node.config
        with open(self.cfg_path) as f:
            text = f.read()
        text = text.replace("rpc = http://127.0.0.1:%d" % self.node.port, "rpc = " + self.local.url())
        text = "\n".join("genesis = " if l.startswith("genesis = ") else l for l in text.split("\n"))
        text += ("[watch]\nreference_rpc = %s\nconfirm_checks = 3\nstall_minutes = 15\nmax_lag_blocks = 30\n"
                 "min_peers = 3\ndisk_percent = 101\nhalt_warn_blocks = 20000\n" % self.ref.url())
        with open(self.cfg_path, "w") as f:
            f.write(text)
        cfgdir = os.path.join(self.node.home, "gnoland-data", "config")
        os.makedirs(cfgdir)
        self.toml = os.path.join(cfgdir, "config.toml")
        with open(self.toml, "w") as f:
            f.write('moniker = "x"\nhalt_height = 0\nskip_upgrade_height = 0\n\n[p2p]\npex = true\n')
        self.cfg = Config(self.cfg_path)
        self.sent = []

    def tearDown(self):
        self.local.close()
        self.ref.close()
        self.node.cleanup()

    def levels(self):
        return {r.key: r.level for r in run_checks(self.cfg)}

    def texts(self):
        return {r.key: r.text for r in run_checks(self.cfg)}


class Preflight(Base):
    def test_all_good(self):
        lv = self.levels()
        self.assertNotIn("fail", [v for k, v in lv.items() if k != "genesis"], self.texts())
        self.assertEqual(lv["next-binary"], "ok")
        self.assertEqual(lv["halt"], "ok")

    def test_halt_without_binary(self):
        self.local.s["halt"] = (5000, "v1.9.0")
        t = self.texts()
        self.assertEqual(self.levels()["halt"], "fail")
        self.assertIn("governance halt at 5000 in 4000 blocks", t["halt"])
        self.assertIn("about 3 h", t["halt"])                 # 4000 blocks x 3 s
        self.assertIn("needs v1.9.0 or newer: not installed", t["halt"])

    def test_halt_far_ahead_is_a_warning(self):
        self.local.s["halt"] = (500000, "v1.9.0")
        self.assertEqual(self.levels()["halt"], "warn")

    def test_halt_with_binary_installed_but_not_recorded(self):
        # Close to the halt this is a failure: the runner would restart the old binary at the halt.
        self.node.add_version("v1.9.0")
        self.local.s["halt"] = (5000, "v1.9.0")
        self.assertEqual(self.levels()["halt"], "fail")
        self.assertIn("install --pending", self.texts()["halt"])

    def test_halt_far_ahead_installed_but_not_recorded_is_a_warning(self):
        self.node.add_version("v1.9.0")
        self.local.s["halt"] = (500000, "v1.9.0")
        self.assertEqual(self.levels()["halt"], "warn")

    def test_past_halt_params_are_not_a_scheduled_halt(self):
        # The params are never cleared: after an upgrade they still show the last halt.
        self.local.s["halt"] = (30, "v1.8.0")
        self.assertEqual(self.levels()["halt"], "ok")
        self.assertIn("none scheduled ahead", self.texts()["halt"])

    def test_config_halt_height_trap(self):
        with open(self.toml, "w") as f:
            f.write("halt_height = 5000\n[p2p]\n")
        self.assertEqual(self.levels()["config-halt"], "fail")

    def test_wrong_chain(self):
        self.local.s["network"] = "other-1"
        self.assertEqual(self.levels()["chain"], "fail")

    def test_lag(self):
        self.ref.s["height"] = 1100
        self.assertEqual(self.levels()["lag"], "warn")


class Watch(Base):
    def run_check(self, now):
        return check_once(self.cfg, now=now, send=self.sent.append, log=lambda m: None)

    def test_quiet_when_fine(self):
        for i in range(5):
            self.local.s["height"] += 10
            self.ref.s["height"] = self.local.s["height"]
            self.run_check(1000 + 300 * i)
        self.assertEqual(self.sent, [])

    def test_confirms_before_alerting_and_repeats(self):
        self.local.s["peers"] = 1
        for i in range(3):
            self.local.s["height"] += 10
            self.ref.s["height"] = self.local.s["height"]
            self.run_check(1000 + 300 * i)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("PROBLEM: 1 peers", self.sent[0])
        self.local.s["height"] += 10
        self.ref.s["height"] = self.local.s["height"]
        self.run_check(1000 + 300 * 3)
        self.assertEqual(len(self.sent), 1)                    # no repeat before 12 h
        self.local.s["height"] += 10
        self.ref.s["height"] = self.local.s["height"]
        self.run_check(1000 + 13 * 3600)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("STILL WRONG", self.sent[1])

    def test_clears_quietly(self):
        self.local.s["peers"] = 1
        for i in range(3):
            self.local.s["height"] += 10
            self.ref.s["height"] = self.local.s["height"]
            self.run_check(1000 + 300 * i)
        self.local.s["peers"] = 9
        self.local.s["height"] += 10
        self.ref.s["height"] = self.local.s["height"]
        self.run_check(2000)
        self.assertEqual(len(self.sent), 1)                    # no "recovered" message by default

    def test_stall(self):
        # This node is stuck while the network moves on (by less than the lag limit).
        self.ref.s["height"] = self.local.s["height"]
        self.run_check(1000)
        self.ref.s["height"] += 10
        self.run_check(1000 + 10 * 60)
        self.assertEqual(self.sent, [])
        self.ref.s["height"] += 10
        self.run_check(1000 + 16 * 60)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("block height stuck at 1000", self.sent[0])
        self.assertIn("the reference node is at 1020", self.sent[0])

    def test_network_wide_stall_is_quiet(self):
        # A coordinated halt: the reference node has stopped at the same height. No alert.
        self.ref.s["height"] = self.local.s["height"]
        for t in (1000, 1000 + 10 * 60, 1000 + 16 * 60, 1000 + 40 * 60):
            self.run_check(t)
        self.assertEqual(self.sent, [])

    def test_stall_without_reference_alerts(self):
        self.cfg.cp.set("watch", "reference_rpc", "")
        self.run_check(1000)
        self.run_check(1000 + 16 * 60)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("block height stuck at 1000", self.sent[0])

    def test_reference_down_does_not_alert(self):
        self.ref.close()
        for i in range(4):
            self.local.s["height"] += 10
            self.run_check(1000 + 300 * i)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.levels()["reference"], "warn")
        self.ref = FakeRPC()   # tearDown closes it

    def test_node_down(self):
        self.local.close()
        for i in range(3):
            self.run_check(1000 + 300 * i)
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn("local node RPC not answering", self.sent[0])
        self.local = FakeRPC()   # tearDown closes it


if __name__ == "__main__":
    unittest.main()
