import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from .config import DATA


def uid():
    return uuid4().hex


@contextmanager
def connection():
    con = sqlite3.connect(DATA / "metadata.sqlite", timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "spool").mkdir(exist_ok=True)
    with connection() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
          salt BLOB NOT NULL, digest BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY, owner TEXT REFERENCES users(id),
          expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS user_profiles(owner TEXT PRIMARY KEY REFERENCES users(id),
          department TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS kbs(id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id),
          name TEXT NOT NULL, UNIQUE(owner, name));
        CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, kb TEXT NOT NULL REFERENCES kbs(id),
          filename TEXT NOT NULL, sha TEXT NOT NULL, created INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
          attempt INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
          n_chunks INTEGER NOT NULL DEFAULT 0, manifest TEXT NOT NULL DEFAULT '{}', UNIQUE(kb,sha));
        CREATE INDEX IF NOT EXISTS doc_queue ON docs(status,created);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
          doc TEXT NOT NULL REFERENCES docs(id), attempt INTEGER NOT NULL,
          stamp INTEGER NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL);
        """)
        columns = {r[1] for r in con.execute("PRAGMA table_info(docs)")}
        for name, declaration in {
            "index_version": "INTEGER NOT NULL DEFAULT 1",
            "index_revision": "TEXT NOT NULL DEFAULT ''",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "metadata_provenance": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if name not in columns:
                con.execute(f"ALTER TABLE docs ADD COLUMN {name} {declaration}")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS index_jobs(
          id TEXT PRIMARY KEY, doc TEXT NOT NULL REFERENCES docs(id),
          status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued',
          created INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT, error TEXT NOT NULL DEFAULT '');
        CREATE UNIQUE INDEX IF NOT EXISTS one_index_job ON index_jobs(doc)
          WHERE status IN ('queued','running');
        CREATE TABLE IF NOT EXISTS index_history(
          doc TEXT NOT NULL REFERENCES docs(id), revision TEXT NOT NULL,
          index_version INTEGER NOT NULL, metadata_json TEXT NOT NULL,
          metadata_provenance TEXT NOT NULL, n_chunks INTEGER NOT NULL,
          activated INTEGER NOT NULL, PRIMARY KEY(doc,revision));
        """)


def password_digest(password, salt):
    return hashlib.scrypt(
        password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024
    )


def add_user(name, password):
    if not 1 <= len(name.strip()) <= 64 or not 10 <= len(password) <= 256:
        raise ValueError("账号 1–64 字符，密码 10–256 字符。")
    salt = secrets.token_bytes(16)
    with connection() as con:
        con.execute(
            "INSERT INTO users VALUES(?,?,?,?)",
            (uid(), name.strip(), salt, password_digest(password, salt)),
        )


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login(name, password):
    with connection() as con:
        row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        check = password_digest(password, row["salt"] if row else b"unknown-local-id")
        if not row or not secrets.compare_digest(check, row["digest"]):
            return None
        token = secrets.token_urlsafe(32)
        con.execute("DELETE FROM sessions WHERE expires<=?", (int(time.time()),))
        con.execute(
            "INSERT INTO sessions VALUES(?,?,?)",
            (token_hash(token), row["id"], int(time.time()) + 43200),
        )
        return token


def event(con, doc, attempt, stage, message=""):
    con.execute(
        "INSERT INTO events(doc,attempt,stamp,stage,message) VALUES(?,?,?,?,?)",
        (doc, attempt, int(time.time()), stage, message),
    )


def stage(doc, attempt, name):
    with connection() as con:
        con.execute("UPDATE docs SET stage=? WHERE id=?", (name, doc))
        event(con, doc, attempt, name)


def requeue_interrupted():
    with connection() as con:
        for row in con.execute(
            "SELECT id,attempt FROM docs WHERE status='running'"
        ).fetchall():
            event(con, row["id"], row["attempt"], "interrupted", "进程重启，重新排队")
        con.execute(
            "UPDATE docs SET status='queued',stage='queued' WHERE status='running'"
        )
        con.execute(
            "UPDATE index_jobs SET status='queued',stage='queued' WHERE status='running'"
        )


def claim():
    with connection() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM docs WHERE status='queued' ORDER BY created,rowid LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        attempt = row["attempt"] + 1
        con.execute(
            "UPDATE docs SET status='running',stage='start',attempt=?,error='' WHERE id=?",
            (attempt, row["id"]),
        )
        event(con, row["id"], attempt, "start")
        return dict(row) | {"attempt": attempt}


def enqueue_index(con, doc_id, metadata_json=None):
    """Caller holds BEGIN IMMEDIATE; ready documents remain searchable throughout the job."""
    from .parsing import Problem

    doc = con.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if not doc or doc["status"] != "ready":
        raise Problem("只能重新索引已就绪的文档。")
    pending = con.execute(
        "SELECT id FROM index_jobs WHERE doc=? AND status IN ('queued','running')",
        (doc_id,),
    ).fetchone()
    if pending:
        if metadata_json is not None:
            raise Problem("该文档正在重新索引，请完成后再修改 Metadata。")
        return {"id": pending["id"], "doc": doc_id, "duplicate": True}
    job = uid()
    con.execute(
        "INSERT INTO index_jobs(id,doc,created,metadata_json) VALUES(?,?,?,?)",
        (job, doc_id, int(time.time()), metadata_json),
    )
    return {"id": job, "doc": doc_id, "duplicate": False}


def claim_index():
    with connection() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM index_jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1"
        ).fetchone()
        if not row:
            return None
        con.execute(
            "UPDATE index_jobs SET status='running',stage='load',attempt=attempt+1,error='' WHERE id=?",
            (row["id"],),
        )
        return dict(row) | {"attempt": row["attempt"] + 1}


def remember_index(con, doc):
    con.execute(
        "INSERT OR IGNORE INTO index_history VALUES(?,?,?,?,?,?,?)",
        (
            doc["id"],
            doc["index_revision"],
            doc["index_version"],
            doc["metadata_json"],
            doc["metadata_provenance"],
            doc["n_chunks"],
            int(time.time()),
        ),
    )


def retrieval_context(owner, kb):
    """Trusted server context, narrowed to the selected authorized knowledge base."""
    from .parsing import Problem

    with connection() as con:
        permitted = con.execute(
            "SELECT id FROM kbs WHERE id=? AND owner=?", (kb, owner)
        ).fetchone()
        profile = con.execute(
            "SELECT department FROM user_profiles WHERE owner=?", (owner,)
        ).fetchone()
    if not permitted:
        raise Problem("知识库不存在或无权访问。")
    return {
        "user_id": owner,
        "department": profile[0] if profile else "",
        "allowed_kb_ids": [kb],
        "selected_kb_id": kb,
        "scope": "current_knowledge_base",
    }
