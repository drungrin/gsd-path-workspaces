"""Two workspaces share one member repository, each through its own clone.

Each workspace holds a dedicated coordinator cloned from its own local bare
repository and a separate clone of the shared member. Both milestones build
before either ships, so the second ship lands on a member default branch that
the first ship has already moved.

The test drives the real GSD Path runtime from a source checkout, the same
way that checkout's own two-repo full-cycle test does, and reuses that test's
artifact fixtures. Point GSD_PATH_SOURCE at the checkout; it defaults to a
sibling `gsd-path` directory.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SOURCE, SandboxTestCase  # noqa: E402  (puts the source checkout on sys.path)

from gsd_path_workspaces import operations  # noqa: E402
from gsd_path_workspaces.config import Config  # noqa: E402
from scripts import isolation, lean_verification  # noqa: E402
from tests import test_member_full_cycle as cycle  # noqa: E402
from tests.test_full_cycle import EVIDENCE, RESEARCH, SYNTHESIS  # noqa: E402

SCRIPTS = SOURCE / "scripts"
MEMBER_URL = "https://github.com/acme/web.git"
BRANCH = "feature/milestone-001"
SLUG = "demo"
DEFAULT = "main"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, encoding="utf-8", errors="replace",
                          capture_output=True, check=False)


def script(name, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=SOURCE, encoding="utf-8",
                          errors="replace", capture_output=True, check=False)


def variant(text, module, value):
    """The upstream fixture with its `lib` module and value 2 replaced."""
    text = re.sub(r"\blib\b", module, text)
    for before in ("value = ", "value == ", "value is ", "`value` to ", "value` is "):
        text = text.replace(f"{before}2", f"{before}{value}")
    return text


class GateError(AssertionError):
    pass


class Workspace:
    """One workspace: a coordinator from a local bare plus a member clone."""

    def __init__(self, base: Path, name: str, project: str, member_origin: Path, module: str, value: int):
        self.root = base / name
        self.project = project
        self.module = module
        self.value = value
        self.repo = self.root / "orchestrator"
        self.member = self.root / "web"
        self.origin = base / ".orchestrators" / f"{name}.git"
        self.member_origin = member_origin
        self.joined = False

    @classmethod
    def from_tool(cls, config: Config, name: str, member_origin: Path, module: str, value: int):
        """A workspace that gsd-path-workspaces created: its coordinator already lists the member."""
        project = config.workspace(name)["project"]
        workspace = cls(config.root, name, project, member_origin, module, value)
        workspace.repo = config.coordinator_dir(name)
        workspace.origin = config.bare_dir(project)
        workspace.joined = True
        return workspace

    # --- helpers
    def write(self, relative, content, root=None):
        path = (root or self.repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))

    def text(self, fixture):
        return variant(fixture, self.module, self.value)

    def state(self, phase, status, archive="null"):
        body = cycle.STATE.format(slug=SLUG, phase=phase, status=status, branch=BRANCH, archive=archive,
                                  today=cycle.TODAY, default="")
        self.write(".project/STATE.md", body.replace("project: acme", f"project: {self.project}"))

    def commit(self, message):
        git(self.repo, "add", "-A")
        result = git(self.repo, "commit", "-q", "-m", message)
        if result.returncode:
            raise GateError(result.stderr)
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def gate(self, name, *args):
        result = script(name, *args)
        if result.returncode:
            raise GateError(f"{name} {' '.join(args)}\n{result.stdout}{result.stderr}")
        return json.loads(result.stdout) if result.stdout.strip().startswith("{") else result.stdout

    def transition(self, event, expect_phase, expect_status, *, set_phase, set_status):
        return self.gate("pipeline_state.py", "transition", "--repo", str(self.repo), "--event", event,
                         "--expect-pipeline", "gsd-path/v2", "--expect-project", self.project,
                         "--expect-milestone", SLUG, "--expect-phase", expect_phase,
                         "--expect-status", expect_status, "--expect-branch", BRANCH,
                         "--expect-archive", "null", "--set-phase", set_phase, "--set-status", set_status)

    @property
    def bound_ref(self):
        return f"refs/heads/feature/{self.project}-milestone-001"

    # --- phases
    def create(self):
        """What gsd-path-workspaces will do: bare, coordinator clone, member clone, membership."""
        self.root.mkdir(parents=True)
        self.origin.parent.mkdir(parents=True, exist_ok=True)
        assert git(self.root, "init", "--bare", "-q", "-b", DEFAULT, str(self.origin)).returncode == 0
        assert git(self.root, "clone", "-q", str(self.origin), str(self.repo)).returncode == 0
        git(self.repo, "switch", "-q", "-c", DEFAULT)
        self.write("README.md", f"{self.project} workspace\n")
        self.commit("workspace: coordinator")
        hooks = subprocess.run(["node", str(SCRIPTS / "install.mjs"), "--claude", "--local", "--project",
                                str(self.repo), "--hooks", "--no-color"], cwd=self.repo, capture_output=True,
                               text=True)
        if hooks.returncode:
            raise GateError(hooks.stdout + hooks.stderr)
        self.commit("router: install guard hooks")
        pushed = git(self.repo, "push", "-q", "-u", "origin", DEFAULT)
        if pushed.returncode:
            raise GateError(pushed.stderr)
        git(self.repo, "remote", "set-head", "origin", "--auto")

        # The member keeps its forge origin; Git reaches it through a local bare.
        cloned = git(self.root, "-c", f"url.{self.member_origin}.insteadOf={MEMBER_URL}", "clone", "-q",
                     MEMBER_URL, str(self.member))
        if cloned.returncode:
            raise GateError(cloned.stderr)
        git(self.member, "config", f"url.{self.member_origin}.insteadOf", MEMBER_URL)

    def plan(self):
        git(self.repo, "switch", "-q", "-c", BRANCH)
        self.state("plan", "active")
        self.write(".project/intent/INTENT.md", self.text(cycle.INTENT))
        self.write(".project/research/RESEARCH.md", RESEARCH)
        self.write(".project/research/evidence-domain.md", EVIDENCE)
        self.write(".project/research/SYNTHESIS.md", SYNTHESIS)
        self.commit("define: intent, research, and synthesis")
        if not self.joined:
            self.gate("members.py", "add", "--repo", str(self.repo), "--name", "web", "--checkout",
                      str(self.member))
            installed = subprocess.run([sys.executable, str(SCRIPTS / "install.py"), "--member-of",
                                        str(self.repo), "--project", str(self.member)],
                                       capture_output=True, text=True)
            if installed.returncode:
                raise GateError(installed.stdout + installed.stderr)
        self.write(".project/plan/PLAN.md", self.text(cycle.PLAN))
        self.write(f".project/tasks/T001-raise.md", self.text(cycle.TASK.format(today=cycle.TODAY)))
        self.gate("check_handoffs.py", "plan", "--repo", str(self.repo))
        plan_head = self.commit("plan: draft")
        self.approval = self.gate("pipeline_state.py", "approve", "--repo", str(self.repo), "--kind", "plan",
                                  "--expected-head", plan_head)

    def build(self):
        repo = self.repo
        self.transition("build started", "plan", "done", set_phase="build", set_status="active")
        start = isolation.checkpoint(repo, self.approval["commit"], "build: start", "Why: build started",
                                     [".project"])
        base = start["commit"]
        isolated = isolation.isolate_member_task(repo, "web", "T001")
        isolation.activate_member_task(repo, "web", "T001", "cycle-coder", ".project/tasks/T001-raise.md", base)
        self.write(f"{self.module}.py", f"value = {self.value}\n", Path(isolated["worktree"]))
        landed = isolation.land_member(repo, "web", "T001", "raise value", ".project/tasks/T001-raise.md",
                                       base, isolated["member_base"])
        isolation.retire_member_task(repo, "web", "T001")
        self.member_tip = git(self.member, "rev-parse", self.bound_ref).stdout.strip()
        assert self.member_tip == landed["landing"], (self.member_tip, landed)
        self.write(".project/review/wave-1.cycle1.md", self.text(cycle.WAVE_REVIEW))
        self.gate("check_handoffs.py", "wave", "--repo", str(repo), "--review", ".project/review/wave-1.cycle1.md")
        wave = isolation.checkpoint(repo, landed["commit"], "build: wave 1 reviewed",
                                    f"Why: wave review passed\nWave: 1\nTasks: T001\nBase: {landed['commit']}",
                                    [".project"])
        self.transition("build done; final review pending", "build", "active", set_phase="ship",
                        set_status="active")
        self.reviewed = isolation.checkpoint(repo, wave["commit"], "build: final review",
                                             "Why: build done; final review pending", [".project"])["commit"]

    def final_review(self):
        verified = lean_verification.verify_project(self.repo, self.reviewed)
        if not verified["passed"]:
            raise GateError(f"project verify failed: {verified['execution']}")
        assert verified["execution"]["members"] == {"web": self.member_tip}, verified["execution"]
        self.write(".project/review/FINAL.md", self.text(cycle.FINAL.format(head=self.reviewed,
                                                                             member=self.member_tip)))
        self.gate("check_handoffs.py", "final", "--repo", str(self.repo))
        self.prepared = self.gate("archive_milestone.py", "prepare", "--repo", str(self.repo), "--slug", SLUG)
        self.gate("archive_milestone.py", "render-manifest", "--repo", str(self.repo))
        self.gate("archive_milestone.py", "preflight", "--repo", str(self.repo))

    def close(self):
        repo = self.repo
        closed = self.gate("archive_milestone.py", "close-members", "--repo", str(repo))
        self.gate("pipeline_state.py", "record-shipment", "--repo", str(repo), "--archive",
                  self.prepared["archive"], "--event", "archive preflight passed; shipment recorded")
        git(repo, "add", "-A")
        ship = git(repo, "commit", "-q", "-m", f"ship: M001 — {SLUG}", "-m", closed["body"].rstrip("\n"))
        if ship.returncode:
            raise GateError(ship.stderr)
        self.gate("archive_milestone.py", "validate", "--repo", str(repo))
        integrated = self.gate("archive_milestone.py", "integrate", "--repo", str(repo), "--slug", SLUG)
        return closed, integrated


class TwoWorkspacesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        environment = mock.patch.dict(os.environ, {
            "GSD_PATH_WORKTREE_ROOT": str(self.base / "gsd-path-state"),
            "GIT_AUTHOR_NAME": "Workspaces", "GIT_AUTHOR_EMAIL": "workspaces@example.invalid",
            "GIT_COMMITTER_NAME": "Workspaces", "GIT_COMMITTER_EMAIL": "workspaces@example.invalid"})
        environment.start()
        self.addCleanup(environment.stop)
        self.member_origin = self.base / "web-origin.git"
        seed = self.base / "web-seed"
        self.assertEqual(git(self.base, "init", "--bare", "-q", "-b", DEFAULT, str(self.member_origin)).returncode, 0)
        git(self.base, "init", "-q", "-b", DEFAULT, str(seed))
        (seed / "lib.py").write_text("value = 1\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "web: existing library")
        self.assertEqual(git(seed, "push", "-q", str(self.member_origin), DEFAULT).returncode, 0)

    def workspaces(self, second_project, second_module, second_value, review_second=True):
        first = Workspace(self.base, "new-screen", "new-screen", self.member_origin, "lib", 2)
        second = Workspace(self.base, "other-screen", second_project, self.member_origin, second_module, second_value)
        for workspace in (first, second):
            workspace.create()
            workspace.plan()
        # Both milestones build before either ships.
        first.build()
        second.build()
        first.final_review()
        if review_second:
            second.final_review()
        return first, second

    def member_default(self):
        return git(self.member_origin, "rev-parse", DEFAULT).stdout.strip()

    def test_disjoint_member_changes_ship_from_both_workspaces(self):
        first, second = self.workspaces("other-screen", "other", 2)
        closed, _ = first.close()
        first_merge = closed["members"][0]["merge"]
        self.assertEqual(self.member_default(), first_merge)

        closed, integrated = second.close()
        second_merge = closed["members"][0]["merge"]
        self.assertEqual(self.member_default(), second_merge)
        parents = git(self.member_origin, "rev-list", "--parents", "-n", "1", DEFAULT).stdout.split()[1:]
        self.assertEqual(parents, [first_merge, second.member_tip])
        tree = git(self.member_origin, "ls-tree", "--name-only", DEFAULT).stdout.split()
        self.assertEqual(sorted(tree), ["lib.py", "other.py"])
        for project in ("new-screen", "other-screen"):
            tag = git(self.member_origin, "rev-parse", "--verify", "--quiet", f"refs/tags/milestone/{project}-001-{SLUG}")
            self.assertEqual(tag.returncode, 0, project)
        self.assertEqual(git(second.origin, "rev-parse", DEFAULT).stdout.strip(), integrated["integrate"])

    def test_conflict_is_found_before_final_review_when_the_first_ships_earlier(self):
        first, second = self.workspaces("other-screen", "lib", 3, review_second=False)
        self.assertEqual(isolation.member_mergeability(second.repo)["verdict"], "clean")
        first.close()
        checked = isolation.member_mergeability(second.repo)
        self.assertEqual(checked["verdict"], "conflict", checked)
        self.assertEqual(checked["members"][0]["member"], "web")
        print(f"\nearly mergeability check: {checked['members'][0]}", file=sys.stderr)

    def test_conflict_after_the_second_archive_is_prepared_blocks_its_close(self):
        first, second = self.workspaces("other-screen", "lib", 3)
        first.close()
        moved = self.member_default()
        with self.assertRaises(GateError) as raised:
            second.close()
        print(f"\nconflicting second ship: {raised.exception}", file=sys.stderr)
        self.assertEqual(self.member_default(), moved)

    def test_same_project_name_in_two_workspaces_collides(self):
        first, second = self.workspaces("new-screen", "other", 2)
        first.close()
        moved = self.member_default()
        tag = f"refs/tags/milestone/new-screen-001-{SLUG}"
        published = {ref: git(self.member_origin, "rev-parse", ref).stdout.strip()
                     for ref in (tag, first.bound_ref)}
        with self.assertRaises(GateError) as raised:
            second.close()
        print(f"\nsame project name: {raised.exception}", file=sys.stderr)
        self.assertEqual(self.member_default(), moved)
        self.assertEqual({ref: git(self.member_origin, "rev-parse", ref).stdout.strip() for ref in published},
                         published)

    def test_one_member_clone_cannot_serve_two_workspaces(self):
        first = Workspace(self.base, "new-screen", "new-screen", self.member_origin, "lib", 2)
        second = Workspace(self.base, "other-screen", "other-screen", self.member_origin, "other", 2)
        first.create()
        first.plan()
        second.create()
        second.member = first.member
        with self.assertRaises(GateError) as raised:
            second.plan()
        self.assertIn("is already a member of new-screen", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


class ToolWorkspacesTests(SandboxTestCase):
    """Workspaces that gsd-path-workspaces creates ship and are then removed without --force."""

    def test_tool_created_workspaces_ship_and_are_removed_cleanly(self):
        self.member_origin("web", {"lib.py": "value = 1\n"})
        root = self.base / "workspaces"
        root.mkdir()
        config = Config.create(root)
        operations.add_member(config, "web", "https://github.com/acme/web.git", integration="direct")
        config.save()
        from gsd_path_workspaces import system
        release = system.read_release(self.release_file)
        lines = []
        for name in ("new-screen", "other-screen"):
            operations.create_workspace(config, name, None, None, release, lines.append)
        first = Workspace.from_tool(config, "new-screen", self.bare("web"), "lib", 2)
        second = Workspace.from_tool(config, "other-screen", self.bare("web"), "other", 2)
        for workspace in (first, second):
            workspace.plan()
        first.build()
        second.build()
        first.final_review()
        second.final_review()
        first.close()
        second.close()
        for project in ("new-screen", "other-screen"):
            self.assertEqual(git(self.bare("web"), "rev-parse", "--verify", "--quiet",
                                 f"refs/tags/milestone/{project}-001-{SLUG}").returncode, 0, project)

        for name in ("new-screen", "other-screen"):
            self.assertEqual(system.read_state(config.coordinator_dir(name))["phase"], "shipped")
            operations.remove_workspace(config, name, lines.append)
            self.assertFalse((root / name).exists())
        self.assertEqual(config.workspaces, {})
        for project in ("new-screen", "other-screen"):
            self.assertTrue(config.bare_dir(project).is_dir())
        self.assertEqual(config.reserved, ["new-screen", "other-screen"])
        state_root = self.base / "gsd-path-state"
        leftovers = [path for path in state_root.rglob("*") if path.is_dir() and (path / ".git").exists()]
        self.assertEqual(leftovers, [])
