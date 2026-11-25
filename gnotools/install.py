"""Install gnoland binaries from the ledger, each checked against its published SHA-256,
with GNOROOT checked out at the exact commit of that release.

Layout under <root>/versions/<tag>/:
    gnoland          the binary (mode 0755, only after its hash matched)
    gno/             GNOROOT: a git checkout of the release commit
    INSTALLED.json   what was installed, its hash, commit and time
"""

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

from .ledger import binary_for, load_json


class InstallError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url, dest, timeout=600):
    req = urllib.request.Request(url, headers={"User-Agent": "gno-node-tools"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def version_dir(cfg, version):
    return os.path.join(cfg.versions_dir, version)


def installed_versions(cfg, verify=True):
    """Return {version: info} for versions whose binary is present (and, with verify, still matches its hash)."""
    out = {}
    base = cfg.versions_dir
    if not os.path.isdir(base):
        return out
    for v in sorted(os.listdir(base)):
        meta = os.path.join(base, v, "INSTALLED.json")
        binp = os.path.join(base, v, "gnoland")
        if not (os.path.isfile(meta) and os.path.isfile(binp)):
            continue
        try:
            info = load_json(meta)
        except (OSError, ValueError):
            continue
        if verify and sha256_file(binp) != info.get("sha256"):
            info["broken"] = "binary hash no longer matches %s" % info.get("sha256")
        out[v] = info
    return out


def verified_set(cfg):
    return {v for v, i in installed_versions(cfg).items() if not i.get("broken")}


def _git(args, cwd=None):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise InstallError("git %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()[:300]))
    return r.stdout.strip()


def checkout_gnoroot(cfg, commit, dest):
    """Shallow checkout of one commit of the gno repo into dest; verified by commit hash."""
    if os.path.isdir(os.path.join(dest, ".git")):
        if _git(["rev-parse", "HEAD"], cwd=dest) == commit:
            return
        raise InstallError("%s holds a different commit; move it away first" % dest)
    os.makedirs(dest, exist_ok=True)
    _git(["init", "-q"], cwd=dest)
    _git(["remote", "add", "origin", cfg.get("node", "gno_repo")], cwd=dest)
    _git(["fetch", "-q", "--depth", "1", "origin", commit], cwd=dest)
    _git(["checkout", "-q", "FETCH_HEAD"], cwd=dest)
    got = _git(["rev-parse", "HEAD"], cwd=dest)
    if got != commit:
        raise InstallError("GNOROOT checkout is at %s, expected %s" % (got, commit))


def install_version(cfg, ledger, version, url=None, sha256=None, commit=None,
                    check_version=True, log=print):
    """Install one version. Values not given are taken from the ledger (or the pending file)."""
    entry = ledger.entry(version) if ledger else None
    if entry is None and not (url and sha256 and commit):
        raise InstallError("%s is not in the ledger; give --url, --sha256 and --commit" % version)
    if entry is not None:
        u, s = binary_for(entry, cfg.platform)
        url, sha256, commit = url or u, (sha256 or s), commit or entry.get("commit")
    if not url or not sha256:
        raise InstallError("no %s binary for %s in the ledger" % (cfg.platform, version))
    sha256 = sha256.lower()
    vdir = version_dir(cfg, version)
    os.makedirs(vdir, exist_ok=True)
    binp = os.path.join(vdir, "gnoland")

    if os.path.isfile(binp) and sha256_file(binp) == sha256:
        log("%s: binary already installed and matches %s" % (version, sha256[:16]))
    else:
        fd, tmp = tempfile.mkstemp(prefix=".gnoland-", dir=vdir)
        os.close(fd)
        try:
            log("%s: downloading %s" % (version, url))
            _download(url, tmp)
            got = sha256_file(tmp)
            if got != sha256:
                raise InstallError("%s: checksum mismatch: got %s, expected %s" % (version, got, sha256))
            os.chmod(tmp, 0o755)
            os.replace(tmp, binp)
            log("%s: binary checksum ok (%s)" % (version, sha256[:16]))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    if commit:
        checkout_gnoroot(cfg, commit, os.path.join(vdir, "gno"))
        log("%s: GNOROOT at commit %s" % (version, commit[:12]))

    reported = ""
    if check_version:
        env = dict(os.environ, GNOROOT=os.path.join(vdir, "gno"))
        r = subprocess.run([binp, "version"], capture_output=True, text=True, env=env, timeout=60)
        reported = (r.stdout + r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
        if version not in reported:
            raise InstallError("%s: `gnoland version` printed %r, not the expected tag" % (version, reported))
        log("%s: reports %s" % (version, reported))

    info = {
        "version": version,
        "sha256": sha256,
        "url": url,
        "commit": commit or "",
        "platform": cfg.platform,
        "reported_version": reported,
        "installed_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_json(os.path.join(vdir, "INSTALLED.json"), info)
    return info


def install_genesis(cfg, ledger, url=None, log=print):
    dest = cfg.get("node", "genesis")
    url = url or cfg.get("node", "genesis_url")
    want = ledger.genesis_sha256
    if not dest:
        raise InstallError("set [node] genesis to the path of genesis.json")
    if os.path.isfile(dest) and sha256_file(dest) == want:
        log("genesis already present and matches %s" % want[:16])
        return
    if not url:
        raise InstallError("set [node] genesis_url or pass --url")
    d = os.path.dirname(dest) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".genesis-", dir=d)
    os.close(fd)
    try:
        _download(url, tmp)
        got = sha256_file(tmp)
        if got != want:
            raise InstallError("genesis checksum mismatch: got %s, ledger says %s" % (got, want))
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        log("genesis checksum ok (%s)" % want[:16])
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def add_pending(cfg, version, halt_height, halt_min_version, commit, url, sha256, log=print):
    """Record an upgrade the official ledger does not list yet, so the runner can switch to it."""
    path = cfg.get("node", "pending")
    if not path:
        raise InstallError("set [node] pending to a file path first")
    data = {"pending": []}
    if os.path.isfile(path):
        data = load_json(path)
    items = [p for p in data.get("pending", []) if p.get("version") != version]
    entry = {
        "kind": "upgrade" if halt_height else "rolling",
        "version": version,
        "commit": commit,
        "halt_height": int(halt_height) if halt_height else None,
        "halt_min_version": halt_min_version or None,
        "binaries": {cfg.platform: "%s?checksum=sha256:%s" % (url, sha256.lower())},
    }
    items.append(entry)
    data["pending"] = items
    _write_json(path, data)
    log("pending %s recorded in %s (halt height %s)" % (version, path, halt_height or "none: rolling"))
    return entry


def _write_json(path, obj):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
