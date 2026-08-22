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

# Only needed for the live camera feed, which decodes RTSP. Everything else
# works without it, so a failure here is a warning rather than a stop.
if ! command -v ffmpeg >/dev/null 2>&1; then
  if apt-get install -y -qq ffmpeg >/dev/null 2>&1; then
    note "ffmpeg installed (for the camera feed)"
  else
    note "!! ffmpeg could not be installed; the camera feed will not work"
  fi
else
  note "ffmpeg present"
fi

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
# 0.0.0.0, because the documented setup puts Caddy on a *different* machine and
# reaches this one over Tailscale or WireGuard. 127.0.0.1 is right only when
# the proxy runs on this same host - otherwise the service starts perfectly,
# listens on loopback, and every visitor gets a 502 from a reverse proxy that
# cannot dial it. That failure looks like a network problem and is not one.
#
# Nothing is exposed by this that was not already: the container has no public
# address, and a direct caller on the LAN gets the same rate limits as anyone
# else - client_ip falls back to the socket peer when the forwarding header is
# absent, which a direct caller cannot fake.
POSPRINTWEB_HOST=0.0.0.0
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
# This one REFUSES the message and says so, which tells whoever is probing
# exactly what to edit.
POSPRINTWEB_BLOCKLIST=

# The quiet counterpart. A match is accepted with a normal success response,
# charged against the sender's quota, logged, and never printed. Nothing about
# it appears on the page, so there is nothing to iterate against.
#   cp /root/posprint/web/deploy/shadowlist.example.txt /etc/posprintweb-shadowlist.txt
POSPRINTWEB_SHADOWLIST=

# Not keyed on IP, because an attacker's address is not a scarce resource.
# Refuses content already printed within this many hours, however it has been
# re-spaced or re-cased. 0 disables.
POSPRINTWEB_REPEAT_HOURS=24

# Burst cap across everyone. Blunts a flood without ending the day the way the
# daily budget would. 0 disables.
POSPRINTWEB_GLOBAL_HOURLY=30

# The short window, and the only limit that answers a flood from a rented
# proxy pool: rotating addresses defeats everything keyed on IP, and random
# text defeats the repeat check. A minute is fatal to a flood and invisible to
# a person, since the per-IP cooldown already stops anyone reaching it alone.
# Retry-After says exactly when a slot frees, so the worst wait is under a
# minute. 0 disables.
POSPRINTWEB_GLOBAL_BURST=8
POSPRINTWEB_GLOBAL_BURST_SECONDS=60

# Proof of work: every print must arrive with a solved puzzle, which is the one
# cost a rented address cannot pay. The flood that made this necessary posted
# straight to /api/print, so a button or a checkbox would have changed nothing.
# Difficulty is leading zero bits, so each bit doubles the sender's work. 18 is
# about a fifth of a second on a desktop and a second on an old phone, and the
# page solves it while the message is being typed. Lower it if visitors on slow
# phones complain; raising it costs them far more than it costs an attacker,
# who is not running JavaScript. 0 disables the check.
POSPRINTWEB_POW_BITS=18
POSPRINTWEB_POW_TTL_SECONDS=300

# Siege mode: the only control here that is a guarantee rather than a price.
# While the printer is under attack, messages queue for approval on /admin
# instead of printing, so nothing reaches paper without you. The trigger is
# refusals rather than prints - a flood hammers a closed door hundreds of times
# a minute, friends taking turns do not - which is what keeps this switched off
# during an ordinary busy evening. 0 disables.
POSPRINTWEB_HOLD_THRESHOLD=20
POSPRINTWEB_HOLD_WINDOW_SECONDS=300
POSPRINTWEB_HOLD_FOR_SECONDS=1800
POSPRINTWEB_HOLD_MAX_QUEUE=200

# The second trigger, and the one that exists because the repository is public.
# Refusals only happen when someone overshoots, so a reader who knows the
# threshold above can pace exactly at the burst cap and never trip it. Nobody
# sends sixty messages an hour to a stranger printer, however politely spaced.
POSPRINTWEB_HOLD_VOLUME=60
POSPRINTWEB_HOLD_VOLUME_SECONDS=3600

# The visual puzzle offered during a siege: solve it and print now instead of
# waiting in the queue. Not a wall - no captcha is, and this one is rendered by
# code anybody can read. Failing it is not refusal, only the ordinary wait.
POSPRINTWEB_CAPTCHA=true

# Set both of these. Unset, they are random per process, which is fine for one
# worker and breaks across a restart mid-solve. They are not in the repository
# and must not be: openssl rand -hex 32
#POSPRINTWEB_POW_SECRET=
#POSPRINTWEB_CAPTCHA_SECRET=

# --- live camera (optional) ------------------------------------------------
# RTSP URL of a camera pointed at the printer. Empty disables the feed.
# Tapo: enable Advanced Settings -> Camera Account in the app first, then
#   rtsp://<user>:<pass>@<camera-ip>:554/stream2   (360p, easy on the uplink)
#   rtsp://<user>:<pass>@<camera-ip>:554/stream1   (1080p)
# These credentials never reach a browser; the page only ever gets JPEGs.
POSPRINTWEB_CAMERA_URL=

# always | after_print | off
# THIS IS A PRIVACY SETTING. "always" puts a live view of the room on a public
# URL, permanently. Point the camera at the printer and nothing else.
POSPRINTWEB_CAMERA_MODE=always

# 0 uses the camera's own frame rate and resolution. Lower them if the flat's
# upload bandwidth suffers - that is the bottleneck, not the VPS.
POSPRINTWEB_CAMERA_FPS=0
POSPRINTWEB_CAMERA_WIDTH=0
POSPRINTWEB_CAMERA_QUALITY=6
POSPRINTWEB_CAMERA_MAX_VIEWERS=6

# Cuts the picture immediately, without stopping printing:
#   touch /etc/posprintweb-camera.disabled
POSPRINTWEB_CAMERA_KILLSWITCH=/etc/posprintweb-camera.disabled

# ONLY set this to true once a reverse proxy or tunnel is actually in front.
# With it on and nothing in front, anyone can forge the header below and mint
# themselves unlimited prints.
POSPRINTWEB_TRUST_PROXY=false

# How many proxies of your own stand in front. The client address is that
# many entries from the END of X-Forwarded-For, because each proxy appends
# the peer it saw - so the last entry is the one yours wrote and anything
# left of it is whatever the sender claimed. 1 for Caddy alone, 2 if
# Cloudflare is in front of it. Too high fails safe.
POSPRINTWEB_PROXY_HOPS=1

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
