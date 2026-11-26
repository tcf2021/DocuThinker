"""Read-only compatibility checks shared by startup and the upgrade tool."""

import sqlite3
from contextlib import closing


def check_readiness(database, settings):
    if settings.get("intent_mode") == "rules":
        raise ValueError("升级前请选择 auto、llm 或 manual 意图模式。")
    if not database.exists():
        return
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(docs)")}
        if not columns:
            return
        condition = " AND index_version != 3" if "index_version" in columns else ""
        rows = con.execute("SELECT id,filename FROM docs WHERE status='ready'" + condition)
        affected = [f"{name} ({doc})" for doc, name in rows]
    if affected:
        raise ValueError("请在原版本重新索引下列文档后升级：\n" + "\n".join(affected))
