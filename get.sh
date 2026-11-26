#!/usr/bin/env bash
# One-line installer / updater for Claude Pulse:
#   curl -fsSL https://raw.githubusercontent.com/hardwaremack-prog/claude-pulse/main/get.sh | bash
# Run it again any time to update. Your config and history are kept.
set -euo pipefail
REPO="hardwaremack-prog/claude-pulse"
BRANCH="${CLAUDE_PULSE_BRANCH:-main}"

command -v curl >/dev/null || { sudo apt-get update && sudo apt-get install -y curl; }
command -v tar >/dev/null || { sudo apt-get update && sudo apt-get install -y tar; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "==> Downloading Claude Pulse ($REPO@$BRANCH)"
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$TMP"
cd "$TMP"/claude-pulse-*
bash ./install.sh
