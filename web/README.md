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
| Every per-IP limit bypassed by *changing IP* | Three controls that never look at the address: the same content is refused for `REPEAT_HOURS` however it is re-spaced or re-cased, and `GLOBAL_BURST` and `GLOBAL_HOURLY` cap the rate across everyone. An attacker's address is not a scarce resource; your paper is. |
| Someone iterating to find which slurs get through | The quiet filter, below. A match is accepted, charged, logged and never printed — so there is no feedback to iterate against. |
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

Both `header_up` lines are still worth having, but neither is load-bearing any
more. `reverse_proxy` *appends* to any inbound `X-Forwarded-For` by default, so
without the first line a visitor's own value lands leftmost — which is why the
app reads the header **from the right**. Each proxy appends the peer it saw, so
the last entry is the one Caddy wrote and everything before it is whatever the
sender chose to claim. `POSPRINTWEB_PROXY_HOPS` says how many proxies of your
own are in front (1 for Caddy alone; 2 if Cloudflare is in front of that), and
the client is that many entries from the end. A header shorter than the chain
claims falls back to the socket peer.

**On IP spoofing generally.** Over TCP, the socket peer cannot be forged: a
handshake cannot be completed without receiving the SYN-ACK, which goes to the
real owner of the address. So the only lie available is the *header*, and
reading it from the right is the whole defence. A challenge-response step —
fetch a key bound to your address, then spend it — adds nothing on top, because
the key travels back over the sender's own connection whatever address they
claim; TCP has already performed that round trip.

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

## Gallery

`/gallery` shows messages you have approved by hand. **Nothing appears there
until you approve it** — the back catalogue and every new print start as `new`,
which is the safe direction given what turns up.

Review them at **`/admin#<your admin key>`**:

```
https://print.example.com/admin#Xf3k...
```

The key travels in the URL *fragment*, which browsers never send to the server
— so unlike a query string it cannot appear in Caddy's access log, an upstream
request line or a `Referer` header. The page moves it into `sessionStorage` and
rewrites the address bar via `replaceState`, so it is gone from the URL and the
back stack before you have finished reading the first message. From there it
travels as the same `X-Admin-Key` header the API has always used: no cookie, no
session, no second credential format.

Three lists, and every decision is reversible:

| List | Actions |
| --- | --- |
| **Waiting** | Approve → public · Hide → never shown |
| **Approved** | Remove from gallery → takes it back down |
| **Hidden** | Publish · Back to queue |

`hidden` is a state rather than a delete, so taking something down does not
also destroy the record of what was sent. Nothing here reprints anything.

Slips are drawn at the width of the roll rather than the width of the page —
48 characters and no more, which is what a till roll is. The serrated ends are
`::before`/`::after` on `.paper`, so any page that uses the class gets them.

### Filtering and paging

The gallery can be narrowed to one day, and the day lives in the URL:

```
https://print.example.com/gallery?day=2026-08-18
```

so a day can be linked to and the back button walks between days. The filter is
built from `gallery_days()` — the days that actually have something approved on
them — so every option in the control leads somewhere and none of them returns
an empty page. `day` is the printer's local date as written on the row, so a
message belongs to the day it came out rather than to a recomputed UTC one.

Paging is keyset on `id`, never `OFFSET`: approving something while a visitor is
part-way down would shift every later page by one and silently skip an entry.
The cursor and the day compose, so paging inside a day is the same walk as
paging across all of them. `?limit=` caps at 100.

The day list rides along with the *first* page only. It cannot change while
paging, so sending it with every "Show older" would be waste — and rebuilding
the control mid-walk would reset the one the visitor is looking at.

Entries are drawn by `Receipt.render` in `static/receipt.js` — the same
function the print page uses for its live preview. Both surfaces therefore show
the same 48-column wrap, the same code-page degradation (`—` → `-`) and the
same header and from-line, so the gallery shows what actually came off the
roll. That shared module exists because `wrap()` was built to match Python's
`textwrap` line for line; a second copy would drift the first time either was
touched.

