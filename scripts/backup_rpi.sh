#!/bin/bash
# Esegui questo script sul RPi (EQ8 o Askar). Crea un .tar.gz in /tmp/
# con tutte le config Ekos/KStars/PHD2/bridge e simili.
# Lo script saltare gli astrometry index (pesano GB, si riscaricano).
set -e

HOSTNAME=$(hostname)
TS=$(date +%Y%m%d-%H%M)
OUT="/tmp/astroarch-backup-${HOSTNAME}-${TS}.tar.gz"

cd /
echo "==> Building backup for $HOSTNAME ..."

# Lista path da includere (relativi a /). Esistono solo quelli presenti.
PATHS=()

# === KStars / Ekos ===
[ -d "$HOME/.local/share/kstars" ] && PATHS+=("$HOME/.local/share/kstars")
[ -f "$HOME/.config/kstarsrc" ]    && PATHS+=("$HOME/.config/kstarsrc")
[ -f "$HOME/.config/EkosOptions.conf" ] && PATHS+=("$HOME/.config/EkosOptions.conf")
[ -d "$HOME/.config/kstars" ]      && PATHS+=("$HOME/.config/kstars")

# === PHD2 ===
[ -d "$HOME/.PHDGuidingV2" ]       && PATHS+=("$HOME/.PHDGuidingV2")
[ -f "$HOME/.PHDGuidingV2.conf" ]  && PATHS+=("$HOME/.PHDGuidingV2.conf")
[ -f "$HOME/.config/PHDGuidingV2" ] && PATHS+=("$HOME/.config/PHDGuidingV2")
[ -f "$HOME/.config/PHDGuidingV2.conf" ] && PATHS+=("$HOME/.config/PHDGuidingV2.conf")

# === INDI ===
[ -d "$HOME/.indi" ]               && PATHS+=("$HOME/.indi")
[ -d "/etc/indi" ]                 && PATHS+=("/etc/indi")

# === Astroarch bridge (nostro) ===
[ -d "$HOME/.config/astroarch-bridge" ] && PATHS+=("$HOME/.config/astroarch-bridge")
[ -f "/etc/systemd/system/astroarch-bridge.service" ] && PATHS+=("/etc/systemd/system/astroarch-bridge.service")
[ -d "$HOME/.config/systemd/user" ] && PATHS+=("$HOME/.config/systemd/user")

# === Tailscale (richiede root per state) ===
SUDO_TS=""
if [ -d "/var/lib/tailscale" ] && [ -r "/var/lib/tailscale/tailscaled.state" ]; then
  PATHS+=("/var/lib/tailscale/tailscaled.state")
elif [ -d "/var/lib/tailscale" ]; then
  SUDO_TS="/var/lib/tailscale/tailscaled.state"
fi

# === Lista pacchetti installati (utile in caso di reflash) ===
mkdir -p /tmp/_bk_meta
pacman -Qqe > /tmp/_bk_meta/pacman-explicit.txt 2>/dev/null || true
pacman -Qq  > /tmp/_bk_meta/pacman-all.txt 2>/dev/null || true
ip -4 addr show > /tmp/_bk_meta/network.txt 2>/dev/null || true
tailscale ip -4 > /tmp/_bk_meta/tailscale-ip.txt 2>/dev/null || true
tailscale status > /tmp/_bk_meta/tailscale-status.txt 2>/dev/null || true
systemctl --user list-units --type=service > /tmp/_bk_meta/systemd-user.txt 2>/dev/null || true
hostname > /tmp/_bk_meta/hostname.txt
uname -a > /tmp/_bk_meta/uname.txt
date  -u > /tmp/_bk_meta/backup-date-utc.txt
PATHS+=("/tmp/_bk_meta")

echo "==> Including:"
printf '   %s\n' "${PATHS[@]}"

# Tar (la home utente come "home/<user>" relative path leggibile)
echo "==> Creating $OUT ..."
tar czf "$OUT" --warning=no-file-changed --ignore-failed-read \
  --exclude="*.local/share/kstars/astrometry" \
  --exclude="*.cache" \
  --exclude="*/__pycache__" \
  "${PATHS[@]}" 2>/dev/null || true

# Se Tailscale state richiede root, fai un secondo tar dentro lo stesso file
if [ -n "$SUDO_TS" ]; then
  echo "==> Appending tailscale state (needs sudo) ..."
  echo "astro" | sudo -S tar rzf "$OUT" "$SUDO_TS" 2>/dev/null || \
    echo "   skipped (no sudo or no state)"
fi

rm -rf /tmp/_bk_meta

ls -lh "$OUT"
echo ""
echo "FILE: $OUT"
