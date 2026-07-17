#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${GITHUB_ACTIONS:-} == true ]] || {
    printf 'This destructive integration test is restricted to GitHub Actions.\n' >&2
    exit 1
}

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FAKE_BIN=$(mktemp -d)

cleanup() {
    rm -rf -- "$FAKE_BIN"
    rm -rf -- \
        /opt/3x-ui-subscription-dashboard \
        /etc/3x-ui-subscription-dashboard \
        /var/lib/3x-ui-subscription-dashboard \
        /var/backups/3x-ui-subscription-dashboard
    rm -f -- \
        /usr/local/bin/theme \
        /etc/systemd/system/theme-history.service \
        /etc/systemd/system/theme-settings-sync.service \
        /etc/systemd/system/theme-settings-sync.timer \
        /etc/x-ui/x-ui.db
    rmdir /etc/x-ui 2>/dev/null || true
}
trap cleanup EXIT

install -d -m 0755 /etc/x-ui
python3 - <<'PY'
import sqlite3

with sqlite3.connect("/etc/x-ui/x-ui.db") as connection:
    connection.execute(
        "CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT UNIQUE, value TEXT)"
    )
PY

cat >"$FAKE_BIN/systemctl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat >"$FAKE_BIN/curl" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$FAKE_BIN/systemctl" "$FAKE_BIN/curl"

PATH="$FAKE_BIN:$PATH" THEME_SOURCE_DIR="$ROOT" bash "$ROOT/install.sh"

test -L /opt/3x-ui-subscription-dashboard/current
test -x /usr/local/bin/theme
if grep -q '__HISTORY_API_PORT__' /opt/3x-ui-subscription-dashboard/current/theme/index.html; then
    printf 'History API port placeholder was not replaced.\n' >&2
    exit 1
fi
menu_output=$(printf '0\n' | /usr/local/bin/theme)
grep -q 'Theme Manager' <<<"$menu_output"
grep -q '2) Uninstall' <<<"$menu_output"
cancel_output=$(printf 'n\n' | /usr/local/bin/theme uninstall)
grep -Fq 'Continue? [y/N]:' <<<"$cancel_output"
grep -q 'Cancelled.' <<<"$cancel_output"
test -L /opt/3x-ui-subscription-dashboard/current
python3 - <<'PY'
import sqlite3

with sqlite3.connect("/etc/x-ui/x-ui.db") as connection:
    value = connection.execute(
        "SELECT value FROM settings WHERE key='subThemeDir'"
    ).fetchone()[0]
assert value == "/opt/3x-ui-subscription-dashboard/current/theme/", value
PY

PATH="$FAKE_BIN:$PATH" /usr/local/bin/theme uninstall true

test ! -e /opt/3x-ui-subscription-dashboard
test ! -e /etc/3x-ui-subscription-dashboard
test ! -e /var/lib/3x-ui-subscription-dashboard
test ! -e /var/backups/3x-ui-subscription-dashboard
test ! -e /usr/local/bin/theme
python3 - <<'PY'
import sqlite3

with sqlite3.connect("/etc/x-ui/x-ui.db") as connection:
    value = connection.execute(
        "SELECT value FROM settings WHERE key='subThemeDir'"
    ).fetchone()[0]
assert value == "", value
PY

cat >"$FAKE_BIN/curl" <<'SH'
#!/usr/bin/env bash
exit 22
SH
cat >"$FAKE_BIN/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$FAKE_BIN/curl" "$FAKE_BIN/sleep"

if PATH="$FAKE_BIN:$PATH" THEME_SOURCE_DIR="$ROOT" bash "$ROOT/install.sh"; then
    printf 'Installer unexpectedly passed a failed health check.\n' >&2
    exit 1
fi
test ! -e /opt/3x-ui-subscription-dashboard
test ! -e /etc/3x-ui-subscription-dashboard
test ! -e /var/lib/3x-ui-subscription-dashboard
test ! -e /var/backups/3x-ui-subscription-dashboard
test ! -e /usr/local/bin/theme
python3 - <<'PY'
import sqlite3

with sqlite3.connect("/etc/x-ui/x-ui.db") as connection:
    value = connection.execute(
        "SELECT value FROM settings WHERE key='subThemeDir'"
    ).fetchone()[0]
assert value == "", value
PY

printf 'installer and uninstall integration test passed\n'