Two things deliberately cannot be approved:

- **Shadowed messages.** The whole point of the quiet filter is that they did
  not happen, so they never reach the queue.
- **Failed prints.** They produced no paper; there is nothing to show off.

`state` and `gallery` are separate columns because "did it print" and "should
strangers see it" are different questions. `/api/gallery` is public and its
projection omits `ip` at the SQL level, so the column is not one typo away from
a page anyone can read.

## The quiet filter

There are two wordlists and the difference between them is the point.

`POSPRINTWEB_BLOCKLIST` refuses the message and says so: *"That message was
blocked."* Honest, and useful for keeping ordinary people honest — but it tells
whoever is probing precisely what to edit, and they will.

`POSPRINTWEB_SHADOWLIST` says nothing. A match gets a normal success response,
is charged against the sender's quota, appears in `/admin/log` with
`state: shadowed`, and never reaches the paper. Someone testing which slurs get
through learns nothing, has nothing to iterate against, and spends their daily
allowance on receipts that do not exist.

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

### Limits that ignore the sender's address

Per-IP cooldowns and daily caps are worth little against someone who can change
address at will. Two controls don't look at the address at all:

| Setting | What it does |
| --- | --- |
| `REPEAT_HOURS` (24) | Refuses content already printed in that window, matched on a folded fingerprint — case, accents and all whitespace removed. Re-indenting or re-casing the same drawing does not get a second print. |
| `GLOBAL_HOURLY` (30) | Caps prints per hour across everyone. Blunts a flood without ending the day the way the daily budget would. |
| `GLOBAL_BURST` (8 per 60s) | Caps prints per *minute* across everyone. The only one of these that answers a flood from a rented proxy pool. |

`GLOBAL_BURST` exists because of a real run: 50 prints in 19 seconds — 2.6 a
second — each from a different address, each with 500 characters of random
text. Nothing else caught it. The addresses were all different, so the cooldown
and the per-IP daily never fired; the messages were all different, so the
fingerprint never fired; there were no words, so the shadow list never fired.
The addresses were real, too — none of them in unroutable space, a quarter of
them in mobile-carrier ranges — so this was a rented pool, not a forged header,
and no amount of per-IP accounting would have helped.

A minute is the shortest useful window. It is fatal to a flood, invisible to a
person (the per-IP cooldown is 60s, so nobody reaches it alone), and
self-healing: the window slides one slot at a time, and `Retry-After` carries
the real number of seconds until the next one opens. That last part is the
difference from `GLOBAL_HOURLY`, which used to answer a flat ten minutes
however close the window was to opening — which is what made it feel like a
punishment rather than a queue. Both now report the truth.

Blocked attempts never reach the insert, so hammering does not push the window
out: a flood cannot extend its own block.

### Running this where the attacker can read it

This repository is public, and it is safe to assume whoever is abusing the
printer pulls it and reads the diffs. That is fine for the mechanisms and fatal
for the numbers, so the split matters:

**Publication does not weaken.** Proof of work costs what it costs whether or
not you know how it works. The rate limits are arithmetic. Siege mode holding
prints for approval cannot be argued out of by understanding it. The HMAC
constructions are secure with the algorithm known and the key secret, which is
the ordinary arrangement.

**Publication does weaken.** Anything whose value was that nobody had looked at
it. Three specifics:

- **The shadow list.** Its entire premise is that the sender believes their
  message printed. `deploy/shadowlist.example.txt` is a format example with no
  real terms in it — keep the real list outside the repository and point
  `POSPRINTWEB_SHADOWLIST` there.
- **The thresholds.** Refusals only happen when someone overshoots a limit, so
  a reader who knows `HOLD_THRESHOLD` can pace exactly at the burst cap and
  never trip it. `HOLD_VOLUME` closes that specific hole, but the general rule
  stands: change the numbers in `/etc/posprintweb.env` so they are not the
  published defaults.
