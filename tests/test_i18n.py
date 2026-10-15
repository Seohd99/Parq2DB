import ast
import json
import re
from pathlib import Path

from app.core.errors import ERROR_STATUS, WARNING_CODES
from app.core.languages import (
    default_language,
    language_code_for_locale,
    language_codes,
    supported_languages,
)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
I18N = APP / "i18n"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load(lang: str) -> dict:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def messages(catalog: dict) -> dict[str, str]:
    return {key: value for key, value in catalog.items() if key != "meta"}


def test_all_catalogs_have_matching_keys_and_placeholders():
    languages = supported_languages()
    assert languages
    codes = tuple(language["code"] for language in languages)
    assert codes == language_codes()
    catalogs = {code: load(code) for code in codes}

    for language in languages:
        metadata = catalogs[language["code"]].get("meta")
        assert isinstance(metadata, dict)
        assert metadata.get("native_name") == language["native_name"]

    baseline = messages(catalogs[default_language(languages)])
    for lang, catalog in catalogs.items():
        translated = messages(catalog)
        assert set(translated) == set(baseline), f"catalog key mismatch for {lang}"
        for key in baseline:
            fields = set(PLACEHOLDER.findall(baseline[key]))
            translated_fields = set(PLACEHOLDER.findall(translated[key]))
            assert fields == translated_fields, f"placeholder mismatch for {lang} key {key}"


def test_language_discovery_uses_only_catalog_filenames_and_metadata(tmp_path):
    (tmp_path / "ja.json").write_text(
        json.dumps({"meta": {"native_name": "日本語"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    languages = supported_languages(tmp_path)
    assert languages == [{"code": "ja", "native_name": "日本語"}]
    assert language_codes(tmp_path) == ("ja",)
    assert language_code_for_locale("ja-JP", languages) == "ja"
    assert language_code_for_locale("Japanese_Japan", languages) == "ja"
    assert default_language(languages) == "ja"


def test_error_and_warning_keys_match_code_registry():
    for language in supported_languages():
        translations = messages(load(language["code"]))
        error_keys = {key.removeprefix("error.") for key in translations if key.startswith("error.")}
        warning_keys = {key.removeprefix("warning.") for key in translations if key.startswith("warning.")}
        assert error_keys == set(ERROR_STATUS), language["code"]
        assert warning_keys == set(WARNING_CODES), language["code"]


def test_raised_codes_and_parameters_match_translations():
    used_codes: set[str] = set()
    calls: list[tuple[str, int, str, set[str]]] = []
    for source in APP.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            if name not in {"PBError", "warning"} or not node.args:
                continue
            code_node = node.args[0]
            if not isinstance(code_node, ast.Constant) or not isinstance(code_node.value, str):
                continue
            code = code_node.value
            used_codes.add(code)
            params = {
                item.arg
                for item in node.keywords
                if item.arg is not None and item.arg != "status"
            }
            calls.append((str(source), node.lineno, code, params))

    declared_codes = set(ERROR_STATUS) | set(WARNING_CODES)
    # HTTP exception handlers return these codes directly instead of raising PBError.
    for source in APP.rglob("*.py"):
        if source.name == "errors.py":
            continue
        text = source.read_text(encoding="utf-8")
        used_codes.update(re.findall(r"['\"]([EW]_[A-Z0-9_]+)['\"]", text))
    assert used_codes == declared_codes

    for language in supported_languages():
        translations = messages(load(language["code"]))
        for source, line, code, params in calls:
            prefix = "error." if code in ERROR_STATUS else "warning."
            key = prefix + code
            assert key in translations, f"{language['code']} missing {source}:{line} translation for {code}"
            placeholders = set(PLACEHOLDER.findall(translations[key]))
            assert params <= placeholders, (
                f"{language['code']} {source}:{line} passes unused params for {code}: "
                f"{params - placeholders}"
            )


def test_frontend_i18n_references_exist_in_all_languages():
    html = (APP / "static" / "index.html").read_text(encoding="utf-8")
    js = (APP / "static" / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n="([^\"]+)"', html))
    keys.update(re.findall(r'\bt\(\s*["\']([^"\']+)["\']', js))
    dynamic_prefixes = re.findall(r'\bt\(\s*`([^`$]+)\$\{', js)
    for language in supported_languages():
        translations = messages(load(language["code"]))
        missing = sorted(key for key in keys if key not in translations)
        assert not missing, f"{language['code']} missing frontend keys: {missing}"
        for prefix in dynamic_prefixes:
            assert any(key.startswith(prefix) for key in translations), (
                f"{language['code']} has no keys for dynamic prefix {prefix}"
            )
