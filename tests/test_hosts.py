import pytest

from app.core.hosts import allowed_host_names, host_header_name, origin_host_name, request_rejection


def test_allowed_host_names_strip_optional_ports():
    allowed = allowed_host_names("0.0.0.0", ["pb.lan:8765", "[::1]:8765"])

    assert "pb.lan" in allowed
    assert "pb.lan:8765" not in allowed
    assert "::1" in allowed


@pytest.mark.parametrize(
    ("value", "expected"),
    [("[::1]:8765", "::1"), ("PB.LAN.:8765", "pb.lan"), ("pb.lan:3000", "pb.lan")],
)
def test_host_header_name_normalizes_brackets_trailing_dot_and_port(value, expected):
    assert host_header_name(value) == expected


def test_null_origin_is_opaque_and_rejected_for_posts():
    allowed = allowed_host_names("127.0.0.1")

    assert origin_host_name("null") is None
    assert request_rejection("POST", "127.0.0.1:8765", "null", allowed) == "E_ORIGIN_NOT_ALLOWED"


def test_posts_require_same_origin_host_scheme_and_port():
    allowed = allowed_host_names("127.0.0.1")

    assert request_rejection("POST", "127.0.0.1:8765", "http://127.0.0.1:8765", allowed) is None
    assert request_rejection("POST", "127.0.0.1:8765", "http://127.0.0.1:3000", allowed) == "E_ORIGIN_NOT_ALLOWED"
    assert request_rejection("POST", "127.0.0.1:8765", "https://127.0.0.1:8765", allowed) == "E_ORIGIN_NOT_ALLOWED"


def test_missing_host_is_rejected_and_get_keeps_origin_behavior():
    allowed = allowed_host_names("127.0.0.1")

    assert host_header_name(None) is None
    assert request_rejection("GET", None, None, allowed) == "E_HOST_NOT_ALLOWED"
    assert request_rejection("GET", "127.0.0.1:8765", "http://127.0.0.1:3000", allowed) is None