- **The captcha.** Its honest advantage was that no solver existed for it. With
  `captcha.py` public, writing one is an afternoon rather than a research
  project — the file says which four shapes, which six colours, and that
  exactly one property differs.

**Set these, so they are not per-process randoms and not the defaults:**

```bash
POSPRINTWEB_POW_SECRET=$(openssl rand -hex 32)
POSPRINTWEB_CAPTCHA_SECRET=$(openssl rand -hex 32)
```

Neither is in the repository and neither should be. Nothing else here depends
on secrecy to work.

### Siege mode

Everything above is a **price**. The burst cap prices paper, proof of work
prices a request, the quotas price an address. A determined sender pays them
all and keeps going — which is what happened: the flood came back, paid, hit
the per-minute cap and settled in to occupy every slot it allowed.

Prices bound damage. They do not stop it. So while the printer is under attack,
messages **queue for your approval instead of printing**. Nothing reaches paper
without a decision. That is a guarantee rather than a cost, and it is the only
thing here that is.

**The trigger is refusals, not prints.** A flood bounces off the rate limits
hundreds of times a minute because it keeps trying. Friends taking turns at a
party produce prints and almost no refusals, because people wait for each
other. Counting prints would put a busy evening and an attack in the same
bucket; counting refusals separates them and errs toward leaving an ordinary
busy night alone.

| Setting | Default | Meaning |
| --- | --- | --- |
| `HOLD_THRESHOLD` | 20 | Refusals within the window that start a siege. `0` disables |
| `HOLD_WINDOW_SECONDS` | 300 | How far back refusals are counted |
| `HOLD_FOR_SECONDS` | 1800 | How long it lasts, refreshed by further refusals |
| `HOLD_MAX_QUEUE` | 200 | Ceiling on held messages; past it, new ones are refused |

Note what the burst cap turns into during a siege. Refusals happen before the
hold does, so the cap stops admitting messages to *paper* and starts admitting
them to the *queue* — at the same eight a minute, with the queue ceiling behind
it. Either way the receipt count is zero.

Held messages appear under **Held** on `/admin`, oldest first, because this is
a queue to work through rather than a feed to browse. Each has *Print it* and
*Discard*; there is also *Discard all held*, since after a flood the queue is
hundreds of machine-written strings and going through them individually is not
a real option. A siege ends on its own once the hammering stops, or by hand
with *End siege mode* — a timer cannot know the wave has passed, but the person
looking at the queue can.

Two deliberate details. The sender is **told the truth** — "your message is in
the queue" — unlike the shadow filter, which lies on purpose; a held message is
a real one that arrived at a bad moment and whoever wrote it deserves to know.
And the admin key skips the hold entirely, because being locked out of your own
printer by an attacker would be its own kind of win for them.

Releasing goes through the same upstream call as an ordinary print, so a
released message is indistinguishable on paper. If the printer fails mid-release
the message goes back in the queue rather than being lost.

### Proof of work

`GLOBAL_BURST` bounds the paper. It does not stop a flood *occupying* those
slots while a person waits, and nothing keyed on the sender can, because the
sender is renting their identity.

So every print must arrive with a solved puzzle. The server names a challenge;
the page searches for a counter whose SHA-256 starts with `POW_BITS` zero bits;
the server checks it in one hash and spends it. Finding an answer costs a few
hundred thousand hashes. Checking one costs a single hash. That asymmetry is
the entire idea.

**A button or a checkbox would not have helped.** The flood never loaded the
page — it posted straight to `/api/print`, where there is nothing to click.
This is verifiable in one line:

```bash
curl -X POST https://print.example.com/api/print   -H 'Content-Type: application/json' -d '{"message":"hello"}'
```

Before, that printed a receipt. Now it is a `428`. The "I'm not a robot" click
in a commercial captcha is a wrapper around the same server-side token check;
the click itself is not what protects anything, which is why headless browsers
performing it is an industry rather than a surprise.

