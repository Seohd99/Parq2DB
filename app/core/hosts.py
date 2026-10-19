"""Host and Origin checks that block DNS rebinding and cross-site requests.

A page on another site can rebind its own domain to 127.0.0.1 and become same-origin with
this server, so the Host header must name this server. Cross-site form posts (such as an
upload) skip CORS preflight, so state-changing requests that carry an Origin must match the
request's scheme, host, and port.
"""

from __future__ import annotations

import ipaddress
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
    normalized: set[str] = set()
    for value in names:
        name = normalize_allowed_host(value)
        if name:
            normalized.add(name)
    return frozenset(normalized)


def normalize_allowed_host(value: str) -> str | None:
    """Return a normalized allowed host name, dropping an optional port."""
    parsed = _parse_authority(value, allow_bare_ipv6=True)
    return parsed[0] if parsed is not None else None


def _normalize(name: str) -> str:
    return name.strip().strip("[]").rstrip(".").casefold()


def _parse_authority(value: str, *, allow_bare_ipv6: bool = False) -> tuple[str, int | None] | None:
    raw = value.strip()
    if not raw:
        return None
    if allow_bare_ipv6 and raw.count(":") > 1 and not raw.startswith("["):
        try:
            ipaddress.IPv6Address(raw)
        except ValueError:
            return None
        return _normalize(raw), None
    try:
        parts = urlsplit(f"//{raw}")
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if (
        not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return None
    return _normalize(hostname), port


def host_header_name(value: str | None) -> str | None:
    """Return the host name from a Host header, or None when it is missing or malformed."""
    if not value:
        return None
    parsed = _parse_authority(value)
    return parsed[0] if parsed is not None else None


def _origin_parts(value: str) -> tuple[str, str, int | None] | None:
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.casefold()
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or not hostname
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return None
    return _normalize(hostname), scheme, port


def origin_host_name(value: str) -> str | None:
    """Return the host name from an Origin header; an opaque origin ("null") has none."""
    parts = _origin_parts(value)
    return parts[0] if parts is not None else None


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme)


def request_rejection(
    method: str,
    host: str | None,
    origin: str | None,
    allowed: frozenset[str],
    scheme: str = "http",
) -> str | None:
    """Return an error code when a request must be refused, otherwise None."""
    host_parts = _parse_authority(host) if host else None
    if host_parts is None or host_parts[0] not in allowed:
        return "E_HOST_NOT_ALLOWED"
    if origin is None or method.upper() in SAFE_METHODS:
        return None

    origin_parts = _origin_parts(origin)
    request_scheme = scheme.casefold()
    if origin_parts is None or request_scheme not in {"http", "https"}:
        return "E_ORIGIN_NOT_ALLOWED"
    origin_name, origin_scheme, origin_port = origin_parts
    host_name, host_port = host_parts
    request_port = host_port if host_port is not None else _default_port(request_scheme)
    origin_port = origin_port if origin_port is not None else _default_port(origin_scheme)
    if (
        origin_name != host_name
        or origin_name not in allowed
        or origin_scheme != request_scheme
        or origin_port != request_port
    ):
        return "E_ORIGIN_NOT_ALLOWED"
    return None
