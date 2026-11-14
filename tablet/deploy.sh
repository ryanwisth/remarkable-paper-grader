#!/bin/bash
# deploy.sh — stage the XOVI grading stack onto a fresh dev-mode reMarkable Paper Pro.
#
# Run from the Mac with the tablet plugged in over USB.
# Phase 1 asks for the tablet's root password ONCE (from Settings > General >
# Help > About > Copyrights and licenses, under "GPLv3 Compliance") to install
# an SSH key. Everything after that is key-authenticated.
#
# SAFETY PROPERTIES (do not "improve" these away):
#   * XOVI is tethered BY DESIGN. This script never touches systemd, never edits
#     the root partition, never sets up autostart. A reboot always returns the
#     tablet to pure stock. (Autostart on an encrypted RMPP = bootloop + manual
#     recovery, per asivery's own warning.)
#   * Everything lands under /home/root. Nothing under / .
#   * A full backup of the notebook store is pulled to the Mac BEFORE xovi ever
#     starts.
#   * This script does NOT start xovi. That is a separate, deliberate command.

set -euo pipefail

RM="${RM_HOST:-root@10.11.99.1}"   # the tablet over USB; override with RM_HOST
DIST="$(cd "$(dirname "$0")" && pwd)/dist"
BACKUPS="$(cd "$(dirname "$0")" && pwd)/backups"
SSH="ssh -o ConnectTimeout=6 $RM"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

step "Phase 1: install SSH key (you will be asked for the tablet root password once)"
if ssh -o BatchMode=yes -o ConnectTimeout=4 $RM true 2>/dev/null; then
  echo "key auth already works — skipping"
else
  ssh $RM "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" < ~/.ssh/id_ed25519.pub
  ssh -o BatchMode=yes $RM "echo key-auth-verified"
fi

step "Phase 2: device identity"
$SSH "uname -m; cat /etc/os-release 2>/dev/null | grep -E 'IMG_VERSION|VERSION_ID' || cat /usr/share/remarkable/update.conf 2>/dev/null; df -h /home | tail -1"

step "Phase 3: enable SSH over Wi-Fi for future untethered work"
$SSH "rm-ssh-over-wlan on 2>/dev/null && echo wlan-ssh: on || echo 'rm-ssh-over-wlan not present (ok on some builds)'"
$SSH "ip -4 addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*' || echo 'wlan0: no address (fine if Wi-Fi off)'"

step "Phase 4: FULL notebook-store backup to Mac (before anything is installed)"
mkdir -p "$BACKUPS"
TS=$(date +%Y%m%d-%H%M%S)
$SSH "tar czf - -C /home/root/.local/share/remarkable xochitl 2>/dev/null" > "$BACKUPS/xochitl-$TS.tar.gz"
ls -la "$BACKUPS/xochitl-$TS.tar.gz"

step "Phase 5: push the stack"
scp -q "$DIST/xovi-aarch64.tar.gz" $RM:/home/root/
$SSH "cd /home/root && tar xzf xovi-aarch64.tar.gz && rm xovi-aarch64.tar.gz && ls xovi/"

# activate the two modules rm-shot needs; leave the rest inactive
$SSH "cd /home/root/xovi && mv -v inactive-extensions/xovi-message-broker.so extensions.d/ && mv -v inactive-extensions/framebuffer-spy.so extensions.d/"

scp -q "$DIST/rm-shot-aarch64.so" "$DIST/scene-assistant-aarch64.so" $RM:/home/root/xovi/extensions.d/
$SSH "ls -la /home/root/xovi/extensions.d/"

step "Phase 6: rebuild the hashtable (required after adding extensions)"
$SSH "cd /home/root/xovi && ./rebuild_hashtable && echo hashtable-ok"

step "DONE — staged, NOT started."
cat <<EOF

Next (deliberate, separate step):
  ssh $RM '/home/root/xovi/start'
The tablet's UI will restart with the extension environment loaded.
To return to stock at any time:  ssh $RM '/home/root/xovi/stock'
Or simply reboot the tablet — a reboot ALWAYS returns to stock.
EOF
