import importlib.util
import sqlite3
import sys
from pathlib import Path

from app import db


def test_v1_sqlite_schema_upgrade_preserves_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    with sqlite3.connect(tmp_path / "metadata.sqlite") as con:
        con.execute(
            "CREATE TABLE docs(id TEXT PRIMARY KEY,kb TEXT,filename TEXT,sha TEXT,created INTEGER,status TEXT,stage TEXT,attempt INTEGER,error TEXT,n_chunks INTEGER,manifest TEXT)"
        )
        con.execute(
            "INSERT INTO docs VALUES('old','kb','test.pdf','hash',1,'ready','ready',1,'',7,'{}')"
        )
    db.init_db()
    db.init_db()
    with db.connection() as con:
        d = con.execute("SELECT * FROM docs WHERE id='old'").fetchone()
    assert d["index_version"] == 1
    assert d["index_revision"] == ""
    assert d["metadata_json"] == "{}"
    assert d["n_chunks"] == 7 and d["status"] == "ready"


def load_upgrade():
    spec = importlib.util.spec_from_file_location(
        "upgrade_script", Path(__file__).parents[1] / "scripts/upgrade.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_backs_up_data_and_does_not_overwrite_credentials(
    tmp_path, monkeypatch
):
    module = load_upgrade()
    target, source = tmp_path / "installed", tmp_path / "release"
    for p in (target / "app", target / "state/models", source / "app"):
        p.mkdir(parents=True)
    for name, text in [
        (".env", "ORIGINAL_NETWORK=true"),
        ("settings.json", '{"chat_key":"LOCAL_ONLY_TEST"}'),
        ("compose.yaml", "old compose"),

        ("state/models/cache", "downloadable"),
        ("app/main.py", "old code"),
    ]:
        (target / name).write_text(text)
    (source / "app/main.py").write_text("new code")
    (source / "compose.yaml").write_text("new compose")
    with sqlite3.connect(target / "state/metadata.sqlite") as con:
        con.execute("CREATE TABLE docs(id TEXT, filename TEXT, status TEXT)")
    calls = []
    monkeypatch.setattr(module, "SOURCE", source)
    monkeypatch.setattr(
        module.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    monkeypatch.setattr(sys, "argv", ["upgrade.py", "--target", str(target)])
    module.main()
    backups = list(tmp_path.glob("installed-backup-*"))
    assert len(backups) == 1
    backup = backups[0]
    assert (backup / "state/metadata.sqlite").read_bytes() == (target / "state/metadata.sqlite").read_bytes()
    assert not (backup / "state/models").exists()
    assert (backup / "settings.json").read_text() == (
        target / "settings.json"
    ).read_text()
    assert (target / ".env").read_text() == "ORIGINAL_NETWORK=true"
    assert (target / "state/models/cache").read_text() == "downloadable"
    assert (target / "app/main.py").read_text() == "new code"
    assert calls[1][-2:] == ["stop", "agent"]
    assert "--build" in calls[-1]


def test_preflight_requires_current_ready_indexes_and_supported_planner(tmp_path):
    import pytest
    from app.readiness import check_readiness

    database = tmp_path / "metadata.sqlite"
    with sqlite3.connect(database) as con:
        con.execute("CREATE TABLE docs(id,filename,status,index_version)")
        con.execute("INSERT INTO docs VALUES ('d','old.pdf','ready',2)")
    with pytest.raises(ValueError, match="old.pdf"):
        check_readiness(database, {})
    with sqlite3.connect(database) as con:
        con.execute("UPDATE docs SET index_version=3")
    with pytest.raises(ValueError, match="manual"):
        check_readiness(database, {"intent_mode": "rules"})
    check_readiness(database, {"intent_mode": "manual"})


def test_upgrade_rechecks_after_stop_and_restarts_previous_release(tmp_path, monkeypatch):
    import pytest

    module = load_upgrade()
    target = tmp_path / "installed"
    (target / "app").mkdir(parents=True)
    (target / "state").mkdir()
    for name, value in (("settings.json", "{}"), (".env", ""), ("compose.yaml", "")):
        (target / name).write_text(value)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[-2:] == ["stop", "agent"]:
            with sqlite3.connect(target / "state/metadata.sqlite") as con:
                con.execute("CREATE TABLE docs(id,filename,status,index_version)")
                con.execute("INSERT INTO docs VALUES ('d','late.pdf','ready',1)")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["upgrade.py", "--target", str(target)])
    with pytest.raises(ValueError, match="late.pdf"):
        module.main()
    assert calls[-1][-2:] == ["start", "agent"]
    assert not list(tmp_path.glob("installed-backup-*"))
