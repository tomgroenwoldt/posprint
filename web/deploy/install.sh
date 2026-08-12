#!/usr/bin/env bash
#
# Run this INSIDE THE CONTAINER as root.
#
#   ./web/deploy/install.sh
#
# Installs posprint-web to /opt/posprint-web in a virtualenv, creates a system
# user, and starts it under systemd. Only touches the web front end: the
# printer service at the repo root has its own installer and belongs in its own
# container.
#
# Idempotent: re-running upgrades the code in place and keeps the existing
# config, database and admin key.

set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/opt/posprint-web"
ENV_FILE="/etc/posprintweb.env"
USER_NAME="posprintweb"

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
note() { echo "  $*"; }

[[ $EUID -eq 0 ]] || die "must run as root inside the container"
[[ -d "$SRC/posprintweb" ]] || die "run this as web/deploy/install.sh from a posprint checkout ($SRC looks wrong)"

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

step "Installing code to $DEST"
mkdir -p "$DEST"
cp -r "$SRC/posprintweb" "$DEST/"
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
  ADMIN_KEY="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  cat > "$ENV_FILE" <<EOF
# posprint-web configuration. Restart after editing:
#   systemctl restart posprintweb

# --- upstream: the posprint service ----------------------------------------
# Same container: http://127.0.0.1:8080
# Separate container: http://<posprint-container-ip>:8080
POSPRINTWEB_UPSTREAM=http://127.0.0.1:8080

# REQUIRED. Copy the value of POSPRINT_API_KEY from posprint's /etc/posprint.env.
# This is the credential that must never reach a browser.
POSPRINTWEB_UPSTREAM_KEY=

# --- this service ----------------------------------------------------------
POSPRINTWEB_HOST=127.0.0.1
POSPRINTWEB_PORT=8000
POSPRINTWEB_TITLE=Print to my receipt printer
POSPRINTWEB_BLURB=This prints on a real thermal printer in my flat.
POSPRINTWEB_TZ=Europe/Berlin

# MUST match POSPRINT_CODEPAGE in posprint's /etc/posprint.env. The printer has
# no glyphs outside it, so this decides which characters are refused up front
# rather than printed as a strip of question marks.
POSPRINTWEB_CODEPAGE=cp858

# --- abuse controls --------------------------------------------------------
# All three matter. See the README's threat model before loosening any of them.
POSPRINTWEB_COOLDOWN_SECONDS=60
POSPRINTWEB_PER_IP_DAILY=5
POSPRINTWEB_GLOBAL_DAILY=200
POSPRINTWEB_MAX_CHARS=500
POSPRINTWEB_MAX_LINES=20

# The printer is in your home. 22:00-08:00 local by default.
POSPRINTWEB_QUIET_START=22
POSPRINTWEB_QUIET_END=8

# Optional word blocklist, one term per line. Comments start with #.
POSPRINTWEB_BLOCKLIST=

# ONLY set this to true once a reverse proxy or tunnel is actually in front.
# With it on and nothing in front, anyone can forge the header below and mint
# themselves unlimited prints.
POSPRINTWEB_TRUST_PROXY=false

# The single header trusted for the client address. Must name a header the
# proxy in front OVERWRITES, not one it merely appends to or passes through:
#   Caddy / nginx / HAProxy -> x-forwarded-for
#   Cloudflare              -> cf-connecting-ip
POSPRINTWEB_CLIENT_IP_HEADER=x-forwarded-for

# Bypasses cooldown, quotas and quiet hours. Send as X-Admin-Key. Also unlocks
# GET /admin/log.
POSPRINTWEB_ADMIN_KEYS=${ADMIN_KEY}

POSPRINTWEB_DB=/var/lib/posprintweb/prints.db

# Emergency stop, checked on every request. No restart needed:
#   touch /etc/posprintweb.disabled
POSPRINTWEB_KILLSWITCH=/etc/posprintweb.disabled
EOF
  note "generated with a fresh admin key"
fi
chmod 0640 "$ENV_FILE"
chown root:"$USER_NAME" "$ENV_FILE"

if ! grep -qE '^POSPRINTWEB_UPSTREAM_KEY=.+' "$ENV_FILE"; then
  note ""
  note "!! POSPRINTWEB_UPSTREAM_KEY is empty in $ENV_FILE."
  note "!! Copy POSPRINT_API_KEY from posprint's /etc/posprint.env, then:"
  note "!!     systemctl restart posprintweb"
fi

step "Installing the systemd unit"
install -m 0644 "$SRC/deploy/posprintweb.service" /etc/systemd/system/posprintweb.service

DROPIN_DIR="/etc/systemd/system/posprintweb.service.d"
if systemd-detect-virt --container --quiet; then
  mkdir -p "$DROPIN_DIR"
  install -m 0644 "$SRC/deploy/posprintweb-container.conf" "$DROPIN_DIR/10-container.conf"
  note "container detected ($(systemd-detect-virt --container)) - relaxed mount-namespace hardening"
else
  rm -f "$DROPIN_DIR/10-container.conf"
  note "bare metal or VM - full hardening in effect"
fi

systemctl daemon-reload
systemctl enable --quiet posprintweb
# Must not be fatal under `set -e`: a failed start has to fall through to the
# Result block below, which dumps the journal.
systemctl restart posprintweb || true

sleep 2
step "Result"
if systemctl is-active --quiet posprintweb; then
  PORT="$(grep -E '^POSPRINTWEB_PORT=' "$ENV_FILE" | cut -d= -f2)"
  KEY="$(grep -E '^POSPRINTWEB_ADMIN_KEYS=' "$ENV_FILE" | cut -d= -f2)"
  echo "  service:   active"
  echo "  local url: http://127.0.0.1:${PORT:-8000}"
  echo "  admin key: ${KEY:-<none>}"
  echo
  echo "  Check it:"
  echo "      curl -s http://127.0.0.1:${PORT:-8000}/api/status"
  echo
  echo "  It is bound to localhost. Put a tunnel in front to publish it -"
  echo "  see 'Exposing it' in the README. Do not port-forward this directly."
else
  echo "  service FAILED to start. Logs:" >&2
  echo >&2
  journalctl -u posprintweb -n 40 --no-pager >&2
  exit 1
fi
echo
