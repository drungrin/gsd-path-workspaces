"""gsd-path-workspaces: one disposable GSD Path coordinator per roadmap.

Layout under the root that holds workspaces.json:

  .orchestrators/<project>.git  bare origin of the workspace coordinator
  <ws>/<project>/              coordinator (<project> defaults to <ws>); open the agent here
  <ws>/<member>/              member clone, origin on the forge
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import operations, system
from .config import INTEGRATIONS, Config, WorkspacesError, find_root


def report(line: str) -> None:
    print(line, flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="gsd-path-workspaces", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--root", type=Path, help="root holding workspaces.json (default: search upward from the current directory)")
    result.add_argument("--release", type=Path,
                        help="GSD Path release record (default: ~/.gsd-path/fork-release.json)")
    commands = result.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create workspaces.json in the current directory (or in --root)")

    imported = commands.add_parser("import", help="register the members of an existing orchestrator")
    imported.add_argument("orchestrator", type=Path)

    member = commands.add_parser("member", help="member registry").add_subparsers(dest="action", required=True)
    added = member.add_parser("add", help="register a member")
    added.add_argument("name")
    added.add_argument("--remote", required=True, help="origin URL on the forge")
    added.add_argument("--default-branch", help="default: the remote HEAD")
    added.add_argument("--integration", choices=INTEGRATIONS, default="pull-request")
    added.add_argument("--reference", type=Path, help="local clone reused to clone faster")
    updated = member.add_parser("update", help="change a member (existing workspaces are not changed)")
    updated.add_argument("name")
    updated.add_argument("--remote")
    updated.add_argument("--default-branch")
    updated.add_argument("--integration", choices=INTEGRATIONS)
    reference = updated.add_mutually_exclusive_group()
    reference.add_argument("--reference", type=Path)
    reference.add_argument("--no-reference", action="store_true")
    removed = member.add_parser("remove", help="unregister a member that no workspace uses")
    removed.add_argument("name")
    member.add_parser("list", help="list the members")

    workspace = commands.add_parser("workspace", help="workspaces").add_subparsers(dest="action", required=True)
    created = workspace.add_parser("create", help="create a workspace with a coordinator and members")
    created.add_argument("name")
    created.add_argument("--members", help="comma-separated members, in ship order (default: all)")
    created.add_argument("--project", help="project name and coordinator folder name (default: <name>; gives the branch feature/<project>-milestone-NNN)")
    created.add_argument("--skip-remote-check", action="store_true",
                         help="do not check the member remotes for an already used project name")
    joined = workspace.add_parser("add-member", help="clone a member and join it to a workspace")
    joined.add_argument("name")
    joined.add_argument("member")
    joined.add_argument("--skip-remote-check", action="store_true")
    left = workspace.add_parser("remove-member", help="take a member out of a workspace and delete its clone")
    left.add_argument("name")
    left.add_argument("member")
    left.add_argument("--force", action="store_true")
    deleted = workspace.add_parser("remove", help="delete a workspace; the bare repository and the project name are kept")
    deleted.add_argument("name")
    deleted.add_argument("--force", action="store_true", help="delete even with an active milestone or unsaved work")
    deleted.add_argument("--purge-bare", action="store_true", help="also delete the bare repository (loses the ship history)")
    listed = workspace.add_parser("list", help="list the workspaces and the phase of each")
    listed.add_argument("--json", action="store_true")
    return result


def release(arguments) -> dict:
    return system.read_release(arguments.release or system.default_release_file())


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "init":
            config = Config.create(arguments.root or Path.cwd())
            report(f"created {config.root / 'workspaces.json'}")
            return 0
        config = Config.load(arguments.root.resolve() if arguments.root else find_root())
        if arguments.command == "import":
            operations.import_orchestrator(config, arguments.orchestrator, report)
            config.save()
        elif arguments.command == "member":
            run_member(config, arguments)
        else:
            run_workspace(config, arguments)
    except WorkspacesError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def run_member(config: Config, arguments) -> None:
    if arguments.action == "list":
        for name, entry in config.members.items():
            report(f"{name}: {entry['remote']} ({entry['default_branch']}, {entry['integration']})"
                   + (f" ref {entry['reference']}" if entry.get("reference") else ""))
        return
    if arguments.action == "add":
        operations.add_member(config, arguments.name, arguments.remote, arguments.default_branch,
                              arguments.integration, arguments.reference)
    elif arguments.action == "update":
        operations.update_member(config, arguments.name, arguments.remote, arguments.default_branch,
                                 arguments.integration, arguments.reference, arguments.no_reference)
    elif arguments.action == "remove":
        operations.remove_member(config, arguments.name)
    config.save()
    report(f"member {arguments.name}: {arguments.action} ok")


def run_workspace(config: Config, arguments) -> None:
    action = arguments.action
    if action == "list":
        rows = operations.describe_workspaces(config)
        if arguments.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return
        for row in rows:
            report(f"{row['name']}: {row['phase']}/{row['status']} {row['milestone']} "
                   f"[{', '.join(row['members'])}] GSD Path {row['version']}\n  {row['coordinator']}")
        return
    if action == "create":
        members = [item.strip() for item in arguments.members.split(",")] if arguments.members else None
        coordinator = operations.create_workspace(config, arguments.name, members, arguments.project,
                                                  release(arguments), report,
                                                  check_remotes=not arguments.skip_remote_check)
        report(f"\ndone. Open the agent session in:\n  {coordinator}\nand run /gsd-path.")
    elif action == "add-member":
        operations.add_workspace_member(config, arguments.name, arguments.member, release(arguments), report,
                                        check_remotes=not arguments.skip_remote_check)
    elif action == "remove-member":
        operations.remove_workspace_member(config, arguments.name, arguments.member, report, arguments.force)
    elif action == "remove":
        operations.remove_workspace(config, arguments.name, report, arguments.force, arguments.purge_bare)


if __name__ == "__main__":
    raise SystemExit(main())
