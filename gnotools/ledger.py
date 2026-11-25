"""Read a gno.land upgrade ledger (misc/deployments/<chain>/upgrades.json) and work out
which binary serves which block.

The rule comes from the ledger itself (gno.land/pkg/upgrades): a version runs the blocks
from its own halt_height + 1 (1 for genesis) up to and including the next coordinated
upgrade's halt_height. A rolling release has no halt and serves the same blocks as the
entry before it.

An operator can add entries the official ledger does not have yet (an upgrade whose halt
has been voted but not reached) in a local "pending" file with the same fields.
"""

import json
import re
import urllib.request

_SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


class LedgerError(Exception):
    pass


def parse_version(v):
    """Return a sortable key for a vMAJOR.MINOR.PATCH[-pre] tag, or None if it does not parse.

    A pre-release sorts below its release (v1.8.0-rc.1 < v1.8.0), as semver says and as the
    node's own minimum-version check does.
    """
    m = _SEMVER.match(v or "")
    if not m:
        return None
    major, minor, patch, pre = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if pre is None:
        return (major, minor, patch, 1, ())
    ids = []
    for part in pre.split("."):
        ids.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return (major, minor, patch, 0, tuple(ids))


def meets_min_version(binary, floor):
    """True if `binary` satisfies `floor`. Unparseable versions only match exactly, like the node."""
    if not floor:
        return True
    a, b = parse_version(binary), parse_version(floor)
    if a is None or b is None:
        return binary == floor
    return a >= b


def load_json(source, timeout=30):
    """Load JSON from a local path or an https:// URL."""
    if source.startswith("https://") or source.startswith("http://"):
        req = urllib.request.Request(source, headers={"User-Agent": "gno-node-tools"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    with open(source, encoding="utf-8") as f:
        return json.load(f)


def binary_for(entry, platform):
    """Return (url, sha256) for a platform such as 'linux/amd64', or (None, None)."""
    url = (entry.get("binaries") or {}).get(platform)
    if not url:
        return None, None
    if "?checksum=sha256:" in url:
        base, ck = url.split("?checksum=sha256:", 1)
        return base, ck.lower()
    return url, (entry.get("sha256") or "").lower() or None


class Era:
    """A block range served by one coordinated release and any rolling patches on top of it."""

    def __init__(self, start, end, versions, halt_min_version=None):
        self.start = start            # first block of the range
        self.end = end                # last block (inclusive), or None if still open
        self.versions = versions      # oldest first
        self.halt_min_version = halt_min_version

    def contains(self, height):
        return height >= self.start and (self.end is None or height <= self.end)

    def __repr__(self):
        return "Era(%s..%s, %s)" % (self.start, self.end if self.end is not None else "", ",".join(self.versions))


class Ledger:
    def __init__(self, data, pending=None):
        self.chain_id = data.get("chain_id")
        self.genesis_sha256 = (data.get("genesis_sha256") or "").lower()
        entries = list(data.get("upgrades") or [])
        known = {e.get("version") for e in entries}
        for p in pending or []:
            if p.get("version") not in known:   # once the official ledger has it, the ledger wins
                q = dict(p)
                q.setdefault("kind", "upgrade" if q.get("halt_height") else "rolling")
                q["pending"] = True
                entries.append(q)
        self.entries = entries
        self._validate()
        self.eras = self._eras()

    @classmethod
    def load(cls, source, pending_file=None):
        data = load_json(source)
        pending = []
        if pending_file:
            try:
                pending = load_json(pending_file).get("pending", [])
            except FileNotFoundError:
                pending = []
        return cls(data, pending)

    def _validate(self):
        if not self.entries:
            raise LedgerError("ledger has no entries")
        if self.entries[0].get("kind") != "genesis":
            raise LedgerError("the first ledger entry must be the genesis")
        last_v, last_h = None, 0
        for e in self.entries:
            v = e.get("version")
            if parse_version(v) is None:
                raise LedgerError("version %r does not parse as vMAJOR.MINOR.PATCH" % v)
            if last_v is not None and parse_version(v) <= parse_version(last_v):
                raise LedgerError("versions must strictly increase: %s after %s" % (v, last_v))
            kind = e.get("kind")
            if kind not in ("genesis", "upgrade", "rolling"):
                raise LedgerError("unknown kind %r for %s" % (kind, v))
            if kind == "genesis" and e is not self.entries[0]:
                raise LedgerError("only the first entry may be the genesis")
            if kind == "upgrade":
                h = e.get("halt_height")
                if not isinstance(h, int) or h <= last_h:
                    raise LedgerError("halt heights must strictly increase: %s at %r" % (v, h))
                last_h = h
            if kind == "rolling" and e.get("halt_height"):
                raise LedgerError("a rolling release has no halt height: %s" % v)
            last_v = v

    def _eras(self):
        eras = []
        for e in self.entries:
            kind = e["kind"]
            if kind == "rolling":
                eras[-1].versions.append(e["version"])
                continue
            start = 1 if kind == "genesis" else e["halt_height"] + 1
            if eras:
                eras[-1].end = start - 1
            eras.append(Era(start, None, [e["version"]], e.get("halt_min_version")))
        return eras

    def era_for(self, height):
        for era in self.eras:
            if era.contains(height):
                return era
        return None

    def entry(self, version):
        for e in self.entries:
            if e.get("version") == version:
                return e
        return None

    def next_halt_after(self, height):
        """The first known halt height at or after `height`, or None."""
        for era in self.eras:
            if era.end is not None and era.end >= height:
                return era.end
        return None


def parse_plan(text):
    """Parse an explicit replay plan such as '1:v1.7.1-rc.1, 1203401:v1.8.0-rc.1'.

    Returns a list of (start_height, version), sorted by height. An empty string means no plan.
    """
    plan = []
    for item in (text or "").replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise LedgerError("plan item %r must look like HEIGHT:VERSION" % item)
        h, v = item.split(":", 1)
        h, v = int(h.strip()), v.strip()
        if parse_version(v) is None:
            raise LedgerError("plan version %r does not parse" % v)
        plan.append((h, v))
    plan.sort()
    if plan and plan[0][0] != 1:
        raise LedgerError("a replay plan must start at height 1")
    return plan


def choose_version(ledger, next_height, installed, plan=None):
    """Pick the binary that must process block `next_height`.

    `installed` is a set of version tags whose binaries are present and verified.
    With an explicit plan, the plan decides; otherwise the newest installed version of the
    era that contains the block. Returns (version, reason) or (None, reason).
    """
    if plan:
        # The plan covers blocks up to the end of the era its last item starts in.
        # Later blocks (an upgrade added after the plan was written) fall back to the ledger rule.
        last_era = ledger.era_for(plan[-1][0])
        limit = last_era.end if last_era is not None else None
        chosen = None
        if limit is None or next_height <= limit:
            for start, v in plan:
                if start <= next_height:
                    chosen = v
        if chosen is not None:
            if chosen in installed:
                return chosen, "replay plan: %s serves block %d" % (chosen, next_height)
            return None, "replay plan wants %s for block %d, but it is not installed" % (chosen, next_height)
    era = ledger.era_for(next_height)
    if era is None:
        return None, "no ledger entry covers block %d" % next_height
    for v in reversed(era.versions):
        if v in installed:
            return v, "block %d is in the range %s; newest installed version of that range" % (next_height, _range(era))
    return None, "block %d needs one of %s (range %s); none is installed" % (
        next_height, ", ".join(era.versions), _range(era))


def _range(era):
    return "%d..%s" % (era.start, era.end if era.end is not None else "tip")
