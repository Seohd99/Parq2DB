"""Host and Origin checks that block DNS rebinding and cross-site requests.

A page on another site can rebind its own domain to 127.0.0.1 and become same-origin with
this server, so the Host header must name this server. Cross-site form posts (such as an
upload) skip CORS preflight, so state-changing requests that carry an Origin must also
come from an allowed host.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
WILDCARD_HOSTS: frozenset[str] = frozenset({"0.0.0.0", "::", ""})
SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def allowed_host_names(bind_host: str, extra: Iterable[str] = ()) -> frozenset[str]:
    """Return lowercase host names, without ports, that requests may address."""
    names = set(LOOPBACK_HOSTS)
    if bind_host not in WILDCARD_HOSTS:
        names.add(bind_host)
    names.update(extra)
    return frozenset(_normalize(name) for name in names if name)


def _normalize(name: str) -> str:
    return name.strip().strip("[]").rstrip(".").casefold()


def host_header_name(value: str | None) -> str | None:
    """Return the host name from a Host header, or None when it is missing or malformed."""
    if not value:
        return None
    try:
        name = urlsplit(f"//{value}").hostname
    except ValueError:
        return None
    return _normalize(name) if name else None


def origin_host_name(value: str) -> str | None:
    """Return the host name from an Origin header; an opaque origin ("null") has none."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return _normalize(parts.hostname)


def request_rejection(
    method: str,
    host: str | None,
    origin: str | None,
    allowed: frozenset[str],
) -> str | None:
    """Return an error code when a request must be refused, otherwise None."""
    if host_header_name(host) not in allowed:
        return "E_HOST_NOT_ALLOWED"
    if origin is not None and method.upper() not in SAFE_METHODS and origin_host_name(origin) not in allowed:
        return "E_ORIGIN_NOT_ALLOWED"
    return None
