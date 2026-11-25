"""Configuration: one INI file per node. See examples/onyx.ini."""

import configparser
import os
import platform

DEFAULTS = {
    "node": {
        "chain_id": "",
        "ledger": "",
        "pending": "",            # local file with upgrades the official ledger does not list yet
        "root": "/opt/gno",      # versions/<tag>/gnoland and versions/<tag>/gno live here
        "home": "/var/lib/gnoland",  # working directory; gnoland-data/ sits inside it
        "genesis": "",
        "genesis_url": "",
        "gno_repo": "https://github.com/gnolang/gno.git",
        "rpc": "http://127.0.0.1:26657",
        "start_args": "--chainid {chain_id} --genesis {genesis}",
        "platform": "",           # linux/amd64 or linux/arm64; detected when empty
        "plan": "",               # optional explicit replay plan, e.g. 1:v1.7.1-rc.1, 1203401:v1.8.0-rc.1
        "state_dir": "",          # default: <root>/state
    },
    "runner": {
        "poll_seconds": "5",
        "halt_grace_seconds": "20",   # after the halt line, give the node this long before stopping it
        "stop_timeout_seconds": "90",
        "min_run_seconds": "30",      # a child that dies faster than this without progress ends the runner
        "max_switches": "50",
    },
    "watch": {
        "reference_rpc": "",
        "every_seconds": "300",
        "confirm_checks": "3",
        "stall_minutes": "15",
        "max_lag_blocks": "30",
        "min_peers": "3",
        "disk_percent": "90",
        "disk_path": "",              # default: node home
        "repeat_hours": "12",
        "send_resolved": "false",
        "service": "",                # systemd unit to check, e.g. gnoland-onyx
        "halt_warn_blocks": "20000",  # warn about a scheduled halt this many blocks ahead without a binary
    },
    "notify": {
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "discord_webhook": "",
        "webhook_url": "",
        "prefix": "[gno-node]",
    },
}


class Config:
    def __init__(self, path=None, overrides=None):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        if path:
            with open(path, encoding="utf-8") as f:
                cp.read_file(f)
        for (section, key), value in (overrides or {}).items():
            cp.set(section, key, str(value))
        self.cp = cp
        self.path = path

    def get(self, section, key):
        return self.cp.get(section, key).strip()

    def getint(self, section, key):
        return int(self.get(section, key))

    @property
    def root(self):
        return self.get("node", "root")

    @property
    def home(self):
        return self.get("node", "home")

    @property
    def state_dir(self):
        return self.get("node", "state_dir") or os.path.join(self.root, "state")

    @property
    def versions_dir(self):
        return os.path.join(self.root, "versions")

    @property
    def platform(self):
        p = self.get("node", "platform")
        if p:
            return p
        m = platform.machine().lower()
        arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)
        return "%s/%s" % (platform.system().lower(), arch)

    def start_args(self):
        return self.get("node", "start_args").format(
            chain_id=self.get("node", "chain_id"),
            genesis=self.get("node", "genesis"),
            home=self.home,
            root=self.root,
        )
