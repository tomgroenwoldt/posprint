# posprint-web

A public web page that lets anyone on the internet print a short message on a
real thermal receipt printer in your home.

It is the internet-facing half of [posprint](../README.md), the LAN-only
service at the root of this repo that actually drives the USB printer. The two
live together but deploy separately: different containers, different systemd
units, different dependencies.

```
  browser  ──(no credentials)──▶  posprint-web  ──(X-API-Key)──▶  posprint  ──▶  USB
   public                            web/                        repo root     /dev/usb/lp0
```

## Why a proxy and not a static page

The obvious version of this — a static page that calls the printer API directly
from JavaScript — cannot be made safe. The page would have to ship the posprint
API key to every visitor, and that key is not "permission to print a message".
It unlocks the whole posprint API, including:

- `POST /print/raw`, arbitrary ESC/POS bytes: change the codepage, feed the
  entire roll onto the floor, reprogram the printer's NV memory
- `POST /drawer`, which physically opens the cash drawer

So the key lives here, server-side, and the public surface is deliberately
tiny: **one message, one optional name**. The receipt layout is assembled on
the server from validated fields. Client-supplied blocks are never forwarded,
which is what keeps `raw` and `drawer` unreachable no matter what a visitor
posts.

## Threat model

The thing that makes this different from a normal web form is that every
request consumes a **physical, finite, non-refundable** resource in your home,
and produces noise at the other end. The controls below are not decoration;
removing any one of them leaves a hole worth caring about.

| Risk | Control |
| --- | --- |
| Someone loops the endpoint and burns a whole roll | Per-IP cooldown, per-IP daily cap, and a **global** daily cap. The global one is the real backstop — the first two are per-IP and IPs are cheap. |
| ESC/POS command injection through the message body | All C0/C1 control bytes are stripped before the text goes upstream. `0x1B` is the ESC byte; letting one through would hand a visitor the command set. See `test_escape_byte_is_stripped`. |
| Blocklist evasion with invisible characters | Zero-width and bidi-override characters are stripped; matching folds accents and strips separators, so `b-à-d` still matches `bad`. |
| Character floods (`AAAA…` ×5000) | Rejected before the length check can be gamed; runs of 200 blank lines collapse too. |
| A whole print spent on `??????` | The printer has one 8-bit code page. Text it cannot render — Korean, Chinese, Cyrillic, emoji — is refused with the offending characters named, and the live preview shows what the paper will actually say rather than what the browser can display. Accents and smart quotes are *not* refused: posprint degrades those to `e` and `"`, which still reads. |
| A picture costing far more roll than a message | Braille art prints as a bitmap (see below), so it gets its own limits — a grid and a height in dots, not a character count. `POSPRINTWEB_BRAILLE_MAX_DOTS` caps the paper one picture may spend; `POSPRINTWEB_BRAILLE=false` disables the feature outright. |
| Printer chattering at 03:00 | Quiet hours, local to your timezone, wrapping midnight correctly. |
| Rate limits bypassed by forging `X-Forwarded-For` | Ignored unless `POSPRINTWEB_TRUST_PROXY=true`, which you set **only** once a trusted proxy is actually in front. |
| It all goes wrong at once | `touch /etc/posprintweb.disabled` stops printing immediately, no restart. |
| Abuse you need to trace afterwards | Every attempt is logged to SQLite with timestamp, IP and body, readable at `GET /admin/log`. |

What none of this solves: **someone will eventually print something horrible.**
A wordlist is a speed bump, not a filter. The controls that actually matter
when that happens are the killswitch and the log. Decide in advance that you
are fine with this before you publish the URL, and say so on the page — the
default footer text already does.

## Running it locally

