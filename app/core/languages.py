"""Discover the language catalogs shared by the web UI and CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


I18N_DIR = Path(__file__).resolve().parents[1] / "i18n"


def supported_languages(directory: Path | None = None) -> list[dict[str, str]]:
    """Return catalog codes and their self-names, sorted by code."""
    catalog_dir = directory or I18N_DIR
    languages: list[dict[str, str]] = []
    for path in sorted(catalog_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as source:
            catalog: dict[str, Any] = json.load(source)
        metadata = catalog.get("meta")
        native_name = metadata.get("native_name") if isinstance(metadata, dict) else None
        if not isinstance(native_name, str) or not native_name.strip():
            raise ValueError(f"{path} must define meta.native_name")
        languages.append({"code": path.stem, "native_name": native_name.strip()})
    return languages


def language_codes(directory: Path | None = None) -> tuple[str, ...]:
    return tuple(language["code"] for language in supported_languages(directory))


def default_language(languages: list[dict[str, str]] | None = None) -> str:
    available = languages if languages is not None else supported_languages()
    for language in available:
        if language["code"] == "en":
            return "en"
    return available[0]["code"] if available else "en"


def language_code_for_locale(
    value: object,
    languages: list[dict[str, str]] | None = None,
) -> str | None:
    """Match a locale such as ``ja-JP`` to a discovered catalog code."""
    if not isinstance(value, str):
        return None
    base = value.strip().casefold().replace("_", "-").split("-", 1)[0]
    for language in languages if languages is not None else supported_languages():
        code = language["code"]
        if base.startswith(code.casefold()):
            return code
    return None
