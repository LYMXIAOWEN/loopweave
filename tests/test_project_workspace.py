from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopweave.project_workspace import (
    InvalidProjectSlug,
    ProjectWorkspaceConflict,
    WorkspaceNotFound,
    resolve_project_workspace,
)


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects = self.root / "projects"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_default_workspace_is_project_root(self) -> None:
        binding = resolve_project_workspace(
            projects_dir=self.projects,
            project_slug="demo",
            workspace=None,
        )

        self.assertEqual(binding.project_root, (self.projects / "demo").resolve())
        self.assertEqual(binding.workspace_root, binding.project_root)
        payload = json.loads(
            (binding.project_root / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["project_slug"], "demo")
        self.assertEqual(payload["workspace_root"], str(binding.project_root))

    def test_external_workspace_is_persisted_and_reused(self) -> None:
        external = self.root / "external"
        external.mkdir()

        first = resolve_project_workspace(self.projects, "example-project", external)
        second = resolve_project_workspace(self.projects, "example-project", external)

        self.assertEqual(first, second)
        self.assertEqual(first.workspace_root, external.resolve())

    def test_existing_project_rejects_workspace_change(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        resolve_project_workspace(self.projects, "demo", first)

        with self.assertRaises(ProjectWorkspaceConflict):
            resolve_project_workspace(self.projects, "demo", second)

    def test_slug_and_missing_workspace_fail_closed(self) -> None:
        for slug in ("../escape", "/absolute", "Bad Name", ""):
            with self.subTest(slug=slug):
                with self.assertRaises(InvalidProjectSlug):
                    resolve_project_workspace(self.projects, slug, None)

        with self.assertRaises(WorkspaceNotFound):
            resolve_project_workspace(
                self.projects,
                "demo",
                self.root / "missing",
            )

    def test_malformed_existing_binding_fails_closed(self) -> None:
        project = self.projects / "demo"
        project.mkdir(parents=True)
        (project / "project.json").write_text("[]\n", encoding="utf-8")

        with self.assertRaises(ProjectWorkspaceConflict):
            resolve_project_workspace(self.projects, "demo", None)


if __name__ == "__main__":
    unittest.main()
