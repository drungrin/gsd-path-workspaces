"""Shared fixtures: a GSD Path source checkout, a synthetic release, and member origins.

Member remotes keep forge URLs (`https://github.com/acme/<name>.git`); Git
reaches them through local bare repositories by an environment-level
`url.<base>.insteadOf` rewrite, so the GSD Path member checks see a GitHub
origin while nothing leaves the machine.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

SOURCE = Path(os.environ.get("GSD_PATH_SOURCE", REPO.parent / "gsd-path")).resolve()
if not (SOURCE / "scripts" / "isolation.py").is_file():
    raise unittest.SkipTest(f"GSD Path source checkout not found: {SOURCE}; set GSD_PATH_SOURCE")
if str(SOURCE) not in sys.path:
    sys.path.insert(1, str(SOURCE))

FORGE = "https://github.com/acme/"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, encoding="utf-8", errors="replace",
                          capture_output=True, check=False)


def forge_url(name):
    return f"{FORGE}{name}.git"


class SandboxTestCase(unittest.TestCase):
    """A temporary root with member origins, a release record, and an isolated Git setup."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.remotes = self.base / "remotes"
        self.remotes.mkdir()
        environment = mock.patch.dict(os.environ, {
            "GSD_PATH_WORKTREE_ROOT": str(self.base / "gsd-path-state"),
            "GIT_AUTHOR_NAME": "Workspaces", "GIT_AUTHOR_EMAIL": "workspaces@example.invalid",
            "GIT_COMMITTER_NAME": "Workspaces", "GIT_COMMITTER_EMAIL": "workspaces@example.invalid",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.remotes.as_posix()}/.insteadOf",
            "GIT_CONFIG_VALUE_0": FORGE,
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.release_file = self.base / "fork-release.json"
        commit = git(SOURCE, "rev-parse", "HEAD").stdout.strip()
        version = json.loads(git(SOURCE, "show", f"{commit}:package.json").stdout)["version"]
        self.release_file.write_text(json.dumps({
            "schema": "gsd-path/fork-release/v1", "checkout": str(SOURCE), "commit": commit,
            "source": "https://example.invalid/gsd-path", "version": version,
            "announced_at": "2026-11-12T00:00:00Z"}), encoding="utf-8")
        self.version = version

    def member_origin(self, name, files=None, default="main"):
        """A bare origin for `name` seeded with one commit; returns its forge URL."""
        bare = self.remotes / f"{name}.git"
        seed = self.base / f"seed-{name}"
        self.assertEqual(git(self.base, "init", "--bare", "-q", "-b", default, str(bare)).returncode, 0)
        git(self.base, "init", "-q", "-b", default, str(seed))
        for relative, content in (files or {"README.md": f"{name}\n"}).items():
            path = seed / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", f"{name}: seed")
        self.assertEqual(git(seed, "push", "-q", str(bare), default).returncode, 0)
        return forge_url(name)

    def bare(self, name):
        return self.remotes / f"{name}.git"
