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
        ("state/metadata.sqlite", "old database"),
        ("state/models/cache", "downloadable"),
        ("app/main.py", "old code"),
    ]:
        (target / name).write_text(text)
    (source / "app/main.py").write_text("new code")
    (source / "compose.yaml").write_text("new compose")
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
    assert (backup / "state/metadata.sqlite").read_text() == "old database"
    assert not (backup / "state/models").exists()
    assert (backup / "settings.json").read_text() == (
        target / "settings.json"
    ).read_text()
    assert (target / ".env").read_text() == "ORIGINAL_NETWORK=true"
    assert (target / "state/models/cache").read_text() == "downloadable"
    assert (target / "app/main.py").read_text() == "new code"
    assert calls[1][-2:] == ["stop", "agent"]
    assert "--build" in calls[-1]
