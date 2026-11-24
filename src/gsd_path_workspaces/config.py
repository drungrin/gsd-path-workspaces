"""The `workspaces.json` file at the workspace root."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

CONFIG_NAME = "workspaces.json"
BARES_DIR = ".orchestrators"
SCHEMA = "gsd-path-workspaces/v1"
# GSD Path's member name rule (members.py NAME_RE). Workspace names follow
# it too, so the coordinator folder slug equals the project name exactly.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
INTEGRATIONS = ("direct", "pull-request", "external-landing")


class WorkspacesError(Exception):
    pass


def valid_name(kind: str, name: str) -> str:
    if not NAME_RE.fullmatch(name) or name.endswith("-") or "--" in name:
        raise WorkspacesError(f"invalid {kind} name: {name!r} (use a-z, 0-9 and single hyphens)")
    return name


def find_root(start: Optional[Path] = None) -> Path:
    """The nearest directory, from `start` upward, that holds workspaces.json."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if (directory / CONFIG_NAME).is_file():
            return directory
    raise WorkspacesError(f"{CONFIG_NAME} not found from {current} upward; run `init` at the root")


class Config:
    def __init__(self, root: Path, data: dict):
        self.root = root
        self.data = data

    # --- persistence
    @classmethod
    def create(cls, root: Path) -> "Config":
        path = root / CONFIG_NAME
        if path.exists():
            raise WorkspacesError(f"already exists: {path}")
        config = cls(root.resolve(), {"schema": SCHEMA, "members": {}, "workspaces": {}, "reserved_projects": []})
        config.save()
        return config

    @classmethod
    def load(cls, root: Path) -> "Config":
        path = root / CONFIG_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkspacesError(f"could not read {path}: {error}") from error
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise WorkspacesError(f"{path} does not have schema {SCHEMA}")
        for key, kind in (("members", dict), ("workspaces", dict), ("reserved_projects", list)):
            if not isinstance(data.get(key), kind):
                raise WorkspacesError(f"{path}: field {key} missing or invalid")
        return cls(root.resolve(), data)

    def save(self) -> None:
        path = self.root / CONFIG_NAME
        text = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        handle, temporary = tempfile.mkstemp(prefix=".workspaces-", suffix=".json", dir=self.root)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as output:
                output.write(text)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    # --- members
    @property
    def members(self) -> dict:
        return self.data["members"]

    def member(self, name: str) -> dict:
        try:
            return self.members[name]
        except KeyError:
            raise WorkspacesError(f"member not registered: {name}") from None

    # --- workspaces
    @property
    def workspaces(self) -> dict:
        return self.data["workspaces"]

    def workspace(self, name: str) -> dict:
        try:
            return self.workspaces[name]
        except KeyError:
            raise WorkspacesError(f"workspace not registered: {name}") from None

    def workspace_dir(self, name: str) -> Path:
        return self.root / name

    def coordinator_dir(self, name: str) -> Path:
        return self.workspace_dir(name) / self.workspace(name)["project"]

    def bare_dir(self, project: str) -> Path:
        """Named by project, which is never reused, so a kept bare never blocks a workspace name."""
        return self.root / BARES_DIR / f"{project}.git"

    # --- project names
    @property
    def reserved(self) -> list:
        return self.data["reserved_projects"]

    def reserve(self, project: str) -> None:
        if project not in self.reserved:
            self.reserved.append(project)
