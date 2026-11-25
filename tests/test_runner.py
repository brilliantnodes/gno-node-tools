"""End-to-end runner tests against the fake gnoland in tests/fakegnoland.py.

The test chain has governance halts at 10, 20 and 30 (each with a version floor), the network tip
at 35, and an upgrade voted for block 45 that the official ledger does not list yet (pending).
"""

import json
import os
import unittest

from helpers import TestNode, wait_port_closed
from gnotools.config import EXIT_STOP

C = "c" * 40
LEDGER = [
    {"kind": "genesis", "version": "v1.5.0", "commit": C, "halt_height": None},
    {"kind": "upgrade", "version": "v1.6.0", "commit": C, "halt_height": 10, "halt_min_version": "v1.6.0"},
    {"kind": "upgrade", "version": "v1.7.0", "commit": C, "halt_height": 20, "halt_min_version": "v1.7.0"},
    {"kind": "rolling", "version": "v1.7.1", "commit": C, "halt_height": None},
    {"kind": "upgrade", "version": "v1.8.0", "commit": C, "halt_height": 30, "halt_min_version": "v1.8.0"},
]
PENDING = [{"kind": "upgrade", "version": "v1.9.0", "commit": C, "halt_height": 45, "halt_min_version": "v1.9.0"}]
CHAIN = {"tip": 35, "halts": [[10, "v1.6.0"], [20, "v1.7.0"], [30, "v1.8.0"], [45, "v1.9.0"]],
         "first_block": {"v1.8.0": 31, "v1.9.0": 46}}
ALL = ["v1.5.0", "v1.6.0", "v1.7.0", "v1.7.1", "v1.8.0", "v1.9.0"]


def started_versions(log):
    return [line.split("starting ")[1].split(" ")[0] for line in log.splitlines() if "gno-runner: starting " in line]


class RunnerFromGenesis(unittest.TestCase):
    def test_replays_every_halt_and_switches_at_the_tip(self):
        n = TestNode(LEDGER, PENDING, CHAIN, ALL)
        try:
            p = n.start_runner()
            ok = n.wait_for(lambda: n.height() >= 48, timeout=90)
            rc = n.stop(p)
            log = n.log_text()
            self.assertTrue(ok, "node did not get past the tip halt:\n" + log[-3000:])
            self.assertEqual(rc, 0, log[-2000:])
            # one version per range; v1.7.1 (rolling) preferred over v1.7.0; pending v1.9.0 after the tip halt
            self.assertEqual(started_versions(log), ["v1.5.0", "v1.6.0", "v1.7.1", "v1.8.0", "v1.9.0"])
            self.assertIn("stopping the node (halted at block 45)", log)    # tip halt: process had to be stopped
            with open(os.path.join(n.root, "state", "runner.json")) as f:
                st = json.load(f)
            self.assertEqual(st["version"], "v1.9.0")
            self.assertEqual(st["last_halt"], 45)
        finally:
            n.cleanup()

    def test_explicit_replay_plan(self):
        # Like Onyx's notes: one rolling binary replays the early ranges, then the ledger rule.
        chain = dict(CHAIN, halts=[[10, ""], [20, ""], [30, "v1.8.0"], [45, "v1.9.0"]])
        n = TestNode(LEDGER, PENDING, chain, ALL, plan="1:v1.7.1, 31:v1.8.0")
        try:
            p = n.start_runner()
            ok = n.wait_for(lambda: n.height() >= 47, timeout=90)
            n.stop(p)
            log = n.log_text()
            self.assertTrue(ok, log[-3000:])
            self.assertEqual(started_versions(log), ["v1.7.1", "v1.7.1", "v1.7.1", "v1.8.0", "v1.9.0"])
        finally:
            n.cleanup()


