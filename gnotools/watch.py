"""gno-tools watch: check the node and alert only when something is definitely wrong.

A problem must be seen on `confirm_checks` checks in a row before one alert goes out
(a stalled height is the exception: it already takes `stall_minutes` to count as a problem,
and it is not alerted when the reference node has stopped at the same height, as every node
does during a coordinated upgrade halt).
An open problem is repeated every `repeat_hours`. Nothing is sent when things are fine,
when the watch starts, or when a problem clears (unless `send_resolved = true`).
Run it from a systemd timer (examples/systemd/gno-watch.timer) or cron.
"""

import json
import os
import subprocess
import time

from . import notify, rpc
from .checks import run_checks


def _state_file(cfg):
    return os.path.join(cfg.state_dir, "watch.json")


def _load(cfg):
    try:
        with open(_state_file(cfg), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen": {}, "alerted": {}, "h": None, "h_t": None}


def _save(cfg, st):
    os.makedirs(cfg.state_dir, exist_ok=True)
    tmp = _state_file(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, _state_file(cfg))


def problems(cfg, st, now):
    """Return {key: text} of problems found in this check."""
    found = {}
    unit = cfg.get("watch", "service")
    if unit:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        state = (r.stdout or "").strip() or "unknown"
        if state != "active":
            found["service"] = "service %s is %s" % (unit, state)
    try:
        status = rpc.status(cfg.get("node", "rpc"))
    except rpc.RPCError:
        status = None
    for res in run_checks(cfg, local_status=status):
        if res.level == "fail":
            found[res.key] = res.text
        elif res.level == "warn" and res.key in ("lag", "peers"):
            # lag only counts when the node is not still catching up from far behind
            if res.key == "lag" and status and status["catching_up"]:
                continue
            found[res.key] = res.text
    if status:
        h = status["height"]
        if h != st.get("h"):
            st["h"], st["h_t"] = h, now
        elif st.get("h_t") and now - st["h_t"] >= 60 * cfg.getint("watch", "stall_minutes"):
            mins = (now - st["h_t"]) // 60
            ref_h = _reference_height(cfg)
            if ref_h is not None and ref_h <= h:
                # The whole network is stopped (for example at a coordinated upgrade halt): every node
                # stalls together, so this node is not at fault. Logged, not alerted.
                st["network_stall"] = "network stopped at %d for %d min (reference node at %d)" % (h, mins, ref_h)
            else:
                where = " (a governance halt height)" if _is_halt(cfg, h) else ""
                ahead = "; the reference node is at %d" % ref_h if ref_h is not None else ""
                found["stall"] = "block height stuck at %d%s for %d min%s" % (h, where, mins, ahead)
                st.pop("network_stall", None)
    return found, status


def _reference_height(cfg):
    ref = cfg.get("watch", "reference_rpc")
    if not ref:
        return None
    try:
        return rpc.status(ref)["height"]
    except rpc.RPCError:
        return None


def _is_halt(cfg, h):
    try:
        return h in cfg.load_ledger().halt_heights()
    except Exception:
        return False


def check_once(cfg, now=None, send=None, log=print):
    now = int(now or time.time())
    send = send or (lambda text: notify.send(cfg, text, log=log))
    st = _load(cfg)
    st.setdefault("seen", {})
    st.setdefault("alerted", {})
    found, status = problems(cfg, st, now)
    confirm = cfg.getint("watch", "confirm_checks")
    repeat = 3600 * cfg.getint("watch", "repeat_hours")
    resolved = cfg.get("watch", "send_resolved").lower() == "true"
    info = ("height %d, binary %s" % (status["height"], status["build_version"] or "?")) if status else "node not answering"
    msgs = []
    for k, text in found.items():
        st["seen"][k] = st["seen"].get(k, 0) + 1
        a = st["alerted"].get(k)
        if a is None and (k == "stall" or st["seen"][k] >= confirm):
            st["alerted"][k] = {"since": now, "sent": now}
            msgs.append("PROBLEM: %s (seen %d checks in a row). %s" % (text, st["seen"][k], info))
        elif a is not None and now - a["sent"] >= repeat:
            a["sent"] = now
            msgs.append("STILL WRONG: %s (for %d h). %s" % (text, (now - a["since"]) // 3600, info))
    for k in [k for k in list(st["seen"]) if k not in found]:
        if resolved and k in st["alerted"]:
            msgs.append("RESOLVED: %s. %s" % (k, info))
        st["seen"].pop(k, None)
        st["alerted"].pop(k, None)
    for m in msgs:
        log(m)
        send(m)
    st["last"] = now
    _save(cfg, st)
    return msgs, found
