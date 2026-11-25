"""Small read-only client for a gno.land (Tendermint2) RPC endpoint."""

import base64
import json
import urllib.parse
import urllib.request


class RPCError(Exception):
    pass


def _get(base, path, timeout):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, headers={"User-Agent": "gno-node-tools"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # connection refused, timeout, bad JSON
        raise RPCError("%s: %s" % (url, e))
    if body.get("error"):
        raise RPCError("%s: %s" % (url, body["error"]))
    return body.get("result") or {}


def status(base, timeout=6):
    r = _get(base, "/status", timeout)
    s = r.get("sync_info") or {}
    try:
        height = int(s.get("latest_block_height"))
    except (TypeError, ValueError):
        raise RPCError("status has no block height")
    return {
        "height": height,
        "catching_up": bool(s.get("catching_up")),
        "block_time": s.get("latest_block_time") or "",
        "build_version": r.get("build_version") or "",
        "network": (r.get("node_info") or {}).get("network") or "",
    }


def peers(base, timeout=6):
    r = _get(base, "/net_info", timeout)
    try:
        return int(r.get("n_peers"))
    except (TypeError, ValueError):
        return None


def block_time(base, height, timeout=10):
    r = _get(base, "/block?height=%d" % height, timeout)
    return ((r.get("block") or {}).get("header") or {}).get("time") or ""


def query_param(base, key, timeout=8):
    """Read a chain param such as 'node:p:halt_height'. Returns the decoded JSON value, or None if unset."""
    path = "/abci_query?path=" + urllib.parse.quote('"params/%s"' % key)
    r = _get(base, path, timeout)
    resp = (r.get("response") or {}).get("ResponseBase") or {}
    if resp.get("Error"):
        raise RPCError("param %s: %s" % (key, resp.get("Error")))
    data = resp.get("Data")
    if not data:
        return None
    raw = base64.b64decode(data).decode("utf-8")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def scheduled_halt(base, timeout=8):
    """Return (halt_height:int, halt_min_version:str) from chain state. (0, '') if none is set."""
    h = query_param(base, "node:p:halt_height", timeout)
    v = query_param(base, "node:p:halt_min_version", timeout)
    try:
        h = int(h) if h not in (None, "") else 0
    except (TypeError, ValueError):
        h = 0
    return h, (v or "")
