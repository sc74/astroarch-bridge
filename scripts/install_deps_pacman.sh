#!/bin/bash
# Install astroarch-bridge dipendenze via PACMAN (Arch Linux ARM).
#
# Quando usarlo:
#   - Sistemi con Python troppo nuovo per il `requirements.txt` pinnato
#     (es. AstroArch con Python 3.14: pydantic-core 0.22.2 non compila
#     perché PyO3 max supported è 3.13).
#   - Vuoi evitare di compilare pacchetti nativi (Pillow, pydantic-core,
#     numpy) che richiedono toolchain Rust/C — usi i wheels pre-compilati
#     mantenuti dai mantenitori Arch ARM.
#
# Uso:
#   bash scripts/install_deps_pacman.sh
#   sudo bash deploy/install.sh --user $(whoami) --no-pip
#
# Lo script:
#   1. rimuove eventuali file watchdog conflittuali da un install pip
#      precedente
#   2. installa via pacman: pydantic, fastapi, Pillow, numpy, astropy,
#      watchdog, websockets, multipart, qrcode, h11
#   3. installa uvicorn via pip --user --break-system-packages
#      (uvicorn e' puro-Python, non richiede compilazione)
#   4. verifica che gli import critici funzionano
set -e

PASSWORD="${SUDO_PASSWORD:-astro}"

echo "==> Cleanup eventuali file watchdog di pip precedente"
echo "$PASSWORD" | sudo -S rm -rf /usr/lib/python*/site-packages/watchdog* 2>/dev/null || true

echo ""
echo "==> Pacman install pacchetti nativi"
echo "$PASSWORD" | sudo -S pacman -S --noconfirm --needed --overwrite '*' \
    python-pip python-pydantic python-pydantic-settings python-fastapi \
    python-pillow python-numpy python-astropy python-watchdog \
    python-websockets python-multipart python-qrcode python-h11

echo ""
echo "==> Pip install uvicorn (pure-Python)"
pip install --break-system-packages --user --quiet "uvicorn[standard]"

echo ""
echo "==> Verifica import critici"
python -c "import fastapi, uvicorn, pydantic, pydantic_settings, astropy, numpy, PIL, websockets, watchdog, qrcode; print('ALL OK')"

echo ""
echo "==> Done. Adesso esegui:"
echo "    sudo bash deploy/install.sh --user \$USER --no-pip"
