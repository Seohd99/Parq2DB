import json
from pathlib import Path

import pytest

from app.core.config import load_config
from app.core.errors import PBError

SECRET = "s3cr3t-do-not-leak"


def write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_no_config_file_is_fully_offline(tmp_path):
    cfg = load_config(tmp_path / "none.toml", tmp_path / "none.local.toml", env={})
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8765
    assert cfg.load.batch_size == 10_000
    assert cfg.load.child_max_depth == 3
    for kind in ("oracle", "mysql"):
        assert not cfg.db[kind].online
        assert cfg.db[kind].offline_reason == "E_DB_NOT_CONFIGURED"
    assert cfg.sources == ()


def test_profile_online_with_env_password(tmp_path):
    main = write(tmp_path / "pb.toml", """
[db.oracle]
host = "h"
port = 1521
service_name = "svc"
user = "u"
password_env = "PB_TEST_ORA_PW"
""")
    cfg = load_config(main, tmp_path / "x.toml", env={"PB_TEST_ORA_PW": SECRET})
    ora = cfg.db["oracle"]
    assert ora.online
    assert ora.password == SECRET
    assert ora.public() == {"kind": "oracle", "online": True, "target": "h:1521/svc", "user": "u"}
    assert not cfg.db["mysql"].online


def test_profile_offline_when_password_missing(tmp_path):
    main = write(tmp_path / "pb.toml", """
[db.mysql]
host = "h"
port = 3306
database = "d"
user = "u"
password_env = "PB_TEST_MY_PW"
""")
    cfg = load_config(main, tmp_path / "x.toml", env={})
    my = cfg.db["mysql"]
    assert not my.online
    assert my.offline_reason == "E_DB_PASSWORD_MISSING"
    assert my.public()["reason"] == {"code": "E_DB_PASSWORD_MISSING", "params": {"env": "PB_TEST_MY_PW"}}


def test_missing_required_key(tmp_path):
    main = write(tmp_path / "pb.toml", '[db.oracle]\nhost = "h"\nport = 1521\n')
    cfg = load_config(main, tmp_path / "x.toml", env={})
    ora = cfg.db["oracle"]
    assert ora.offline_reason == "E_DB_CONFIG_INVALID"
    assert set(ora.offline_params["missing"]) == {"service_name", "user"}


def test_password_in_main_file_is_ignored_and_local_file_wins(tmp_path):
    main = write(tmp_path / "pb.toml", f"""
[db.oracle]
host = "h"
port = 1521
service_name = "svc"
user = "u"
password = "{SECRET}"
""")
    cfg = load_config(main, tmp_path / "x.toml", env={})
    assert not cfg.db["oracle"].online
    assert [w["code"] for w in cfg.warnings] == ["W_PASSWORD_IN_MAIN_CONFIG"]

    local = write(tmp_path / "pb.local.toml", f'[db.oracle]\npassword = "{SECRET}"\nuser = "u2"\n')
    cfg = load_config(main, local, env={})
    assert cfg.db["oracle"].online
    assert cfg.db["oracle"].user == "u2"
    assert cfg.sources == ("pb.toml", "pb.local.toml")


def test_public_view_never_contains_password(tmp_path):
    main = write(tmp_path / "pb.toml", """
[db.oracle]
host = "h"
port = 1521
service_name = "svc"
user = "u"
password_env = "E1"
[db.mysql]
host = "h"
port = 3306
database = "d"
user = "u"
password_env = "E2"
""")
    cfg = load_config(main, tmp_path / "x.toml", env={"E1": SECRET, "E2": SECRET})
    dumped = json.dumps(cfg.public()) + repr(cfg)
    assert SECRET not in dumped


def test_server_section_and_warnings(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    main = write(tmp_path / "pb.toml", f"""
[server]
host = "0.0.0.0"
port = 9000
upload_max_mb = 10
allowed_roots = [{json.dumps(str(root))}, {json.dumps(str(tmp_path / "nope"))}]
allowed_hosts = [" pb.lan:8765 "]
[load]
batch_size = 500
""")
    cfg = load_config(main, tmp_path / "x.toml", env={})
    assert cfg.server.port == 9000
    assert cfg.server.upload_max_bytes == 10 * 1024 * 1024
    assert cfg.server.allowed_roots == (root.resolve(),)
    assert cfg.server.allowed_hosts == ("pb.lan",)
    assert cfg.load.batch_size == 500
    assert {w["code"] for w in cfg.warnings} == {"W_BIND_ALL_INTERFACES", "W_ALLOWED_ROOT_MISSING"}


@pytest.mark.parametrize("snippet", [
    '[server]\nport = "8765"\n',
    "[server]\nport = true\n",
    "[load]\nbatch_size = 0\n",
    '[server]\nallowed_roots = "D:/data"\n',
    '[server]\nallowed_hosts = "pb.lan"\n',
    '[server]\nallowed_hosts = [""]\n',
    '[server]\nallowed_hosts = ["pb.lan:not-a-port"]\n',
    "[server\n",
])
def test_invalid_config_raises(tmp_path, snippet):
    main = write(tmp_path / "pb.toml", snippet)
    with pytest.raises(PBError) as ei:
        load_config(main, tmp_path / "x.toml", env={})
    assert ei.value.code == "E_CONFIG_INVALID"