Challenges are signed with an HMAC, so nothing is stored to know we issued one,
and **spent exactly once** — a replayable answer is a one-off cost rather than
a per-print one. The admin key skips the check, which is also the answer to
being locked out of your own printer during a flood.

**What this does and does not buy.** It ends casual scripting completely: a
`curl` loop now has to implement a SHA-256 search. It does not stop someone
determined, because browsers are roughly fifty times slower at hashing than
native code, so any difficulty tolerable in a page is milliseconds in C. The
security is in *requiring proof at all*, not in the number of bits — which is
why `POW_BITS` should be chosen for the slowest phone you care about rather
than pushed as high as it will go.

Measured at 18 bits, with the search started on the first keystroke so it
overlaps with typing:

| Device | Median | 95th percentile |
| --- | --- | --- |
| Desktop (~870k hashes/s) | 0.2s | 0.9s |
| Mid-range phone | 0.5s | 2s |
| Old phone | 1.2s | 5s |

In practice the answer is ready before the button is pressed: typing mweol took
1.8s, and the print went out 152ms after the click. If it is not ready the
button counts hashes rather than freezing.

The solver is plain integer arithmetic — `Uint32Array`, `Math.clz32`,
`MessageChannel`. **No `crypto.subtle`**, which is asynchronous (a promise per
hash turns a quarter-million hashes into minutes) and needs a secure context;
no WebAssembly; no worker. It runs anywhere with ES2015, which is a wider net
than the Web Crypto API casts.

The JavaScript SHA-256 is checked against the browser's own `crypto.subtle`
over 300+ random inputs and every block-boundary length, plus the published
`abc` vector. Python pins the recipe in `test_challenge.py`: if the two ever
disagree, no browser can print at all.

The repeat check counts shadowed prints too — otherwise a caught message could
be resent indefinitely, one address at a time. It does *not* count failed
prints, since those produced no paper.

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
was looking — waking a laptop is the case that used to need the refresh.

The server's side of this is unchanged and already correct: `stream()` ends the
response when the producer stalls, with the comment *"let the client retry"*.
The client simply had no way to know.

### Trying it without a camera

```bash
python web/scripts/dev.py --fake --camera --camera-drop=8
```

`--camera` serves a synthetic feed with a moving block, so a frozen picture is
obvious at a glance. `--camera-drop=8` ends every stream after 8 seconds, the
way a real one ends when its producer stalls. Before the fix the picture froze
at the first drop and stayed frozen; now it comes back after a short backoff.

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

### How dark is too dark

A thermal head makes black by heating an element, so a filled-in picture prints
slowly, runs hot and drains the roll's contrast. `BRAILLE_MAX_INK` caps the
fraction of dots that may be black. Measured on real samples:

| | Ink |
| --- | --- |
| Line art (mweol) | 12% |
| Sparse dots | 13% |
| **Half-filled cells** | **50%** |
| Solid block | 100% |

The default is **55**, not 50, and that gap is deliberate: 50% is exactly
"every other dot", which is an ordinary dithering pattern rather than abuse.
Setting the limit at 50 would refuse real photographs while the thing actually
worth stopping is the solid rectangle at 100%.

