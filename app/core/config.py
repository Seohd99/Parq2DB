"""Load settings and determine which databases are offline.

Read order:
  1. config/parqbridge.toml (if absent, use defaults and keep all databases offline)
  2. config/parqbridge.local.toml (if present, overrides (1); the only config file that may contain a password)

A database profile is online only when [db.<kind>] exists, includes all required keys,
and a password is available. Otherwise, an offline reason is recorded as an error code.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.errors import PBError, warning
from app.core.dialects import SUPPORTED_DIALECTS

log = logging.getLogger("parqbridge.config")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "parqbridge.toml"
DEFAULT_LOCAL_PATH = CONFIG_DIR / "parqbridge.local.toml"
DEFAULT_UPLOAD_DIR = PROJECT_ROOT / "workspace" / "uploads"

# Required keys by database kind. target_key is used in the displayed connection target (host:port/<target>).
DB_KINDS: dict[str, dict[str, Any]] = {
    "oracle": {"required": ("host", "port", "service_name", "user"), "target_key": "service_name"},
    "mysql": {"required": ("host", "port", "database", "user"), "target_key": "database"},
    "postgres": {
        "required": ("host", "database", "user"),
        "target_key": "database",
        "default_port": 5432,
    },
}


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    upload_max_mb: int = 2048
    allowed_roots: tuple[Path, ...] = ()
    upload_dir: Path = DEFAULT_UPLOAD_DIR
    # Extra Host header names accepted besides loopback and the bind host (see app/core/hosts.py).
    allowed_hosts: tuple[str, ...] = ()

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024


@dataclass(frozen=True)
class LoadConfig:
    batch_size: int = 10_000
    child_max_depth: int = 3


@dataclass(frozen=True)
class DbProfile:
    kind: str
    host: str = ""
    port: int = 0
    target: str = ""
    user: str = ""
    schema: str | None = None
    password: str | None = field(default=None, repr=False)
    offline_reason: str | None = None
    offline_params: dict[str, Any] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return self.offline_reason is None

    def public(self) -> dict[str, Any]:
        """Return information safe to expose in the API or UI, without the password."""
        out: dict[str, Any] = {"kind": self.kind, "online": self.online}
        if self.host:
            out["target"] = f"{self.host}:{self.port}/{self.target}"
            out["user"] = self.user
        if not self.online:
            out["reason"] = {"code": self.offline_reason, "params": self.offline_params}
        return out


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    load: LoadConfig = field(default_factory=LoadConfig)
    db: dict[str, DbProfile] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    warnings: tuple[dict[str, Any], ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "server": {
                "upload_max_mb": self.server.upload_max_mb,
                "allowed_roots": [str(p) for p in self.server.allowed_roots],
            },
            "load": {
                "batch_size": self.load.batch_size,
                "child_max_depth": self.load.child_max_depth,
            },
            "db": {
                kind: self.db.get(kind, DbProfile(kind=kind, offline_reason="E_DB_NOT_CONFIGURED")).public()
                for kind in SUPPORTED_DIALECTS
            },
            "sources": list(self.sources),
            "warnings": list(self.warnings),
        }


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise PBError("E_CONFIG_INVALID", file=path.name, detail=str(e)) from e


def _deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _expect(section: str, key: str, value: Any, typ: type, file: str) -> Any:
    # bool is a subclass of int, so reject it separately.
    if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
        raise PBError("E_CONFIG_INVALID", file=file, key=f"{section}.{key}", expected=typ.__name__)
    return value


def _positive(section: str, key: str, value: Any, file: str) -> int:
    v = _expect(section, key, value, int, file)
    if v <= 0:
        raise PBError("E_CONFIG_INVALID", file=file, key=f"{section}.{key}", expected="positive int")
    return v


def _parse_server(raw: Mapping[str, Any], file: str, warnings: list) -> ServerConfig:
    d = ServerConfig()
    host = _expect("server", "host", raw.get("host", d.host), str, file)
    if host in ("0.0.0.0", "::", ""):
        warnings.append(warning("W_BIND_ALL_INTERFACES", host=host))
    port = _positive("server", "port", raw.get("port", d.port), file)
    upload_max_mb = _positive("server", "upload_max_mb", raw.get("upload_max_mb", d.upload_max_mb), file)

    roots_raw = raw.get("allowed_roots", [])
    if not isinstance(roots_raw, list) or not all(isinstance(r, str) for r in roots_raw):
        raise PBError("E_CONFIG_INVALID", file=file, key="server.allowed_roots", expected="list[str]")
    roots: list[Path] = []
    for r in roots_raw:
        p = Path(r).expanduser().resolve()
        if not p.is_dir():
            warnings.append(warning("W_ALLOWED_ROOT_MISSING", root=r))
            continue
        if p not in roots:
            roots.append(p)

    hosts_raw = raw.get("allowed_hosts", [])
    if not isinstance(hosts_raw, list) or not all(isinstance(h, str) and h.strip() for h in hosts_raw):
        raise PBError("E_CONFIG_INVALID", file=file, key="server.allowed_hosts", expected="list[str]")

    return ServerConfig(
        host=host,
        port=port,
        upload_max_mb=upload_max_mb,
        allowed_roots=tuple(roots),
        allowed_hosts=tuple(h.strip() for h in hosts_raw),
    )


def _parse_load(raw: Mapping[str, Any], file: str) -> LoadConfig:
    d = LoadConfig()
    return LoadConfig(
        batch_size=_positive("load", "batch_size", raw.get("batch_size", d.batch_size), file),
        child_max_depth=_positive("load", "child_max_depth", raw.get("child_max_depth", d.child_max_depth), file),
    )


def _parse_db(kind: str, raw: Mapping[str, Any] | None, env: Mapping[str, str]) -> DbProfile:
    if raw is None:
        return DbProfile(kind=kind, offline_reason="E_DB_NOT_CONFIGURED")

    spec = DB_KINDS[kind]
    missing = [k for k in spec["required"] if k not in raw]
    port = raw.get("port", spec.get("default_port", 0))
    schema = raw.get("schema") if kind == "postgres" else None
    base = dict(
        kind=kind,
        host=str(raw.get("host", "")),
        port=port if isinstance(port, int) and not isinstance(port, bool) else 0,
        target=str(raw.get(spec["target_key"], "")),
        user=str(raw.get("user", "")),
        schema=schema if isinstance(schema, str) else None,
    )
    if kind == "postgres" and "schema" in raw and (not isinstance(schema, str) or not schema):
        return DbProfile(**base, offline_reason="E_DB_CONFIG_INVALID", offline_params={"key": "schema"})
    if missing:
        return DbProfile(**base, offline_reason="E_DB_CONFIG_INVALID", offline_params={"missing": missing})
    if base["port"] <= 0:
        return DbProfile(**base, offline_reason="E_DB_CONFIG_INVALID", offline_params={"key": "port"})

    password = raw.get("password")  # The main config's password is removed before this function is called.
    env_name = raw.get("password_env")
    if not password and isinstance(env_name, str) and env_name:
        password = env.get(env_name) or None
    if not password:
        return DbProfile(
            **base,
            offline_reason="E_DB_PASSWORD_MISSING",
            offline_params={"env": env_name if isinstance(env_name, str) else None},
        )
    return DbProfile(**base, password=str(password))


def load_config(
    path: Path | None = None,
    local_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load settings into AppConfig; if the files are absent, use defaults and keep all databases offline."""
    path = path or DEFAULT_CONFIG_PATH
    local_path = local_path or DEFAULT_LOCAL_PATH
    env = os.environ if env is None else env

    warnings: list[dict[str, Any]] = []
    sources: list[str] = []
    merged: dict[str, Any] = {}

    if path.is_file():
        main = _read_toml(path)
        # Ignore passwords in the main config file because it may be committed.
        for kind, sec in (main.get("db") or {}).items():
            if isinstance(sec, dict) and "password" in sec:
                sec.pop("password")
                warnings.append(warning("W_PASSWORD_IN_MAIN_CONFIG", kind=kind, file=path.name))
        merged = main
        sources.append(path.name)
    if local_path.is_file():
        merged = _deep_merge(merged, _read_toml(local_path))
        sources.append(local_path.name)

    file_label = " + ".join(sources) or "(defaults)"
    server = _parse_server(merged.get("server") or {}, file_label, warnings)
    load = _parse_load(merged.get("load") or {}, file_label)

    db_raw = merged.get("db") or {}
    db = {kind: _parse_db(kind, db_raw.get(kind), env) for kind in SUPPORTED_DIALECTS}

    cfg = AppConfig(server=server, load=load, db=db, sources=tuple(sources), warnings=tuple(warnings))
    for kind, prof in db.items():
        log.info("db.%s: %s", kind, "online" if prof.online else f"offline ({prof.offline_reason})")
    for w in warnings:
        log.warning("config warning: %s %s", w["code"], w["params"])
    return cfg
