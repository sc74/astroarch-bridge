#!/usr/bin/env bash
# Install script per astroarch-bridge su AstroArch / ArchLinux / Debian.
#
# Uso:
#   sudo bash install.sh           (installazione standard)
#   sudo bash install.sh --user $(whoami)  (per usare il proprio utente invece di "astroarch")
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

TARGET_USER="astroarch"
INSTALL_PIP=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) TARGET_USER="$2"; shift 2;;
    --no-pip) INSTALL_PIP=0; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "must be run as root (use sudo)"; exit 1
fi

echo "==> astroarch-bridge install (user: $TARGET_USER)"

# 1. utente
if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
  echo "==> creating user $TARGET_USER"
  useradd -m -s /bin/bash "$TARGET_USER"
fi

# 2. python
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not installed - install it via your package manager"; exit 1
fi

# 3. pip install
if [[ $INSTALL_PIP -eq 1 ]]; then
  echo "==> installing python deps system-wide"
  python3 -m pip install --break-system-packages --upgrade pip || true
  python3 -m pip install --break-system-packages -r "$BACKEND_DIR/requirements.txt"
  python3 -m pip install --break-system-packages "$BACKEND_DIR"
fi

# 4. cartelle
HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
mkdir -p "$HOME_DIR/.config/astroarch-bridge"
mkdir -p "$HOME_DIR/Pictures/Ekos"
chown -R "$TARGET_USER":"$TARGET_USER" "$HOME_DIR/.config/astroarch-bridge" "$HOME_DIR/Pictures/Ekos"
chmod 700 "$HOME_DIR/.config/astroarch-bridge"

# 5. systemd unit
SERVICE_SRC="$SCRIPT_DIR/astroarch-bridge.service"
SERVICE_DST="/etc/systemd/system/astroarch-bridge.service"
cp "$SERVICE_SRC" "$SERVICE_DST"
sed -i "s|^User=.*|User=$TARGET_USER|" "$SERVICE_DST"
sed -i "s|^Group=.*|Group=$TARGET_USER|" "$SERVICE_DST"
sed -i "s|/home/astroarch/|$HOME_DIR/|g" "$SERVICE_DST"

systemctl daemon-reload
systemctl enable astroarch-bridge.service
systemctl restart astroarch-bridge.service

# 6. mostra token + URL su LAN e Tailscale
# La URL Tailscale è quella che il telefono userà fuori casa, quindi va
# stampata in evidenza. La LAN è utile solo da casa.
sleep 1
TOKEN_FILE="$HOME_DIR/.config/astroarch-bridge/token"

# Risolve l'IP "esterno" (Tailscale) — fallback a LAN, poi 127.0.0.1.
TS_IP=""
if command -v tailscale &>/dev/null; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"
fi
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$LAN_IP" ]] && LAN_IP="127.0.0.1"
PRIMARY_IP="${TS_IP:-$LAN_IP}"

if [[ -f "$TOKEN_FILE" ]]; then
  echo
  echo "==> astroarch-bridge installed and running"
  echo
  if [[ -n "$TS_IP" ]]; then
    echo "    URL (Tailscale, da fuori casa):"
    echo "      http://${TS_IP}:8765"
    echo "    URL (LAN, solo sulla stessa WiFi):"
    echo "      http://${LAN_IP}:8765"
  else
    echo "    URL: http://${PRIMARY_IP}:8765"
    echo "    NOTA: Tailscale non rilevato. Per accedere fuori casa,"
    echo "          installa Tailscale: sudo pacman -S tailscale && sudo tailscale up"
  fi
  echo
  echo "    Token: $(cat "$TOKEN_FILE")"
  echo
  echo "    QR di accoppiamento (con IP Tailscale):"
  echo "      curl http://${PRIMARY_IP}:8765/api/system/qr?fmt=png -o pairing-qr.png"
  echo
  echo "    Status: systemctl --user status astroarch-bridge"
  echo "    Logs:   journalctl --user -u astroarch-bridge -f"
else
  echo "==> installed but token file not yet created (service may still be starting)"
fi
