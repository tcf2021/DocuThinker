"""Create a local account with docker compose exec agent python -m app.users cf."""

import argparse
import getpass
import sqlite3
from . import db


def main():
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--department", help="只更新已有用户的部门上下文，不授予知识库权限")
    args = p.parse_args()
    name = args.name
    db.init_db()
    if args.department is not None:
        department = args.department.strip()
        if len(department) > 120 or any(ord(c) < 32 for c in department):
            raise SystemExit("部门名称最多 120 字符，不含控制字符。")
        with db.connection() as con:
            row = con.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
            if not row:
                raise SystemExit("用户不存在，请先创建账号。")
            con.execute(
                "INSERT INTO user_profiles(owner,department) VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET department=excluded.department",
                (row[0], department),
            )
        print("已更新部门上下文；知识库访问权限未改变。")
        return
    pwd = getpass.getpass("New password (10+ characters): ")
    if pwd != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords differ.")
    db.init_db()
    try:
        db.add_user(name, pwd)
    except sqlite3.IntegrityError:
        raise SystemExit(
            "This username already exists. Use that account or choose another name."
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    print("Account ready:", name)


if __name__ == "__main__":
    main()
