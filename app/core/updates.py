"""Check GitHub for a newer Parq2DB release.

The check sends one unauthenticated GET to the GitHub releases API with a Parq2DB
User-Agent and nothing else about the user, files, or databases. Results are kept in
memory only: a successful answer for a day, a failure for an hour, so restarts and page
reloads stay well under GitHub's unauthenticated rate limit. Failures never raise; the
UI simply shows nothing. Set ``[updates] check = false`` to turn the check off.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from app import __version__

REPOSITORY = "Seohd99/Parq2DB"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases/tag/v{{version}}"
SUCCESS_TTL_SECONDS = 24 * 60 * 60
FAILURE_TTL_SECONDS = 60 * 60
TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 1024 * 1024

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")

Fetcher = Callable[[], dict[str, Any]]


def parse_version(value: str) -> tuple[int, int, int, tuple[int, ...] | None] | None:
    """Parse MAJOR.MINOR.PATCH with an optional pre-release; return None when invalid.

    A pre-release sorts before its release, matching SemVer for the tags this project uses
    (for example 0.3.0-rc.1 < 0.3.0). Pre-release parts compare numerically where possible.
    """
    match = _VERSION.match(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    pre_key = None if pre is None else tuple(int(part) if part.isdigit() else -1 for part in pre.split("."))
    return int(major), int(minor), int(patch), pre_key


def is_newer(candidate: str, current: str) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    if new is None or old is None:
        return False
    if new[:3] != old[:3]:
        return new[:3] > old[:3]
    if new[3] is None or old[3] is None:
        return new[3] is None and old[3] is not None
    return new[3] > old[3]


def fetch_latest_release() -> dict[str, Any]:
    """Return the latest published release JSON from GitHub."""
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Parq2DB/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("release response is too large")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("release response is not an object")
    return data


class UpdateChecker:
    """Cache the latest-release answer and compare it with the running version."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        current_version: str = __version__,
        fetcher: Fetcher = fetch_latest_release,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self.current_version = current_version
        self._fetcher = fetcher
        self._clock = clock
        self._lock = threading.Lock()
        self._result: dict[str, Any] | None = None
        self._expires_at = 0.0

    def status(self) -> dict[str, Any]:
        """Return the cached state, refreshing it when it has expired."""
        if not self.enabled:
            return self._payload("disabled")
        with self._lock:
            if self._result is None or self._clock() >= self._expires_at:
                self._result = self._check()
                ttl = FAILURE_TTL_SECONDS if self._result["status"] == "unavailable" else SUCCESS_TTL_SECONDS
                self._expires_at = self._clock() + ttl
            return dict(self._result)

    def _check(self) -> dict[str, Any]:
        try:
            release = self._fetcher()
        except Exception:
            # Offline, proxy, rate-limit, and malformed-response failures all mean "unknown".
            return self._payload("unavailable")
        tag = release.get("tag_name")
        if release.get("draft") or release.get("prerelease") or not isinstance(tag, str):
            return self._payload("unavailable")
        parsed = parse_version(tag)
        if parsed is None:
            return self._payload("unavailable")
        latest = tag.strip().removeprefix("v")
        if not is_newer(latest, self.current_version):
            return self._payload("current", latest_version=latest)
        # Build the link from the validated version instead of trusting a URL from the response.
        return self._payload(
            "available",
            latest_version=latest,
            release_url=RELEASE_PAGE.format(version=latest),
        )

    def _payload(self, status: str, **fields: Any) -> dict[str, Any]:
        return {
            "status": status,
            "current_version": self.current_version,
            "latest_version": fields.get("latest_version"),
            "release_url": fields.get("release_url"),
        }
