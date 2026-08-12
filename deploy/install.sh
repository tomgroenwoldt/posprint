#!/usr/bin/env bash
#
# Run this INSIDE THE LXC CONTAINER as root.
#
#   ./deploy/install.sh
#
# Installs posprint to /opt/posprint in a virtualenv, creates a system user,
# generates an API key, and starts it under systemd.
#
# Idempotent: re-running upgrades the code in place and keeps the existing
# API key and settings.

set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/opt/posprint"
ENV_FILE="/etc/posprint.env"
USER_NAME="posprint"

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
note() { echo "  $*"; }

[[ $EUID -eq 0 ]] || die "must run as root inside the container"
[[ -f "$SRC/requirements.txt" ]] || die "run this from the posprint checkout ($SRC looks wrong)"

step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev curl >/dev/null
note "python3 $(python3 --version 2>&1 | cut -d' ' -f2)"

step "Creating the service user"
if id "$USER_NAME" &>/dev/null; then
  note "user $USER_NAME already exists"
else
  useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
  note "created $USER_NAME"
fi
# Group lp owns the printer node under the udev rule; harmless if the rule set
# 0666 anyway, and it's what makes a tightened 0660 setup work later.
if getent group lp >/dev/null; then
  usermod -aG lp "$USER_NAME"
  note "added $USER_NAME to group lp"
fi

step "Installing code to $DEST"
mkdir -p "$DEST"
# Copy only what runs; leave the venv alone across upgrades.
cp -r "$SRC/posprint" "$DEST/"
cp "$SRC/requirements.txt" "$SRC/pyproject.toml" "$DEST/"
[[ -f "$SRC/README.md" ]] && cp "$SRC/README.md" "$DEST/"
note "copied"

step "Setting up the virtualenv"
if [[ ! -x "$DEST/venv/bin/python" ]]; then
  python3 -m venv "$DEST/venv"
  note "created $DEST/venv"
fi
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet -r "$DEST/requirements.txt"
note "dependencies installed"

chown -R root:root "$DEST"
chmod -R a+rX "$DEST"

step "Configuring $ENV_FILE"
if [[ -f "$ENV_FILE" ]]; then
  note "keeping existing config (delete it and re-run to regenerate)"
else
  API_KEY="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  cat > "$ENV_FILE" <<EOF
# posprint configuration. Restart the service after editing:
#   systemctl restart posprint

# Leave empty to auto-discover /dev/usb/lp* - this is what you want, because it
# keeps working when a replug re-enumerates the printer as lp1.
POSPRINT_DEVICE=

# 58 or 80. Sets both the dot width and the column count.
POSPRINT_PAPER_MM=80

# cp858 = cp850 plus the euro sign. If accented characters print as garbage,
# try cp437 (the most universally supported table on no-name printers).
POSPRINT_CODEPAGE=cp858

POSPRINT_HOST=0.0.0.0
POSPRINT_PORT=8080

# Required for every endpoint except /health. Empty disables auth entirely.
POSPRINT_API_KEY=${API_KEY}

# Bump CHUNK_DELAY_MS to 20-50 if long receipts come out with dropped or
# garbled sections - some cheap printers overrun their input buffer.
POSPRINT_CHUNK_BYTES=4096
POSPRINT_CHUNK_DELAY_MS=0

POSPRINT_AUTO_INIT=true
POSPRINT_AUTO_CUT=true
EOF
  note "generated with a fresh API key"
fi
chmod 0640 "$ENV_FILE"
chown root:"$USER_NAME" "$ENV_FILE"

step "Installing the systemd unit"
install -m 0644 "$SRC/deploy/posprint.service" /etc/systemd/system/posprint.service
systemctl daemon-reload
systemctl enable --quiet posprint
systemctl restart posprint
note "started"

sleep 2
step "Result"
if systemctl is-active --quiet posprint; then
  PORT="$(grep -E '^POSPRINT_PORT=' "$ENV_FILE" | cut -d= -f2)"
  KEY="$(grep -E '^POSPRINT_API_KEY=' "$ENV_FILE" | cut -d= -f2)"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "  service: active"
  echo "  url:     http://${IP:-<container-ip>}:${PORT:-8080}"
  echo "  docs:    http://${IP:-<container-ip>}:${PORT:-8080}/docs"
  echo "  api key: ${KEY:-<none>}"
  echo
  echo "  Check the printer is reachable:"
  echo "      curl -s http://localhost:${PORT:-8080}/health"
  echo
  echo "  Print a self-test page:"
  echo "      curl -X POST http://localhost:${PORT:-8080}/print/test -H 'X-API-Key: ${KEY}'"
else
  echo "  service FAILED to start. Logs:" >&2
  echo >&2
  journalctl -u posprint -n 40 --no-pager >&2
  exit 1
fi
echo
