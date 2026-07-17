# 3X-UI Subscription Template

A polished custom subscription page for [3X-UI](https://github.com/MHSanaei/3x-ui), with a server-side seven-day traffic chart, live subscription data, automatic refresh, responsive mobile layout, dark/light themes, and Persian/English languages.

The collector reads 3X-UI directly. A subscriber does **not** need to open the page before traffic is recorded.

## Install

Run as `root` on the 3X-UI server:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/masihamini00/3x-ui-subscription-template/main/install.sh)
```

The installer prints the selected history API port. It starts at `2097/tcp` and selects the next free port when necessary. If a firewall is enabled, allow the printed TCP port.

Requirements:

- A current 3X-UI installation managed by `x-ui.service`
- Linux with systemd
- `bash`, `curl`, `tar`, and Python 3
- For PostgreSQL installations: `psql` and `pg_dump`

The installer does not configure a domain, certificate, or firewall. Those remain under the control of 3X-UI and the server administrator.

## Manage

Run:

```bash
theme
```

The manager contains only:

```text
Theme Manager
1) Update Template
2) Uninstall
0) Exit
```

`Uninstall` removes the template, collector, history database, backups, manager, and related systemd units. It restores the previous 3X-UI `subThemeDir` value when the setting still points to this project. It never removes user certificates or the 3X-UI database.

## How it works

- The installer backs up the 3X-UI database before every install or update.
- It installs versioned releases below `/opt/3x-ui-subscription-dashboard/releases/` and switches the `current` symlink atomically.
- It sets 3X-UI `subThemeDir` to `/opt/3x-ui-subscription-dashboard/current/theme/` automatically.
- The collector reads traffic counters every 15 minutes and stores safe daily deltas in its own SQLite database.
- A reset/rebound guard prevents an old Xray counter from being counted again after renewals or temporary resets.
- The page refreshes subscription and chart data automatically every five minutes.
- A hidden systemd timer checks 3X-UI certificate settings every five minutes and switches the history API between HTTP and HTTPS as needed.

The browser builds the history URL from the current subscription URL. If the subscription uses an IP address, domain, HTTP, HTTPS, or later changes host, the dashboard follows it automatically and only replaces the port with the selected history API port.

The seven-day database starts at installation time; traffic from days before installation cannot be reconstructed reliably. A newly created subscription is baselined on its first collector pass, so old counter values are not mistaken for new traffic.

## Paths

| Purpose | Path |
| --- | --- |
| Current release | `/opt/3x-ui-subscription-dashboard/current/` |
| Configuration | `/etc/3x-ui-subscription-dashboard/` |
| Seven-day history | `/var/lib/3x-ui-subscription-dashboard/history.db` |
| Installer backups | `/var/backups/3x-ui-subscription-dashboard/` |
| Manager | `/usr/local/bin/theme` |

## Security model

The public endpoint only returns seven-day usage and the same basic subscription state already associated with a valid subscription ID. It does not expose panel credentials, database paths, API tokens, or SSH access. The collector opens the 3X-UI SQLite database read-only, or uses the local PostgreSQL client when that backend is selected.

QR codes are generated locally in the browser. Subscription and configuration links are not sent to a QR service.

Keep subscription links private. Anyone who knows a valid subscription ID can query that subscription's history endpoint, just as they can open its subscription URL.

## Development

```bash
python3 tests/test_history_service.py
python3 tests/test_panel_settings.py
python3 tests/test_package.py
node tests/test_template.js
bash -n install.sh src/bin/theme
```

The destructive lifecycle test in `tests/test_installer.sh` is intentionally restricted to GitHub Actions.

## License

GPL-3.0. See [LICENSE](LICENSE).
