# posprint-web

A public web page that lets anyone on the internet print a short message on a
real thermal receipt printer in your home.

It is the internet-facing half of [posprint](../README.md), the LAN-only
service at the root of this repo that drives the USB printer. The two live in
one repository and deploy to different machines, with different systemd units
and different dependencies.

```
  browser  ──(no credentials)──▶  posprint-web  ──(X-API-Key)──▶  posprint  ──▶  USB
   public                            web/                        repo root     /dev/usb/lp0
```

**New here and just want it running?** [Deployment](#deployment) is the exact
sequence. **Something is wrong right now?** [Operating it](#operating-it) and
[Limits, and how to reset them](#limits-and-how-to-reset-them) are the two
sections you want, and neither assumes you have read the rest.

---

## Contents

- [The three machines](#the-three-machines)
- [Why a proxy and not a static page](#why-a-proxy-and-not-a-static-page)
- [Threat model](#threat-model)
- [Deployment](#deployment) — exact, machine by machine
- [Configuration](#configuration) — every setting, and how to verify what is actually running
- [Operating it](#operating-it) — start, stop, deploy, switch off
- [Limits, and how to reset them](#limits-and-how-to-reset-them)
- [Gallery](#gallery)
- [The quiet filter](#the-quiet-filter)
- [Limits that ignore the sender's address](#limits-that-ignore-the-senders-address)
- [Running this where the attacker can read it](#running-this-where-the-attacker-can-read-it)
- [Siege mode](#siege-mode)
- [Proof of work](#proof-of-work)
- [Live camera](#live-camera)
- [Braille art](#braille-art)
- [API](#api)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## The three machines

A full deployment is three hosts. Only the first two are required; the relay is
a bandwidth optimisation you can add later.

```
                      the internet
                            │
                            ▼
 ┌─ VPS ────────────────────────────────────────┐
 │ Caddy :443                                   │
 │ posprintweb-relay :8001   (camera fan-out)   │
 └──────────────────────────┬───────────────────┘
                            │  Tailscale / WireGuard
                            ▼
 ┌─ your flat ──────────────────────────────────┐
 │ CT 111   posprint-web :8000   ◀── camera     │
 │    │                              (RTSP)     │
 │    │  X-API-Key                              │
 │    ▼                                         │
 │ CT 110   posprint :8080  ──▶  USB printer    │
 └──────────────────────────────────────────────┘
```

**The container IDs are examples.** This README uses **CT 110** for the printer
and **CT 111** for the web front end throughout, so the commands can be pasted
as they stand. Nothing depends on those numbers — substitute your own, or set
them once per shell and paste anyway:

```bash
PRINTER=110      # the container running posprint
WEB=111          # the container running posprint-web
```

Then `pct exec $WEB -- ...` wherever a command below says `pct exec 111 -- ...`.

| Machine | Runs | Unit | Code | Config |
| --- | --- | --- | --- | --- |
| **CT 110** | posprint | `posprint` | `/opt/posprint` | `/etc/posprint.env` |
| **CT 111** | posprint-web | `posprintweb` | `/opt/posprint-web` | `/etc/posprintweb.env` |
| **VPS** | Caddy + camera relay | `posprintweb-relay` | `/opt/posprint-relay` | `/etc/posprintweb-relay.env` |

On every machine the git checkout lives at **`/root/posprint`** and is separate
from the installed code. The installers *copy* out of the checkout; they do not
run from it. That distinction matters every time you upgrade — see
[Deploying a change](#deploying-a-change).

Running posprint-web in CT 110 alongside the printer also works and is one less
thing to manage — set `POSPRINTWEB_UPSTREAM=http://127.0.0.1:8080`. The split
exists because CT 111 is the process exposed to the internet, and a compromise
there should not immediately land on the box with the printer attached.

---

## Why a proxy and not a static page

The obvious version of this — a static page that calls the printer API directly
from JavaScript — cannot be made safe. The page would have to ship the posprint
API key to every visitor, and that key is not "permission to print a message".
It unlocks the whole posprint API, including:

- `POST /print/raw`, arbitrary ESC/POS bytes: change the codepage, feed the
  entire roll onto the floor, reprogram the printer's NV memory
- `POST /drawer`, which physically opens the cash drawer

So the key lives here, server-side, and the public surface is deliberately
tiny: **one message, one name**. The receipt layout is assembled on the server
from validated fields. Client-supplied blocks are never forwarded, which is
what keeps `raw` and `drawer` unreachable no matter what a visitor posts.

## Threat model

The thing that makes this different from a normal web form is that every
request consumes a **physical, finite, non-refundable** resource in your home,
and produces noise at the other end.

| Risk | Control |
| --- | --- |
| The printer API key leaking | Never leaves the server; the browser gets no credential at all |
| Arbitrary ESC/POS | Only `message` and `name` cross the wire; the receipt is assembled server-side |
| One person printing all night | `COOLDOWN_SECONDS`, `PER_IP_DAILY` |
| Everyone printing all night | `GLOBAL_DAILY`, `GLOBAL_HOURLY`, `GLOBAL_BURST` |
| A rented proxy pool | `GLOBAL_BURST`, proof of work, siege mode |
| The same thing sent repeatedly | `REPEAT_HOURS`, matched on a folded fingerprint |
| Slurs and abuse | `BLOCKLIST` (refuses loudly), `SHADOWLIST` (refuses silently) |
| Unprintable characters | Charset filter derived from the code page |
| Enormous braille pictures | Grid, dot and ink limits |
| Noise at 3am | Quiet hours |
| Anything unforeseen | The killswitch, and siege mode holding prints for approval |

Nothing here is decoration. Removing any one of them leaves a hole worth
caring about, and most of them exist because something actually happened.

---

## Deployment

Three parts. Do them in order — CT 111 is useless without CT 110's API key, and
the relay is useless without CT 111.

### Prerequisites

- Proxmox host with CT 110 already running posprint. If not, do
  [the root README's install](../README.md#install) first.
- A domain, and a VPS or Cloudflare account to terminate TLS. **Do not
  port-forward this service.** It speaks plain HTTP, so a forwarded port
  publishes an unencrypted page and puts your home IP in every visitor's
  history.

### 1. Create CT 111 (on the Proxmox host)

```bash
pveam update
pveam available --section system | grep -E 'debian-1[23]'
```

```bash
pct create 111 local:vztmpl/<template-from-that-list> \
  --hostname posprint-web --cores 1 --memory 512 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=0 \
  --storage local-lvm --rootfs local-lvm:4 --start 1
```

No device passthrough and no udev rules here — this container never touches the
printer.

### 2. Install the service (inside CT 111)

```bash
pct exec 111 -- bash -c "apt-get update -qq && apt-get install -y -qq git && \
  git clone https://github.com/tomgroenwoldt/posprint.git /root/posprint && \
  bash /root/posprint/web/deploy/install.sh"
```

The installer creates `/opt/posprint-web` with a virtualenv, a `posprintweb`
system user, `/etc/posprintweb.env` with a freshly generated admin key, and a
systemd unit. It also installs ffmpeg, which only the camera needs — if that
fails you get a warning rather than a stop, and everything except the feed
works.

It prints the admin key when it finishes. **Write it down**; it is the only
thing that gets you into `/admin`.

### 3. Wire it to the printer

Get the key from CT 110:

```bash
pct exec 110 -- grep POSPRINT_API_KEY /etc/posprint.env
```

Put it in CT 111 along with the printer's address:

```bash
pct exec 111 -- sh -c 'cat >> /etc/posprintweb.env <<EOF
POSPRINTWEB_UPSTREAM=http://<CT-110-IP>:8080
POSPRINTWEB_UPSTREAM_KEY=<the key from above>
EOF
systemctl restart posprintweb'
```

Confirm:

```bash
pct exec 111 -- curl -s localhost:8000/api/status
```

`printer_state` should read `ready`. `offline` or `misconfigured` means the two
lines above are wrong — check `journalctl -u posprintweb -n 20`.

### 4. Put TLS in front

The service binds to `0.0.0.0` so a proxy on another machine can reach it. That
is only safe because the container is not routable from the internet; nothing
here should ever be reachable directly.

**A VPS running Caddy**, which is the setup this is written against. An `A`
record points at the VPS, Caddy gets a Let's Encrypt cert, and Tailscale or
WireGuard carries traffic back to the container:

```caddy
print.example.com {
    handle /api/camera.mjpg {
        reverse_proxy 127.0.0.1:8001 {
            flush_interval -1
        }
    }
    handle /api/camera.jpg {
        reverse_proxy 127.0.0.1:8001
    }
    handle {
        reverse_proxy <ct111-tailscale-ip>:8000 {
            header_up X-Forwarded-For {remote_host}
            header_up -CF-Connecting-IP
        }
    }
}
```

The two camera routes are only needed once you have installed the relay in step
5; without it, delete them and let everything go to the container.

**Cloudflare Tunnel** works too, if the zone is on Cloudflare's nameservers. No
inbound firewall rule and no VPS. It genuinely requires the zone to be *on*
Cloudflare — the tunnel hostname CNAMEs to `<uuid>.cfargotunnel.com`, which
only resolves inside Cloudflare's DNS.

```bash
cloudflared tunnel login
cloudflared tunnel create posprint
cloudflared tunnel route dns posprint print.example.com
cloudflared tunnel run --url http://127.0.0.1:8000 posprint
```

Then set `POSPRINTWEB_CLIENT_IP_HEADER=cf-connecting-ip`.

Either way, finish by turning on proxy trust:

```bash
pct exec 111 -- sh -c 'echo POSPRINTWEB_TRUST_PROXY=true >> /etc/posprintweb.env && systemctl restart posprintweb'
```

**Getting that order wrong is the one mistake with teeth:**

- Trust on, no proxy in front → anyone sends `X-Forwarded-For: <random>` and
  has unlimited prints.
- Trust off, proxy in front → every visitor shares one bucket, so the first
  person to print locks out everyone else for the cooldown.

<details>
<summary>Why the forwarding header is read from the right</summary>

`reverse_proxy` *appends* to any inbound `X-Forwarded-For`, so a visitor's own
value lands leftmost. Each proxy appends the peer it saw, which means the last
entry is the one your proxy wrote and everything before it is whatever the
sender chose to claim. `POSPRINTWEB_PROXY_HOPS` says how many proxies of your
own are in front — 1 for Caddy alone, 2 if Cloudflare is in front of that — and
the client is that many entries from the end. A header shorter than the chain
claims falls back to the socket peer.

Over TCP the socket peer cannot be forged: a handshake cannot complete without
receiving the SYN-ACK, which goes to the real owner of the address. So the only
lie available is the header, and reading it from the right is the whole
defence. A challenge-response step — fetch a key bound to your address, then
spend it — adds nothing, because the key travels back over the sender's own
connection whatever address they claim. TCP has already done that round trip.

</details>

### 5. The camera relay (on the VPS, optional)

Only worth doing if you have the camera enabled and more than a couple of
people watching. See [Fanning it out from the VPS](#fanning-it-out-from-the-vps)
for why.

```bash
apt-get install -y git
git clone https://github.com/tomgroenwoldt/posprint.git /root/posprint
bash /root/posprint/web/deploy/relay-install.sh
```

Then point it at the container and restart:

```bash
sed -i 's|^POSPRINTWEB_RELAY_UPSTREAM=.*|POSPRINTWEB_RELAY_UPSTREAM=http://<ct111-tailscale-ip>:8000/api/camera.mjpg|' \
  /etc/posprintweb-relay.env
systemctl restart posprintweb-relay
curl -s localhost:8001/healthz
```

`upstream_live: true` means it can see the container's feed. Add the two camera
`handle` blocks to the Caddyfile from step 4, `systemctl reload caddy`, and drop
the container's own viewer cap to 2 — the relay is its only viewer, plus a slot
so a reconnect can overlap a dying connection:

```bash
pct exec 111 -- sh -c 'echo POSPRINTWEB_CAMERA_MAX_VIEWERS=2 >> /etc/posprintweb.env && systemctl restart posprintweb'
```

### 6. Harden the defaults

The repository is public. Do these before you publish the URL — the reasoning
is in [Running this where the attacker can read it](#running-this-where-the-attacker-can-read-it).

```bash
pct exec 111 -- sh -c "cat >> /etc/posprintweb.env <<EOF
POSPRINTWEB_POW_SECRET=$(openssl rand -hex 32)
POSPRINTWEB_CAPTCHA_SECRET=$(openssl rand -hex 32)
EOF
systemctl restart posprintweb"
```

Then change the rate-limit numbers so they are not the published defaults, and
point `POSPRINTWEB_SHADOWLIST` at a wordlist that lives outside the repository.

### Deploying a change

**`/opt/posprint-web` is not a git checkout.** The installer copies code into
it. Pulling there fails, and pulling in the checkout alone changes nothing that
is running. The upgrade is always *pull in the checkout, then re-run the
installer*:

```bash
# CT 111
pct exec 111 -- sh -c 'git -C /root/posprint pull && bash /root/posprint/web/deploy/install.sh'

# CT 110
pct exec 110 -- sh -c 'git -C /root/posprint pull && bash /root/posprint/deploy/install.sh'

# VPS
sh -c 'git -C /root/posprint pull && bash /root/posprint/web/deploy/relay-install.sh'
```

Both installers are idempotent: they replace the code, keep `/etc/*.env`, keep
the database and the admin key, and restart the unit. Re-running one when
nothing has changed is harmless and is the standard fix for a unit file that has
drifted.

Front-end changes reach browsers immediately. Each HTML page is served
`no-store` and every `/static` URL carries a build stamp, so a new deploy is by
definition not in anyone's cache.

---

## Configuration

Every setting is an environment variable, read **once at startup** from
`/etc/posprintweb.env`. Nothing is re-read while running; every change needs
`systemctl restart posprintweb`.

### Two traps worth knowing before you edit it

**Later assignments win.** systemd's `EnvironmentFile` applies lines in order,
so appending `POSPRINTWEB_PER_IP_DAILY=0` to a file that already sets it to 5
works — the last one is what the process gets. This is convenient and it is also
how the file becomes an unreadable pile of duplicates. Tidy it occasionally.

**The file is not the truth; the process is.** After a few appends, the only
reliable way to see what is actually running:

```bash
pct exec 111 -- sh -c 'tr "\0" "\n" < /proc/$(pgrep -f posprintweb | head -1)/environ | grep POSPRINTWEB | sort'
```

Use that whenever a setting "isn't taking". It has settled several arguments
that `grep` on the env file got wrong.

### Upstream and network

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_UPSTREAM` | `http://127.0.0.1:8080` | Base URL of the posprint service |
| `POSPRINTWEB_UPSTREAM_KEY` | *(empty)* | posprint's `POSPRINT_API_KEY`. Required in practice |
| `POSPRINTWEB_UPSTREAM_TIMEOUT` | `30` | Seconds to wait on posprint |
| `POSPRINTWEB_HOST` / `_PORT` | `0.0.0.0` / `8000` | `0.0.0.0` is required when the proxy is on another machine |
| `POSPRINTWEB_TRUST_PROXY` | `false` | Trust the forwarding header for the client address. Read [step 4](#4-put-tls-in-front) first |
| `POSPRINTWEB_PROXY_HOPS` | `1` | How many proxies of your own are in front. Too high fails safe |
| `POSPRINTWEB_CLIENT_IP_HEADER` | `x-forwarded-for` | Must name a header your proxy **overwrites**. `cf-connecting-ip` behind Cloudflare |

### Page and paper

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_TITLE` / `_BLURB` | see `config.py` | Page heading and intro text |
| `POSPRINTWEB_COLUMNS` | `48` | Paper width in characters. 32 for 58mm paper |
| `POSPRINTWEB_CODEPAGE` | `cp858` | **Must match posprint's `POSPRINT_CODEPAGE`.** Decides which characters are refused rather than printed as `?` |
| `POSPRINTWEB_PRINTER_DOTS` | `576` | Match posprint's `POSPRINT_DOTS`; 384 for 58mm |
| `POSPRINTWEB_MAX_CHARS` | `500` | ~8cm of 80mm paper |
| `POSPRINTWEB_MAX_LINES` | `20` | |
| `POSPRINTWEB_MAX_NAME_CHARS` | `32` | The name is **required**, not optional — every receipt carries a from-line |
| `POSPRINTWEB_TZ` | `Europe/Berlin` | Timezone for quiet hours and the daily rollover |

### Rate limits

Every one of these disables at `0`. See
[Limits, and how to reset them](#limits-and-how-to-reset-them).

| Variable | Default | Scope |
| --- | --- | --- |
| `POSPRINTWEB_COOLDOWN_SECONDS` | `60` | Minimum gap between prints from one address |
| `POSPRINTWEB_PER_IP_DAILY` | `5` | Per-address daily cap |
| `POSPRINTWEB_GLOBAL_DAILY` | `200` | Paper budget for everyone combined |
| `POSPRINTWEB_GLOBAL_HOURLY` | `30` | Hourly cap across everyone |
| `POSPRINTWEB_GLOBAL_BURST` | `8` | Per-minute cap across everyone — the one that answers a proxy-pool flood |
| `POSPRINTWEB_GLOBAL_BURST_SECONDS` | `60` | The burst window |
| `POSPRINTWEB_REPEAT_HOURS` | `24` | Refuse content already printed in this window, whatever the address |
| `POSPRINTWEB_QUIET_START` / `_END` | `22` / `8` | Local hours. **Set both equal to disable** |

### Anti-abuse

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_POW_BITS` | `18` | Proof-of-work difficulty in leading zero bits. Each bit doubles the sender's cost. `0` disables |
| `POSPRINTWEB_POW_TTL_SECONDS` | `300` | How long a challenge stays solvable |
| `POSPRINTWEB_POW_SECRET` | *(random per process)* | HMAC key. **Set it** — otherwise every restart invalidates outstanding challenges |
| `POSPRINTWEB_CAPTCHA` | `true` | The visual puzzle offered as a fast lane during a siege |
| `POSPRINTWEB_CAPTCHA_SECRET` | *(random per process)* | As above. **Set it** |
| `POSPRINTWEB_HOLD_THRESHOLD` | `20` | Refusals within the window that start a siege. `0` disables |
| `POSPRINTWEB_HOLD_WINDOW_SECONDS` | `300` | How far back refusals are counted |
| `POSPRINTWEB_HOLD_FOR_SECONDS` | `1800` | How long a siege lasts, refreshed while it continues |
| `POSPRINTWEB_HOLD_VOLUME` | `60` | Prints in the volume window that also start a siege. Catches a sender who paces politely under every limit. `0` disables |
| `POSPRINTWEB_HOLD_VOLUME_SECONDS` | `3600` | The volume window |
| `POSPRINTWEB_HOLD_MAX_QUEUE` | `200` | Ceiling on the hold queue |
| `POSPRINTWEB_BLOCKLIST` | *(empty)* | Path to a wordlist. Refuses the message **and says so** |
| `POSPRINTWEB_SHADOWLIST` | *(empty)* | Path to a wordlist. Accepts, charges, logs, never prints |
| `POSPRINTWEB_SHADOW_DELAY_MS` | `900` | Makes a swallowed message take as long as a real print |

### Auction

An optional page at `/auction` describing something you are selling, with a nav
entry next to Gallery. **Empty `AUCTION_URL` switches the whole thing off** —
no nav link on any page, and `/auction` returns 404 — so this costs nothing on
a deployment with nothing for sale.

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_AUCTION_URL` | *(empty)* | The listing. Empty disables the feature entirely. Refused at startup unless `http://`, `https://` or a `/path` |
| `POSPRINTWEB_AUCTION_LABEL` | `Auction` | The nav entry's text |
| `POSPRINTWEB_AUCTION_NOTE` | *(empty)* | One line under the heading — "Ends Sunday 20:00", "Sold". Shown in amber |

The item's photographs and copy live in `static/auction.html` and
`static/auction/`, because they describe one specific object; only the link and
the status line are configuration.

**The listing itself cannot be embedded.** eBay serves item pages with
`X-Frame-Options: SAMEORIGIN`, measured rather than assumed, so an iframe
pointed at one renders nothing — and the widgets that used to allow it were
retired years ago. The page therefore describes the object in its own words,
shows photographs of it, and links out.

If you replace the photographs, **strip the EXIF**. Phone pictures taken at
home carry GPS: the originals behind the current set placed the flat to within
a few metres. `ImageOps.exif_transpose` first so the rotation survives, then
save without an `exif=` argument.

### Camera

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_CAMERA_URL` | *(empty)* | RTSP URL. Empty disables the feed. **Holds credentials; never reaches a browser** |
| `POSPRINTWEB_CAMERA_MODE` | `always` | `always`, `after_print`, or `off`. **A privacy setting** — read [Live camera](#live-camera) |
| `POSPRINTWEB_CAMERA_WINDOW` | `90` | Seconds the feed stays live after a print, in `after_print` mode |
| `POSPRINTWEB_CAMERA_FPS` | `0` | `0` uses the camera's own rate |
| `POSPRINTWEB_CAMERA_WIDTH` | `0` | `0` means no rescaling |
| `POSPRINTWEB_CAMERA_QUALITY` | `6` | ffmpeg `-q:v`; 2 best, 31 worst |
| `POSPRINTWEB_CAMERA_MAX_VIEWERS` | `6` | Concurrent streams. Drop to `2` once a relay is in front |
| `POSPRINTWEB_CAMERA_IDLE` | `15` | Seconds with no viewer before ffmpeg is stopped |
| `POSPRINTWEB_CAMERA_KILLSWITCH` | `/etc/posprintweb-camera.disabled` | Cuts the picture without stopping printing |

### The relay (read only by `python -m posprintweb.relay`, on the VPS)

Lives in `/etc/posprintweb-relay.env`, not the main env file.

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_RELAY_UPSTREAM` | *(empty)* | The container's feed. Required. Use its tailnet address, not a public one |
| `POSPRINTWEB_RELAY_HOST` / `_PORT` | `127.0.0.1` / `8001` | Caddy should be its only client |
| `POSPRINTWEB_RELAY_MAX_VIEWERS` | `24` | The real viewer cap once a relay is in front |
| `POSPRINTWEB_RELAY_IDLE_TIMEOUT` | `30` | Longer than the container's, so a reload does not restart ffmpeg |

### Braille

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_BRAILLE` | `true` | Accept braille art and print it as a decoded bitmap |
| `POSPRINTWEB_BRAILLE_MAX_COLS` | `72` | Art width in cells. 72 cells = 144 dots, so scale 4 fills an 80mm head |
| `POSPRINTWEB_BRAILLE_MAX_ROWS` | `40` | Art height in cells |
| `POSPRINTWEB_BRAILLE_MAX_SCALE` | `8` | Stops a tiny drawing being blown up to fill the roll |
| `POSPRINTWEB_BRAILLE_MAX_DOTS` | `640` | Paper budget for one picture, ~80mm at 203dpi |
| `POSPRINTWEB_BRAILLE_MAX_INK` | `55` | Percent of dots allowed to be black. **Not a rate limit** — a heat and paper protection. `0` refuses all braille |

### Access and switches

| Variable | Default | Notes |
| --- | --- | --- |
| `POSPRINTWEB_ADMIN_KEYS` | *(generated)* | Comma-separated. Bypasses quotas, proof of work, quiet hours, the killswitch and siege — but **not** input validation |
| `POSPRINTWEB_KILLSWITCH` | `/etc/posprintweb.disabled` | Printing stops while this file exists. No restart needed |
| `POSPRINTWEB_ENABLED` | `true` | Permanent off switch |
| `POSPRINTWEB_DB` | `/var/lib/posprintweb/prints.db` | Quota ledger and audit log |

---

## Operating it

### Start, stop, restart

```bash
pct exec 111 -- systemctl restart posprintweb     # after an env change
pct exec 111 -- systemctl stop posprintweb        # site goes 502
pct exec 111 -- systemctl start posprintweb
pct exec 111 -- systemctl status posprintweb
```

The other two units, for reference:

```bash
pct exec 110 -- systemctl restart posprint        # the printer service
systemctl restart posprintweb-relay               # on the VPS
```

### Switch printing off without stopping anything

Checked on every request, so it takes effect immediately and needs no restart.
This is the right tool for "stop, now" — `systemctl stop` takes the whole site
down and shows visitors a 502.

```bash
pct exec 111 -- touch /etc/posprintweb.disabled   # off
pct exec 111 -- rm /etc/posprintweb.disabled      # on
```

The page says *"Printing is switched off right now"* and the submit button
greys out. **It also cuts the camera** — if printing is off, so is the picture
of the printer. That is deliberate, and it is the usual explanation for a
camera that has gone dark for no apparent reason.

To cut only the picture:

```bash
pct exec 111 -- touch /etc/posprintweb-camera.disabled
```

### Watch what is happening

```bash
pct exec 111 -- journalctl -u posprintweb -f
```

Every refusal path logs except the killswitch, quiet hours and a plain quota
`429`. `/api/status` covers the first two, so this pair between them names any
blocked print:

```bash
pct exec 111 -- sh -c 'curl -s localhost:8000/api/status | python3 -m json.tool | head -30; echo === ; journalctl -u posprintweb -n 0 -f'
```

### Admin

`/admin#<your admin key>` in a browser. The key travels in the URL *fragment*,
which browsers never send to the server, so it cannot appear in Caddy's access
log or a `Referer` header; the page moves it into `sessionStorage` and rewrites
the address bar before you have read the first message.

From a shell:

```bash
KEY=<admin key from /etc/posprintweb.env>

# who printed what, with addresses
curl -s -H "X-Admin-Key: $KEY" localhost:8000/admin/log | python3 -m json.tool

# print something yourself, ignoring every limit
curl -X POST localhost:8000/api/print \
  -H "X-Admin-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"message":"testing","name":"me"}'

# the hold queue, and whether a siege is running
curl -s -H "X-Admin-Key: $KEY" localhost:8000/api/admin/held | python3 -m json.tool

# end a siege by hand
curl -X POST -H "X-Admin-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"action":"lift"}' localhost:8000/api/admin/held

# empty the hold queue after a flood
curl -X POST -H "X-Admin-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"action":"empty"}' localhost:8000/api/admin/held
```

The admin key bypasses quotas, proof of work, quiet hours, the killswitch and
siege mode. It does **not** bypass input validation — a blank name or an
unprintable character is still a 422, because those are about what the printer
can physically produce.

### Back up

One file holds the gallery, the audit log and every quota counter:

```bash
pct exec 111 -- sh -c 'systemctl stop posprintweb && \
  cp -a /var/lib/posprintweb/prints.db /var/lib/posprintweb/prints.db.$(date +%F).bak && \
  systemctl start posprintweb'
```

---

## Limits, and how to reset them

This is the section that gets used at 1am, so it is written to be read out of
order.

### The one thing to know first

**The limits are not stored in the database.** They are numbers in
`/etc/posprintweb.env`. The `prints` table holds one row per accepted message,
and each quota is a `COUNT(*)` over some window of those rows, evaluated fresh
on every request.

So *resetting a limit is a config change, not a database change* — and every
limit already skips its query entirely when set to `0`:

```python
if per_ip_daily > 0:            # store.py — the shape of every check
    used = cur.execute(...)
```

With the number at zero, the rows are inert history. Deleting them changes
nothing about who may print, and takes the gallery and your audit log with it.

### Where each limit actually lives

| Limit | Counter lives in | Turn it off with | Survives a restart? |
| --- | --- | --- | --- |
| `COOLDOWN_SECONDS` | `prints` table | `=0` | yes |
| `PER_IP_DAILY` | `prints` table | `=0` | yes |
| `GLOBAL_DAILY` | `prints` table | `=0` | yes |
| `GLOBAL_HOURLY` | `prints` table | `=0` | yes |
| `GLOBAL_BURST` | `prints` table | `=0` | yes |
| `REPEAT_HOURS` | `prints` table | `=0` | yes |
| `POW_BITS` | stateless HMAC | `=0` | n/a |
| `CAPTCHA` | stateless HMAC | `=false` | n/a |
| **Siege mode** | **process memory** | `HOLD_THRESHOLD=0` + `HOLD_VOLUME=0` | **no — restart clears it** |
| Quiet hours | the clock | `QUIET_START` = `QUIET_END` | n/a |
| Killswitch | the filesystem | `rm /etc/posprintweb.disabled` | yes |

The siege row is the one that surprises people. It is held in RAM on purpose —
it is a fact about *right now*, it should evaporate on restart, and a flood must
not be able to grow your disk by being refused. **No amount of database
surgery will clear it.** `systemctl restart posprintweb` will, and so will
`{"action":"lift"}`.

### Let everybody print again, right now

```bash
pct exec 111 -- sh -c 'cat >> /etc/posprintweb.env <<EOF
POSPRINTWEB_COOLDOWN_SECONDS=0
POSPRINTWEB_PER_IP_DAILY=0
POSPRINTWEB_GLOBAL_DAILY=0
POSPRINTWEB_GLOBAL_HOURLY=0
POSPRINTWEB_GLOBAL_BURST=0
POSPRINTWEB_REPEAT_HOURS=0
POSPRINTWEB_POW_BITS=0
POSPRINTWEB_HOLD_THRESHOLD=0
POSPRINTWEB_HOLD_VOLUME=0
EOF
rm -f /etc/posprintweb.disabled
systemctl restart posprintweb'
```

The restart is what clears any siege already running. Note what is *not* in
that list:

- **`QUIET_START`/`QUIET_END`** — between 22:00 and 08:00 nothing prints
  regardless. Add `POSPRINTWEB_QUIET_START=0` and `POSPRINTWEB_QUIET_END=0` if
  you want it printing at night.
- **`BRAILLE_MAX_INK`** — leave it at 55. It is a heat and paper protection,
  not a rate limit, and `0` refuses all braille including a small drawing.

To restore, append the original values — last assignment wins — or delete the
block you added and restart.

### Reset one thing at a time

```bash
# a specific limit
pct exec 111 -- sh -c 'echo POSPRINTWEB_PER_IP_DAILY=0 >> /etc/posprintweb.env && systemctl restart posprintweb'

# a siege, without touching config
curl -X POST -H "X-Admin-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"action":"lift"}' localhost:8000/api/admin/held

# just yourself, leaving everyone else limited: use the admin key
```

### "No prints left today" when nothing is limited

`remaining_today` is `max(0, per_ip_daily - used_today)`. When the cap is `0` —
meaning *no cap* — that arithmetic also yields `0`, which older builds rendered
as "No prints left today." The limit was off and the label said the opposite.

Fixed: the page now says nothing about quota when the cap is disabled. If you
still see the old wording, the container is running old code —
[deploy](#deploying-a-change). Either way the message is cosmetic: nothing
client-side or server-side gates a print on `remaining_today`.

### When you really do want to touch the database

There is one good reason: an attack has filled your review queue with hundreds
of machine-written messages and you want them gone. That is a cleanup, not a
limit reset.

**Back up first, stop the service, and write as the service user** — the
service holds an open connection and takes `BEGIN IMMEDIATE` on every print,
and SQLite's sidecar journal files must not end up owned by root:

```bash
pct exec 111 -- sh -c 'systemctl stop posprintweb && \
  cp -a /var/lib/posprintweb/prints.db /var/lib/posprintweb/prints.db.$(date +%F-%H%M).bak'
```

Look before you delete:

```bash
pct exec 111 -- sudo -u posprintweb /opt/posprint-web/venv/bin/python -c "
import sqlite3
db = sqlite3.connect('/var/lib/posprintweb/prints.db')
print('total          :', db.execute('SELECT COUNT(*) FROM prints').fetchone()[0])
for s, n in db.execute('SELECT state, COUNT(*) FROM prints GROUP BY state'):
    print(f'  {s:<10} {n}')
print('in the gallery :', db.execute(\"SELECT COUNT(*) FROM prints WHERE gallery='approved'\").fetchone()[0])
"
```

Then remove everything that is not in the gallery:

```bash
pct exec 111 -- sudo -u posprintweb /opt/posprint-web/venv/bin/python -c "
import sqlite3
db = sqlite3.connect('/var/lib/posprintweb/prints.db')
db.execute(\"DELETE FROM prints WHERE gallery != 'approved'\")
db.commit(); db.execute('VACUUM')
print(db.total_changes, 'rows removed')
"
pct exec 111 -- systemctl start posprintweb
```

**Two traps.**

`UPDATE prints SET state='rejected'` looks like a tidy way to zero the counters
without deleting anything — the quota queries all exclude rejected rows. It also
**empties your gallery**, because the gallery selects `state='printed'`. It is
reversible, since `set_gallery()` only ever marked rows that had printed:

```bash
pct exec 111 -- sudo -u posprintweb /opt/posprint-web/venv/bin/python -c "
import sqlite3
db = sqlite3.connect('/var/lib/posprintweb/prints.db')
db.execute(\"UPDATE prints SET state='printed' WHERE gallery='approved'\")
db.commit(); print(db.total_changes, 'gallery entries restored')
"
```

And an approved gallery entry from today still counts against today's quota.
There is no state that is both gallery-visible and quota-invisible: the gallery
needs `state='printed'`, and the quotas count exactly the rows that are not
`'rejected'`. A handful of surviving rows against a 200/day budget is a
rounding error, but it is why "clear the counters" and "keep the gallery"
cannot both be perfectly true.

### Why quota refusals never create rows

A refused print does not reach the `INSERT`, so a flood cannot extend its own
block by hammering, and `Retry-After` carries the real number of seconds until
the sliding window opens a slot.

The exception is a print that *passed* every quota and then failed at the
printer: `release()` marks it `'rejected'` so the visitor is not charged for an
empty roll. That has a consequence worth watching — while the printer is broken,
every attempt is refunded, so an attacker gets unlimited free attempts and your
daily budget never fills. If the log shows hundreds of `rejected` rows, check
the roll before you go looking for anything cleverer.

---

## Gallery

`/gallery` shows messages you have approved by hand. **Nothing appears there
until you approve it** — the back catalogue and every new print start as `new`,
which is the safe direction given what turns up.

Review at `/admin#<your admin key>`. Three lists, every decision reversible:

| List | Actions |
| --- | --- |
| **Waiting** | Approve → public · Hide → never shown |
| **Approved** | Remove from gallery → takes it back down |
| **Hidden** | Publish · Back to queue |

`hidden` is a state rather than a delete, so taking something down does not
destroy the record of what was sent. Nothing here reprints anything.

Slips are drawn at the width of the roll rather than the width of the page — 48
characters and no more, which is what a till roll is. The serrated ends are
`::before`/`::after` on `.paper`, so any page using the class gets them.

### Filtering and paging

The gallery narrows to one day, and the day lives in the URL:

```
https://print.example.com/gallery?day=2026-08-18
```

so a day can be linked to and the back button walks between days. The filter is
built from `gallery_days()` — the days that actually have something approved —
so every option leads somewhere and none returns an empty page. `day` is the
printer's local date as written on the row, so a message belongs to the day it
came out rather than to a recomputed UTC one.

Paging is keyset on `id`, never `OFFSET`: approving something while a visitor is
part-way down would shift every later page by one and silently skip an entry.
The cursor and the day compose. `?limit=` caps at 100. The day list rides along
with the *first* page only — it cannot change while paging.

Entries are drawn by `Receipt.render` in `static/receipt.js`, the same function
the print page uses for its live preview, so both surfaces show the same
48-column wrap and the same code-page degradation (`—` → `-`). That shared
module exists because `wrap()` was built to match Python's `textwrap` line for
line; a second copy would drift the first time either was touched.

Two things deliberately cannot be approved: **shadowed messages**, since the
whole point is that they did not happen, and **failed prints**, which produced
no paper. `state` and `gallery` are separate columns because "did it print" and
"should strangers see it" are different questions. `/api/gallery` is public and
its projection omits `ip` at the SQL level, so the column is not one typo away
from a page anyone can read.

## The quiet filter

There are two wordlists and the difference between them is the point.

`POSPRINTWEB_BLOCKLIST` refuses the message and says so: *"That message was
blocked."* Honest, and useful for keeping ordinary people honest — but it tells
whoever is probing precisely what to edit, and they will.

`POSPRINTWEB_SHADOWLIST` says nothing. A match gets a normal success response,
is charged against the sender's quota, appears in `/admin/log` with
`state: shadowed`, and never reaches paper. Someone testing which slurs get
through learns nothing and spends their daily allowance on receipts that do not
exist.

```bash
cp /root/posprint/web/deploy/shadowlist.example.txt /etc/posprintweb-shadowlist.txt
# then set POSPRINTWEB_SHADOWLIST=/etc/posprintweb-shadowlist.txt and restart
```

Three details that matter:

- **Nothing about it is visible client-side.** Not in `/api/status`, not in the
  page, not in the response body. `test_the_filter_is_invisible_in_the_public_api`
  pins that.
- **The reply is delayed by `SHADOW_DELAY_MS` (900ms).** A real print takes
  about a second of printer time; returning instantly is the one tell a
  determined sender could measure.
- **Matching allows separators inside a term, not around it.** `f-u-c-k` is
  caught; `peacock` and `Scunthorpe` are not. A false positive here is
  invisible to everyone including you, so read the log occasionally.

The admin key bypasses it, so your own messages are never swallowed.

## Limits that ignore the sender's address

Per-IP cooldowns and daily caps are worth little against someone who can change
address at will. Three controls do not look at the address at all:

| Setting | What it does |
| --- | --- |
| `REPEAT_HOURS` (24) | Refuses content already printed in that window, matched on a folded fingerprint — case, accents and all whitespace removed. Re-indenting or re-casing the same drawing does not get a second print |
| `GLOBAL_HOURLY` (30) | Caps prints per hour across everyone. Blunts a flood without ending the day the way the daily budget would |
| `GLOBAL_BURST` (8 per 60s) | Caps prints per *minute* across everyone. The only one that answers a flood from a rented proxy pool |

`GLOBAL_BURST` exists because of a real run: 50 prints in 19 seconds — 2.6 a
second — each from a different address, each with 500 characters of random text.
Nothing else caught it. The addresses were all different, so the cooldown and
the per-IP daily never fired; the messages were all different, so the
fingerprint never fired; there were no words, so the shadow list never fired.
The addresses were real, too — none in unroutable space, a quarter of them in
mobile-carrier ranges — so this was a rented pool, not a forged header, and no
amount of per-IP accounting would have helped.

A minute is the shortest useful window. It is fatal to a flood, invisible to a
person (the per-IP cooldown is 60s, so nobody reaches it alone), and
self-healing: the window slides one slot at a time and `Retry-After` carries the
real number of seconds until the next opens.

A second flood arrived later as **random braille**, which is worth understanding
because it walked around three separate controls at once: braille skips
`max_chars` (it is measured as a grid, not a character count), prints as a
bitmap rather than text so the wordlists never see it, and random dots land at
almost exactly 50% ink against a 55% ceiling. The thing that stopped it was
siege mode, not any of the prices.

## Running this where the attacker can read it

This repository is public, and it is safe to assume whoever is abusing the
printer pulls it and reads the diffs. That is fine for the mechanisms and fatal
for the numbers.

**Publication does not weaken.** Proof of work costs what it costs whether or
not you know how it works. The rate limits are arithmetic. Siege mode holding
prints for approval cannot be argued out of by understanding it. The HMAC
constructions are secure with the algorithm known and the key secret, which is
the ordinary arrangement.

**Publication does weaken** anything whose value was that nobody had looked at
it:

- **The shadow list.** Its entire premise is that the sender believes their
  message printed. `deploy/shadowlist.example.txt` is a format example with no
  real terms — keep the real list outside the repository.
- **The thresholds.** Refusals only happen when someone overshoots a limit, so a
  reader who knows `HOLD_THRESHOLD` can pace exactly at the burst cap and never
  trip it. `HOLD_VOLUME` closes that specific hole; the general rule stands.
  **Change the numbers in `/etc/posprintweb.env` so they are not the published
  defaults.**
- **The captcha.** Its honest advantage was that no solver existed for it. With
  `captcha.py` public, writing one is an afternoon.

Set both secrets so they are neither per-process randoms nor defaults:

```bash
POSPRINTWEB_POW_SECRET=$(openssl rand -hex 32)
POSPRINTWEB_CAPTCHA_SECRET=$(openssl rand -hex 32)
```

Neither is in the repository and neither should be. Nothing else here depends on
secrecy to work.

## Siege mode

Everything above is a **price**. The burst cap prices paper, proof of work
prices a request, the quotas price an address. A determined sender pays them all
and keeps going — which is what happened: the flood came back, paid, hit the
per-minute cap and settled in to occupy every slot it allowed.

Prices bound damage. They do not stop it. So while the printer is under attack,
messages **queue for your approval instead of printing**. Nothing reaches paper
without a decision. That is a guarantee rather than a cost, and it is the only
thing here that is.

**The first trigger is refusals, not prints.** A flood bounces off the rate
limits hundreds of times a minute because it keeps trying. Friends taking turns
at a party produce prints and almost no refusals, because people wait for each
other. Counting prints would put a busy evening and an attack in the same
bucket.

**The second is volume, and it exists because this repository is public.** A
reader who knows the thresholds can pace exactly at the burst cap and never
overshoot — no refusals, no siege, a receipt every seven seconds forever. Nobody
sends sixty messages an hour to a printer in a stranger's flat, however politely
they space them out.

| Setting | Default | Meaning |
| --- | --- | --- |
| `HOLD_THRESHOLD` | 20 | Refusals within the window that start a siege. `0` disables |
| `HOLD_WINDOW_SECONDS` | 300 | How far back refusals are counted |
| `HOLD_FOR_SECONDS` | 1800 | How long it lasts, refreshed by further trouble |
| `HOLD_VOLUME` | 60 | Prints in the volume window that also start one. `0` disables |
| `HOLD_VOLUME_SECONDS` | 3600 | The volume window |
| `HOLD_MAX_QUEUE` | 200 | Ceiling on held messages; past it, new ones are refused |

Note what the burst cap turns into during a siege. Refusals happen before the
hold does, so the cap stops admitting messages to *paper* and starts admitting
them to the *queue* — at the same eight a minute, with the queue ceiling behind
it. Either way the receipt count is zero.

Held messages appear under **Held** on `/admin`, oldest first, because this is a
queue to work through rather than a feed to browse. Each has *Print it* and
*Discard*; there is also *Discard all held*, since after a flood the queue is
hundreds of machine-written strings. A siege ends on its own once the hammering
stops, or by hand with *End siege mode* — a timer cannot know the wave has
passed, but the person looking at the queue can.

Two deliberate details. The sender is **told the truth** — "your message is in
the queue" — unlike the shadow filter, which lies on purpose; a held message is
a real one that arrived at a bad moment. And the admin key skips the hold
entirely, because being locked out of your own printer by an attacker would be
its own kind of win.

Releasing goes through the same upstream call as an ordinary print, so a
released message is indistinguishable on paper. If the printer fails mid-release
the message goes back in the queue rather than being lost.

**It lives in process memory.** It does not survive a restart, it is not in the
database, and clearing rows will not touch it. See
[the limits table](#where-each-limit-actually-lives).

## Proof of work

`GLOBAL_BURST` bounds the paper. It does not stop a flood *occupying* those
slots while a person waits, and nothing keyed on the sender can, because the
sender is renting their identity.

So every print must arrive with a solved puzzle. The server names a challenge;
the page searches for a counter whose SHA-256 starts with `POW_BITS` zero bits;
the server checks it in one hash and spends it. Finding an answer costs a few
hundred thousand hashes. Checking one costs a single hash. That asymmetry is the
entire idea.

**A button or a checkbox would not have helped.** The flood never loaded the
page — it posted straight to `/api/print`, where there is nothing to click.
Verifiable in one line:

```bash
curl -X POST https://print.example.com/api/print -H 'Content-Type: application/json' -d '{"message":"hello"}'
```

Before, that printed a receipt. Now it is a `428`. The "I'm not a robot" click
in a commercial captcha is a wrapper around the same server-side token check;
the click itself is not what protects anything, which is why headless browsers
performing it is an industry rather than a surprise.

Challenges are signed with an HMAC, so nothing is stored to know we issued one,
and **spent exactly once** — a replayable answer is a one-off cost rather than a
per-print one. The admin key skips the check, which is also the answer to being
locked out of your own printer during a flood.

**What this does and does not buy.** It ends casual scripting completely: a
`curl` loop now has to implement a SHA-256 search. It does not stop someone
determined, because browsers are roughly fifty times slower at hashing than
native code. The security is in *requiring proof at all*, not in the number of
bits — which is why `POW_BITS` should be chosen for the slowest phone you care
about rather than pushed as high as it will go.

Measured at 18 bits, with the search started on the first keystroke so it
overlaps with typing:

| Device | Median | 95th percentile |
| --- | --- | --- |
| Desktop (~870k hashes/s) | 0.2s | 0.9s |
| Mid-range phone | 0.5s | 2s |
| Old phone | 1.2s | 5s |

In practice the answer is ready before the button is pressed: typing mweol took
1.8s, and the print went out 152ms after the click.

The solver is plain integer arithmetic — `Uint32Array`, `Math.clz32`,
`MessageChannel`. **No `crypto.subtle`**, which is asynchronous (a promise per
hash turns a quarter-million hashes into minutes) and needs a secure context; no
WebAssembly; no worker. It runs anywhere with ES2015, a wider net than the Web
Crypto API casts. The JavaScript SHA-256 is checked against the browser's own
`crypto.subtle` over 300+ random inputs and every block-boundary length; Python
pins the recipe in `test_challenge.py`. If the two ever disagree, no browser can
print at all.

## Live camera

A camera pointed at the printer, streamed on the page. Same rule as the API key:
the browser never learns where the camera is. It asks this service for
`/api/camera.mjpg`; this service holds the RTSP credentials and streams JPEGs
back. Reading the page source tells a visitor nothing they could point VLC at.

**This is a privacy decision, not a feature flag.** With `CAMERA_MODE=always`
there is a live view of a room in your home on a URL strangers already have.
Point the camera at the printer and as little else as possible.

Two things keep the cost bounded: **ffmpeg only runs while someone is watching**
(a request starts it, an idle timeout stops it), and **one decode feeds
everyone** — frames are shared, so ten viewers cost the camera and your uplink
the same as one.

### Why the page reads the stream instead of using `<img src>`

An `<img>` pointed at a multipart stream cannot report that the stream stopped.
Measured in Chrome, against a feed ended three ways — closed cleanly, reset
mid-frame, and simply going silent — the element fires **no event at all** in
every case. No `error`, no `abort`, no `stalled`. It keeps `complete === true`,
keeps its `naturalWidth`, and goes on showing the last frame it received.

So the reconnect logic could only ever fire *before* the first frame arrived.
Once there was a picture on screen, any later failure froze it and nothing
noticed — which is why the camera used to need a page refresh.

The page now reads the body itself and paints each frame, which turns all three
failures into one observable event: the body ending, or no frame within
`CAMERA_TIMEOUT`. Either one reconnects with backoff. Returning to the tab also
reconnects immediately rather than serving out a backoff counted while nobody
was looking.

### Fanning it out from the VPS

Caddy's `reverse_proxy` opens a **separate connection per viewer**, so every
person watching used to pull their own MJPEG stream out of the flat. That, and
not the VPS, is why `CAMERA_MAX_VIEWERS` was ever as low as six.

The relay pulls the feed **once** and hands it to everyone:

```
camera ──► CT 111 (ffmpeg) ──► relay on the VPS ──┬──► viewer
                                                  ├──► viewer
          one connection out of the flat          └──► viewer
```

Install it with [step 5](#5-the-camera-relay-on-the-vps-optional). `flush_interval -1`
is required on the streaming route or Caddy buffers it into uselessness.

**The container stays the authority on whether the feed may be shown.** It holds
the RTSP credentials, `CAMERA_MODE` and the killswitch; the relay only ever sees
the picture the site already publishes, and gets a 404 when the container says
no. Bandwidth moves to the VPS; the privacy decision does not.

The number to check after deploying is not on the relay but upstream. With
several people watching, the container should still report one viewer. Measured
on a laptop with eight viewers on the relay: the container reported **1**
connection throughout, and every viewer received every frame. At 600 concurrent
viewers the relay sustained 61 Mbit/s with no frame loss.

The relay reads the stream with the same care the browser does — a body that
ends is a dead feed, a silent stall needs a timeout, and frames are taken by
their declared `Content-Length` rather than by scanning for the next boundary,
because a JPEG can contain anything including the boundary itself.

A "switched off" answer from the container **expires** rather than latching. The
gate that reads it refuses the request before anything would re-ask, so a
permanent answer meant a feed switched back on stayed dark until someone
restarted the relay by hand — which is exactly what happened once, since the
printing killswitch also closes the camera.

### Trying it without a camera

```bash
python web/scripts/dev.py --fake --camera --camera-drop=8
```

`--camera` serves a synthetic feed with a moving block, so a frozen picture is
obvious at a glance. `--camera-drop=8` ends every stream after 8 seconds, the
way a real one ends when its producer stalls.

`web/scripts/camera-bitrate.py` measures what a viewer actually costs. At
640×360, 15fps and `-q:v 6` that is roughly 3 Mbit/s.

### Tapo TC70

Enable **Advanced Settings → Camera Account** in the Tapo app first; that
username and password are separate from your TP-Link login.

```
rtsp://<user>:<pass>@<camera-ip>:554/stream2    # 360p, easy on the uplink
rtsp://<user>:<pass>@<camera-ip>:554/stream1    # 1080p
```

`stream2` is usually the right choice — you are watching a strip of paper
appear, not reading it. `stream1` with `CAMERA_WIDTH=800` is a reasonable middle
once a relay carries the fan-out.

That URL contains a password. **Redact it before pasting config anywhere:**

```bash
grep CAMERA_URL /etc/posprintweb.env | sed -E 's|(rtsp://)[^@]*@|\1<redacted>@|'
```

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
  mixing braille with ordinary text is refused rather than half-rendered. Spaces
  are fine as padding, as is `U+2800`, the blank cell.
- **`max_chars` does not apply.** It measures the wrong thing once the message
  is a picture: 500 cells might be 72 wide and 7 tall or 8 wide and 62, and
  those cost very different amounts of roll. The grid and dot limits apply
  instead. This is also why a braille flood needs siege mode rather than a
  character limit to stop it.
- **Scaling is integer-only.** Stretching to fill the head exactly would make
  some dots four pixels across and others five, which on a 1-bit image reads as
  a texture crawling through the picture.

### How dark is too dark

A thermal head makes black by heating an element, so a filled-in picture prints
slowly, runs hot and drains the roll's contrast. `BRAILLE_MAX_INK` caps the
fraction of dots that may be black:

| | Ink |
| --- | --- |
| Line art (mweol) | 12% |
| Sparse dots | 13% |
| **Half-filled cells** | **50%** |
| Solid block | 100% |

The default is **55**, not 50, and that gap is deliberate: 50% is exactly "every
other dot", an ordinary dithering pattern rather than abuse. Setting the limit
at 50 would refuse real photographs while the thing worth stopping is the solid
rectangle at 100%.

Each braille codepoint carries eight dots, so its set bits *are* its ink — the
page computes the same number the server does and warns before you send.

The same decoder is available as a command-line tool for art too big for the
public limits: `scripts/braille_print.py`, which talks to posprint directly.

## API

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /` | — | The page |
| `GET /api/status` | — | Limits, printer state, your remaining quota |
| `POST /api/print` | — | `{"message": "...", "name": "...", "challenge": ..., "counter": ...}` |
| `GET /api/challenge` | — | A proof-of-work challenge |
| `GET /api/captcha` | — | A visual puzzle. 404 when `CAPTCHA=false` |
| `GET /gallery` | — | Approved messages |
| `GET /auction` | — | The item for sale. 404 when `AUCTION_URL` is unset |
| `GET /api/gallery` | — | `?limit=&before=&day=` — keyset paged, `day` is `YYYY-MM-DD` or 422. Never includes `ip` |
| `GET /api/camera.mjpg` | — | The feed. 404 when switched off |
| `GET /api/camera.jpg` | — | One frame |
| `GET /admin` | — | Review page shell. Inert; data comes from the routes below |
| `GET /api/admin/queue` | `X-Admin-Key` | Printed messages awaiting a decision |
| `POST /api/admin/gallery` | `X-Admin-Key` | `{"id": 12, "action": "approve"\|"hide"\|"reset"}` |
| `GET /api/admin/held` | `X-Admin-Key` | The hold queue, plus siege status |
| `POST /api/admin/held` | `X-Admin-Key` | `{"action": "print"\|"discard"\|"empty"\|"lift", "id": 12}` |
| `GET /admin/log` | `X-Admin-Key` | Recent prints with IP and body. 404s without the key |
| `GET /healthz` | — | For the tunnel's health check |

`POST /api/print` returns `200` printed, `202` held for review, `422` rejected
input, `428` missing or spent proof of work, `429` rate limited (with
`Retry-After`), `502` the printer failed, `503` switched off or quiet hours. A
`502` refunds the quota — an empty roll is not the visitor's fault.

The relay exposes only `/api/camera.mjpg`, `/api/camera.jpg` and `/healthz`. It
has no database, no printer, no admin surface and no pages, so what is exposed
on a rented machine is as small as the job allows.

`GET /api/status` carries everything the page needs to render itself honestly:

| Field | Why the page needs it |
| --- | --- |
| `printer_state` | `ready` / `out_of_paper` / `offline`. `online` is kept for anything already reading it, but says only *whether*, not *which* |
| `charset.printable` | Every character the code page can express, derived from the codec. The preview shows what the paper will say rather than what the browser can display |
| `charset.replacements` | The degradations posprint applies — `—` → `-`, `…` → `...` |
| `braille` | Grid and scale limits, so the page can estimate paper cost and knows not to refuse braille as unprintable |
| `limits`, `you`, `printed_today` | Caps, remaining quota, global count |

---

## Troubleshooting

### Nothing prints, and I need to know why

Nine gates can refuse a print, and only one of them reads the database. In
order:

| Gate | What the visitor sees |
| --- | --- |
| Proof of work | `428` "This page needs refreshing before it can print." |
| Killswitch | `503` "Printing is switched off right now." |
| Quiet hours (22–8) | `503` "…asleep until 08:00." |
| Validation | `422` — blank name, too long, unprintable characters |
| **Quotas** | `429`, with `Retry-After` |
| Shadow list | *fake success* — says printed, nothing comes out |
| Siege mode | `202` "…your message is in the queue." |
| Hold queue full | `503` "The printer is swamped and the queue is full." |
| The printer itself | `502` — out of paper, offline, busy |

Every one of these logs except the killswitch, quiet hours and the quota `429`.
`/api/status` reports the first two, so this shows all of them:

```bash
pct exec 111 -- sh -c 'curl -s localhost:8000/api/status | python3 -m json.tool | head -30; echo === ; journalctl -u posprintweb -n 0 -f'
```

If the submit button is greyed out, it is client-side: `printerBlocked`
(killswitch, quiet hours, out of paper, printer offline), an unprintable
character, braille mixed with text, over the length limit, or **an empty name**
— the name is required.

### The site returns 502 and the service is running fine

Check what it is listening on. `POSPRINTWEB_HOST=127.0.0.1` starts cleanly,
looks healthy in `systemctl status`, and is unreachable from a proxy on another
machine:

```bash
pct exec 111 -- ss -ltnp | grep 8000
```

`0.0.0.0:8000` is right; `127.0.0.1:8000` is the bug. Then check the Caddyfile
actually names the container's current address — this failure and a wrong IP
look identical from the outside.

### The camera 404s and the page says it is switched off

Three causes, in order of likelihood:

1. **The printing killswitch is on.** It also closes the camera, by design.
   `ls /etc/posprintweb.disabled`.
2. `CAMERA_MODE=off`, or `CAMERA_URL` is empty.
3. `/etc/posprintweb-camera.disabled` exists.

If `/api/status` says `camera.live: true` and the relay still 404s, the relay is
running old code that latched the "off" answer permanently.
[Redeploy it](#deploying-a-change).

### The camera 503s and the log says `ffmpeg exited (-31)`

A negative code is a signal, and 31 is `SIGSYS`: seccomp killed it. ffmpeg
inherits the unit's `SystemCallFilter`, and Debian's build calls `set_mempolicy`
from libnuma (via libx265) during library init. The unit allows that one call
back explicitly — if you see this, the running unit predates that fix, so re-run
`install.sh`.

The same command working when you run it by hand is the tell: your shell is not
inside the service's sandbox. Confirm on the **host**, not in the container,
since they share a kernel:

```bash
dmesg -T | grep 'comm="ffmpeg"' | tail -3      # look for sig=31 syscall=NNN
```

### "The printer is out of paper" but the roll is fine

posprint reads the printer's status byte before each job. If it says the roll is
empty when it is not, the usual cause is a stale handle on the device: something
still holds `/dev/usb/lp0` and the status read returns `EBUSY`, which surfaces
as offline or out-of-paper.

```bash
pct exec 110 -- systemctl restart posprint
pct exec 110 -- curl -s localhost:8080/health
```

If that does not clear it, `pct stop 110 && pct start 110`. See the root
README's [troubleshooting](../README.md#troubleshooting).

Watch for the knock-on effect: while the printer is refusing, every accepted
print is refunded, so quotas never fill and an attacker gets unlimited free
attempts. A pile of `rejected` rows in the log usually means the roll, not a new
attack.

### Everyone shares one rate limit

A proxy is in front but `POSPRINTWEB_TRUST_PROXY` is still `false`.

### A setting isn't taking effect

Read it from the process, not the file — `/etc/posprintweb.env` may set the same
key several times, and the last one wins:

```bash
pct exec 111 -- sh -c 'tr "\0" "\n" < /proc/$(pgrep -f posprintweb | head -1)/environ | grep POSPRINTWEB | sort'
```

If the value is right there and the behaviour is still wrong, the process
predates the change — settings are read once at startup.

### A code change didn't reach the running service

`/opt/posprint-web` is a copy, not a checkout. `git pull` there fails.
[Deploying a change](#deploying-a-change) is the correct sequence.

### `status=226/NAMESPACE` on start

Unprivileged LXC cannot create the private mount namespace the unit's hardening
wants. `install.sh` should have dropped in `10-container.conf`; re-run it. Do
not "fix" this by enabling `nesting=1` — that loosens the container's isolation
to buy in-container hardening the container already provides.

### The page says "the printer is misconfigured"

posprint returned 401: `POSPRINTWEB_UPSTREAM_KEY` does not match
`POSPRINT_API_KEY`. The detail is in the journal, not on the page — visitors
should not be told which of your secrets is wrong.

### Accented characters look wrong on paper

That is posprint's codepage (`POSPRINT_CODEPAGE`), not this service. Characters
the codepage cannot express are transliterated rather than dropped.

---

## Development

From the repo root:

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r web/requirements.txt pytest
python web/scripts/dev.py --fake                 # http://127.0.0.1:8000
```

`--fake` stands in for the printer and dumps receipts to stdout, so you can work
on the page without hardware. Drop it to talk to a real posprint on `:8080`.

```bash
pytest -q          # 302 tests: both services
pytest web -q      # 243: just this one
```

No `PYTHONPATH` needed — the root `pyproject.toml` puts both package roots on
the path.

The dev harness carries flags for the things that are otherwise hard to
reproduce:

| Flag | What it gives you |
| --- | --- |
| `--fake` | No printer required; receipts go to stdout |
| `--camera` | A synthetic feed with a moving block, so a frozen picture is obvious |
| `--camera-drop=N` | Ends every stream after N seconds, the way a stalled producer does |

To exercise the relay locally, run the fake site and point a relay at it:

```bash
python web/scripts/dev.py --fake --camera
POSPRINTWEB_RELAY_UPSTREAM=http://127.0.0.1:8000/api/camera.mjpg \
  python -m posprintweb.relay
```

Open several tabs against `:8001` and confirm the shape of the win directly: the
relay's `/healthz` reports N viewers while the site reports **1**.
