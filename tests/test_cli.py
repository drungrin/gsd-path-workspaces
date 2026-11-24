"""The CLI against synthetic member origins and the real GSD Path installer."""

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from support import SOURCE, SandboxTestCase, forge_url, git

from gsd_path_workspaces import cli
from gsd_path_workspaces.config import Config

MEMBERS_MD = """# Members

<!-- Written by members.py add. One section per member repository, in
     ship order. Fixed format; change it only through members.py. -->

## web
Checkout: {web}
Remote: {web_remote}
Integration: default
Default branch: main

## api
Checkout: /nonexistent/api
Remote: {api_remote}
Integration: direct
Default branch: master
"""

STATE = """---
pipeline: gsd-path/v2
project: legacy-orchestrator
milestone: something
phase: build
status: active
branch: feature/milestone-004
archive: null
integration_default: pull-request # comment
integration: pull-request
integration_source: default
---
"""


class CliTests(SandboxTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.base / "workspaces"
        self.root.mkdir()
        self.web = self.member_origin("web", {"lib.py": "value = 1\n"})
        self.api = self.member_origin("api", {"api.py": "ok = True\n"}, default="master")

    def cli(self, *args, expect=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--root", str(self.root), "--release", str(self.release_file), *args])
        self.assertEqual(code, expect, f"{args}\n{out.getvalue()}{err.getvalue()}")
        return out.getvalue() + err.getvalue()

    def config(self):
        return Config.load(self.root)

    def register_members(self):
        self.cli("init")
        self.cli("member", "add", "web", "--remote", self.web)
        self.cli("member", "add", "api", "--remote", self.api)

    def state(self, coordinator):
        from gsd_path_workspaces import system
        return system.read_state(coordinator)

    # --- members and import

    def test_member_add_discovers_the_default_branch_and_refuses_duplicates(self):
        self.register_members()
        members = self.config().members
        self.assertEqual(members["api"], {"remote": self.api, "default_branch": "master",
                                          "integration": "pull-request", "reference": None})
        self.assertIn("already registered", self.cli("member", "add", "web", "--remote", self.web, expect=1))
        self.assertIn("invalid", self.cli("member", "add", "Web_X", "--remote", self.web, expect=1))

    def test_import_reads_members_md_resolves_default_integration_and_reserves_the_project(self):
        legacy = self.base / "legacy-orchestrator"
        web_checkout = self.base / "web-checkout"
        git(self.base, "clone", "-q", self.web, str(web_checkout))
        (legacy / ".project").mkdir(parents=True)
        (legacy / ".project" / "MEMBERS.md").write_text(
            MEMBERS_MD.format(web=web_checkout, web_remote=self.web, api_remote=self.api), encoding="utf-8")
        (legacy / ".project" / "STATE.md").write_text(STATE, encoding="utf-8")
        self.cli("init")
        self.cli("import", str(legacy))
        config = self.config()
        self.assertEqual(list(config.members), ["web", "api"])
        self.assertEqual(config.members["web"], {"remote": self.web, "default_branch": "main",
                                                 "integration": "pull-request",
                                                 "reference": str(web_checkout)})
        self.assertEqual(config.members["api"]["integration"], "direct")
        self.assertIsNone(config.members["api"]["reference"])
        self.assertEqual(config.reserved, ["legacy-orchestrator"])
        self.assertIn("already registered, kept", self.cli("import", str(legacy)))
        self.assertIn("was already used",
                      self.cli("workspace", "create", "legacy", "--project", "legacy-orchestrator", expect=1))

    def test_member_remove_refuses_a_member_in_use(self):
        self.register_members()
        self.cli("workspace", "create", "new-screen", "--members", "web")
        self.assertIn("is still in the workspaces: new-screen", self.cli("member", "remove", "web", expect=1))
        self.cli("member", "remove", "api")
        self.assertEqual(list(self.config().members), ["web"])

    # --- workspace create

    def test_a_workspace_named_like_a_member_needs_project(self):
        self.register_members()
        output = self.cli("workspace", "create", "web", "--members", "web", expect=1)
        self.assertIn("has the name of a member; choose another with --project", output)
        self.assertFalse((self.root / "web").exists())
        self.assertEqual(self.config().reserved, [])

    def test_create_builds_the_layout_and_joins_every_member(self):
        self.register_members()
        output = self.cli("workspace", "create", "new-screen")
        coordinator = self.root / "new-screen" / "new-screen"
        self.assertIn(str(coordinator), output)
        bare = self.root / ".orchestrators" / "new-screen.git"
        self.assertEqual(git(coordinator, "remote", "get-url", "origin").stdout.strip(), str(bare))
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "main").stdout.strip(),
                         "chore: initial orchestrator setup")
        self.assertEqual(git(coordinator, "status", "--porcelain").stdout.split("\n")[0][3:], ".project/")
        runtime = json.loads((coordinator / ".gsd-path" / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["version"], self.version)
        self.assertEqual(runtime["provenance"]["source"], "https://example.invalid/gsd-path")

        state = self.state(coordinator)
        self.assertEqual((state["project"], state["integration_default"]), ("new-screen", "direct"))
        members = (coordinator / ".project" / "MEMBERS.md").read_text(encoding="utf-8")
        self.assertIn("## web\n", members)
        self.assertIn("## api\n", members)
        self.assertEqual(members.count("Integration: pull-request"), 2)
        self.assertIn("Default branch: master", members)
        for name, default in (("web", "main"), ("api", "master")):
            checkout = self.root / "new-screen" / name
            self.assertEqual(git(checkout, "config", "--get", "remote.origin.url").stdout.strip(), forge_url(name))
            self.assertEqual(git(checkout, "symbolic-ref", "refs/remotes/origin/HEAD").stdout.strip(),
                             f"refs/remotes/origin/{default}")
            marker = json.loads(Path(git(checkout, "rev-parse", "--path-format=absolute",
                                         "--git-common-dir").stdout.strip(), "gsd-path", "member.json")
                                .read_text(encoding="utf-8"))
            self.assertEqual((marker["project"], marker["coordinator"]),
                             ("new-screen", str(coordinator)))
            hook = Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-path",
                            "hooks/pre-push").stdout.strip())
            self.assertIn("gsd-path member guard", hook.read_text(encoding="utf-8"))

        validated = subprocess.run([sys.executable, "-B", str(SOURCE / "scripts" / "members.py"), "validate",
                                    "--repo", str(coordinator)], capture_output=True, text=True)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        status = subprocess.run([sys.executable, "-B", str(coordinator / ".gsd-path" / "status_runtime.py"),
                                 "--repo", str(coordinator)], capture_output=True, text=True)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(self.config().workspaces["new-screen"]["members"], ["web", "api"])
        self.assertEqual(self.config().reserved, ["new-screen"])

    def test_two_workspaces_share_the_same_members(self):
        self.register_members()
        self.cli("workspace", "create", "new-screen")
        self.cli("workspace", "create", "other-screen", "--members", "api,web")
        coordinator = self.root / "other-screen" / "other-screen"
        members = (coordinator / ".project" / "MEMBERS.md").read_text(encoding="utf-8")
        self.assertLess(members.index("## api"), members.index("## web"))
        listed = json.loads(self.cli("workspace", "list", "--json"))
        self.assertEqual([(row["name"], row["phase"], row["version"]) for row in listed],
                         [("new-screen", "define", self.version), ("other-screen", "define", self.version)])

    def test_create_refuses_a_project_name_already_on_a_member_remote(self):
        self.register_members()
        seed = self.base / "seed-web"
        git(seed, "tag", "-a", "-m", "old", "milestone/new-screen-001-old")
        git(seed, "push", "-q", str(self.bare("web")), "refs/tags/milestone/new-screen-001-old")
        output = self.cli("workspace", "create", "new-screen", expect=1)
        self.assertIn("refs/tags/milestone/new-screen-001-old", output)
        self.assertFalse((self.root / "new-screen").exists())
        self.assertFalse((self.root / ".orchestrators" / "new-screen.git").exists())
        self.assertEqual(self.config().reserved, [])

    def test_failed_creation_leaves_nothing_behind(self):
        self.register_members()
        broken = json.loads(self.release_file.read_text(encoding="utf-8"))
        broken["commit"] = "0" * 40
        self.release_file.write_text(json.dumps(broken), encoding="utf-8")
        self.assertIn("error:", self.cli("workspace", "create", "new-screen", expect=1))
        self.assertFalse((self.root / "new-screen").exists())
        self.assertFalse((self.root / ".orchestrators" / "new-screen.git").exists())
        self.assertEqual(self.config().workspaces, {})

    # --- workspace members and removal

    def test_add_and_remove_a_workspace_member(self):
        self.register_members()
        self.cli("workspace", "create", "new-screen", "--members", "web")
        coordinator = self.root / "new-screen" / "new-screen"
        self.cli("workspace", "add-member", "new-screen", "api")
        self.assertIn("## api\n", (coordinator / ".project" / "MEMBERS.md").read_text(encoding="utf-8"))
        self.assertIn("is already in", self.cli("workspace", "add-member", "new-screen", "api", expect=1))
        self.cli("workspace", "remove-member", "new-screen", "api")
        self.assertNotIn("## api", (coordinator / ".project" / "MEMBERS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "new-screen" / "api").exists())
        self.assertEqual(self.config().workspaces["new-screen"]["members"], ["web"])

    def test_add_member_refuses_a_coordinator_on_another_release(self):
        self.register_members()
        self.cli("workspace", "create", "new-screen", "--members", "web")
        newer = json.loads(self.release_file.read_text(encoding="utf-8"))
        newer["version"] = "9.9.9.99"
        self.release_file.write_text(json.dumps(newer), encoding="utf-8")
        self.assertIn("upgrade the coordinator", self.cli("workspace", "add-member", "new-screen", "api", expect=1))

    def test_remove_refuses_an_active_milestone_and_unsaved_work(self):
        self.register_members()
        self.cli("workspace", "create", "new-screen", "--members", "web")
        self.assertIn("is at define/active", self.cli("workspace", "remove", "new-screen", expect=1))
        web = self.root / "new-screen" / "web"
        (web / "scratch.txt").write_text("unsaved\n", encoding="utf-8")
        self.assertTrue((self.root / "new-screen").exists())
        self.cli("workspace", "remove", "new-screen", "--force")
        self.assertFalse((self.root / "new-screen").exists())
        self.assertTrue((self.root / ".orchestrators" / "new-screen.git").is_dir())
        self.assertEqual(self.config().workspaces, {})
        self.assertEqual(self.config().reserved, ["new-screen"])
        self.assertIn("was already used", self.cli("workspace", "create", "new-screen", expect=1))
        self.cli("workspace", "create", "new-screen", "--project", "new-screen-2-orchestrator", "--members", "web")
        self.assertTrue((self.root / "new-screen" / "new-screen-2-orchestrator").is_dir())