From the repo root:

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r web/requirements.txt pytest
python web/scripts/dev.py --fake                 # http://127.0.0.1:8000
```

`--fake` stands in for the printer and dumps receipts to stdout, so you can work
on the page without hardware. Drop it to talk to a real posprint on `:8080`.

```bash
pytest -q      # 152 tests: both services
pytest web -q  # 93: just this one
```

No `PYTHONPATH` needed — the root `pyproject.toml` puts both package roots on
the path.

## Deploying it

The recommended layout is a **second** LXC container, separate from the one
running posprint. This is the process exposed to the internet; keeping it off
the box with the printer attached means a compromise here does not immediately
land the attacker on the printer host. Running both in CT 110 also works and is
one less thing to manage — set `POSPRINTWEB_UPSTREAM=http://127.0.0.1:8080`.

On the Proxmox host, if you want a separate container:

```bash
pveam update
pveam available --section system | grep -E 'debian-1[23]'
pct create 111 local:vztmpl/<template-from-that-list> \
  --hostname posprint-web --cores 1 --memory 512 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=0 \
  --storage local-lvm --rootfs local-lvm:4 --start 1
```

No device passthrough and no udev rules are needed here — this container never
touches the printer.

Inside the container:

```bash
apt-get update && apt-get install -y git      # PVE templates ship without it
git clone https://github.com/tomgroenwoldt/posprint.git /root/posprint
bash /root/posprint/web/deploy/install.sh
```

The clone carries both services; `web/deploy/install.sh` only installs this one,
so nothing printer-related is set up in this container.

Then wire it to the printer. Get the key from the *posprint* container:

```bash
grep POSPRINT_API_KEY /etc/posprint.env          # on CT 110
```

and put it in this container's `/etc/posprintweb.env` as
`POSPRINTWEB_UPSTREAM_KEY=`, along with `POSPRINTWEB_UPSTREAM=http://<CT-110-IP>:8080`.

```bash
systemctl restart posprintweb
curl -s http://127.0.0.1:8000/api/status
```

Re-running `install.sh` after a `git pull` upgrades in place and keeps your
config, database and admin key.

## Exposing it

The service binds to `127.0.0.1` on purpose. **Do not port-forward it.** It
speaks plain HTTP, so a forwarded port publishes an unencrypted page and leaves
your home IP address in every visitor's history.

Put something in front that terminates TLS. Two setups that work:

**Cloudflare Tunnel**, if the zone is on Cloudflare's nameservers. No inbound
firewall rule, home IP never in DNS, and you get rate limiting and country
blocking for free. Note this genuinely requires the zone to be *on* Cloudflare:
the tunnel hostname CNAMEs to `<uuid>.cfargotunnel.com`, which only resolves
inside Cloudflare's DNS, so a third-party provider cannot point at it. NS-
delegating a single subdomain is an Enterprise-only feature.

```bash
cloudflared tunnel login
cloudflared tunnel create posprint
cloudflared tunnel route dns posprint print.example.com
cloudflared tunnel run --url http://127.0.0.1:8000 posprint
```

Then set `POSPRINTWEB_CLIENT_IP_HEADER=cf-connecting-ip`.

**A small VPS running Caddy**, if DNS lives elsewhere. An `A` record points at
the VPS, Caddy gets a Let's Encrypt cert, and a WireGuard or Tailscale link
carries traffic back to the container. Your home IP stays out of DNS and no
port is opened at home.

```caddy
print.example.com {
    reverse_proxy <container-vpn-ip>:8000 {
        header_up X-Forwarded-For {remote_host}
        header_up -CF-Connecting-IP
    }
}
```

Both `header_up` lines are load-bearing. `reverse_proxy` *appends* to any
inbound `X-Forwarded-For` by default, so without the first line a visitor sends
their own and it lands leftmost — exactly the value the rate limiter reads. The
second deletes a header Caddy has no reason to set, so a visitor cannot supply
it and have the app trust it.

Once something is in front, set `POSPRINTWEB_TRUST_PROXY=true` and restart, so
rate limiting keys on the real visitor address rather than seeing every request
as coming from the proxy.

