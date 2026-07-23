from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOTS = ("src", "tests", "plugins", "docs", "bin")
PUBLIC_FILES = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "pyproject.toml",
)
FORBIDDEN_IDENTIFIERS = (
    "zero" + "_one",
    "zero" + "-one",
    "zero" + " one",
    "ZERO" + "_ONE",
    "Zero" + "One",
    "零" + "壹",
    "零" + "一",
    "z" + "1-",
)


def _public_files() -> list[Path]:
    files = [REPOSITORY_ROOT / name for name in PUBLIC_FILES]
    for root_name in PUBLIC_ROOTS:
        files.extend(
            path
            for path in (REPOSITORY_ROOT / root_name).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and not any(part.endswith(".egg-info") for part in path.parts)
        )
    return files


def test_public_tree_uses_loopweave_identity_only() -> None:
    violations: list[str] = []
    for path in _public_files():
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        for identifier in FORBIDDEN_IDENTIFIERS:
            if identifier.lower() in relative.lower():
                violations.append(relative)
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for identifier in FORBIDDEN_IDENTIFIERS:
            if identifier.lower() in content.lower():
                violations.append(relative)
    assert not violations, "historical identity found in: {}".format(
        ", ".join(sorted(set(violations)))
    )
