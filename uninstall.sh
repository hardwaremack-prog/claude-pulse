#!/usr/bin/env bash
# Removes Claude Pulse. Your config and history are kept unless you pass --purge.
set -uo pipefail
RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
sudo systemctl disable --now claude-pulse.service 2>/dev/null
sudo rm -f /etc/systemd/system/claude-pulse.service
sudo systemctl daemon-reload
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$RUN_HOME/Desktop")"
rm -f "$DESKTOP_DIR/claude-pulse.desktop" "$RUN_HOME/Desktop/claude-pulse.desktop" \
      "$RUN_HOME/.local/share/applications/claude-pulse.desktop"
rm -rf "$RUN_HOME/claude-pulse"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$RUN_HOME/.config/claude-pulse" "$RUN_HOME/.local/share/claude-pulse"
  echo "Claude Pulse removed, including config and history."
else
  echo "Claude Pulse removed. Config (~/.config/claude-pulse) and history (~/.local/share/claude-pulse) kept; use --purge to delete them."
fi
