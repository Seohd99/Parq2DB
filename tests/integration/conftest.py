"""Local profiles are optional; CI requires its isolated database profile."""

from pathlib import Path

import pytest

from app.core.config import load_config


def pytest_addoption(parser):
    group = parser.getgroup("database integration")
    group.addoption("--db-config", type=Path, help="Dedicated integration-test TOML profile")
    group.addoption("--require-db", action="store_true", help="Fail instead of skipping an offline profile")


@pytest.fixture(params=[
    pytest.param("oracle", marks=pytest.mark.oracle),
    pytest.param("mysql", marks=pytest.mark.mysql),
    pytest.param("postgres", marks=pytest.mark.postgres),
])
def db_profile(request):
    path = request.config.getoption("--db-config")
    # An explicit test configuration must not merge a developer's local passwords.
    cfg = load_config(path, path.with_name(path.name + ".local")) if path else load_config()
    profile = cfg.db[request.param]
    if not profile.online:
        reason = profile.offline_reason or "Database profile is offline"
        if request.config.getoption("--require-db"):
            pytest.fail(f"Required {request.param} integration profile is offline: {reason}")
        pytest.skip(reason)
    return profile
