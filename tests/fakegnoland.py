#!/usr/bin/env python3
"""A stand-in for `gnoland` used only by the tests.

It imitates the parts of gnoland the runner depends on, with the same log wording
(checked against gno's chain/mainnet branch):
  * the startup version gates (gno.land/pkg/gnoland/node_params.go);
  * governance halts that fire during replay and at the tip (tm2 BaseApp halt + EndBlocker);
  * exit code 2 when a halt fires while syncing, and an idle process when it fires at the tip;
  * "wrong Block.Header.AppHash" when a version is used outside the blocks it can process;
  * a /status and /abci_query RPC (height, catching_up, build_version, the halt params).

Halt params follow the real chain: a halt becomes visible in state only once its GovDAO
proposal has executed ("proposed_at", default 5 blocks before the halt, never before the
previous halt), and the params are never cleared, so between halts they still show the
previous halt.

Chain description: JSON file in $FAKE_CHAIN. Node data (its height): JSON file in $FAKE_DATA.
The version is read from a VERSION file next to this script.
Test switches in the chain file: "noise_lines" (lines of non-UTF-8 output printed at start),
"quiet_halt" (at the tip, halt without printing the halt line), "noise_per_block".
"""

import base64
import json
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(sys.argv[0]))
VERSION = open(os.path.join(HERE, "VERSION")).read().strip()


def vkey(v):
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", v)
    a = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return a + ((1,) if m.group(4) is None else (0,))


def meets(v, floor):
    return not floor or vkey(v) >= vkey(floor)


def halts_of(chain):
    out, prev = [], 0
    for item in chain["halts"]:
        x, f = int(item[0]), item[1]
        proposed = int(item[2]) if len(item) > 2 else max(prev + 1, x - 5)
        out.append((x, f, proposed))
        prev = x
    return out


def params_at(halts, h):
    """The halt params in committed state at height h: the last halt whose proposal executed."""
    hh, floor = 0, ""
    for x, f, proposed in halts:
        if proposed <= h:
            hh, floor = x, f
    return hh, floor


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "version":
        print("gnoland version: %s" % VERSION)
        return 0
    chain = json.load(open(os.environ["FAKE_CHAIN"]))
    data_f = os.environ["FAKE_DATA"]
    try:
        h = json.load(open(data_f))["height"]
    except (OSError, ValueError, KeyError):
        h = 0
    halts = halts_of(chain)
    tip = chain["tip"]
    out = sys.stdout.buffer

    hh, floor = params_at(halts, h)
    if hh and floor:
        if h >= hh and not meets(VERSION, floor):
            print('Error: unable to create the Gnoland app, binary version "%s" does not meet the minimum '
                  'version "%s" required by governance; please upgrade to a compatible binary before restarting'
                  % (VERSION, floor), flush=True)
            return 1
        if h < hh and meets(VERSION, floor):
            print('Error: unable to create the Gnoland app, binary version "%s" is an upgrade intended for halt '
                  'height %d, but the chain is at height %d; please use the previous binary until the halt, '
                  'or set skip_upgrade_height = %d in config.toml if you have already migrated'
                  % (VERSION, hh, h, hh), flush=True)
            return 1

    first_block = chain.get("first_block", {}).get(VERSION, 1)
    state = {"h": h, "stop": False}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/abci_query":
                key = unquote(parse_qs(u.query).get("path", [""])[0]).strip('"')
                ph, pf = params_at(halts, state["h"])
                val = None
                if key == "params/node:p:halt_height" and ph:
                    val = json.dumps(str(ph))
                elif key == "params/node:p:halt_min_version" and pf:
                    val = json.dumps(pf)
                data = base64.b64encode(val.encode()).decode() if val else None
                res = {"response": {"ResponseBase": {"Error": None, "Data": data}}}
            else:
                res = {"node_info": {"network": chain.get("chain_id", "test-1")},
                       "build_version": VERSION,
                       "sync_info": {"latest_block_height": str(state["h"]),
                                     "latest_block_time": "2026-11-25T10:00:00Z",
                                     "catching_up": state["h"] < tip}}
            body = json.dumps({"jsonrpc": "2.0", "id": "", "result": res}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", int(chain["rpc_port"])), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def on_term(*a):
        state["stop"] = True
    signal.signal(signal.SIGTERM, on_term)
    print("starting node %s at height %d" % (VERSION, h), flush=True)
    for i in range(int(chain.get("noise_lines", 0))):
        out.write(b"debug \xff\xfe not utf-8 " + (b"x" * 200) + b" %d\n" % i)
    out.flush()

    armed = None
    while not state["stop"]:
        nxt = state["h"] + 1
        if armed is not None:
            msg = "halt height %d reached, node shutting down" % armed
            if state["h"] < tip:
                print("panic: " + msg, flush=True)
                srv.shutdown()
                return 2
            if not chain.get("quiet_halt"):
                print('CONSENSUS FAILURE!!!  err="%s"' % msg, flush=True)
            while not state["stop"]:       # the process stays up at the tip
                time.sleep(0.05)
            break
        if nxt < first_block:
            time.sleep(0.05)
            print("panic: wrong Block.Header.AppHash at height %d" % nxt, flush=True)
            srv.shutdown()
            return 1
        time.sleep(chain["sync_interval"] if state["h"] < tip else chain["tip_interval"])
        state["h"] = nxt
        json.dump({"height": nxt}, open(data_f + ".tmp", "w"))
        os.replace(data_f + ".tmp", data_f)
        for i in range(int(chain.get("noise_per_block", 0))):
            out.write(b"debug \xc3\x28 block %d line %d " % (nxt, i) + (b"y" * 300) + b"\n")
        out.flush()
        for x, _f, _p in halts:
            if x == nxt:
                print("GovDAO halt height reached, will halt after this block height=%d halt_height=%d" % (x, x),
                      flush=True)
                armed = x
    srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
