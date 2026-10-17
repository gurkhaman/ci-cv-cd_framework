"""Fail when a committed Markdown file names a repository path that is gone."""

from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
CODE_SPAN = re.compile(r"`([^`\s]+)`")
LINK_TARGET = re.compile(r"\]\(([^)\s#]+)")
NOT_A_PATH = re.compile(r"://|[*?<>{}\[\]$:@=~|]|^[/-]")


def _git(
    root: Path, *arguments: str, stdin: str | None = None, check: bool = True
) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        check=check,
    ).stdout


def _with_parents(files: set[str]) -> set[str]:
    entries = set(files)
    for name in files:
        parent = posixpath.dirname(name)
        while parent and parent not in entries:
            entries.add(parent)
            parent = posixpath.dirname(parent)
    return entries


def _candidates(document: str, token: str, entries: set[str]) -> list[str]:
    path = token.rstrip("/")
    if "/" not in path or NOT_A_PATH.search(path):
        return []
    directory = posixpath.dirname(document)
    first = path.split("/", 1)[0]
    if first in {".", ".."}:
        return [posixpath.normpath(posixpath.join(directory, path))]
    candidates: list[str] = []
    if first in entries:
        candidates.append(posixpath.normpath(path))
    if directory and posixpath.join(directory, first) in entries:
        candidates.append(posixpath.normpath(posixpath.join(directory, path)))
    return candidates


def missing_paths(root: Path) -> list[str]:
    tracked = {name for name in _git(root, "ls-files", "-z").split("\0") if name}
    entries = _with_parents(tracked)
    named: list[tuple[str, list[str]]] = []
    for document in sorted(tracked):
        if not document.endswith(".md") or document.startswith("CV/gazebo/models/"):
            continue
        lines = (root / document).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            for token in CODE_SPAN.findall(line) + LINK_TARGET.findall(line):
                candidates = _candidates(document, token, entries)
                if candidates and not entries.intersection(candidates):
                    named.append((f"{document}:{number}: {token}", candidates))
    ignored = set(
        _git(
            root,
            "check-ignore",
            "--no-index",
            "--stdin",
            stdin="\n".join(path for _, paths in named for path in paths),
            check=False,
        ).splitlines()
    )
    return [where for where, paths in named if ignored.isdisjoint(paths)]


def test_committed_markdown_names_only_existing_paths() -> None:
    missing = missing_paths(SOURCE_ROOT)

    assert not missing, (
        "Markdown names a repository path that does not exist; "
        "correct or delete each reference:\n" + "\n".join(missing)
    )


def test_a_missing_path_is_reported_with_its_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "docs/config").mkdir(parents=True)
    (tmp_path / "docs/config/example.env").write_text("KEY=\n", encoding="utf-8")
    (tmp_path / "docs/guide.md").write_text(
        "See `docs/guide.md` and [the notes](notes.md).\n"
        "Copy it to the ignored `config/local/settings.env` first.\n"
        "Read `docs/gone.md` and [the setup](../setup/README.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/notes.md").write_text("Notes.\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("/docs/config/local/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)

    assert missing_paths(tmp_path) == [
        "docs/guide.md:3: docs/gone.md",
        "docs/guide.md:3: ../setup/README.md",
    ]
