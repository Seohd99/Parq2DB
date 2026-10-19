import io
import json

import pytest
from fastapi.testclient import TestClient

from app.core import updates
from app.core.config import AppConfig, UpdatesConfig, load_config
from app.core.errors import PBError
from app.core.updates import UpdateChecker, is_newer, parse_version
from app.main import app


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def release(tag="v0.4.0", **extra):
    return {"tag_name": tag, "html_url": "https://example.invalid/not-used", "draft": False, "prerelease": False, **extra}


@pytest.mark.parametrize(("candidate", "current", "newer"), [
    ("0.4.0", "0.3.0", True),
    ("v0.3.1", "0.3.0", True),
    ("1.0.0", "0.9.9", True),
    ("0.3.0", "0.3.0", False),
    ("0.2.9", "0.3.0", False),
    ("0.3.0", "0.3.0-rc.1", True),
    ("0.3.0-rc.2", "0.3.0-rc.1", True),
    ("0.3.0-rc.1", "0.3.0", False),
    ("latest", "0.3.0", False),
    ("0.4", "0.3.0", False),
])
def test_version_comparison(candidate, current, newer):
    assert is_newer(candidate, current) is newer


def test_parse_version_rejects_unexpected_text():
    assert parse_version("v0.3.0") == (0, 3, 0, None)
    assert parse_version("0.3.0 ; rm -rf") is None
    assert parse_version(None) is None


def test_available_release_builds_the_link_from_the_version():
    checker = UpdateChecker(current_version="0.3.0", fetcher=lambda: release("v0.4.0"))
    assert checker.status() == {
        "status": "available",
        "current_version": "0.3.0",
        "latest_version": "0.4.0",
        "release_url": "https://github.com/Seohd99/Parq2DB/releases/tag/v0.4.0",
    }


@pytest.mark.parametrize(("payload", "status"), [
    (release("v0.3.0"), "current"),
    (release("v0.2.1"), "current"),
    (release("v0.4.0", prerelease=True), "unavailable"),
    (release("v0.4.0", draft=True), "unavailable"),
    (release("nightly"), "unavailable"),
    ({"message": "API rate limit exceeded"}, "unavailable"),
])
def test_non_upgrade_answers_show_no_link(payload, status):
    result = UpdateChecker(current_version="0.3.0", fetcher=lambda: payload).status()
    assert result["status"] == status
    assert result["release_url"] is None


def test_results_are_cached_for_a_day_and_failures_for_an_hour():
    clock = Clock()
    calls = []

    def fetch():
        calls.append(clock.now)
        if len(calls) == 1:
            raise OSError("offline")
        return release("v0.4.0")

    checker = UpdateChecker(current_version="0.3.0", fetcher=fetch, clock=clock)
    assert checker.status()["status"] == "unavailable"
    clock.now += updates.FAILURE_TTL_SECONDS - 1
    assert checker.status()["status"] == "unavailable"
    assert len(calls) == 1
    clock.now += 1
    assert checker.status()["status"] == "available"
    clock.now += updates.SUCCESS_TTL_SECONDS - 1
    assert checker.status()["status"] == "available"
    assert len(calls) == 2
    clock.now += 1
    checker.status()
    assert len(calls) == 3


def test_disabled_checker_never_fetches():
    def fetch():
        raise AssertionError("the update check must not contact GitHub when disabled")

    assert UpdateChecker(enabled=False, current_version="0.3.0", fetcher=fetch).status()["status"] == "disabled"


def test_any_fetch_failure_is_reported_as_unavailable():
    import http.client

    for error in (OSError("offline"), TimeoutError(), http.client.IncompleteRead(b""), ValueError("bad json")):
        def fetch(error=error):
            raise error

        assert UpdateChecker(current_version="0.3.0", fetcher=fetch).status()["status"] == "unavailable"


def test_fetch_sends_only_a_user_agent_and_limits_the_response(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return Response(json.dumps(release()).encode())

    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    assert updates.fetch_latest_release()["tag_name"] == "v0.4.0"
    assert seen["url"] == "https://api.github.com/repos/Seohd99/Parq2DB/releases/latest"
    assert seen["timeout"] == updates.TIMEOUT_SECONDS
    assert seen["headers"]["User-agent"].startswith("Parq2DB/")
    assert request_has_no_credentials(seen["headers"])

    monkeypatch.setattr(updates, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(ValueError):
        updates.fetch_latest_release()


def request_has_no_credentials(headers):
    return not any(name.lower() in {"authorization", "cookie"} for name in headers)


def test_updates_config_section(tmp_path):
    assert load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={}).updates.check is True
    main = tmp_path / "pb.toml"
    main.write_text("[updates]\ncheck = false\n", encoding="utf-8")
    assert load_config(main, tmp_path / "x.toml", env={}).updates.check is False
    main.write_text('[updates]\ncheck = "no"\n', encoding="utf-8")
    with pytest.raises(PBError) as caught:
        load_config(main, tmp_path / "x.toml", env={})
    assert caught.value.code == "E_CONFIG_INVALID"


def test_updates_endpoint_uses_the_app_checker():
    app.state.config = AppConfig(updates=UpdatesConfig(check=False))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/updates").json()["status"] == "disabled"
        app.state.updates = UpdateChecker(current_version="0.3.0", fetcher=lambda: release("v0.4.0"))
        body = client.get("/api/updates").json()
        assert body["status"] == "available"
        assert body["latest_version"] == "0.4.0"
        assert client.get("/api/updates", headers={"host": "evil.example"}).status_code == 403