Each braille codepoint carries eight dots, so its set bits *are* its ink — the
page computes the same number the server does and warns before you send.

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
| `POSPRINTWEB_BRAILLE_MAX_INK` | `55` | Percent of dots allowed to be black. Refuses solid blocks; see below |
| `POSPRINTWEB_PRINTER_DOTS` | `576` | Match posprint's `POSPRINT_DOTS`; 384 for 58mm paper |
| `POSPRINTWEB_COOLDOWN_SECONDS` | `60` | Minimum gap between prints from one IP |
| `POSPRINTWEB_PER_IP_DAILY` | `5` | Per-IP daily cap. `0` disables |
| `POSPRINTWEB_GLOBAL_DAILY` | `200` | Paper budget for everyone combined. `0` disables |
| `POSPRINTWEB_MAX_CHARS` | `500` | ~8cm of 80mm paper |
| `POSPRINTWEB_MAX_LINES` | `20` | |
| `POSPRINTWEB_MAX_NAME_CHARS` | `32` | Length of the optional sender name |
| `POSPRINTWEB_QUIET_START` / `_END` | `22` / `8` | Local hours. Set both equal to disable |
| `POSPRINTWEB_TZ` | `Europe/Berlin` | Timezone for quiet hours and daily rollover |
| `POSPRINTWEB_BLOCKLIST` | *(empty)* | Path to a wordlist. Refuses the message **and says so** |
| `POSPRINTWEB_SHADOWLIST` | *(empty)* | Path to a wordlist. Accepts, charges, logs, never prints. See "The quiet filter" |
| `POSPRINTWEB_SHADOW_DELAY_MS` | `900` | Makes a swallowed message take as long as a real print |
| `POSPRINTWEB_REPEAT_HOURS` | `24` | Refuse content already printed in this window, whatever the sender's IP. `0` disables |
| `POSPRINTWEB_GLOBAL_HOURLY` | `30` | Hourly cap across everyone. `0` disables |
| `POSPRINTWEB_GLOBAL_BURST` | `8` | Per-minute cap across everyone — the one that stops a proxy-pool flood. `0` disables |
| `POSPRINTWEB_GLOBAL_BURST_SECONDS` | `60` | The burst window |
| `POSPRINTWEB_HOLD_THRESHOLD` | `20` | Refusals in the window that trigger siege mode. `0` disables |
| `POSPRINTWEB_HOLD_WINDOW_SECONDS` | `300` | How far back refusals are counted |
| `POSPRINTWEB_HOLD_FOR_SECONDS` | `1800` | How long a siege lasts, refreshed while it continues |
| `POSPRINTWEB_HOLD_MAX_QUEUE` | `200` | Ceiling on the hold queue |
| `POSPRINTWEB_POW_BITS` | `18` | Proof-of-work difficulty in leading zero bits. Each bit doubles the sender's cost. `0` disables |
| `POSPRINTWEB_POW_TTL_SECONDS` | `300` | How long a challenge stays solvable |
| `POSPRINTWEB_POW_SECRET` | *(random)* | HMAC key for challenges. Set it to survive restarts or run more than one worker |
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
| `GET /gallery` | — | Approved messages |
| `GET /api/gallery` | — | `?limit=&before=&day=` — keyset paged, `day` is `YYYY-MM-DD` or 422. Never includes `ip` |
| `GET /admin` | — | Review page shell. Inert; data comes from the routes below |
| `GET /api/admin/queue` | `X-Admin-Key` | Printed messages awaiting a decision |
| `POST /api/admin/gallery` | `X-Admin-Key` | `{"id": 12, "action": "approve"\|"hide"\|"reset"}` |
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

**The camera 503s and the log says `ffmpeg exited (-31)`.** A negative code is a
signal, and 31 is `SIGSYS`: seccomp killed it. ffmpeg inherits the unit's
`SystemCallFilter`, and Debian's build calls `set_mempolicy` from libnuma (via
libx265) during library init. The unit allows that one call back explicitly —
if you see this, the running unit predates that fix, so re-run `install.sh`.

The same command working when you run it by hand is the tell: your shell is not
inside the service's sandbox. Confirm it on the **host**, not in the container,
since they share a kernel:

```bash
dmesg -T | grep 'comm="ffmpeg"' | tail -3      # look for sig=31 syscall=NNN
```

**`status=226/NAMESPACE` on start.** Unprivileged LXC. `install.sh` should have
dropped in `10-container.conf`; re-run it. Do not "fix" this by enabling
`nesting=1`.

**Accented characters look wrong on paper.** That is posprint's codepage
(`POSPRINT_CODEPAGE`), not this service. Characters the codepage cannot express
are transliterated rather than dropped.