Getting that order wrong is the one mistake with teeth:

- Trust on, no proxy in front → anyone sends `X-Forwarded-For: <random>` and
  has unlimited prints.
- Trust off, proxy in front → every visitor shares one bucket, so the first
  person to print locks out everyone else for the cooldown.

Tailscale Funnel works too and is less setup, but gives you no request-level
controls of its own.

## Live camera

A camera pointed at the printer, streamed on the page. Same rule as the API
key: the browser never learns where the camera is. It asks this service for
`/api/camera.mjpg`; this service holds the RTSP credentials and streams JPEGs
back. Reading the page source tells a visitor nothing they could point VLC at.

**This is a privacy decision, not a feature flag.** With `CAMERA_MODE=always`
there is a live view of a room in your home on a URL strangers already have.
Point the camera at the printer and as little else as possible.

Two things keep the cost bounded:

- **ffmpeg only runs while someone is watching.** A request starts it; an idle
  timeout stops it. Most of the time nobody is looking and nothing is reading
  from the camera at all.
- **One decode feeds everyone.** Frames are shared, so ten viewers cost the
  camera and your uplink the same as one. What scales per viewer is bytes
  leaving the VPS, which is what `CAMERA_MAX_VIEWERS` caps.

That cap matters more than it looks. At 640×360, 15fps and `-q:v 6` a viewer is
roughly 3 Mbit/s, and a domestic upload link runs out long before a Hetzner
CAX11 does. Six viewers is about 18 Mbit/s leaving the flat. Lower
`CAMERA_FPS`, `CAMERA_WIDTH` or `CAMERA_QUALITY` if that hurts.

### Tapo TC70

Enable **Advanced Settings → Camera Account** in the Tapo app first; that
username and password are separate from your TP-Link login. Then:

```
rtsp://<user>:<pass>@<camera-ip>:554/stream2    # 360p, easy on the uplink
rtsp://<user>:<pass>@<camera-ip>:554/stream1    # 1080p
```

`stream2` is the right choice here — you are watching a strip of paper appear,
not reading it.

```bash
# cut the picture immediately, without stopping printing
touch /etc/posprintweb-camera.disabled
```

`ffmpeg` is required and `install.sh` installs it. Nothing else on the page
depends on it, so a machine without it still prints; the feed just stays dark.

## Braille art

`U+2800`–`U+28FF` has no glyph in any ESC/POS code page, so braille art sent as
text prints as question marks — the charset filter refuses it alongside Korean
and emoji.

But braille art is not really text. Each character encodes a 2×4 grid of dots,
so a W×H grid of them *is* a 2W×4H bitmap in disguise. A message containing
braille is decoded back into that bitmap, scaled by a whole number of pixels,
and sent to posprint as an `image` block. Nothing is dithered or approximated:
the picture that comes out is exactly the one that went in.

Three consequences worth knowing:

- **Art must be on its own.** A caption cannot be drawn as dots, so a message
  mixing braille with ordinary text is refused rather than half-rendered.
  Spaces are fine as padding, as is `U+2800`, the blank cell.
- **`max_chars` does not apply.** It measures the wrong thing once the message
  is a picture: 500 cells might be 72 wide and 7 tall or 8 wide and 62, and
  those cost very different amounts of roll. The grid and dot limits apply
  instead.
- **Scaling is integer-only.** Stretching to fill the head exactly would make
  some dots four pixels across and others five, which on a 1-bit image reads as
  a texture crawling through the picture.

The same decoder is available as a command-line tool for art too big for the
public limits: `scripts/braille_print.py`, which talks to posprint directly.

## Configuration

