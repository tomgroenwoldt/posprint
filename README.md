# posprint

An HTTP service for a USB-attached ESC/POS receipt printer, designed to run in an
LXC container on Proxmox and give a USB-only printer an IP address.

```
curl -X POST http://10.0.0.50:8080/print/text \
  -H "X-API-Key: $KEY" \
  -d '{"text":"Hello from anywhere on the LAN"}'
```

---

## What is in here

| Path | What it is |
| --- | --- |
| `posprint/`, `tests/`, `deploy/` | This service. Talks to the USB printer. **LAN only** — the API key it uses unlocks raw ESC/POS and the cash drawer, so it must never be exposed to the internet. |
| `scripts/` | Standalone tools that talk to the API. `braille_print.py` decodes Unicode braille art back into the bitmap it encodes and prints it as graphics — the printer has no glyphs for `U+2800`–`U+28FF`. |
| [`web/`](web/README.md) | `posprint-web`, an optional public page that lets strangers print a short message. It proxies to this service so that key stays server-side. |

They deploy to separate containers with separate installers. You do not need
`web/` to use the printer; skip it entirely if the LAN API is all you want.

`pytest` from this directory runs both suites (152 tests).

---

## How it works

```
HTTP client  ──▶  FastAPI  ──▶  spooler thread  ──▶  /dev/usb/lp0  ──▶  printer
                              (one job at a time)         │
                                                    bind mount
                                                          │
                                              Proxmox host /dev/usb
```

Three design decisions worth knowing before you change anything:

**The kernel `usblp` driver, not libusb.** A USB printer-class device gets a
`/dev/usb/lp0` character node for free, and printing is then just `write()`.
This avoids libusb in the container, avoids detaching the kernel driver, and
makes container passthrough a plain bind mount. The cost is that it only works
if your printer exposes `bInterfaceClass 07`; see [Troubleshooting](#no-devusblp-appears).

**The `/dev/usb` *directory* is bind-mounted, not the `lp0` node.** Binding the
node pins one specific device: replug the printer, it re-enumerates as `lp1`,
and the mount is stale until you restart the container. Binding the directory
lets new nodes appear live, and the service globs `/dev/usb/lp*` at job time
rather than caching a path.

**One writer thread.** A thermal printer is a single non-reentrant resource.
Two concurrent writes to the device interleave and produce confetti. Every
request funnels through one spooler thread; concurrency is handled by queueing,
not by locking at the device.

---

## Install

### 1. Create the container (on the Proxmox host)

Debian 12 is what this is tested against. Unprivileged is fine.

Find and fetch a template first — the exact version string changes over time,
so list what your host offers rather than copying one from here:

```bash
pveam update
pveam available --section system | grep -E 'debian-1[23]'
pveam download local debian-12-standard_<version>_amd64.tar.zst
```

Then create the container, substituting the same template name:

```bash
pct create 110 local:vztmpl/debian-12-standard_<version>_amd64.tar.zst \
  --hostname posprint \
  --cores 1 --memory 512 --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 \
  --start 1
```

`nesting=1` is deliberately not enabled — see
[the service fails with 226/NAMESPACE](#the-service-fails-to-start-with-226namespace).

### 2. Wire up the USB passthrough (on the Proxmox host)

Proxmox does not ship `git`, so either install it or pull a tarball:

```bash
apt-get install -y git
git clone https://github.com/tomgroenwoldt/posprint.git /root/posprint
```

```bash
# no-git alternative
curl -sL https://github.com/tomgroenwoldt/posprint/archive/refs/heads/main.tar.gz \
  | tar xz -C /root && mv /root/posprint-main /root/posprint
```

```bash
/root/posprint/deploy/host-setup.sh 110
```

This loads `usblp` and pins it for reboot, installs the udev rule, adds the two
passthrough lines to `/etc/pve/lxc/110.conf`, and tells you to restart the
container. It is idempotent.

The lines it adds:

```
lxc.cgroup2.devices.allow: c 180:* rwm
lxc.mount.entry: /dev/usb dev/usb none bind,optional,create=dir
```

Major 180 is `usblp`; the wildcard minor covers `lp0`–`lp15`.

> **On the udev rule and `MODE="0666"`** — in an unprivileged container, root
> maps to host uid 100000, so the default `root:lp 0660` node is unwritable from
> inside no matter which group the service joins. 0666 on a receipt printer on a
> home LAN is the pragmatic trade. If you'd rather keep 0660, use a privileged
> container (`--unprivileged 0`) and drop the `MODE=` from
> `deploy/99-posprint.rules`.

Restart and verify the node made it in:

```bash
pct stop 110 && pct start 110
pct exec 110 -- ls -l /dev/usb/
```

### 3. Install the service (inside the container)

From the Proxmox host:

```bash
pct exec 110 -- bash -c "apt-get update -qq && apt-get install -y -qq git && \
  git clone https://github.com/tomgroenwoldt/posprint.git /root/posprint && \
  bash /root/posprint/deploy/install.sh"
```

The installer creates `/opt/posprint` with a virtualenv, a `posprint` system
user, a generated API key in `/etc/posprint.env`, and a systemd unit. It prints
the URL and key when it finishes.

To upgrade later, pull and re-run — it replaces the code and keeps your config:

```bash
pct exec 110 -- bash -c "git -C /root/posprint pull && \
  bash /root/posprint/deploy/install.sh"
```

### 4. Confirm

```bash
curl -s http://<container-ip>:8080/health | jq
curl -X POST http://<container-ip>:8080/print/test -H "X-API-Key: $KEY"
```

The self-test page prints a column ruler, every text style, an accented-character
sample, a barcode and a QR code — enough to diagnose most setup problems at a
glance. Interactive API docs are at `http://<container-ip>:8080/docs`.

---

## Configuration

All via `/etc/posprint.env`; `systemctl restart posprint` to apply.

| Variable | Default | Notes |
|---|---|---|
| `POSPRINT_DEVICE` | *(empty)* | Empty auto-discovers `/dev/usb/lp*`. Leave it empty. |
| `POSPRINT_PAPER_MM` | `80` | `58` or `80`. Sets dot width and column count. |
| `POSPRINT_COLUMNS` | *(from paper)* | Override if your printer disagrees. |
| `POSPRINT_DOTS` | *(from paper)* | Override print head width in dots. |
| `POSPRINT_CODEPAGE` | `cp858` | cp850 + euro sign. See [garbled characters](#accented-characters-print-as-garbage). |
| `POSPRINT_HOST` / `POSPRINT_PORT` | `0.0.0.0` / `8080` | |
| `POSPRINT_API_KEY` | *(generated)* | Empty disables auth entirely. |
| `POSPRINT_CHUNK_BYTES` | `4096` | Write chunk size. |
| `POSPRINT_CHUNK_DELAY_MS` | `0` | Raise to 20–50 if long receipts garble. |
| `POSPRINT_QUEUE_MAX` | `100` | Queue depth before returning 503. |
| `POSPRINT_AUTO_INIT` | `true` | Send `ESC @` before each job. |
| `POSPRINT_AUTO_CUT` | `true` | Cut after each job unless overridden. |

Paper profiles: 58mm → 384 dots / 32 columns, 80mm → 576 dots / 48 columns.

---

## API

Every endpoint except `/health` needs `X-API-Key: <key>` (or
`Authorization: Bearer <key>`).

Status codes: **200** printed, **202** queued (fire-and-forget or still in
flight), **422** bad request, **502** printer refused, **503** printer
unreachable or queue full. A job id always comes back, so anything can be
polled at `/jobs/{id}`.

### `POST /print/text`

```bash
curl -X POST http://printer:8080/print/text -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Order up!","align":"center","width":2,"height":2,"bold":true}'
```

### `POST /print/receipt`

```bash
curl -X POST http://printer:8080/print/receipt -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{
    "title": "Groenwoldt Cafe",
    "header_lines": ["Order #1042", "2026-08-12 14:30"],
    "items": [
      {"name": "Espresso", "qty": 2, "unit_price": 2.50},
      {"name": "Croissant", "qty": 1, "total": 3.00, "note": "warmed"}
    ],
    "subtotal": 8.00, "tax": 1.68, "total": 9.68,
    "paid": 10.00, "change": 0.32,
    "currency": "EUR",
    "qr": "https://example.com/order/1042",
    "footer_lines": ["Thank you!"],
    "open_drawer": false
  }'
```

Item totals are computed from `qty × unit_price` when `total` is omitted.

### `POST /print` — block documents

The general form. `blocks` is an ordered list; each has a `type` and an optional
`align`.

| Type | Fields |
|---|---|
| `text` | `text`, `bold`, `underline` (0–2), `invert`, `width`/`height` (1–8), `font` (`a`/`b`), `wrap` |
| `columns` | `left`, `right`, `bold` — right side flushed to the margin |
| `rule` | `char` |
| `feed` | `lines` |
| `barcode` | `data`, `symbology`, `height`, `width` (2–6), `hri` |
| `qr` | `data`, `size` (1–16), `ecc` (`L`/`M`/`Q`/`H`) |
| `image` | `data_base64`, `max_width`, `dither` |
| `cut` | `partial`, `feed_before` |
| `drawer` | `pin`, `on_ms`, `off_ms` |
| `raw` | `data_base64` — arbitrary ESC/POS |

Symbologies: `upca`, `upce`, `ean13`, `ean8`, `code39`, `itf`, `codabar`,
`code93`, `code128`. CODE128 gets the `{B` code-set prefix added automatically.

```bash
curl -X POST http://printer:8080/print -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{
    "blocks": [
      {"type":"text","text":"SHELF LABEL","align":"center","bold":true,"width":2},
      {"type":"rule","char":"="},
      {"type":"columns","left":"Widget, blue","right":"12.99"},
      {"type":"barcode","data":"5901234123457","symbology":"ean13"},
      {"type":"feed","lines":2}
    ]
  }'
```

An explicit `cut` block suppresses the automatic one, so you get exactly one cut.

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `POST /print/image` | multipart upload; scaled to paper width, Floyd–Steinberg dithered |
| `POST /print/raw` | base64 ESC/POS passthrough |
| `POST /print/test` | self-test page |
| `POST /drawer` | kick the cash drawer |
| `GET /health` | unauthenticated; `state`, device presence, paper/error bits, queue depth |
| `GET /jobs` `GET /jobs/{id}` | recent job history, including a `reason` on failures |

Every print endpoint accepts `wait` (default `true`) and `timeout` (seconds).
Set `"wait": false` for fire-and-forget.

### Printer state

`GET /health` reports one authoritative `state`, and returns 200 only for
`ready`:

| `state` | Meaning | HTTP |
|---|---|---|
| `ready` | Will print, as far as can be known in advance | 200 |
| `out_of_paper` | Device is fine, the roll is empty | 503 |
| `offline` | No device node: unplugged, powered off, no permission | 503 |

The distinction is load-bearing rather than cosmetic. **A thermal printer with
no paper still accepts bytes over USB** — the write succeeds, the data lands in
the printer's buffer, and nothing reaches paper. Without an explicit check every
job reports success and silently produces nothing. So the spooler reads the
status byte before each job and fails it with `reason: "out_of_paper"` instead.

Printers that do not implement `LPGETSTATUS` report paper as unknown rather
than empty, and are assumed willing — refusing to print at all would be the
worse failure.

A failed job carries both a human `error` and a stable `reason`
(`out_of_paper`, `offline`, `write_failed`, `error`) so callers can branch
without parsing prose.

---

## Home Assistant

`configuration.yaml`:

```yaml
rest_command:
  print_receipt:
    url: "http://10.0.0.50:8080/print/text"
    method: POST
    headers:
      X-API-Key: !secret posprint_key
      Content-Type: application/json
    payload: '{"text": "{{ message }}", "align": "center"}'
```

```yaml
actions:
  - action: rest_command.print_receipt
    data:
      message: "Doorbell rang at {{ now().strftime('%H:%M') }}"
```

`rest_command` has no config flow, so this is one of the few things that really
does belong in `configuration.yaml`. Reload with `rest_command.reload` rather
than restarting.

Printing **through** posprint rather than driving the USB device from Home
Assistant directly is worth the extra hop: only one process can hold
`/dev/usb/lp0`, and posprint's spooler is what stops two jobs interleaving into
one garbled receipt. It also means the paper and offline states above are
visible to your automations.

---

## Troubleshooting

### No `/dev/usb/lp*` appears

Work down this list on the Proxmox host:

```bash
lsusb                                    # enumerated at all?
lsmod | grep usblp                       # driver loaded?
dmesg | grep -iE 'usblp|usb.*printer'    # what did the kernel say?
lsusb -v 2>/dev/null | grep -B20 'bInterfaceClass.*7 Printer'
```

If it enumerates but has **no printer-class interface**, `usblp` will never bind
and no amount of udev will help — that printer needs the libusb/pyusb path
instead. Say so and the device layer can be swapped; the rest of the service is
unaffected.

If the node exists on the host but not in the container, the container was
started before the node existed. `pct stop 110 && pct start 110`.

### The service fails to start with 226/NAMESPACE

```
Failed to set up mount namespacing: /run/systemd/unit-root/proc: Permission denied
Failed at step NAMESPACE spawning /opt/posprint/venv/bin/python: Permission denied
status=226/NAMESPACE
```

The unit's `ProtectSystem`/`PrivateTmp`/`ProtectKernelTunables` hardening needs a
private mount namespace, which an unprivileged LXC cannot create. `install.sh`
detects containers and drops in `deploy/posprint-container.conf` to disable
exactly those directives; if you see this, the drop-in is missing:

```bash
ls /etc/systemd/system/posprint.service.d/
bash /root/posprint/deploy/install.sh   # re-run; it will install it
```

Setting `--features nesting=1` on the container also makes the error go away,
but don't: it loosens the container's isolation to buy in-container hardening
that duplicates what the container already provides. A single-purpose
unprivileged LXC running one non-root service with an empty capability set is
already the security boundary. `NoNewPrivileges`, `CapabilityBoundingSet=` and
every seccomp-based restriction keep working without a mount namespace.

### Permission denied on the device

The udev rule didn't apply. Check `ls -l /dev/usb/lp0` on the **host** — it
should be `crw-rw-rw-`. Re-run `udevadm control --reload-rules && udevadm trigger`,
or replug the printer.

### Accented characters print as garbage

Wrong code page. Try `POSPRINT_CODEPAGE=cp437` (the most universally supported
table on no-name printers), then `cp850`, then `cp1252`. Print `/print/test`
after each — it includes an accented sample row for exactly this.

Characters the code page genuinely lacks degrade gracefully: `€` → `EUR` only
when the table has no euro sign, `ö` → `o` rather than failing the job.

### Long receipts come out garbled or truncated

The printer's input buffer is overrunning. Set `POSPRINT_CHUNK_DELAY_MS=20`
(then 50) in `/etc/posprint.env` and restart.

### Nothing cuts, or it cuts through the text

Some printers only support full cuts — send `{"type":"cut","partial":false}`.
If it cuts through the last lines, raise `feed_before`: the cutter sits about
15mm above the print head and needs the paper advanced past it first.

### QR codes don't print

Native `GS ( k` is well supported but not universal. If yours ignores it,
render the QR client-side and send it through `/print/image`.

### Check the logs

```bash
journalctl -u posprint -f
```

### Bypass the service entirely

If in doubt about whether the problem is software or hardware, write to the
device directly from the Proxmox host:

```bash
printf '\x1b@hello\n\n\n\n\x1dV\x42\x00' > /dev/usb/lp0
```

If that doesn't print, the problem is below this service.

---

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx
PYTHONPATH=. .venv/bin/python -m pytest tests -q
```

55 tests, no hardware required — the device is stood in for by an ordinary file,
so the full spooler-to-device path runs for real. The suite asserts on exact
byte sequences, because ESC/POS mistakes fail silently as a blank receipt.

The package imports cleanly on Windows (`fcntl` is loaded lazily) so the encoder
and renderers can be developed and tested off-target.
