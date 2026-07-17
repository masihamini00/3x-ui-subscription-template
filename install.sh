#!/usr/bin/env bash
set -Eeuo pipefail

APP="3x-ui-subscription-dashboard"
REPO="${THEME_REPO:-masihamini00/3x-ui-subscription-dashboard}"
REF="${THEME_REF:-main}"
INSTALL_ROOT="/opt/${APP}"
CONFIG_DIR="/etc/${APP}"
DATA_DIR="/var/lib/${APP}"
BACKUP_DIR="/var/backups/${APP}"
CURRENT_LINK="${INSTALL_ROOT}/current"
MANAGED_THEME_DIR="${CURRENT_LINK}/theme/"
HISTORY_SERVICE="theme-history.service"
SYNC_SERVICE="theme-settings-sync.service"
SYNC_TIMER="theme-settings-sync.timer"
UPDATE=false
WORK_DIR=""
SOURCE_DIR=""
RELEASE_DIR=""
OLD_TARGET=""
OLD_THEME_SETTING=""
PANEL_CHANGED=false
INSTALLED_BEFORE=false
MUTATION_STARTED=false
HAD_INSTALL_ROOT=false
HAD_CONFIG=false
HAD_DATA=false
HAD_BACKUPS=false
DB_BACKUP_PATH=""
SETTING_BACKUP_PATH=""

log() {
    printf '\033[1;36m==>\033[0m %s\n' "$*"
}

die() {
    printf '\033[1;31mError:\033[0m %s\n' "$*" >&2
    exit 1
}

cleanup() {
    [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
}

finish() {
    local status=$?
    trap - EXIT ERR
    if [[ "$status" -ne 0 && -n "$WORK_DIR" && "$MUTATION_STARTED" == true ]]; then
        rollback "$status"
    fi
    cleanup
    exit "$status"
}

restore_file() {
    local name="$1" destination="$2"
    if [[ -f "$WORK_DIR/backup/$name" ]]; then
        install -m 0644 "$WORK_DIR/backup/$name" "$destination"
    else
        rm -f -- "$destination"
    fi
}

rollback() {
    local status="$1"
    trap - EXIT ERR
    set +e
    printf '\nInstallation failed; rolling back.\n' >&2

    if [[ "$PANEL_CHANGED" == true && -n "$RELEASE_DIR" ]]; then
        python3 "$RELEASE_DIR/tools/panel_settings.py" set subThemeDir "$OLD_THEME_SETTING" >/dev/null 2>&1
    fi

    if [[ -n "$OLD_TARGET" ]]; then
        ln -s "$OLD_TARGET" "$INSTALL_ROOT/.current.rollback"
        mv -Tf "$INSTALL_ROOT/.current.rollback" "$CURRENT_LINK"
    else
        rm -f -- "$CURRENT_LINK"
    fi

    restore_file "$HISTORY_SERVICE" "/etc/systemd/system/$HISTORY_SERVICE"
    restore_file "$SYNC_SERVICE" "/etc/systemd/system/$SYNC_SERVICE"
    restore_file "$SYNC_TIMER" "/etc/systemd/system/$SYNC_TIMER"
    if [[ -f "$WORK_DIR/backup/theme" ]]; then
        install -m 0755 "$WORK_DIR/backup/theme" /usr/local/bin/theme
    elif [[ "$INSTALLED_BEFORE" == false ]]; then
        rm -f -- /usr/local/bin/theme
    fi

    if [[ -d "$WORK_DIR/config-backup" ]]; then
        rm -rf -- "$CONFIG_DIR"
        cp -a "$WORK_DIR/config-backup" "$CONFIG_DIR"
    elif [[ "$INSTALLED_BEFORE" == false ]]; then
        rm -rf -- "$CONFIG_DIR"
    fi

    systemctl daemon-reload >/dev/null 2>&1
    if [[ "$INSTALLED_BEFORE" == true ]]; then
        systemctl restart "$HISTORY_SERVICE" >/dev/null 2>&1
        systemctl restart x-ui.service >/dev/null 2>&1
    else
        systemctl disable --now "$SYNC_TIMER" "$HISTORY_SERVICE" >/dev/null 2>&1
        [[ "$HAD_INSTALL_ROOT" == true ]] || rm -rf -- "$INSTALL_ROOT"
        [[ "$HAD_CONFIG" == true ]] || rm -rf -- "$CONFIG_DIR"
        [[ "$HAD_DATA" == true ]] || rm -rf -- "$DATA_DIR"
        if [[ "$HAD_BACKUPS" == true ]]; then
            [[ -z "$DB_BACKUP_PATH" ]] || rm -f -- "$DB_BACKUP_PATH"
            [[ -z "$SETTING_BACKUP_PATH" ]] || rm -f -- "$SETTING_BACKUP_PATH"
        else
            rm -rf -- "$BACKUP_DIR"
        fi
    fi
    [[ -z "$RELEASE_DIR" ]] || rm -rf -- "$RELEASE_DIR"
    cleanup
    exit "$status"
}

for argument in "$@"; do
    case "$argument" in
        --update) UPDATE=true ;;
        *) die "Unknown option: $argument" ;;
    esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "Run this installer as root."
