"""Member and workspace operations."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import system
from .config import INTEGRATIONS, Config, WorkspacesError, valid_name
from .system import git

Report = Callable[[str], None]
SETUP_COMMIT = "chore: initial orchestrator setup"


# --- members

def add_member(config: Config, name: str, remote: str, default_branch: Optional[str] = None,
               integration: str = "pull-request", reference: Optional[Path] = None) -> dict:
    valid_name("member", name)
    if name in config.members:
        raise WorkspacesError(f"member already registered: {name}")
    entry = {"remote": remote, "default_branch": default_branch or system.remote_default_branch(remote),
             "integration": _integration(integration), "reference": _reference(reference)}
    config.members[name] = entry
    return entry


def update_member(config: Config, name: str, remote: Optional[str] = None, default_branch: Optional[str] = None,
                  integration: Optional[str] = None, reference: Optional[Path] = None,
                  clear_reference: bool = False) -> dict:
    entry = config.member(name)
    if remote is not None:
        entry["remote"] = remote
    if default_branch is not None:
        entry["default_branch"] = default_branch
    if integration is not None:
        entry["integration"] = _integration(integration)
    if clear_reference:
        entry["reference"] = None
    elif reference is not None:
        entry["reference"] = _reference(reference)
    return entry


def remove_member(config: Config, name: str) -> None:
    config.member(name)
    users = [workspace for workspace, entry in config.workspaces.items() if name in entry["members"]]
    if users:
        raise WorkspacesError(f"{name} is still in the workspaces: {', '.join(users)}")
    del config.members[name]


def _integration(mode: str) -> str:
    if mode not in INTEGRATIONS:
        raise WorkspacesError(f"invalid integration: {mode} (use {', '.join(INTEGRATIONS)})")
    return mode


def _reference(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not (resolved / ".git").exists():
        raise WorkspacesError(f"reference is not a Git clone: {resolved}")
    return str(resolved)


# --- import

def parse_members_md(text: str) -> list:
    """Sections of a GSD Path `.project/MEMBERS.md`, in ship order."""
    sections, current = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            current = {"name": line[3:].strip()}
            sections.append(current)
        elif current is not None and ":" in line and not line.startswith(("<!--", " ")):
            key, _, value = line.partition(":")
            current[key.strip().lower().replace(" ", "_")] = value.strip()
    return sections


def import_orchestrator(config: Config, orchestrator: Path, report: Report) -> list:
    """Register the members of an existing coordinator and reserve its project name."""
    orchestrator = orchestrator.expanduser().resolve()
    path = orchestrator / ".project" / "MEMBERS.md"
    try:
        sections = parse_members_md(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise WorkspacesError(f"could not read {path}: {error}") from error
    if not sections:
        raise WorkspacesError(f"{path} lists no members")
    state = system.read_state(orchestrator)
    if not state.get("project"):
        raise WorkspacesError(f"{orchestrator} has no .project/STATE.md with a project")
    inherited = state.get("integration_default", "direct")
    imported = []
    for section in sections:
        name = valid_name("member", section["name"])
        remote = section.get("remote")
        if not remote:
            raise WorkspacesError(f"{path}: member {name} has no Remote")
        integration = section.get("integration", "default")
        entry = {"remote": remote, "default_branch": section.get("default_branch", "main"),
                 "integration": _integration(inherited if integration == "default" else integration),
                 "reference": None}
        checkout = section.get("checkout")
        if checkout and (Path(checkout) / ".git").exists():
            entry["reference"] = str(Path(checkout).resolve())
        if name in config.members:
            if {key: config.members[name][key] for key in ("remote", "default_branch")} != {
                    key: entry[key] for key in ("remote", "default_branch")}:
                raise WorkspacesError(f"member {name} already registered with a different remote or branch")
            report(f"member {name}: already registered, kept")
            continue
        config.members[name] = entry
        imported.append(name)
        report(f"member {name}: {entry['remote']} ({entry['default_branch']}, {entry['integration']})")
    config.reserve(state["project"])
    report(f"project reserved: {state['project']}")
    return imported


# --- workspaces

def create_workspace(config: Config, name: str, members: Optional[Sequence[str]], project: Optional[str],
                     release: dict, report: Report, check_remotes: bool = True) -> Path:
    valid_name("workspace", name)
    # The member bound branch is feature/<project>-milestone-NNN, so the project defaults to the workspace name.
    project = valid_name("project", project or name)
    if name in config.workspaces:
        raise WorkspacesError(f"workspace already exists: {name}")
    if project in config.reserved:
        raise WorkspacesError(f"the project name {project} was already used; choose another with --project")
    selected = list(members) if members else list(config.members)
    if not selected:
        raise WorkspacesError("no members registered; use `member add` or `import`")
    for member in selected:
        config.member(member)
    if len(set(selected)) != len(selected):
        raise WorkspacesError("member repeated in the list")
    if project in selected:
        raise WorkspacesError(f"the project {project} has the name of a member; choose another with --project")
    workspace = config.workspace_dir(name)
    bare = config.bare_dir(project)
    for path in (workspace, bare):
        if path.exists():
            raise WorkspacesError(f"already exists: {path}")
    if check_remotes:
        for member in selected:
            taken = system.project_refs(config.member(member)["remote"], project)
            if taken:
                raise WorkspacesError(f"{member} already has refs of the project {project}: {', '.join(taken[:5])}")

    coordinator = workspace / project
    with system.release_source(release) as source:
        try:
            _create_coordinator(coordinator, bare, source, release, project, report)
        except BaseException:
            # Nothing is registered yet: undo only what this call created.
            for path in (workspace, bare):
                if path.exists():
                    system.remove_tree(path)
            raise
        config.reserve(project)
        config.workspaces[name] = {"project": project, "members": [],
                                   "created": datetime.date.today().isoformat()}
        config.save()
        # From here the workspace is registered; a failed join is retried with `workspace add-member`.
        for member in selected:
            _join(config, name, member, source, report)
    return coordinator


def _create_coordinator(coordinator: Path, bare: Path, source: Path, release: dict, project: str,
                        report: Report) -> None:
    bare.parent.mkdir(parents=True, exist_ok=True)
    system.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)])
    coordinator.parent.mkdir()
    system.run(["git", "clone", "-q", str(bare), str(coordinator)])
    git(coordinator, "symbolic-ref", "HEAD", "refs/heads/main")
    (coordinator / ".gitignore").write_bytes(b"__pycache__/\n")
    report(f"coordinator: {coordinator} (origin {bare})")
    _install(coordinator, source, release)
    git(coordinator, "add", "-A")
    git(coordinator, "commit", "-q", "-m", SETUP_COMMIT)
    git(coordinator, "push", "-q", "-u", "origin", "main")
    git(coordinator, "remote", "set-head", "origin", "--auto")
    report(f"GSD Path {release['version']} installed and pushed to the bare repository")
    initialized = system.python(system.installed_script(coordinator, "detect_project.py"), "initialize",
                                "--repo", str(coordinator), "--template",
                                str(coordinator / ".claude/skills/gsd-path/templates/state.md"),
                                "--require-git", check=False)
    if initialized.returncode != 0 or '"wrote_state": true' not in initialized.stdout:
        raise WorkspacesError(f"initialize failed:\n{initialized.stdout}{initialized.stderr}")
    state = system.read_state(coordinator)
    if state.get("project") != project:
        raise WorkspacesError(f"STATE.md recorded project {state.get('project')!r}, expected {project}")
    report(f"STATE.md: {state.get('phase')}/{state.get('status')}, project {project}")


def add_workspace_member(config: Config, name: str, member: str, release: dict, report: Report,
                         check_remotes: bool = True) -> None:
    entry = config.workspace(name)
    config.member(member)
    if member in entry["members"]:
        raise WorkspacesError(f"{member} is already in {name}")
    coordinator = config.coordinator_dir(name)
    installed = system.installed_version(coordinator)
    if installed != release["version"]:
        raise WorkspacesError(f"the coordinator has GSD Path {installed} and the release is {release['version']}; "
                              "upgrade the coordinator first (consumer_upgrade.py apply)")
    if check_remotes:
        taken = system.project_refs(config.member(member)["remote"], entry["project"])
        if taken:
            raise WorkspacesError(f"{member} already has refs of the project {entry['project']}: {', '.join(taken[:5])}")
    with system.release_source(release) as source:
        _join(config, name, member, source, report)


def remove_workspace_member(config: Config, name: str, member: str, report: Report, force: bool = False) -> None:
    entry = config.workspace(name)
    if member not in entry["members"]:
        raise WorkspacesError(f"{member} is not in {name}")
    checkout = config.workspace_dir(name) / member
    if checkout.exists():
        _check_unsaved([checkout], force)
    system.python(system.installed_script(config.coordinator_dir(name), "members.py"), "remove", "--repo",
                  str(config.coordinator_dir(name)), "--name", member)
    entry["members"].remove(member)
    config.save()
    if checkout.exists():
        _remove_worktrees(checkout)
        system.remove_tree(checkout)
    report(f"{member} removed from {name}; the changed MEMBERS.md goes into the next pipeline checkpoint")


def remove_workspace(config: Config, name: str, report: Report, force: bool = False,
                     purge_bare: bool = False) -> None:
    entry = config.workspace(name)
    workspace = config.workspace_dir(name)
    coordinator = workspace / entry["project"]
    if coordinator.exists():
        state = system.read_state(coordinator)
        if state and state.get("phase") != "shipped" and not force:
            raise WorkspacesError(f"{name} is at {state.get('phase')}/{state.get('status')}; "
                                  "finish or abandon the milestone, or use --force")
    repositories = [path for path in [coordinator, *(workspace / member for member in entry["members"])]
                    if path.exists()]
    _check_unsaved(repositories, force)
    for repository in repositories:
        _remove_worktrees(repository)
    if workspace.exists():
        system.remove_tree(workspace)
    bare = config.bare_dir(entry["project"])
    if purge_bare and bare.exists():
        system.remove_tree(bare)
    del config.workspaces[name]
    config.save()
    kept = "" if purge_bare else f"; bare repository kept at {bare}"
    report(f"workspace {name} deleted; project {entry['project']} stays reserved{kept}")


def describe_workspaces(config: Config) -> list:
    rows = []
    for name, entry in config.workspaces.items():
        coordinator = config.workspace_dir(name) / entry["project"]
        state = system.read_state(coordinator)
        rows.append({"name": name, "project": entry["project"], "members": list(entry["members"]),
                     "phase": state.get("phase", "?"), "status": state.get("status", "?"),
                     "milestone": state.get("milestone", "?"), "coordinator": str(coordinator),
                     "version": system.installed_version(coordinator)})
    return rows


# --- internals

def _install(coordinator: Path, source: Path, release: dict) -> None:
    system.run(["node", str(source / "scripts" / "install.mjs"), "--claude", "--local", "--project",
                str(coordinator), "--hooks", "--no-color"], cwd=coordinator)
    system.python(source / "scripts" / "install.py", "--project", str(coordinator), "--runtime-upgrade",
                  "--runtime-provenance-source", release["source"],
                  "--runtime-provenance-patch", release["commit"],
                  "--runtime-provenance-note", f"fork {release['version']}: workspace created", cwd=coordinator)


def _join(config: Config, name: str, member: str, source: Path, report: Report) -> None:
    entry = config.member(member)
    coordinator = config.coordinator_dir(name)
    checkout = config.workspace_dir(name) / member
    if checkout.exists():
        raise WorkspacesError(f"already exists: {checkout}")
    clone = ["git", "clone", "-q"]
    if entry.get("reference"):
        clone += ["--reference-if-able", entry["reference"], "--dissociate"]
    system.run([*clone, entry["remote"], str(checkout)])
    default = entry["default_branch"]
    if not git(checkout, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{default}", check=False).strip():
        raise WorkspacesError(f"{member}: origin has no branch {default}")
    git(checkout, "remote", "set-head", "origin", default)
    system.python(system.installed_script(coordinator, "members.py"), "add", "--repo", str(coordinator),
                  "--name", member, "--checkout", str(checkout), "--integration", entry["integration"])
    system.python(source / "scripts" / "install.py", "--member-of", str(coordinator), "--project", str(checkout))
    config.workspace(name)["members"].append(member)
    config.save()
    report(f"member {member}: {checkout} ({entry['integration']})")


def _check_unsaved(repositories: Sequence[Path], force: bool) -> None:
    """Refuse, unless forced, when any repository or linked worktree holds unsaved work."""
    if force:
        return
    problems = []
    for repository in repositories:
        for checkout in [repository, *system.linked_worktrees(repository)]:
            if not checkout.exists():
                continue
            dirty = system.dirty_paths(checkout)
            if dirty:
                problems.append(f"{checkout}: {len(dirty)} changed path(s)")
        commits = system.unpublished_commits(repository)
        if commits:
            problems.append(f"{repository}: {len(commits)} unpublished commit(s), e.g. {commits[0]}")
    if problems:
        raise WorkspacesError("unsaved work:\n- " + "\n- ".join(problems)
                              + "\nuse --force to delete anyway")


def _remove_worktrees(repository: Path) -> None:
    """Linked worktrees live outside the workspace (GSD Path state roots); remove them first."""
    for worktree in system.linked_worktrees(repository):
        git(repository, "worktree", "remove", "--force", "--force", str(worktree), check=False)
    git(repository, "worktree", "prune", check=False)
