import importlib.util
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
HELPER = ROOT / "src" / "tools" / "panel_settings.py"

spec = importlib.util.spec_from_file_location("panel_settings", HELPER)
panel_settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel_settings)

with patch.dict(
    os.environ,
    {"XUI_DB_DSN": "postgresql://panel:p%40ss@localhost:5432/xui?sslmode=disable"},
    clear=False,
):
    pg_environment = panel_settings.postgres_environment()
assert pg_environment["PGHOST"] == "localhost"
assert pg_environment["PGPORT"] == "5432"
assert pg_environment["PGUSER"] == "panel"
assert pg_environment["PGPASSWORD"] == "p@ss"
assert pg_environment["PGDATABASE"] == "xui"
assert pg_environment["PGSSLMODE"] == "disable"


with tempfile.TemporaryDirectory() as directory:
    directory = Path(directory)
    database = directory / "x-ui.db"
    backup = directory / "backup.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        connection.commit()

    environment = {
        **os.environ,
        "XUI_DB_TYPE": "sqlite",
        "XUI_SOURCE_DB": str(database),
    }

    def run(*arguments, check=True):
        return subprocess.run(
            [sys.executable, str(HELPER), *arguments],
            check=check,
            capture_output=True,
            text=True,
            env=environment,
        )

    assert run("exists", "subThemeDir", check=False).returncode == 1
    assert run("get", "subThemeDir").stdout == "\n"
    assert run("get", "subCertFile").stdout == "\n"
    run("set", "subThemeDir", "/opt/example/theme/")
    assert run("exists", "subThemeDir").returncode == 0
    assert run("get", "subThemeDir").stdout.strip() == "/opt/example/theme/"
    run("set", "subThemeDir", "/opt/example/theme-v2/")
    assert run("get", "subThemeDir").stdout.strip() == "/opt/example/theme-v2/"
    run("backup", str(backup))
    with closing(sqlite3.connect(backup)) as connection:
        value = connection.execute(
            "SELECT value FROM settings WHERE key='subThemeDir'"
        ).fetchone()[0]
    assert value == "/opt/example/theme-v2/"
    assert run("exists", "missing", check=False).returncode == 1
    assert run("get", "missing", check=False).returncode == 1

print("all panel settings tests passed")