[[ $(uname -s) == Linux ]] || die "This installer supports Linux only."
for command in python3 systemctl curl tar; do
    command -v "$command" >/dev/null 2>&1 || die "$command is required."
done
systemctl cat x-ui.service >/dev/null 2>&1 || die "The x-ui systemd service was not found."

WORK_DIR=$(mktemp -d)
trap finish EXIT
mkdir -p "$WORK_DIR/backup"

if [[ -n ${THEME_SOURCE_DIR:-} ]]; then
    SOURCE_DIR=$(cd "$THEME_SOURCE_DIR" && pwd)
else
    log "Downloading ${REPO}@${REF}"
    mkdir -p "$WORK_DIR/source"
    curl -fsSL --retry 3 "https://github.com/${REPO}/archive/${REF}.tar.gz" -o "$WORK_DIR/source.tar.gz"
    tar -xzf "$WORK_DIR/source.tar.gz" --strip-components=1 -C "$WORK_DIR/source"
    SOURCE_DIR="$WORK_DIR/source"
fi

for required in \
    VERSION \
    src/theme/index.html \
    src/service/history_service.py \
    src/tools/panel_settings.py \
    src/bin/theme \
    src/systemd/theme-history.service \
    src/systemd/theme-settings-sync.service \
    src/systemd/theme-settings-sync.timer; do
    [[ -f "$SOURCE_DIR/$required" ]] || die "Package file is missing: $required"
done

load_panel_environment() {
    local file
    set -a
    for file in /etc/default/x-ui /etc/conf.d/x-ui /etc/sysconfig/x-ui; do
        if [[ -r "$file" ]]; then
            # These files are installed and owned by the 3X-UI system service.
            # shellcheck disable=SC1090
            . "$file"
        fi
    done
    set +a
    case "${XUI_DB_TYPE:-sqlite}" in
        postgres|postgresql|pg)
            export XUI_DB_TYPE="postgres"
            [[ -n ${XUI_DB_DSN:-} ]] || die "XUI_DB_DSN is missing."
            export PGDATABASE="$XUI_DB_DSN"
            command -v psql >/dev/null 2>&1 || die "psql is required for PostgreSQL."
            command -v pg_dump >/dev/null 2>&1 || die "pg_dump is required for PostgreSQL."
            ;;
        *)
            export XUI_DB_TYPE="sqlite"
            export XUI_SOURCE_DB="${XUI_DB_FOLDER:-/etc/x-ui}/x-ui.db"
            ;;
    esac
}

load_panel_environment
PANEL_HELPER="$SOURCE_DIR/src/tools/panel_settings.py"
python3 "$PANEL_HELPER" exists subThemeDir || die "This 3X-UI version does not support custom subscription templates. Update 3X-UI first."
OLD_THEME_SETTING=$(python3 "$PANEL_HELPER" get subThemeDir)

if [[ -L "$CURRENT_LINK" ]]; then
    INSTALLED_BEFORE=true
    OLD_TARGET=$(readlink -f "$CURRENT_LINK")
elif [[ -e "$CURRENT_LINK" ]]; then
    die "$CURRENT_LINK exists but is not a symbolic link."
fi
if [[ "$UPDATE" == true && "$INSTALLED_BEFORE" == false ]]; then
    die "The dashboard is not installed yet. Run the normal install command first."
fi

for unit in "$HISTORY_SERVICE" "$SYNC_SERVICE" "$SYNC_TIMER"; do
    [[ ! -f "/etc/systemd/system/$unit" ]] || cp -a "/etc/systemd/system/$unit" "$WORK_DIR/backup/$unit"
done
[[ ! -f /usr/local/bin/theme ]] || cp -a /usr/local/bin/theme "$WORK_DIR/backup/theme"
[[ ! -d "$CONFIG_DIR" ]] || cp -a "$CONFIG_DIR" "$WORK_DIR/config-backup"

[[ ! -e "$INSTALL_ROOT" ]] || HAD_INSTALL_ROOT=true
[[ ! -e "$CONFIG_DIR" ]] || HAD_CONFIG=true
[[ ! -e "$DATA_DIR" ]] || HAD_DATA=true
[[ ! -e "$BACKUP_DIR" ]] || HAD_BACKUPS=true
MUTATION_STARTED=true
install -d -m 0755 "$INSTALL_ROOT/releases" "$CONFIG_DIR" "$DATA_DIR" "$BACKUP_DIR"
chmod 0700 "$CONFIG_DIR" "$DATA_DIR" "$BACKUP_DIR"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_extension="sqlite3"
[[ "$XUI_DB_TYPE" != postgres ]] || backup_extension="dump"
log "Backing up the 3X-UI database"
DB_BACKUP_PATH="$BACKUP_DIR/x-ui-${timestamp}.${backup_extension}"
SETTING_BACKUP_PATH="$BACKUP_DIR/subThemeDir-${timestamp}.txt"
python3 "$PANEL_HELPER" backup "$DB_BACKUP_PATH"
printf '%s\n' "$OLD_THEME_SETTING" >"$SETTING_BACKUP_PATH"
chmod 0600 "$SETTING_BACKUP_PATH"

