from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopweave.config import resolve_codex_bin


class CodexBinaryResolutionTests(unittest.TestCase):
    def test_current_chatgpt_desktop_binary_wins_over_legacy_codex_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            legacy = root / "Codex.app" / "Contents" / "Resources" / "codex"
            for binary in (desktop, legacy):
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)

            with patch.dict(
                os.environ,
                {"LOOPWEAVE_CODEX_BIN": ""},
                clear=False,
            ), patch(
                "loopweave.config.DESKTOP_CODEX_BIN",
                desktop,
            ), patch(
                "loopweave.config.CODEX_BIN",
                legacy,
            ):
                resolved = resolve_codex_bin()

        self.assertEqual(resolved, desktop)

    def test_prefers_current_desktop_binary_over_path_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            path_codex = root / "bin" / "codex"
            for binary in (desktop, path_codex):
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)

            with patch.dict(
                os.environ,
                {"LOOPWEAVE_CODEX_BIN": ""},
                clear=False,
            ), patch(
                "loopweave.config.DESKTOP_CODEX_BIN",
                desktop,
                create=True,
            ), patch(
                "loopweave.config.CODEX_BIN",
                root / "missing-legacy-codex",
            ), patch(
                "loopweave.config.shutil.which",
                return_value=str(path_codex),
            ):
                resolved = resolve_codex_bin()

        self.assertEqual(resolved, desktop)

    def test_resolves_path_codex_when_legacy_app_binary_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "codex"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)

            with patch.dict(
                os.environ,
                {"LOOPWEAVE_CODEX_BIN": ""},
                clear=False,
            ), patch(
                "loopweave.config.shutil.which",
                return_value=str(binary),
            ), patch(
                "loopweave.config.CODEX_BIN",
                Path(directory) / "missing-legacy-codex",
            ), patch(
                "loopweave.config.DESKTOP_CODEX_BIN",
                Path(directory) / "missing-desktop-codex",
            ):
                resolved = resolve_codex_bin()

        self.assertEqual(resolved, binary)

    def test_explicit_override_wins_over_path_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "override-codex"
            discovered = Path(directory) / "path-codex"
            for binary in (override, discovered):
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)

            with patch.dict(
                os.environ,
                {"LOOPWEAVE_CODEX_BIN": str(override)},
                clear=False,
            ), patch(
                "loopweave.config.shutil.which",
                return_value=str(discovered),
            ):
                resolved = resolve_codex_bin()

        self.assertEqual(resolved, override)


if __name__ == "__main__":
    unittest.main()
