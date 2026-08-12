"""Run the site locally.

    python scripts/dev.py            # talks to a real posprint at :8080
    python scripts/dev.py --fake     # pretends the printer is there

Sets defaults that make sense on a laptop (a local database file, no quiet
hours, generous quotas) so you can poke at the page without a printer, a
tunnel, or root. Nothing in here runs in production; the service is started by
`python -m posprintweb` under systemd.
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE = "--fake" in sys.argv

os.environ.setdefault("POSPRINTWEB_DB", str(ROOT / "dev-prints.db"))
os.environ.setdefault("POSPRINTWEB_KILLSWITCH", str(ROOT / "dev-disabled"))
os.environ.setdefault("POSPRINTWEB_UPSTREAM", "http://127.0.0.1:8080")
os.environ.setdefault("POSPRINTWEB_HOST", "127.0.0.1")
os.environ.setdefault("POSPRINTWEB_PORT", "8000")
os.environ.setdefault("POSPRINTWEB_QUIET_START", "0")
os.environ.setdefault("POSPRINTWEB_QUIET_END", "0")
os.environ.setdefault("POSPRINTWEB_COOLDOWN_SECONDS", "5")
os.environ.setdefault("POSPRINTWEB_PER_IP_DAILY", "50")
os.environ.setdefault("POSPRINTWEB_ADMIN_KEYS", "dev-admin")

from posprintweb import app as appmod  # noqa: E402

if FAKE:

    class FakePrinter:
        async def start(self):
            print("  [fake printer] no real hardware; jobs go to stdout")

        async def stop(self):
            pass

        async def health(self):
            return {"ok": True, "device_present": True, "paper": 80}

        async def print_message(self, *, message, name, columns, when, note=""):
            bar = "=" * columns
            print(f"\n{bar}\n{'INCOMING'.center(columns)}")
            print(f"{when.strftime('%Y-%m-%d %H:%M').center(columns)}\n{bar}")
            print(message)
            print("-" * columns)
            print(f"from: {name or 'someone on the internet'}".rjust(columns))
            print(f"{bar}\n")
            return {"job_id": "fake-1", "state": "done"}

    appmod.upstream = FakePrinter()

if __name__ == "__main__":
    import uvicorn

    print(f"  http://127.0.0.1:{appmod.cfg.port}")
    uvicorn.run(appmod.app, host=appmod.cfg.host, port=appmod.cfg.port, log_level="warning")
