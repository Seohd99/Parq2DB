from __future__ import annotations

import fnmatch
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TARGETS = (
    Path("README.md"),
    Path("README.ko.md"),
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("docs"),
    Path("config/parqbridge.example.toml"),
    Path("tools"),
    Path("tests"),
    Path("app"),
    Path("pyproject.toml"),
)

_WINDOWS_HOME = re.compile(
    r"(?i)(?<![\w.])[A-Z]:[\\/]+Users[\\/]+[A-Za-z0-9._-]+"
)
_POSIX_HOME = re.compile(
    r"(?i)(?<![\w.])/(?:Users|home)/[A-Za-z0-9._-]+"
)
_APP_DATA = "App" + "Data"
_POSIX_DATA_ROOT = "/" + "home" + "/" + "me" + "/" + "data"


def _gitignore_patterns() -> tuple[str, ...]:
    ignore_file = ROOT / ".gitignore"
    if not ignore_file.exists():
        return ()
    return tuple(
        line.strip().replace("\\", "/")
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _is_gitignored(relative_path: Path, patterns: tuple[str, ...]) -> bool:
    path_text = relative_path.as_posix()
    parts = relative_path.parts
    for pattern in patterns:
        if pattern.startswith("!"):
            continue
        if pattern.endswith("/"):
            directory_pattern = pattern.rstrip("/")
            if any(fnmatch.fnmatch(part, directory_pattern) for part in parts):
                return True
        elif "/" in pattern:
            if fnmatch.fnmatch(path_text, pattern) or relative_path.match(pattern):
                return True
        elif any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def _public_text_files() -> tuple[Path, ...]:
    patterns = _gitignore_patterns()
    files: set[Path] = set()
    for target in PUBLIC_TARGETS:
        candidate = ROOT / target
        if candidate.is_file():
            candidates = (candidate,)
        elif candidate.is_dir():
            candidates = candidate.rglob("*")
        else:
            continue
        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if _is_gitignored(relative, patterns):
                continue
            try:
                raw = path.read_bytes()
                if b"\0" in raw:
                    continue
                raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            files.add(path)
    return tuple(sorted(files))


def _public_path_matches(text: str) -> list[str]:
    # This documented POSIX example is a placeholder and may appear in config/CLI samples.
    text = re.sub(
        re.escape(_POSIX_DATA_ROOT) + r"(?=$|[/\s\"'),;\]])",
        "",
        text,
    )
    matches: list[str] = []
    for pattern in (_WINDOWS_HOME, _POSIX_HOME):
        matches.extend(match.group(0) for match in pattern.finditer(text))
    if _APP_DATA.casefold() in text.casefold():
        matches.append(_APP_DATA)
    return matches


def test_public_text_files_do_not_expose_machine_specific_paths() -> None:
    violations: list[str] = []
    for path in _public_text_files():
        relative = path.relative_to(ROOT).as_posix()
        content = path.read_text(encoding="utf-8")
        violations.extend(
            f"{relative}: {match}"
            for match in _public_path_matches(content)
        )

    assert not violations, "Machine-specific paths in public files:\n" + "\n".join(violations)


def test_local_agent_instructions_are_excluded_from_public_files() -> None:
    relative = Path("AGENTS.local.md")
    patterns = _gitignore_patterns()

    assert "AGENTS.local.md" in patterns
    assert _is_gitignored(relative, patterns)
    assert relative not in {path.relative_to(ROOT) for path in _public_text_files()}
