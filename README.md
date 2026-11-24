# gsd-path-workspaces

Work on several [GSD Path](https://github.com/drungrin/gsd-path) roadmaps at
the same time over the same member repositories.

GSD Path allows a single coordinator per member clone: the member marker lives
in the clone's shared Git directory, so not even a `git worktree` gets around
it. Each roadmap becomes a **workspace** with a disposable coordinator, cloned
from a local bare repository, and its own clones of the members.

```text
<root>/                               e.g. acme-workspaces
  workspaces.json                     members, workspaces and project names already used
  .orchestrators/<project>.git        coordinator bare repository (keeps the ship history)
  <ws>/<ws>/                          coordinator; open Claude here
  <ws>/<member>/                      member clone, with origin on the forge
```

## Requirements

- Python 3.9+, with no third-party dependencies.
- Git.
- Node 18.17+, for the GSD Path installer.
- A local checkout of the [drungrin/gsd-path](https://github.com/drungrin/gsd-path)
  fork. The tool installs GSD Path into each coordinator from a release of that
  checkout (see below).

## Installation

```sh
pipx install git+https://github.com/drungrin/gsd-path-workspaces    # or: uv tool install git+https://...
```

For development, `pipx install -e .` from a clone, or run it without installing:
`PYTHONPATH=src python3 -m gsd_path_workspaces ...`.

### The GSD Path release record

The GSD Path installed in each coordinator comes from the announced release of
the fork, recorded in `~/.gsd-path/fork-release.json`. It is extracted with
`git archive` from the announced commit, never from the working checkout, so
unreleased work never reaches a workspace. To write the record, check out the
release commit of the fork and announce it:

```sh
git clone https://github.com/drungrin/gsd-path && cd gsd-path
python3 scripts/dev/consumer_upgrade.py announce
```

`--release <file>` points the tool at another record.

## Usage

```sh
cd acme-workspaces
gsd-path-workspaces init
gsd-path-workspaces import ../acme-orchestrator          # members + reserves the project name
gsd-path-workspaces workspace create checkout-page       # every member, in registration order
gsd-path-workspaces workspace create search --members acme-api,acme-auth
gsd-path-workspaces workspace list
```

After `create`, open the agent session in `checkout-page/checkout-page` and run
`/gsd-path`. In a parent folder the session runs without the Claude guard and
without `AGENTS.md`.

| Command | What it does |
|---------|--------------|
| `init` | creates `workspaces.json` |
| `import <orchestrator>` | reads `.project/MEMBERS.md` and `STATE.md`; `Integration: default` becomes the orchestrator's `integration_default`; the checkout becomes the `reference`; reserves the `project` |
| `member add <name> --remote <url>` | `--default-branch` (default: the remote HEAD), `--integration` (default `pull-request`), `--reference <local clone>` |
| `member update`, `member remove`, `member list` | `remove` refuses a member in use |
| `workspace create <ws>` | `--members a,b` (ship order), `--project` (default `<ws>`, which gives the branch `feature/<ws>-milestone-NNN` in the members), `--skip-remote-check` |
| `workspace add-member <ws> <member>` | requires the coordinator to be on the same version as the release |
| `workspace remove-member <ws> <member>` | `members.py remove` (only at a milestone boundary) and deletes the clone |
| `workspace remove <ws>` | refuses an active milestone or unsaved work; `--force`, `--purge-bare` |
| `workspace list [--json]` | phase, milestone, members and GSD Path version |

### What `create` does

1. Checks that the project name was never used: not in `reserved_projects`,
   and not in `feature/<project>-milestone-*`, `milestone/<project>-*` and
   similar refs on the member remotes (`git ls-remote`).
2. Creates the bare repository, clones the coordinator, installs GSD Path
   (Claude skills, guards and a runtime with provenance), commits
   `chore: initial orchestrator setup` and pushes it to the bare repository.
3. Runs `detect_project.py initialize`. The project name comes from the
   coordinator folder name, which is why that folder is named `<project>`. The
   coordinator uses `direct` integration (its origin is the bare repository).
4. For each member: clones from the real origin (with `--reference-if-able
   --dissociate` when there is a `reference`), runs `members.py add` with the
   registered integration, and `install.py --member-of`.

If it fails before registering the workspace, it deletes what it created. If
it fails while joining a member, the workspace stays registered and
`workspace add-member` resumes.

### What `remove` does

It refuses if the coordinator is not `shipped`, or if any repository or linked
worktree has changes or unpublished commits. It removes the linked worktrees
(they live outside the workspace, in the GSD Path state folder) and deletes
the folder. The bare repository stays, because the coordinator's archive, ship
commits and tags only exist there, and the project name stays reserved.

## Tests

The tests run the real runtime from a GSD Path source checkout and reuse the
fixtures of its `tests/test_member_full_cycle.py`. Member remotes use GitHub
URLs rewritten to local bare repositories; nothing leaves the machine.

```sh
git clone https://github.com/drungrin/gsd-path ../gsd-path
GSD_PATH_SOURCE=../gsd-path python3 -m unittest discover -s tests -t tests
```

Without `GSD_PATH_SOURCE`, the tests look for a sibling `gsd-path` directory.
They take about 3 minutes. `tests/test_cli.py` covers the commands;
`tests/test_e2e_two_workspaces.py` covers the ship:

| Scenario | Result |
|----------|--------|
| Two workspaces created by the tool, both ship | Both ship; `workspace remove` without `--force` deletes each one, keeps the bare repository and leaves no worktree in the state folder |
| Two workspaces, disjoint changes to the same member, both build before either ships | Both ship; the second merge has the first merge and the second's reviewed tip as parents; both tags exist |
| Conflict, with the first shipping before the second's final review | `isolation.member_mergeability` goes from `clean` to `conflict` (`lib.py`); in the real flow `prepare-final` blocks there and the member rebase adoption is legal |
| Conflict, with the first shipping after the second's archive `prepare` | `close-members` fails without touching the member's `main`; member rebase adoption has already recovered this case in real use |
| Same project name in both workspaces | The second's `close-members` fails with a confusing message (to be improved in the fork when it happens); the first's `main`, tag and published branch stay intact. The tool prevents this case |
| One member clone serving two coordinators | `members.py add` refuses: `is already a member of <project>` |

The members' `pull-request` mode is not exercised at ship (it would need
GitHub); the ship tests use `direct`.

## License

[MIT](LICENSE)
