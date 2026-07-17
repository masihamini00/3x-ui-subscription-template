#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo


SOURCE_DB = os.environ.get("XUI_SOURCE_DB", "/etc/x-ui/x-ui.db")
SOURCE_DB_TYPE = os.environ.get("XUI_SOURCE_DB_TYPE", "sqlite").strip().lower()
HISTORY_DB = os.environ.get(
    "XUI_HISTORY_DB", "/var/lib/3x-ui-subscription-dashboard/history.db"
)
CERT_FILE = os.environ.get("XUI_HISTORY_CERT", "")
KEY_FILE = os.environ.get("XUI_HISTORY_KEY", "")
TLS_ENABLED = os.environ.get("XUI_HISTORY_TLS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TIMEZONE = ZoneInfo(os.environ.get("XUI_HISTORY_TIMEZONE", "Asia/Tehran"))
POLL_SECONDS = max(60, int(os.environ.get("XUI_HISTORY_POLL_SECONDS", "60")))
RESET_GUARD_SECONDS = max(1800, int(os.environ.get("XUI_HISTORY_RESET_GUARD_SECONDS", "21600")))
RESET_REBOUND_MIN_BYTES = max(
    8 * 1024 * 1024,
    int(os.environ.get("XUI_HISTORY_RESET_REBOUND_MIN_BYTES", str(64 * 1024 * 1024))),
)
TLS_HANDSHAKE_TIMEOUT = max(2, int(os.environ.get("XUI_HISTORY_TLS_HANDSHAKE_TIMEOUT", "5")))
REQUEST_TIMEOUT = max(5, int(os.environ.get("XUI_HISTORY_REQUEST_TIMEOUT", "30")))
MAX_ACTIVE_CONNECTIONS = max(8, int(os.environ.get("XUI_HISTORY_MAX_CONNECTIONS", "64")))
SUB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
LOG = logging.getLogger("3x-ui-subscription-dashboard")
DB_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def source_connection():
    uri = f"file:{SOURCE_DB}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=20)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=20000")
    return connection


def postgres_rows(sql):
    """Run a read-only query through the PostgreSQL client without exposing the DSN in argv."""
    if not os.environ.get("PGDATABASE"):
        raise RuntimeError("PGDATABASE is required when PostgreSQL is selected")
    completed = subprocess.run(
        [
            "psql",
            "--no-psqlrc",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--field-separator=\t",
            "--set=ON_ERROR_STOP=1",
            "--command",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "PGCONNECT_TIMEOUT": "10"},
    )
    return [line.split("\t") for line in completed.stdout.splitlines() if line]


