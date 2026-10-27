"""Version information and the per-installation upgrade history.

The version number lives in airbridge/__init__.py (semantic versioning).
The exact build comes from git when running from a checkout, or from
version.json, which install.sh writes because /opt/airbridge has no .git.
Every time a different build starts, it's appended to
<data>/version-history.json, so the Pi keeps its own upgrade log.
"""
import json
import os
import subprocess
import time

from . import __version__

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _git(*args):
    try:
        r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def build_info():
    info = {"version": __version__, "commit": None, "describe": None, "commit_date": None,
            "dirty": False, "installed_at": None, "source": "unknown"}
    vf = os.path.join(ROOT, "version.json")
    if os.path.exists(vf):
        try:
            with open(vf) as f:
                info.update({k: v for k, v in json.load(f).items() if k != "version"})
            info["source"] = "install"
        except (OSError, ValueError):
            pass
    if os.path.isdir(os.path.join(ROOT, ".git")):
        commit = _git("rev-parse", "--short", "HEAD")
        if commit:
            info.update(commit=commit, describe=_git("describe", "--tags", "--always", "--dirty"),
                        commit_date=_git("show", "-s", "--format=%cI", "HEAD"),
                        dirty=bool(_git("status", "--porcelain", "--untracked-files=no")), source="git")
    return info


def label(info=None):
    info = info or build_info()
    s = f"v{info['version']}"
    if info.get("commit"):
        s += f" ({info['commit']}{'+' if info.get('dirty') else ''})"
    return s


class VersionHistory:
    def __init__(self, data_dir, keep=200):
        self.path = os.path.join(data_dir, "version-history.json")
        self.keep = keep

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def record(self, info, now=None):
        """Append this build if it differs from the last one started. Returns (previous, changed)."""
        hist = self.load()
        prev = hist[-1] if hist else None
        key = (info["version"], info.get("commit"))
        if prev and (prev.get("version"), prev.get("commit")) == key:
            return prev, False
        hist.append({"version": info["version"], "commit": info.get("commit"),
                     "describe": info.get("describe"), "started": now or time.time()})
        hist = hist[-self.keep:]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(hist, f, indent=1)
        os.replace(tmp, self.path)
        return prev, True
