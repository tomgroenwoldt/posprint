#!/usr/bin/env bash
#
# Run this ON THE PROXMOX HOST (not inside the container).
#
#   ./host-setup.sh <CTID>
#
# It loads the usblp kernel driver, installs the udev rule that makes the
# printer node writable from inside an unprivileged container, and adds the
# device passthrough lines to that container's config.
#
# Idempotent: safe to re-run.

set -euo pipefail

CTID="${1:-}"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "  $*"; }
step() { echo; echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "must run as root on the Proxmox host"
[[ -n "$CTID" ]] || die "usage: $0 <CTID>   (e.g. $0 110)"
command -v pct >/dev/null || die "pct not found - this is not a Proxmox host"

CONF="/etc/pve/lxc/${CTID}.conf"
[[ -f "$CONF" ]] || die "no such container config: $CONF (create the LXC first)"

step "Loading the usblp driver"
if lsmod | grep -q '^usblp'; then
  note "already loaded"
else
  modprobe usblp || die "modprobe usblp failed"
  note "loaded"
fi
echo "usblp" > /etc/modules-load.d/usblp.conf
note "pinned in /etc/modules-load.d/usblp.conf so it survives reboot"

step "Installing the udev rule"
install -m 0644 "$(dirname "$0")/99-posprint.rules" /etc/udev/rules.d/99-posprint.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usbmisc --subsystem-match=usb
note "installed and reloaded"

step "Looking for the printer"
echo
lsusb || true
echo
if compgen -G "/dev/usb/lp*" > /dev/null; then
  ls -l /dev/usb/lp*
  DEV=$(ls /dev/usb/lp* | head -1)
  note "found $DEV"
else
  cat >&2 <<'EOF'

  !! No /dev/usb/lp* node exists.

  The printer is either off, unplugged, or does not present a USB
  printer-class interface (bInterfaceClass 07) for usblp to bind to.

  Check which of those it is:

      lsusb                                  # is it enumerated at all?
      lsusb -v 2>/dev/null | grep -B20 'bInterfaceClass.*7 Printer'
      dmesg | grep -i -E 'usblp|usb .*printer'

  If it enumerates but has a vendor-specific interface class, usblp will
  never bind and you need the libusb path instead - say so and the service
  can be pointed at pyusb rather than a device node.

EOF
  DEV=""
fi

step "Configuring passthrough for CT ${CTID}"

# Major 180 is usblp. The wildcard minor covers lp0..lp15 so a replug that
# re-enumerates as lp1 still works.
ALLOW="lxc.cgroup2.devices.allow: c 180:* rwm"

# Bind the *directory*, not the node. Binding /dev/usb/lp0 directly pins one
# specific node: if the printer is replugged and comes back as lp1 the mount is
# stale until the container restarts. Binding the directory lets new nodes show
# up live.
MOUNT="lxc.mount.entry: /dev/usb dev/usb none bind,optional,create=dir"

changed=0
for line in "$ALLOW" "$MOUNT"; do
  if grep -qxF "$line" "$CONF"; then
    note "already present: $line"
  else
    echo "$line" >> "$CONF"
    note "added: $line"
    changed=1
  fi
done

step "Done"
if [[ $changed -eq 1 ]]; then
  echo "  Config changed - restart the container to apply:"
  echo
  echo "      pct stop ${CTID} && pct start ${CTID}"
else
  echo "  No config change needed."
fi
echo
echo "  Then verify from inside the container:"
echo
echo "      pct exec ${CTID} -- ls -l /dev/usb/"
if [[ -n "$DEV" ]]; then
  echo
  echo "  And a smoke test straight from the host (bypasses the service entirely):"
  echo
  echo "      printf '\\x1b@posprint host test\\n\\n\\n\\n\\x1dV\\x42\\x00' > ${DEV}"
fi
echo