def history_connection():
    os.makedirs(os.path.dirname(HISTORY_DB), mode=0o700, exist_ok=True)
    connection = sqlite3.connect(HISTORY_DB, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_state (
            sub_id TEXT PRIMARY KEY,
            last_total INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            tracking_since TEXT NOT NULL
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_state)")}
    if "source_updated_at" not in columns:
        connection.execute(
            "ALTER TABLE usage_state ADD COLUMN source_updated_at INTEGER NOT NULL DEFAULT 0"
        )
    if "reset_high_water" not in columns:
        connection.execute(
            "ALTER TABLE usage_state ADD COLUMN reset_high_water INTEGER NOT NULL DEFAULT 0"
        )
    if "reset_guard_until" not in columns:
        connection.execute(
            "ALTER TABLE usage_state ADD COLUMN reset_guard_until INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_usage (
            sub_id TEXT NOT NULL,
            day TEXT NOT NULL,
            bytes INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (sub_id, day)
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_daily_usage_day ON daily_usage(day)")
    return connection


def read_current_totals():
    query = """
            SELECT c.sub_id,
                   COALESCE(t.up, 0) + COALESCE(t.down, 0),
                   COALESCE(c.updated_at, 0)
            FROM clients AS c
            LEFT JOIN client_traffics AS t ON t.email = c.email
            WHERE c.sub_id IS NOT NULL AND c.sub_id != ''
            """
    if SOURCE_DB_TYPE == "postgres":
        rows = postgres_rows(query)
    else:
        with source_connection() as connection:
            rows = connection.execute(query).fetchall()
    return {
        str(sub_id): (max(0, int(total or 0)), max(0, int(updated_at or 0)))
        for sub_id, total, updated_at in rows
        if SUB_ID_RE.fullmatch(str(sub_id))
    }


def calculate_delta(current_total, source_updated_at, previous, timestamp):
    """Return delta and reset-guard state for a traffic counter snapshot.

    3X-UI/Xray can briefly reset a counter during renewal and then restore its old
    value.  Without a guard, that restored value looks like fresh traffic.  While
    guarded, gradual traffic below the old high-water mark is still counted, but a
    jump back near that mark is treated as counter restoration.
    """
    if previous is None:
        return 0, 0, 0

    previous_total, previous_source_updated_at, high_water, guard_until = previous
    guard_active = high_water > 0 and timestamp <= guard_until

    if source_updated_at != previous_source_updated_at:
        high_water = max(high_water if guard_active else 0, previous_total, current_total)
        guard_until = timestamp + RESET_GUARD_SECONDS
        return 0, high_water, guard_until

    if current_total < previous_total:
        high_water = max(high_water if guard_active else 0, previous_total)
        guard_until = timestamp + RESET_GUARD_SECONDS
        return 0, high_water, guard_until

    if not guard_active:
        return current_total - previous_total, 0, 0

    rebound_floor = max(RESET_REBOUND_MIN_BYTES, high_water // 2)
    if current_total >= rebound_floor:
        # Only traffic beyond both the latest sample and the pre-reset high-water
        # can be new. This suppresses partial and multi-step Xray stat restoration.
        delta = max(0, current_total - max(previous_total, high_water))
    else:
        delta = current_total - previous_total
    return max(0, delta), high_water, guard_until


def collect_once():
    totals = read_current_totals()
    now = datetime.now(TIMEZONE)
    day = now.date().isoformat()
    timestamp = int(now.timestamp())
    tracking_since = now.isoformat(timespec="seconds")

    with DB_LOCK, history_connection() as connection:
        states = {
            row[0]: (int(row[1]), int(row[2]), int(row[3]), int(row[4]), row[5])
            for row in connection.execute(
                "SELECT sub_id, last_total, source_updated_at, reset_high_water, "
                "reset_guard_until, tracking_since FROM usage_state"
            )
        }
        for sub_id, (current_total, source_updated_at) in totals.items():
            previous = states.get(sub_id)
            if previous is None:
                delta = 0
                first_seen = tracking_since
                high_water = 0
                guard_until = 0
            else:
                first_seen = previous[4]
                delta, high_water, guard_until = calculate_delta(
                    current_total, source_updated_at, previous[:4], timestamp
                )

            connection.execute(
                "INSERT INTO daily_usage(sub_id, day, bytes) VALUES(?, ?, ?) "
                "ON CONFLICT(sub_id, day) DO UPDATE SET bytes = bytes + excluded.bytes",
                (sub_id, day, delta),
            )
            connection.execute(
                "INSERT INTO usage_state(sub_id, last_total, last_seen, tracking_since, "
                "source_updated_at, reset_high_water, reset_guard_until) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sub_id) DO UPDATE SET last_total=excluded.last_total, "
                "last_seen=excluded.last_seen, source_updated_at=excluded.source_updated_at, "
                "reset_high_water=excluded.reset_high_water, reset_guard_until=excluded.reset_guard_until",
                (
                    sub_id,
                    current_total,
                    timestamp,
                    first_seen,
                    source_updated_at,
                    high_water,
                    guard_until,
                ),
            )

        cutoff_day = (now.date() - timedelta(days=45)).isoformat()
        stale_before = timestamp - (90 * 86400)
        connection.execute("DELETE FROM daily_usage WHERE day < ?", (cutoff_day,))
        connection.execute("DELETE FROM usage_state WHERE last_seen < ?", (stale_before,))

    LOG.info("collected %d subscriptions", len(totals))
    return len(totals)


def subscription_snapshot(sub_id):
    query = """
            SELECT c.total_gb,
                   c.expiry_time,
                   c.enable,
                   COALESCE(t.up, 0),
                   COALESCE(t.down, 0),
                   COALESCE(t.last_online, 0)
            FROM clients AS c
            LEFT JOIN client_traffics AS t ON t.email = c.email
            WHERE c.sub_id={placeholder}
            LIMIT 1
            """
    if SOURCE_DB_TYPE == "postgres":
        safe_sub_id = sub_id.replace("'", "''")
        rows = postgres_rows(query.format(placeholder=f"'{safe_sub_id}'"))
        row = rows[0] if rows else None
    else:
        with source_connection() as connection:
            row = connection.execute(
                query.format(placeholder="?"),
                (sub_id,),
            ).fetchone()
    if row is None:
        return None
    enabled_value = row[2]
    enabled = (
        enabled_value.strip().lower() in {"1", "t", "true", "yes", "on"}
        if isinstance(enabled_value, str)
        else bool(enabled_value)
    )
    return {
        "totalBytes": max(0, int(row[0] or 0)),
        "expiryTime": int(row[1] or 0),
        "enabled": enabled,
        "uploadBytes": max(0, int(row[3] or 0)),
        "downloadBytes": max(0, int(row[4] or 0)),
        "lastOnline": int(row[5] or 0),
    }


def seven_day_history(sub_id):
    snapshot = subscription_snapshot(sub_id)
    if snapshot is None:
        return None
    today = datetime.now(TIMEZONE).date()
    start = today - timedelta(days=6)
    with DB_LOCK, history_connection() as connection:
        values = {
            day: max(0, int(value))
            for day, value in connection.execute(
                "SELECT day, bytes FROM daily_usage WHERE sub_id=? AND day BETWEEN ? AND ?",
                (sub_id, start.isoformat(), today.isoformat()),
            )
        }
        state = connection.execute(
            "SELECT tracking_since FROM usage_state WHERE sub_id=?", (sub_id,)
        ).fetchone()
    days = []
    for offset in range(7):
        day = (start + timedelta(days=offset)).isoformat()
        days.append({"date": day, "bytes": values.get(day, 0)})
    return {
        "timezone": str(TIMEZONE),
        "trackingSince": state[0] if state else None,
        "days": days,
        "totalBytes": sum(item["bytes"] for item in days),
        "subscription": snapshot,
    }


class HistoryHandler(BaseHTTPRequestHandler):
    server_version = "XUIUsageHistory/1.0"

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else "-"
        LOG.info("%s %s %s", self.client_address[0], self.command, status)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/healthz":
            self.send_json(200, {"status": "ok"})
            return
        prefix = "/v1/history/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "not_found"})
            return
        sub_id = path[len(prefix):]
        if not SUB_ID_RE.fullmatch(sub_id):
            self.send_json(400, {"error": "invalid_subscription_id"})
            return
        try:
            payload = seven_day_history(sub_id)
        except (sqlite3.Error, subprocess.SubprocessError, OSError, RuntimeError, ValueError):
            LOG.exception("database read failed")
            self.send_json(503, {"error": "temporarily_unavailable"})
            return
        if payload is None:
            self.send_json(404, {"error": "subscription_not_found"})
            return
        self.send_json(200, payload)


class SafeHTTPServer(ThreadingHTTPServer):
    """Serve bounded HTTP(S), performing optional TLS handshakes in worker threads."""

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, server_address, handler_class, ssl_context=None):
        self.ssl_context = ssl_context
        self.connection_slots = threading.BoundedSemaphore(MAX_ACTIVE_CONNECTIONS)
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            active_request = request
            if self.ssl_context is not None:
                try:
                    request.settimeout(TLS_HANDSHAKE_TIMEOUT)
                    active_request = self.ssl_context.wrap_socket(request, server_side=True)
                except (ssl.SSLError, OSError, TimeoutError):
                    self.shutdown_request(request)
                    return
            active_request.settimeout(REQUEST_TIMEOUT)
            super().process_request_thread(active_request, client_address)
        finally:
            self.connection_slots.release()


def collector_loop():
    while not STOP_EVENT.wait(POLL_SECONDS):
        try:
            collect_once()
        except Exception:
            LOG.exception("usage collection failed")


def run_server(port):
    STOP_EVENT.clear()
    collect_once()
    thread = threading.Thread(target=collector_loop, name="usage-collector", daemon=True)
    thread.start()
    context = None
    protocol = "HTTP"
    if TLS_ENABLED:
        if not CERT_FILE or not KEY_FILE:
            raise RuntimeError("TLS is enabled but certificate or key path is empty")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(CERT_FILE, KEY_FILE)
        protocol = "HTTPS"
    server = SafeHTTPServer(("0.0.0.0", port), HistoryHandler, context)

    def stop_server(signum, frame):
        STOP_EVENT.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    LOG.info("serving %s usage history on port %d", protocol, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP_EVENT.set()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Collect and serve 3X-UI daily usage history")
    parser.add_argument("--once", action="store_true", help="Collect one snapshot and exit")
    parser.add_argument("--port", type=int, default=int(os.environ.get("XUI_HISTORY_PORT", "2097")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.once:
        collect_once()
        return
    run_server(args.port)


if __name__ == "__main__":
    main()
