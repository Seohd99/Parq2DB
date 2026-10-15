import ast
import json
import re
from pathlib import Path

from app.core.errors import ERROR_STATUS, WARNING_CODES

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
I18N = APP / "i18n"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load(lang: str) -> dict[str, str]:
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def test_ko_en_have_same_keys_and_placeholders():
    ko = load("ko")
    en = load("en")
    assert set(ko) == set(en)
    for key in ko:
        ko_fields = set(PLACEHOLDER.findall(ko[key]))
        en_fields = set(PLACEHOLDER.findall(en[key]))
        assert ko_fields == en_fields, f"placeholder mismatch for {key}: ko={ko_fields}, en={en_fields}"


def test_error_and_warning_keys_match_code_registry():
    translations = load("ko")
    error_keys = {key.removeprefix("error.") for key in translations if key.startswith("error.")}
    warning_keys = {key.removeprefix("warning.") for key in translations if key.startswith("warning.")}
    assert error_keys == set(ERROR_STATUS)
    assert warning_keys == set(WARNING_CODES)


def test_raised_codes_and_parameters_match_translations():
    translations = load("ko")
    used_codes: set[str] = set()
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
            prefix = "error." if name == "PBError" else "warning."
            key = prefix + code
            assert key in translations, f"{source}:{node.lineno} uses untranslated {code}"
            params = {
                item.arg
                for item in node.keywords
                if item.arg is not None and item.arg != "status"
            }
            placeholders = set(PLACEHOLDER.findall(translations[key]))
            assert params <= placeholders, (
                f"{source}:{node.lineno} passes unused params for {code}: {params - placeholders}"
            )

    declared_codes = set(ERROR_STATUS) | set(WARNING_CODES)
    # HTTP exception handlers return these codes directly instead of raising PBError.
    for source in APP.rglob("*.py"):
        if source.name == "errors.py":
            continue
        text = source.read_text(encoding="utf-8")
        used_codes.update(re.findall(r"['\"]([EW]_[A-Z0-9_]+)['\"]", text))
    assert used_codes == declared_codes


def test_frontend_i18n_references_exist_in_both_languages():
    html = (APP / "static" / "index.html").read_text(encoding="utf-8")
    js = (APP / "static" / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'data-i18n="([^"]+)"', html))
    keys.update(re.findall(r'\bt\(\s*["\']([^"\']+)["\']', js))
    dynamic_prefixes = re.findall(r'\bt\(\s*`([^`$]+)\$\{', js)
    for lang in ("ko", "en"):
        messages = load(lang)
        missing = sorted(key for key in keys if key not in messages)
        assert not missing, f"{lang} missing frontend keys: {missing}"
        for prefix in dynamic_prefixes:
            assert any(key.startswith(prefix) for key in messages), f"{lang} has no keys for dynamic prefix {prefix}"
