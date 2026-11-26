#!/usr/bin/env bash
# Claude Pulse installer for Raspberry Pi OS (Bookworm or newer) / Debian / Ubuntu.
# Installs to ~/claude-pulse, runs as a systemd service on port 8787,
# and puts a "Claude Pulse" icon on your desktop and in the app menu.
set -euo pipefail

APP_NAME="Claude Pulse"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
INSTALL_DIR="$RUN_HOME/claude-pulse"
CONF_DIR="$RUN_HOME/.config/claude-pulse"
CONF="$CONF_DIR/config.json"
PORT=8787

if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
  echo "Run this as your normal user (it will ask for sudo when needed), not as root." >&2
  exit 1
fi

echo "==> Installing $APP_NAME for user $RUN_USER"
if ! command -v python3 >/dev/null; then
  sudo apt-get update && sudo apt-get install -y python3
fi
python3 -c 'import sys, zoneinfo; assert sys.version_info >= (3, 9)' || {
  echo "Python 3.9+ is required." >&2; exit 1; }

as_user() { if [ "$(id -u)" -eq 0 ]; then sudo -u "$RUN_USER" "$@"; else "$@"; fi; }

# --- app files
as_user mkdir -p "$INSTALL_DIR"
as_user cp -r "$SRC_DIR/claude_pulse" "$INSTALL_DIR/"
as_user cp "$SRC_DIR/README.md" "$SRC_DIR/uninstall.sh" "$INSTALL_DIR/" 2>/dev/null || true
chmod +x "$INSTALL_DIR/uninstall.sh" 2>/dev/null || true

# --- config (kept on reinstall)
as_user mkdir -p "$CONF_DIR"
if [ ! -f "$CONF" ]; then
  as_user cp "$SRC_DIR/config.example.json" "$CONF"
  echo "==> Wrote default config to $CONF"
else
  echo "==> Keeping your existing config at $CONF"
fi
chmod 600 "$CONF"
PORT="$(python3 -c "import json;print(json.load(open('$CONF')).get('port',8787))" 2>/dev/null || echo 8787)"

# Optional keys. Reads from /dev/tty so this also works with: curl ... | bash
save_key() {  # $1 = config field, $2 = value
  CONF="$CONF" FIELD="$1" KEY="$2" python3 - <<'PY'
import json, os
p = os.environ["CONF"]; c = json.load(open(p)); c[os.environ["FIELD"]] = os.environ["KEY"].strip()
json.dump(c, open(p, "w"), indent=2)
PY
}
has_key() { python3 -c "import json,sys;sys.exit(0 if json.load(open('$CONF')).get('$1') else 1)"; }
if [ -z "${CLAUDE_PULSE_NO_PROMPT:-}" ] && ( : </dev/tty ) 2>/dev/null; then
  exec 3</dev/tty
  if ! has_key openrouter_api_key; then
    echo
    echo "Recommended: a FREE OpenRouter key (no credits needed) gives live speed for every Claude model."
    echo "  Get one at https://openrouter.ai/settings/keys  (sign in -> Create Key)"
    echo "Paste it and press Enter (nothing shows while you paste), or just press Enter to skip."
    KEY=""; read -r -s -p "OpenRouter key: " KEY <&3 || true; echo
    [ -n "$KEY" ] && save_key openrouter_api_key "$KEY" && echo "==> OpenRouter key saved."
  fi
  if ! has_key anthropic_api_key; then
    echo
    echo "Optional: an Anthropic API key (sk-ant-...) adds your own API rate limits and instant new-model alerts."
    echo "Most people can skip this. Press Enter to skip."
    KEY=""; read -r -s -p "Anthropic key: " KEY <&3 || true; echo
    [ -n "$KEY" ] && save_key anthropic_api_key "$KEY" && echo "==> Anthropic key saved."
  fi
  exec 3<&-
  echo "Keys are stored in $CONF (readable only by you)."
fi

# --- systemd service
echo "==> Setting up the background service"
sudo tee /etc/systemd/system/claude-pulse.service >/dev/null <<EOF
[Unit]
Description=Claude Pulse dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR/claude_pulse
ExecStart=/usr/bin/env python3 $INSTALL_DIR/claude_pulse/server.py
Environment=CLAUDE_PULSE_CONFIG=$CONF
Environment=CLAUDE_PULSE_DATA=$RUN_HOME/.local/share/claude-pulse
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable claude-pulse.service >/dev/null
sudo systemctl restart claude-pulse.service

# --- desktop icon + app menu entry
ICON="$INSTALL_DIR/claude_pulse/static/icon.svg"
LAUNCHER="$INSTALL_DIR/open-claude-pulse.sh"
as_user tee "$LAUNCHER" >/dev/null <<EOF
#!/usr/bin/env bash
# Starts the service if it's stopped, then opens the dashboard in a browser window.
URL="http://localhost:$PORT"
systemctl is-active --quiet claude-pulse || pkexec systemctl start claude-pulse || true
for b in chromium-browser chromium firefox; do
  if command -v "\$b" >/dev/null; then
    case "\$b" in chromium*) exec "\$b" --app="\$URL" --window-size=1400,950 ;; *) exec "\$b" "\$URL" ;; esac
  fi
done
exec xdg-open "\$URL"
EOF
chmod +x "$LAUNCHER"

DESKTOP_ENTRY="[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Anthropic stock, Claude status, model speed and usage resets
Exec=$LAUNCHER
Icon=$ICON
Terminal=false
Categories=Network;Utility;
StartupNotify=true"

as_user mkdir -p "$RUN_HOME/.local/share/applications"
printf '%s\n' "$DESKTOP_ENTRY" | as_user tee "$RUN_HOME/.local/share/applications/claude-pulse.desktop" >/dev/null

DESKTOP_DIR="$(as_user xdg-user-dir DESKTOP 2>/dev/null || echo "$RUN_HOME/Desktop")"
[ "$DESKTOP_DIR" = "$RUN_HOME" ] && DESKTOP_DIR="$RUN_HOME/Desktop"
as_user mkdir -p "$DESKTOP_DIR"
DESK_FILE="$DESKTOP_DIR/claude-pulse.desktop"
printf '%s\n' "$DESKTOP_ENTRY" | as_user tee "$DESK_FILE" >/dev/null
chmod +x "$DESK_FILE"
# Mark it trusted so the Pi desktop runs it without the "execute?" prompt.
as_user gio set "$DESK_FILE" metadata::trusted true 2>/dev/null || true
# Raspberry Pi OS desktop (pcmanfm): don't ask what to do with launchers.
LIBFM="$RUN_HOME/.config/libfm/libfm.conf"
if [ -f "$LIBFM" ]; then
  if grep -q '^quick_exec=' "$LIBFM"; then sed -i 's/^quick_exec=.*/quick_exec=1/' "$LIBFM"
  else sed -i '/^\[config\]/a quick_exec=1' "$LIBFM"; fi
fi

sleep 2
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
if systemctl is-active --quiet claude-pulse; then
  echo "==> $APP_NAME is running."
else
  echo "!! The service didn't start. Check: journalctl -u claude-pulse -n 50" >&2
fi
echo "    On this Pi:          http://localhost:$PORT   (or double-click the desktop icon)"
[ -n "$IP" ] && echo "    From other devices:  http://$IP:$PORT"
echo "    Config:              $CONF   (then: sudo systemctl restart claude-pulse)"
