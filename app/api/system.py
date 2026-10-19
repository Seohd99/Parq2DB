"""Version, profile status, and database connection routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import __version__
from app.api.deps import get_config, get_db_profile, get_updates
from app.core.db import TABLE_LIST_LIMIT, close_quietly, connect, list_tables, test_connection
from app.core.languages import supported_languages

router = APIRouter()


@router.get("/api/status")
def status(request: Request) -> dict:
    """Return the version, server limits, and database profile status without passwords."""
    return {"version": __version__, "languages": supported_languages(), **get_config(request).public()}


@router.get("/api/updates")
def check_updates(request: Request) -> dict:
    """Report whether a newer ParqBridge release exists; cached, and never an error."""
    return get_updates(request).status()


@router.post("/api/db/{dialect}/test")
def test_db_connection(dialect: str, request: Request) -> dict:
    """Connect with the configured profile and return only the server version and public target details."""
    profile = get_db_profile(request, dialect)
    return {"connected": True, **test_connection(profile)}


@router.get("/api/db/{dialect}/tables")
def list_db_tables(dialect: str, request: Request) -> dict:
    """List table names in the profile's current schema for INSERT target suggestions."""
    profile = get_db_profile(request, dialect)
    connection = connect(profile)
    try:
        names = list_tables(connection, profile, limit=TABLE_LIST_LIMIT + 1)
    finally:
        close_quietly(connection)
    return {"tables": names[:TABLE_LIST_LIMIT], "truncated": len(names) > TABLE_LIST_LIMIT}
