#!/usr/bin/env python3
"""Read and update 3X-UI settings on SQLite or PostgreSQL installations."""

import argparse
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys


KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
PANEL_DEFAULTS = {
    "subThemeDir": "",
    "subCertFile": "",
    "subKeyFile": "",
}


def database_type():
    value = os.environ.get("XUI_DB_TYPE", "sqlite").strip().lower()
    return "postgres" if value in {"postgres", "postgresql", "pg"} else "sqlite"


def sqlite_path():
    explicit = os.environ.get("XUI_SOURCE_DB")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("XUI_DB_FOLDER", "/etc/x-ui")) / "x-ui.db"


def sqlite_connection():
    path = sqlite_path()
    if not path.is_file():
        raise RuntimeError(f"3X-UI SQLite database was not found: {path}")
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def psql(sql, variables=None):
    if not os.environ.get("PGDATABASE"):
        raise RuntimeError("PGDATABASE is required for a PostgreSQL installation")
    if shutil.which("psql") is None:
        raise RuntimeError("psql is required for a PostgreSQL installation")
    command = [
        "psql",
        "--no-psqlrc",
        "--quiet",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
    ]
    for key, value in (variables or {}).items():
        command.extend(["--set", f"{key}={value}"])
    command.extend(["--command", sql])
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
        env={**os.environ, "PGCONNECT_TIMEOUT": "10"},
    )
    return result.stdout.rstrip("\r\n")


def get_setting(key):
    if database_type() == "postgres":
        output = psql(
            "SELECT value FROM settings WHERE key = :'setting_key' LIMIT 1;",
            {"setting_key": key},
        )
        return None if output == "" and not exists_setting(key) else output
    with sqlite_connection() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key=? LIMIT 1", (key,)
        ).fetchone()
    return None if row is None else str(row[0] or "")


def exists_setting(key):
    if database_type() == "postgres":
        count = psql(
            "SELECT COUNT(*) FROM settings WHERE key = :'setting_key';",
            {"setting_key": key},
        ).strip()
        return bool(count) and int(count) > 0
    with sqlite_connection() as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM settings WHERE key=?", (key,)
        ).fetchone()
    return bool(row and row[0] > 0)


def set_setting(key, value):
    if database_type() == "postgres":
        psql(
            """
            BEGIN;
            LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE;
            WITH updated AS (
                UPDATE settings
                   SET value = :'setting_value'
                 WHERE key = :'setting_key'
                RETURNING 1
            )
            INSERT INTO settings (key, value)
            SELECT :'setting_key', :'setting_value'
             WHERE NOT EXISTS (SELECT 1 FROM updated);
            COMMIT;
            """,
            {"setting_key": key, "setting_value": value},
        )
    else:
        with sqlite_connection() as connection:
            cursor = connection.execute(
                "UPDATE settings SET value=? WHERE key=?", (value, key)
            )
            if cursor.rowcount == 0:
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)", (key, value)
                )
            connection.commit()
    if get_setting(key) != value:
        raise RuntimeError(f"failed to verify updated 3X-UI setting: {key}")


def backup_database(destination):
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if database_type() == "postgres":
        if shutil.which("pg_dump") is None:
            raise RuntimeError("pg_dump is required to back up PostgreSQL")
        subprocess.run(
            ["pg_dump", "--format=custom", f"--file={target}"],
            check=True,
            timeout=300,
            env={**os.environ, "PGCONNECT_TIMEOUT": "10"},
        )
        os.chmod(target, 0o600)
        return
    with sqlite_connection() as source, sqlite3.connect(target) as backup:
        source.backup(backup)
    os.chmod(target, 0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("get", "exists"):
        child = subparsers.add_parser(command)
        child.add_argument("key")
    setter = subparsers.add_parser("set")
    setter.add_argument("key")
    setter.add_argument("value")
    backup = subparsers.add_parser("backup")
    backup.add_argument("destination")
    args = parser.parse_args()

    if args.command in {"get", "exists", "set"} and not KEY_RE.fullmatch(args.key):
        parser.error("invalid setting key")

    if args.command == "get":
        value = get_setting(args.key)
        if value is None:
            if args.key not in PANEL_DEFAULTS:
                return 1
            value = PANEL_DEFAULTS[args.key]
        print(value)
    elif args.command == "exists":
        return 0 if exists_setting(args.key) else 1
    elif args.command == "set":
        set_setting(args.key, args.value)
    elif args.command == "backup":
        backup_database(args.destination)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, sqlite3.Error, subprocess.SubprocessError, OSError) as error:
        print(f"panel-settings: {error}", file=sys.stderr)
        sys.exit(1)