if [[ ! -f "$CONFIG_DIR/previous_theme_dir" ]]; then
    printf '%s' "$OLD_THEME_SETTING" >"$CONFIG_DIR/previous_theme_dir"
fi
printf '%s\n' "$REPO" >"$CONFIG_DIR/repo"
printf '%s\n' "$REF" >"$CONFIG_DIR/ref"

if [[ -r "$CONFIG_DIR/history_port" ]]; then
    HISTORY_PORT=$(<"$CONFIG_DIR/history_port")
    [[ "$HISTORY_PORT" =~ ^[0-9]+$ ]] || die "Stored history port is invalid."
else
    HISTORY_PORT=$(python3 - <<'PY'
import socket
for port in range(2097, 2198):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
        print(port)
        break
    except OSError:
        continue
else:
    raise SystemExit("No free TCP port was found between 2097 and 2197")
PY
    )
    printf '%s\n' "$HISTORY_PORT" >"$CONFIG_DIR/history_port"
fi
chmod 0600 "$CONFIG_DIR"/*

version=$(tr -d '[:space:]' <"$SOURCE_DIR/VERSION")
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || die "VERSION is invalid."
RELEASE_DIR="$INSTALL_ROOT/releases/${version}-${timestamp}-$$"
install -d -m 0755 "$RELEASE_DIR/theme" "$RELEASE_DIR/service" "$RELEASE_DIR/tools" "$RELEASE_DIR/bin"
install -m 0644 "$SOURCE_DIR/src/theme/index.html" "$RELEASE_DIR/theme/index.html"
install -m 0755 "$SOURCE_DIR/src/service/history_service.py" "$RELEASE_DIR/service/history_service.py"
install -m 0755 "$SOURCE_DIR/src/tools/panel_settings.py" "$RELEASE_DIR/tools/panel_settings.py"
install -m 0755 "$SOURCE_DIR/src/bin/theme" "$RELEASE_DIR/bin/theme"

python3 - "$RELEASE_DIR/theme/index.html" "$HISTORY_PORT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
content = path.read_text(encoding="utf-8")
placeholder = "__HISTORY_API_PORT__"
if content.count(placeholder) != 1:
    raise SystemExit(f"expected one {placeholder} placeholder")
path.write_text(content.replace(placeholder, sys.argv[2]), encoding="utf-8")
PY

ln -s "$RELEASE_DIR" "$INSTALL_ROOT/.current.new"
mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_LINK"
install -m 0755 "$RELEASE_DIR/bin/theme" /usr/local/bin/theme
install -m 0644 "$SOURCE_DIR/src/systemd/$HISTORY_SERVICE" "/etc/systemd/system/$HISTORY_SERVICE"
install -m 0644 "$SOURCE_DIR/src/systemd/$SYNC_SERVICE" "/etc/systemd/system/$SYNC_SERVICE"
install -m 0644 "$SOURCE_DIR/src/systemd/$SYNC_TIMER" "/etc/systemd/system/$SYNC_TIMER"
systemctl daemon-reload

log "Activating the subscription template"
PANEL_CHANGED=true
python3 "$RELEASE_DIR/tools/panel_settings.py" set subThemeDir "$MANAGED_THEME_DIR"
/usr/local/bin/theme sync --quiet
systemctl enable --now "$HISTORY_SERVICE" "$SYNC_TIMER" >/dev/null
systemctl restart "$HISTORY_SERVICE" x-ui.service

cert=$(python3 "$RELEASE_DIR/tools/panel_settings.py" get subCertFile)
key=$(python3 "$RELEASE_DIR/tools/panel_settings.py" get subKeyFile)
scheme="http"
curl_options=(-fsS)
if [[ -n "$cert" && -n "$key" && -r "$cert" && -r "$key" ]]; then
    scheme="https"
    curl_options=(-kfsS)
fi
healthy=false
for _ in {1..20}; do
    if curl "${curl_options[@]}" --max-time 3 "${scheme}://127.0.0.1:${HISTORY_PORT}/healthz" >/dev/null; then
        healthy=true
        break
    fi
    sleep 1
done
[[ "$healthy" == true ]] || {
    journalctl -u "$HISTORY_SERVICE" -n 20 --no-pager >&2 || true
    die "The history API did not pass its health check."
}

PANEL_CHANGED=false
log "Installed successfully"
printf 'Template path: %s\n' "$MANAGED_THEME_DIR"
printf 'History API port: %s/tcp\n' "$HISTORY_PORT"
printf "Open that TCP port in your firewall if a firewall is enabled.\n"
printf "Run 'theme' to update or uninstall.\n"
