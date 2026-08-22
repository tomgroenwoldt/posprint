#!/usr/bin/env bash
#
# Run this ON THE VPS as root, from a checkout of the repository.
#
#   ./web/deploy/relay-install.sh
#
# Installs the camera relay to /opt/posprint-relay and starts it under systemd.
# The relay pulls the camera feed from the container ONCE and fans it out to
# every viewer, so the flat's uplink carries one MJPEG stream no matter how
# many people are watching. Without it, Caddy opens a separate connection per
# viewer and the flat pays for each one.
#
# This installs nothing else: no database, no printer, no pages. The main
# service stays in the container, and so do the camera credentials, the camera
# mode and the killswitch. The relay only ever sees the picture the site
# already publishes.
#
# Idempotent: re-running upgrades the code and keeps the existing config.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="/opt/posprint-relay"
ENV_FILE="/etc/posprintweb-relay.env"
USER_NAME="posprintrelay"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

echo "==> packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl >/dev/null
# Deliberately no ffmpeg: decoding happens in the flat, next to the camera.

echo "==> user"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --no-create-home \
  --shell /usr/sbin/nologin "$USER_NAME"

echo "==> code"
mkdir -p "$DEST"
rm -rf "$DEST/posprintweb"
cp -r "$SRC/posprintweb" "$DEST/"
cp "$SRC/requirements.txt" "$DEST/"
[[ -f "$SRC/README.md" ]] && cp "$SRC/README.md" "$DEST/"

echo "==> virtualenv"
[[ -d "$DEST/venv" ]] || python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet -r "$DEST/requirements.txt"

chown -R "$USER_NAME:$USER_NAME" "$DEST"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> $ENV_FILE"
  cat > "$ENV_FILE" <<'EOF'
# posprint camera relay. Restart after editing:
#   systemctl restart posprintweb-relay

# The container's feed, over the tunnel. This is the only required setting.
# Use the container's tailnet or WireGuard address, not a public one.
POSPRINTWEB_RELAY_UPSTREAM=http://100.64.0.2:8000/api/camera.mjpg

# Caddy is the only thing that should reach this.
POSPRINTWEB_RELAY_HOST=127.0.0.1
POSPRINTWEB_RELAY_PORT=8001

# The real viewer cap now. The bottleneck moved from a domestic uplink to a
# VPS, so this can be far higher than the container's own limit - which should
# come down to 2, since the relay is its only viewer plus room for a reconnect
# to overlap a dying connection.
POSPRINTWEB_RELAY_MAX_VIEWERS=24

# Longer than the container's camera idle timeout, so a page reload does not
# make ffmpeg stop and start again at the other end.
POSPRINTWEB_RELAY_IDLE_TIMEOUT=30
EOF
  chmod 600 "$ENV_FILE"
  echo "    edit POSPRINTWEB_RELAY_UPSTREAM before this is useful"
else
  echo "==> keeping existing $ENV_FILE"
fi

echo "==> systemd"
cp "$SRC/deploy/posprintweb-relay.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now posprintweb-relay
systemctl restart posprintweb-relay

sleep 2
systemctl --no-pager --lines=5 status posprintweb-relay || true

cat <<EOF

Done. Point Caddy's camera routes at 127.0.0.1:8001 and reload it:

    handle /api/camera.mjpg {
        reverse_proxy 127.0.0.1:8001 {
            flush_interval -1
        }
    }
    handle /api/camera.jpg {
        reverse_proxy 127.0.0.1:8001
    }

Everything else, /api/status included, stays pointed at the container: it is
the authority on whether the feed may be shown at all.

Check it with:

    curl -s localhost:8001/healthz

The number that matters is not there but upstream. With several people
watching, the container's own viewer count should still read 1:

    curl -s -H 'X-Admin-Key: <key>' http://<container>:8000/admin/log?limit=1
EOF
