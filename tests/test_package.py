from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
template = (ROOT / "src" / "theme" / "index.html").read_text(encoding="utf-8")
installer = (ROOT / "install.sh").read_text(encoding="utf-8")
manager = (ROOT / "src" / "bin" / "theme").read_text(encoding="utf-8")
same_origin = (ROOT / "src" / "bin" / "same-origin").read_text(encoding="utf-8")
service = (ROOT / "src" / "service" / "history_service.py").read_text(encoding="utf-8")

assert template.count("__HISTORY_API_PORT__") == 1
assert template.count("__HISTORY_API_SAME_ORIGIN__") == 1
assert "new URL(window.location.href)" in template
assert 'usageHistoryUrl.pathname = "/v1/history/"' in template
assert not re.search(r'https?://(?:\d{1,3}\.){3}\d{1,3}/v1/history', template)
assert "api.qrserver.com" not in template
assert "buildLocalQrDataUrl" in template

for required in (
    "Update Template",
    "Uninstall",
    "Exit",
):
    assert required in manager
assert "Continue? [y/N]:" in manager
assert 'menu_line "${BOLD}${RED}" "2) Uninstall"' in manager
assert "Installation completed successfully" in installer
assert "SSL" not in manager.split("show_menu()", 1)[1].split("main()", 1)[0]
assert "Domain" not in manager.split("show_menu()", 1)[1].split("main()", 1)[0]

assert '"2097"' in service
assert "XUI_HISTORY_TLS" in service
assert "XUI_HISTORY_LISTEN" in service
assert "XUI_SOURCE_DB_TYPE" in service
assert 'XUI_HISTORY_POLL_SECONDS", "60"' in service
assert "const AUTO_DATA_REFRESH_MS = 60 * 1000;" in template
assert 'id="scrollCue"' in template
assert "subscription_scroll_cue_seen_v1" in template
assert 'rootMargin: "0px 0px 8% 0px"' in template
mobile_metric_rule = re.search(
    r"@media\(max-width:420px\).*?\.metric-icon\s*\{(.*?)\}",
    template,
    re.DOTALL,
)
assert mobile_metric_rule
assert "display: grid" in mobile_metric_rule.group(1)
assert "display: none" not in mobile_metric_rule.group(1)
assert "PANEL_CHANGED=true" in installer
assert "rollback" in installer
assert 'healthz" >/dev/null 2>&1' in installer
assert "--same-origin" in installer
assert 'same-origin" enable' in installer
assert 'same-origin" disable' in manager
assert "/v1/history/invalid" in same_origin
assert "previous_sub_port" in same_origin
assert "previous_sub_listen" in same_origin
assert "previous_sub_uri" in same_origin
assert "does not support custom subscription templates" not in installer

private_key_marker = b"BEGIN " + b"OPENSSH PRIVATE KEY"
for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts:
        continue
    data = path.read_bytes()
    assert private_key_marker not in data, path

print("all package tests passed")
