from __future__ import annotations

import ast
import fnmatch
import io
import re
import subprocess
import tokenize
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
    Path("requirements.txt"),
    Path("uv.lock"),
    Path(".python-version"),
)

_WINDOWS_HOME = re.compile(
    r"(?i)(?<![\w.])[A-Z]:[\\/]+Users[\\/]+[A-Za-z0-9._-]+"
)
_POSIX_HOME = re.compile(
    r"(?i)(?<![\w.])/(?:Users|home)/[A-Za-z0-9._-]+"
)
_APP_DATA = "App" + "Data"
_POSIX_DATA_ROOT = "/" + "home" + "/" + "me" + "/" + "data"
_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_COMMENT_CONFIGS = {
    Path(".gitignore"),
    Path(".gitattributes"),
    Path("pyproject.toml"),
    Path("pytest.ini"),
    Path("requirements.txt"),
    Path("config/parqbridge.example.toml"),
}


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


def _tracked_comment_source_files() -> tuple[Path, ...]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    files: list[Path] = []
    for raw_path in tracked:
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8"))
        parts = relative.parts
        in_scope = (
            (parts[0] == "app" and relative.suffix == ".py")
            or (parts[:2] == ("app", "static") and len(parts) == 3 and relative.suffix in {".js", ".css", ".html"})
            or (parts[0] in {"tests", "tools"} and relative.suffix == ".py")
            or relative in _COMMENT_CONFIGS
        )
        if in_scope:
            files.append(ROOT / relative)
    return tuple(files)


def _line_numbers_with_hangul(text: str, start_line: int) -> list[int]:
    return [
        start_line + offset
        for offset, line in enumerate(text.splitlines())
        if _HANGUL.search(line)
    ]


def _python_comment_fragments(source: str) -> list[tuple[int, str]]:
    fragments = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    tree = ast.parse(source)
    docstring_nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, docstring_nodes):
            docstring = ast.get_docstring(node, clean=False)
            if docstring is not None:
                fragments.append((node.body[0].lineno, docstring))
    return fragments


def _slash_comment_fragments(source: str, *, line_comments: bool, templates: bool) -> list[tuple[int, str]]:
    fragments: list[tuple[int, str]] = []
    length = len(source)

    def scan_quoted(index: int, quote: str) -> int:
        index += 1
        while index < length:
            if source[index] == "\\":
                index += 2
            elif source[index] == quote:
                return index + 1
            else:
                index += 1
        return index

    def scan_template(index: int) -> int:
        index += 1
        while index < length:
            if source[index] == "\\":
                index += 2
            elif source[index] == "`":
                return index + 1
            elif source.startswith("${", index):
                index = scan_code(index + 2, stop_at_brace=True)
            else:
                index += 1
        return index

    def scan_code(index: int, *, stop_at_brace: bool = False) -> int:
        brace_depth = 0
        while index < length:
            char = source[index]
            if char in {"'", '"'}:
                index = scan_quoted(index, char)
            elif templates and char == "`":
                index = scan_template(index)
            elif line_comments and source.startswith("//", index):
                end = source.find("\n", index)
                if end < 0:
                    end = length
                fragments.append((source.count("\n", 0, index) + 1, source[index:end]))
                index = end
            elif source.startswith("/*", index):
                end = source.find("*/", index + 2)
                end = length if end < 0 else end + 2
                fragments.append((source.count("\n", 0, index) + 1, source[index:end]))
                index = end
            elif stop_at_brace and char == "{":
                brace_depth += 1
                index += 1
            elif stop_at_brace and char == "}":
                if brace_depth == 0:
                    return index + 1
                brace_depth -= 1
                index += 1
            else:
                index += 1
        return index

    scan_code(0)
    return fragments


def _hash_comment_fragments(source: str, *, whole_line_only: bool) -> list[tuple[int, str]]:
    fragments: list[tuple[int, str]] = []
    quote: str | None = None
    triple = False
    escaped = False
    line_start = 0
    index = 0
    while index < len(source):
        char = source[index]
        if quote is not None:
            delimiter = quote * (3 if triple else 1)
            if triple and source.startswith(delimiter, index):
                quote = None
                triple = False
                index += 3
            elif not triple and escaped:
                escaped = False
                index += 1
            elif not triple and quote == '"' and char == "\\":
                escaped = True
                index += 1
            elif not triple and char == quote:
                quote = None
                index += 1
            else:
                index += 1
            continue

        if char in {'"', "'"}:
            quote = char
            triple = source.startswith(char * 3, index)
            index += 3 if triple else 1
        elif char == "#":
            line_end = source.find("\n", index)
            if line_end < 0:
                line_end = len(source)
            if not whole_line_only or not source[line_start:index].strip():
                fragments.append((source.count("\n", 0, index) + 1, source[index:line_end]))
            index = line_end
        else:
            if char == "\n":
                line_start = index + 1
            index += 1
    return fragments


def _comment_fragments(path: Path, source: str) -> list[tuple[int, str]]:
    if path.suffix == ".py":
        return _python_comment_fragments(source)
    if path.suffix == ".js":
        return _slash_comment_fragments(source, line_comments=True, templates=True)
    if path.suffix == ".css":
        return _slash_comment_fragments(source, line_comments=False, templates=False)
    if path.suffix == ".html":
        return [
            (source.count("\n", 0, match.start()) + 1, match.group(0))
            for match in re.finditer(r"<!--.*?-->", source, flags=re.DOTALL)
        ]
    return _hash_comment_fragments(
        source,
        whole_line_only=path in {Path(".gitignore"), Path(".gitattributes")},
    )


def test_tracked_source_comments_and_docstrings_are_english() -> None:
    violations: list[str] = []
    for path in _tracked_comment_source_files():
        relative = path.relative_to(ROOT)
        source = path.read_text(encoding="utf-8")
        for start_line, fragment in _comment_fragments(relative, source):
            violations.extend(
                f"{relative}:{line}"
                for line in _line_numbers_with_hangul(fragment, start_line)
            )

    assert not violations, "Hangul in source comments or docstrings:\n" + "\n".join(violations)