All settings are environment variables, read once at startup from
`/etc/posprintweb.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_UPSTREAM` | `http://127.0.0.1:8080` | Base URL of the posprint service |
| `POSPRINTWEB_UPSTREAM_KEY` | *(empty)* | posprint's `POSPRINT_API_KEY`. Required in practice |
| `POSPRINTWEB_UPSTREAM_TIMEOUT` | `30` | Seconds to wait on posprint |
| `POSPRINTWEB_HOST` / `_PORT` | `0.0.0.0` / `8000` | `install.sh` writes `127.0.0.1`; keep it there and tunnel in |
| `POSPRINTWEB_TITLE` / `_BLURB` | see `config.py` | Page heading and intro text |
| `POSPRINTWEB_COLUMNS` | `48` | Paper width in characters. 32 for 58mm paper |
| `POSPRINTWEB_CODEPAGE` | `cp858` | **Must match posprint's `POSPRINT_CODEPAGE`.** Decides which characters are refused instead of printed as `?` |
| `POSPRINTWEB_CAMERA_URL` | *(empty)* | RTSP URL of a camera on the printer. Empty disables the feed. Holds credentials; never reaches a browser |
| `POSPRINTWEB_CAMERA_MODE` | `always` | `always`, `after_print`, or `off`. **A privacy setting** — read "Live camera" above |
| `POSPRINTWEB_CAMERA_WINDOW` | `90` | Seconds the feed stays live after a print, in `after_print` mode |
| `POSPRINTWEB_CAMERA_FPS` | `0` | `0` uses the camera's own rate. Lower it if your uplink suffers |
| `POSPRINTWEB_CAMERA_WIDTH` | `0` | `0` means no rescaling |
| `POSPRINTWEB_CAMERA_QUALITY` | `6` | ffmpeg `-q:v`; 2 best, 31 worst |
| `POSPRINTWEB_CAMERA_MAX_VIEWERS` | `6` | Concurrent streams. Caps bandwidth leaving the flat |
| `POSPRINTWEB_CAMERA_IDLE` | `15` | Seconds with no viewer before ffmpeg is stopped |
| `POSPRINTWEB_CAMERA_KILLSWITCH` | `/etc/posprintweb-camera.disabled` | Cuts the picture without stopping printing |
| `POSPRINTWEB_BRAILLE` | `true` | Accept braille art and print it as a decoded bitmap |
| `POSPRINTWEB_BRAILLE_MAX_COLS` | `72` | Art width in cells. 72 cells = 144 dots, so scale 4 fills an 80mm head |
| `POSPRINTWEB_BRAILLE_MAX_ROWS` | `40` | Art height in cells |
| `POSPRINTWEB_BRAILLE_MAX_SCALE` | `8` | Stops a tiny drawing being blown up to fill the roll |
| `POSPRINTWEB_BRAILLE_MAX_DOTS` | `640` | Paper budget for one picture, ~80mm at 203dpi |
| `POSPRINTWEB_PRINTER_DOTS` | `576` | Match posprint's `POSPRINT_DOTS`; 384 for 58mm paper |
| `POSPRINTWEB_COOLDOWN_SECONDS` | `60` | Minimum gap between prints from one IP |
| `POSPRINTWEB_PER_IP_DAILY` | `5` | Per-IP daily cap. `0` disables |
| `POSPRINTWEB_GLOBAL_DAILY` | `200` | Paper budget for everyone combined. `0` disables |
| `POSPRINTWEB_MAX_CHARS` | `500` | ~8cm of 80mm paper |
| `POSPRINTWEB_MAX_LINES` | `20` | |
| `POSPRINTWEB_MAX_NAME_CHARS` | `32` | Length of the optional sender name |
| `POSPRINTWEB_QUIET_START` / `_END` | `22` / `8` | Local hours. Set both equal to disable |
| `POSPRINTWEB_TZ` | `Europe/Berlin` | Timezone for quiet hours and daily rollover |
| `POSPRINTWEB_BLOCKLIST` | *(empty)* | Path to a newline-separated wordlist |
| `POSPRINTWEB_TRUST_PROXY` | `false` | Trust the forwarding header for the client address. Read "Exposing it" first |
| `POSPRINTWEB_CLIENT_IP_HEADER` | `x-forwarded-for` | The one header trusted when the above is on. `cf-connecting-ip` behind Cloudflare |
| `POSPRINTWEB_ADMIN_KEYS` | *(empty)* | Comma-separated. Bypass all limits, unlock `/admin/log` |
| `POSPRINTWEB_KILLSWITCH` | `/etc/posprintweb.disabled` | Printing stops while this file exists |
| `POSPRINTWEB_ENABLED` | `true` | Permanent off switch |
| `POSPRINTWEB_DB` | `/var/lib/posprintweb/prints.db` | Quota ledger and audit log |