class RunnerRefusals(unittest.TestCase):
    def test_missing_binary_stops_with_a_clear_message(self):
        n = TestNode(LEDGER, PENDING, CHAIN, ["v1.5.0", "v1.6.0"])
        try:
            p = n.start_runner()
            rc = p.wait(timeout=60)
            log = n.log_text()
            self.assertEqual(rc, EXIT_STOP)
            self.assertIn("block 21 needs one of v1.7.0, v1.7.1", log)
        finally:
            n.cleanup()

    def test_wrong_binary_for_range(self):
        # No version floors, so the startup gates let v1.8.0 start; it then fails on the first block.
        chain = dict(CHAIN, halts=[[10, ""], [20, ""], [30, ""]])
        n = TestNode(LEDGER, PENDING, chain, ALL, plan="1:v1.8.0")
        try:
            p = n.start_runner()
            rc = p.wait(timeout=60)
            self.assertEqual(rc, EXIT_STOP)
            self.assertIn("wrong AppHash", n.log_text())
        finally:
            n.cleanup()

    def test_pending_with_wrong_halt_is_never_started(self):
        # Pending entry recorded with the wrong halt height (38 instead of 45). At height 40 the chain's
        # params still show the previous halt (30, v1.8.0), so the node's own gate would let v1.9.0 start.
        # The runner must refuse it because the chain does not confirm a halt at 38.
        bad = [dict(PENDING[0], halt_height=38)]
        chain = dict(CHAIN, halts=[[10, "v1.6.0"], [20, "v1.7.0"], [30, "v1.8.0"], [45, "v1.9.0", 44]])
        n = TestNode(LEDGER, bad, chain, ALL)
        try:
            n.set_data_height(40)
            os.makedirs(os.path.join(n.root, "state"))
            with open(os.path.join(n.root, "state", "runner.json"), "w") as f:
                json.dump({"height": 40}, f)
            p = n.start_runner()
            rc = p.wait(timeout=60)
            log = n.log_text()
            self.assertEqual(rc, EXIT_STOP)
            self.assertIn("does not confirm halt 38", log)
            self.assertNotIn("starting v1.9.0", log)
        finally:
            n.cleanup()

    def test_too_new_binary_is_refused_and_runner_stops(self):
        # The chain already confirms the halt at 45 (cached from an earlier run), but the runner's height
        # record is behind it: the node refuses v1.9.0 before its halt and the runner stops.
        n = TestNode(LEDGER, PENDING, CHAIN, ["v1.9.0"])
        try:
            n.set_data_height(40)
            os.makedirs(os.path.join(n.root, "state"))
            with open(os.path.join(n.root, "state", "runner.json"), "w") as f:
                json.dump({"height": 45, "chain_halt": [45, "v1.9.0"]}, f)
            p = n.start_runner()
            rc = p.wait(timeout=60)
            log = n.log_text()
            self.assertEqual(rc, EXIT_STOP)
            self.assertIn("v1.9.0 is meant for after the halt at 45; the chain is at 40", log)
            self.assertIn("block 41 needs one of v1.8.0", log)
        finally:
            n.cleanup()

    def test_unknown_halt_reports_floor(self):
        # The chain halted at 45 for v1.9.0 but nothing tells the runner about v1.9.0.
        n = TestNode(LEDGER, [], CHAIN, ALL)
        try:
            n.set_data_height(45)
            os.makedirs(os.path.join(n.root, "state"))
            with open(os.path.join(n.root, "state", "runner.json"), "w") as f:
                json.dump({"height": 45}, f)
            p = n.start_runner()
            rc = p.wait(timeout=60)
            self.assertEqual(rc, EXIT_STOP)
            self.assertIn("below the governance minimum v1.9.0", n.log_text())
        finally:
            n.cleanup()

    def test_existing_data_without_state(self):
        n = TestNode(LEDGER, PENDING, CHAIN, ALL)
        try:
            os.makedirs(os.path.join(n.home, "gnoland-data", "db"))
            open(os.path.join(n.home, "gnoland-data", "db", "000001.log"), "w").close()
            p = n.start_runner()
            self.assertEqual(p.wait(timeout=30), EXIT_STOP)
            self.assertIn("state --set-height", n.log_text())
        finally:
            n.cleanup()

    def test_tampered_binary_is_not_started(self):
        n = TestNode(LEDGER, PENDING, CHAIN, ALL)
        try:
            with open(os.path.join(n.root, "versions", "v1.5.0", "gnoland"), "a") as f:
                f.write("\n# changed\n")
            p = n.start_runner()
            self.assertEqual(p.wait(timeout=30), EXIT_STOP)
            self.assertIn("block 1 needs one of v1.5.0", n.log_text())   # broken binary is not counted as installed
        finally:
            n.cleanup()

    def test_refuses_when_a_node_already_answers(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        n = TestNode(LEDGER, PENDING, CHAIN, ALL)

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                b = b'{"result":{"sync_info":{"latest_block_height":"7","catching_up":false}}}'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", n.port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            p = n.start_runner()
            self.assertEqual(p.wait(timeout=30), EXIT_STOP)
            self.assertIn("a node already answers", n.log_text())
        finally:
            srv.shutdown()
            srv.server_close()
            n.cleanup()

    def test_second_runner_is_refused(self):
        n = TestNode(LEDGER, PENDING, dict(CHAIN, sync_interval=0.05), ALL)
        try:
            p1 = n.start_runner()
            self.assertTrue(n.wait_for(lambda: n.height() >= 2, timeout=30))
            log1 = os.path.join(n.dir, "runner.log")
            os.rename(log1, log1 + ".1")
            p2 = n.start_runner()
            self.assertEqual(p2.wait(timeout=30), EXIT_STOP)
            self.assertIn("another runner holds the lock", n.log_text())
            n.stop(p1)
        finally:
            n.cleanup()


class RunnerRobustness(unittest.TestCase):
    def test_non_utf8_and_heavy_output_does_not_freeze_the_node(self):
        # 3,000 lines of invalid UTF-8 at start (about 0.7 MB) plus 50 lines per block: the reader must keep
        # draining the pipe, or the node blocks on a full pipe and stops making blocks.
        ledger = [LEDGER[0]]
        chain = {"tip": 35, "halts": [], "noise_lines": 3000, "noise_per_block": 50}
        n = TestNode(ledger, [], chain, ["v1.5.0"])
        try:
            p = n.start_runner()
            ok = n.wait_for(lambda: n.height() >= 40, timeout=60)
            n.stop(p)
            self.assertTrue(ok, "node stopped at height %d" % n.height())
        finally:
            n.cleanup()

    def test_tip_halt_without_a_log_line_is_seen_from_the_height(self):
        ledger = [LEDGER[0], {"kind": "upgrade", "version": "v1.6.0", "commit": C, "halt_height": 40,
                              "halt_min_version": "v1.6.0"}]
        chain = {"tip": 35, "halts": [[40, "v1.6.0"]], "quiet_halt": True}
        n = TestNode(ledger, [], chain, ["v1.5.0", "v1.6.0"], runner_extra={"halt_stall_seconds": 2})
        try:
            p = n.start_runner()
            ok = n.wait_for(lambda: n.height() >= 43, timeout=60)
            n.stop(p)
            log = n.log_text()
            self.assertTrue(ok, log[-3000:])
            self.assertIn("no halt line seen, but the height has stayed at the known halt height 40", log)
            self.assertEqual(started_versions(log), ["v1.5.0", "v1.6.0"])
        finally:
            n.cleanup()

    def test_broken_ledger_at_a_switch_stops_cleanly(self):
        n = TestNode(LEDGER, PENDING, dict(CHAIN, sync_interval=0.05), ALL)
        try:
            p = n.start_runner()
            self.assertTrue(n.wait_for(lambda: n.height() >= 12, timeout=30))
            with open(n.ledger, "w") as f:
                f.write("{not json")
            rc = p.wait(timeout=60)
            self.assertEqual(rc, EXIT_STOP)
            self.assertIn("cannot read the ledger", n.log_text())
            self.assertTrue(wait_port_closed(n.port), "a node is still answering after the runner stopped")
        finally:
            n.cleanup()


if __name__ == "__main__":
    unittest.main()
