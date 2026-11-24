"""Git, process, and GSD Path release helpers."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .config import WorkspacesError

RELEASE_SCHEMA = "gsd-path/fork-release/v1"
# Ref prefixes a member reserves for a coordinator project, as in GSD Path
# members.member_ref_prefixes; the project is already a valid branch slug.
MEMBER_BRANCH_FORMATS = ("feature/{}-milestone-", "chore/task-{}-", "chore/verify-{}-",
                         "chore/integrate-{}-", "chore/rebase-{}-")
MEMBER_TAG_FORMATS = ("milestone/{}-", "milestone-source/{}-")


def run(command: Sequence[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(list(command), cwd=None if cwd is None else str(cwd), capture_output=True,
                            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise WorkspacesError(f"failed: {' '.join(map(str, command))}\n{output[-3000:]}")
    return result


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout


def python(script: Path, *args: str, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    return run([sys.executable, "-B", str(script), *args], cwd=cwd, check=check)


def remote_default_branch(remote: str) -> str:
    output = run(["git", "ls-remote", "--symref", remote, "HEAD"]).stdout
    for line in output.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return line[len("ref: refs/heads/"):-len("\tHEAD")]
    raise WorkspacesError(f"could not find the default branch of {remote}; pass --default-branch")


def project_refs(remote: str, project: str) -> list:
    """Refs on `remote` that already carry the coordinator project name."""
    output = run(["git", "ls-remote", "--heads", "--tags", remote]).stdout
    prefixes = tuple("refs/heads/" + item.format(project) for item in MEMBER_BRANCH_FORMATS) + tuple(
        "refs/tags/" + item.format(project) for item in MEMBER_TAG_FORMATS)
    refs = []
    for line in output.splitlines():
        _, _, ref = line.partition("\t")
        if ref.startswith(prefixes):
            refs.append(ref.removesuffix("^{}"))
    return sorted(set(refs))


def unpublished_commits(repo: Path) -> list:
    """Local branch commits that no remote-tracking ref contains."""
    output = git(repo, "log", "--branches", "--not", "--remotes", "--format=%h %s")
    return [line for line in output.splitlines() if line.strip()]


def linked_worktrees(repo: Path) -> list:
    output = git(repo, "worktree", "list", "--porcelain")
    paths = [line[len("worktree "):] for line in output.splitlines() if line.startswith("worktree ")]
    return [Path(path) for path in paths[1:]]


def dirty_paths(repo: Path) -> list:
    return [line for line in git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines() if line]


def remove_tree(path: Path) -> None:
    """Delete a directory, including Git's read-only object files on Windows."""
    def retry(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if path.is_symlink():
        raise WorkspacesError(f"refusing to delete a symbolic link: {path}")
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:  # pragma: no cover - older interpreters
        shutil.rmtree(path, onerror=retry)


# --- GSD Path release

def default_release_file() -> Path:
    return Path.home() / ".gsd-path" / "fork-release.json"


def read_release(path: Path) -> dict:
    """The fork release record written by `consumer_upgrade.py announce`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkspacesError(f"GSD Path release record not found at {path}: {error}; "
                              "run `scripts/dev/consumer_upgrade.py announce` in the GSD Path checkout or pass --release") from error
    if not isinstance(data, dict) or data.get("schema") != RELEASE_SCHEMA or not all(
            isinstance(data.get(key), str) and data[key] for key in ("checkout", "commit", "source", "version")):
        raise WorkspacesError(f"invalid release record: {path}")
    return data


@contextlib.contextmanager
def release_source(release: dict) -> Iterator[Path]:
    """A pristine tree of the announced commit, never the working checkout."""
    with tempfile.TemporaryDirectory(prefix="gsd-path-workspaces-") as temporary:
        archive = Path(temporary) / "release.tar"
        source = Path(temporary) / "source"
        source.mkdir()
        git(Path(release["checkout"]), "archive", "--format=tar", "-o", str(archive), release["commit"])
        with tarfile.open(archive) as bundle:
            if hasattr(tarfile, "data_filter"):
                bundle.extractall(source, filter="data")
            else:  # pragma: no cover - interpreters without extraction filters
                bundle.extractall(source)
        yield source


def installed_version(coordinator: Path) -> Optional[str]:
    try:
        return json.loads((coordinator / ".gsd-path" / "runtime.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def installed_script(coordinator: Path, name: str) -> Path:
    path = coordinator / ".claude" / "skills" / "gsd-path" / "scripts" / name
    if not path.is_file():
        raise WorkspacesError(f"GSD Path is not installed in {coordinator} (missing {path})")
    return path


def read_state(coordinator: Path) -> dict:
    """The STATE.md frontmatter fields, without comments; empty when absent."""
    path = coordinator / ".project" / "STATE.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.split(" #", 1)[0].strip()
    return fields
