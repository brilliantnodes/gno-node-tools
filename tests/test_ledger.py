import os
import unittest

from gnotools.ledger import (Ledger, LedgerError, choose_version, meets_min_version, parse_plan,
                             parse_version)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class Versions(unittest.TestCase):
    def test_order(self):
        order = ["v1.2.0", "v1.7.0-rc.1", "v1.7.0-rc.2", "v1.7.0", "v1.7.1-rc.1", "v1.7.1", "v1.8.0-rc.1",
                 "v1.8.0", "v1.10.0"]
        keys = [parse_version(v) for v in order]
        self.assertEqual(keys, sorted(keys))

    def test_floor(self):
        self.assertTrue(meets_min_version("v1.8.0", "v1.8.0-rc.1"))
        self.assertFalse(meets_min_version("v1.8.0-rc.1", "v1.8.0"))   # a candidate does not meet its final
        self.assertTrue(meets_min_version("v1.7.1-rc.1", "v1.7.0-rc.1"))
        self.assertTrue(meets_min_version("v1.0.0", ""))
        self.assertFalse(meets_min_version("develop", "v1.0.0"))        # unparseable: exact match only
        self.assertTrue(meets_min_version("develop", "develop"))


class OnyxLedger(unittest.TestCase):
    def setUp(self):
        self.l = Ledger.load(os.path.join(FIX, "onyx-1.upgrades.json"))
        self.all = {e["version"] for e in self.l.entries}

    def test_ranges(self):
        got = [(e.start, e.end, e.versions) for e in self.l.eras]
        self.assertEqual(got, [
            (1, 445000, ["v1.5.0", "v1.5.1-rc.1"]),
            (445001, 754000, ["v1.6.0-rc.1"]),
            (754001, 1203400, ["v1.7.0-rc.1", "v1.7.1-rc.1"]),
            (1203401, None, ["v1.8.0-rc.1"]),
        ])

    def test_choose(self):
        self.assertEqual(choose_version(self.l, 1, self.all)[0], "v1.5.1-rc.1")
        self.assertEqual(choose_version(self.l, 445000, self.all)[0], "v1.5.1-rc.1")
        self.assertEqual(choose_version(self.l, 445001, self.all)[0], "v1.6.0-rc.1")
        self.assertEqual(choose_version(self.l, 1203400, self.all)[0], "v1.7.1-rc.1")
        self.assertEqual(choose_version(self.l, 1203401, self.all)[0], "v1.8.0-rc.1")
        self.assertEqual(choose_version(self.l, 1399999, self.all)[0], "v1.8.0-rc.1")

    def test_choose_falls_back_within_range(self):
        self.assertEqual(choose_version(self.l, 900000, {"v1.7.0-rc.1"})[0], "v1.7.0-rc.1")
        v, why = choose_version(self.l, 900000, {"v1.8.0-rc.1"})
        self.assertIsNone(v)
        self.assertIn("v1.7.1-rc.1", why)

    def test_plan_matches_onyx_notes(self):
        # Onyx's UPGRADES.md: join from genesis on v1.7.1-rc.1, then v1.8.0-rc.1 from its halt.
        plan = parse_plan("1:v1.7.1-rc.1, 1203401:v1.8.0-rc.1")
        self.assertEqual(choose_version(self.l, 5, self.all, plan)[0], "v1.7.1-rc.1")
        self.assertEqual(choose_version(self.l, 500000, self.all, plan)[0], "v1.7.1-rc.1")
        self.assertEqual(choose_version(self.l, 1203401, self.all, plan)[0], "v1.8.0-rc.1")

    def test_pending_after_plan_uses_ledger_rule(self):
        pend = [{"version": "v1.9.0-rc.1", "commit": "a" * 40, "halt_height": 1500000,
                 "halt_min_version": "v1.9.0-rc.1", "binaries": {}}]
        import json
        with open(os.path.join(FIX, "onyx-1.upgrades.json")) as f:
            l = Ledger(json.load(f), pend)
        plan = parse_plan("1:v1.7.1-rc.1, 1203401:v1.8.0-rc.1")
        inst = self.all | {"v1.9.0-rc.1"}
        self.assertEqual(choose_version(l, 1500000, inst, plan)[0], "v1.8.0-rc.1")
        self.assertEqual(choose_version(l, 1500001, inst, plan)[0], "v1.9.0-rc.1")
        self.assertTrue(l.entry("v1.9.0-rc.1")["pending"])

    def test_pending_ignored_once_official(self):
        import json
        with open(os.path.join(FIX, "onyx-1.upgrades.json")) as f:
            data = json.load(f)
        l = Ledger(data, [{"version": "v1.8.0-rc.1", "halt_height": 999, "commit": "b" * 40}])
        self.assertEqual(l.eras[-1].start, 1203401)

    def test_next_halt(self):
        self.assertEqual(self.l.next_halt_after(1), 445000)
        self.assertEqual(self.l.next_halt_after(1203401), None)


class MainnetLedger(unittest.TestCase):
    def test_ranges(self):
        l = Ledger.load(os.path.join(FIX, "gnoland-1.upgrades.json"))
        self.assertEqual(l.chain_id, "gnoland-1")
        got = [(e.start, e.end, e.versions[-1]) for e in l.eras]
        self.assertEqual(got, [
            (1, 36300, "v1.2.0"), (36301, 113000, "v1.3.0"), (113001, 162200, "v1.4.0"),
            (162201, 1183000, "v1.5.1"), (1183001, 1651500, "v1.7.1"), (1651501, None, "v1.8.0")])


class Invalid(unittest.TestCase):
    def base(self):
        return {"chain_id": "x", "genesis_sha256": "0" * 64, "upgrades": [
            {"kind": "genesis", "version": "v1.0.0", "commit": "c" * 40, "halt_height": None}]}

    def test_must_start_with_genesis(self):
        d = self.base()
        d["upgrades"][0]["kind"] = "upgrade"
        with self.assertRaises(LedgerError):
            Ledger(d)

    def test_heights_increase(self):
        d = self.base()
        d["upgrades"] += [{"kind": "upgrade", "version": "v1.1.0", "halt_height": 100},
                          {"kind": "upgrade", "version": "v1.2.0", "halt_height": 50}]
        with self.assertRaises(LedgerError):
            Ledger(d)

    def test_versions_increase(self):
        d = self.base()
        d["upgrades"] += [{"kind": "rolling", "version": "v0.9.0", "halt_height": None}]
        with self.assertRaises(LedgerError):
            Ledger(d)

    def test_plan_must_start_at_one(self):
        with self.assertRaises(LedgerError):
            parse_plan("5:v1.0.0")


if __name__ == "__main__":
    unittest.main()