## API

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /` | — | The page |
| `GET /api/status` | — | Limits, printer state, your remaining quota |
| `POST /api/print` | — | `{"message": "...", "name": "..."}` |
| `GET /admin/log` | `X-Admin-Key` | Recent prints with IP and body. 404s without the key |
| `GET /healthz` | — | For the tunnel's health check |

`POST /api/print` returns `200` printed, `422` rejected input, `429` rate
limited (with `Retry-After`), `502` the printer failed, `503` switched off or
quiet hours. A `502` refunds the quota — an empty roll is not the visitor's
fault.

`GET /api/status` carries everything the page needs to render itself honestly:

| Field | Why the page needs it |
| --- | --- |
| `printer_state` | `ready` / `out_of_paper` / `offline`. `online` is kept for anything already reading it, but says only *whether*, not *which* |
| `charset.printable` | Every character the code page can express, derived from the codec. The preview shows what the paper will say rather than what the browser can display |
| `charset.replacements` | The degradations posprint applies — `—` → `-`, `…` → `...` |
| `braille` | Grid and scale limits, so the page can estimate paper cost and knows not to refuse braille as unprintable |
| `limits`, `you`, `printed_today` | Caps, remaining quota, global count |

## Operating it

```bash
# stop printing right now, no restart, no deploy
touch /etc/posprintweb.disabled
rm /etc/posprintweb.disabled          # and back on

# who printed what
curl -s -H "X-Admin-Key: $KEY" http://127.0.0.1:8000/admin/log | jq '.prints[]'

# print something yourself, ignoring cooldown and quiet hours
curl -X POST http://127.0.0.1:8000/api/print \
  -H "X-Admin-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"message":"testing","name":"me"}'

journalctl -u posprintweb -f
```

## Troubleshooting

**Page says "the printer is misconfigured".** posprint returned 401:
`POSPRINTWEB_UPSTREAM_KEY` does not match `POSPRINT_API_KEY`. The detail is in
`journalctl -u posprintweb`, not on the page — visitors should not be told
which of your secrets is wrong.

**Page says "the printer is out of paper".** It is. `GET /api/status` reports
`printer_state: out_of_paper`, which posprint derives from the printer's status
byte. Change the roll; no restart needed.

**Page says "the printer is offline".** posprint cannot see a device node at
all. Check it directly: `curl -s http://<CT-110-IP>:8080/health`. A `state` of
`offline` with `device_present: false` means the USB node is missing — see
posprint's README.

**The page shows an old message after a deploy.** It should not: `/static` is
served `no-cache` so browsers revalidate. If it persists, you are looking at a
cached page from before that fix — hard-reload once.

**Everyone shares one rate limit.** A proxy is in front but
`POSPRINTWEB_TRUST_PROXY` is still `false`.

**`status=226/NAMESPACE` on start.** Unprivileged LXC. `install.sh` should have
dropped in `10-container.conf`; re-run it. Do not "fix" this by enabling
`nesting=1`.

**Accented characters look wrong on paper.** That is posprint's codepage
(`POSPRINT_CODEPAGE`), not this service. Characters the codepage cannot express
are transliterated rather than dropped.
