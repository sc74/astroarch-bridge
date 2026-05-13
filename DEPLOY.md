# Astroarch Interface — Deploy & Install

**Sviluppatore:** Zarletti-Osservatorio Jupiter
**Versione:** 0.1.0
**Data build:** 2026-05-03

---

## Cosa è pronto in `Desktop/AstroArch_Mobile/`

| File / cartella | Cosa è |
|---|---|
| **`AstroarchInterface-v0.1.apk`** | ⭐ APK Android pronto da installare (23 MB) |
| `backend/` | `astroarch-bridge` daemon Python — da installare sul RPi5 |
| `android_app/` | Sorgenti Flutter (per future modifiche) |
| `mockups.html` | Mockup UI (referenza) |

---

## STEP 1 — Deploy backend `astroarch-bridge` sul RPi5

Il bridge gira **affianco** ad AstroArch, non lo modifica. Si collega a `indiserver:7624` e `phd2:4400` già attivi.

### 1a. Copia il backend sul RPi (via Tailscale)

Dalla shell del PC Windows:

```bash
# Tailscale IP del tuo RPi (dalla memoria: 100.74.22.40)
scp -r "C:\Users\Zarletti\Desktop\AstroArch_Mobile\backend" astroarch@100.74.22.40:/tmp/
```

Inserisci la password SSH dell'utente AstroArch.

### 1b. SSH e installa

```bash
ssh astroarch@100.74.22.40
cd /tmp/backend
sudo bash deploy/install.sh --user astroarch
```

L'installer:
- crea/usa l'utente `astroarch`
- installa dipendenze Python (`fastapi`, `uvicorn`, `astropy`, `pillow`, `watchdog`, ecc)
- installa il package `astroarch_bridge`
- copia il file `systemd` `/etc/systemd/system/astroarch-bridge.service`
- avvia il servizio

A fine installazione vedrai:
```
==> astroarch-bridge installed and running
    URL:   http://100.74.22.40:8765
    Token: kJ3xSn9...mZ7TqW   ← ⭐ COPIA QUESTO TOKEN
```

**📋 Salva il token** — ti serve per l'app.

### 1c. Verifica funzionamento

```bash
# Stato servizio
systemctl status astroarch-bridge

# Log live
journalctl -u astroarch-bridge -f

# Test API (sostituisci TOKEN)
curl http://100.74.22.40:8765/healthz
curl -H "Authorization: Bearer TOKEN" http://100.74.22.40:8765/api/system/info
```

Se hai Ekos già attivo con un profilo INDI, dovresti vedere i device:

```bash
curl -H "Authorization: Bearer TOKEN" http://100.74.22.40:8765/api/indi/devices
# {"devices": ["EQMod Mount", "ZWO ASI2600MC", ...]}
```

---

## STEP 2 — Installa APK sul cellulare Android

### 2a. Trasferisci l'APK al cellulare

Tre opzioni, scegli la più comoda:

- **USB**: collega il cellulare al PC, copia `AstroarchInterface-v0.1.apk` nella cartella `Download/` del telefono
- **Cloud**: carica su Google Drive / Dropbox e scarica dal telefono
- **Tailscale Drop / SSH**: se hai Tailscale anche sul cellulare, usa `tailscale file send`

### 2b. Abilita installazione APK

La prima volta Android chiederà di abilitare "Installa app da fonti sconosciute" per il browser/file manager che apre l'APK. Confermalo.

### 2c. Installa

Apri il file APK dal file manager → tap "Installa" → conferma.

L'app appare come **"Astroarch Interface"** nel launcher.

### 2d. Primo accesso

Apri l'app:

| Campo | Valore |
|---|---|
| **HOST (TAILSCALE)** | `100.74.22.40` (o l'IP Tailscale del tuo RPi) |
| **PORTA** | `8765` |
| **TOKEN** | quello mostrato dall'installer (step 1b) |

Tap **CONNETTI**.

L'app:
1. Pinga `/healthz` (deve dire ok)
2. Scarica lo snapshot iniziale via REST
3. Apre due WebSocket (`/ws/state` per stato live, `/ws/frames` per immagini live)
4. Apre la **Dashboard**

---

## STEP 3 — Test sul campo

Suggerito ordine di verifica:

1. **Dashboard** — devi vedere "INDI: connected" e i device attivi (mount, camera, focuser…)
2. **INDI Panel** (drawer → INDI Panel) — clone della Control Panel di Ekos: tutti i driver con tutte le proprietà live
3. **Mount** — verifica che RA/Dec siano gli stessi mostrati in Ekos. Prova un GoTo (con tappeto pulito o park-position al sicuro)
4. **Capture** — imposta esposizione 1s, gain 100, frame Light, tap SCATTA → quando il FITS appare in `~/Pictures/Ekos/`, l'app dovrebbe mostrarlo nella Dashboard preview e nella Live View
5. **Guide** (se PHD2 attivo) — RMS in tempo reale + grafico

---

## Configurazione opzionale

Per cambiare port, host INDI, ecc, modifica `/etc/systemd/system/astroarch-bridge.service`:

```
[Service]
Environment=ASTROARCH_PORT=8765
Environment=ASTROARCH_INDI_HOST=127.0.0.1
Environment=ASTROARCH_INDI_PORT=7624
Environment=ASTROARCH_PHD2_ENABLED=true
Environment=ASTROARCH_PHD2_HOST=127.0.0.1
Environment=ASTROARCH_PHD2_PORT=4400
Environment=ASTROARCH_IMAGES_DIR=/home/astroarch/Pictures/Ekos
Environment=ASTROARCH_LOG_LEVEL=INFO
```

Poi: `sudo systemctl daemon-reload && sudo systemctl restart astroarch-bridge`

---

## Troubleshooting

| Sintomo | Causa probabile | Fix |
|---|---|---|
| App "unreachable" su connect | Tailscale non attivo o IP cambiato | `tailscale status` su RPi e cellulare |
| App connette ma "INDI disconnected" | indiserver non attivo | Avvia un profilo Ekos sul RPi |
| Token rifiutato | Token sbagliato o regenerated | `cat ~/.config/astroarch-bridge/token` su RPi |
| Frame live non arrivano | Cartella images_dir vuota o sbagliata | Verifica `ls ~/Pictures/Ekos/` |
| PHD2 sempre offline | PHD2 non avviato o server disabilitato | Avvia PHD2 e abilita "Tools → Enable Server" |
| `pip install` fallisce su Arch | break-system-packages | `bash install.sh --no-pip` poi `pip install --user --break-system-packages -r requirements.txt` |
| `systemctl restart` fallisce | typo path nel file unit | `journalctl -xeu astroarch-bridge` |

### Log utili

```bash
# Backend
journalctl -u astroarch-bridge -f --since "5 min ago"

# INDI
journalctl --user -u indiserver -f   # se gestito da systemd user

# Bridge dump dello stato corrente
curl -H "Authorization: Bearer TOKEN" http://100.74.22.40:8765/api/system/snapshot | jq
```

---

## Disinstallazione (se serve)

**Sul RPi:**
```bash
sudo systemctl disable --now astroarch-bridge
sudo rm /etc/systemd/system/astroarch-bridge.service
sudo systemctl daemon-reload
sudo pip uninstall astroarch-bridge
rm -rf ~/.config/astroarch-bridge
```

**Sul cellulare:** disinstalla "Astroarch Interface" come una qualsiasi app.

---

## Cosa fa l'app

| Schermata | Funzioni |
|---|---|
| **Login** | Host/porta/token, persistenza credenziali, toggle tema Pro/Notte |
| **Dashboard** | Target attivo + 4 card live (Mount/Camera/Guide/Focus) + preview ultimo scatto + telemetria |
| **Mount** | GoTo manuale RA/Dec, joypad slew con rate, park/sync/abort, tracking sidereal/lunar/solar |
| **Capture** | Exp/gain/offset/binning/frame type + cooler controls, scatto e abort |
| **Live View** | JPEG ultimo frame con zoom pinch + metadata HFR/stelle/exposure |
| **Focus** | Posizione assoluta, movimento manuale ±1000/100/10, autofocus, abort |
| **Align** | Stato ultimo frame (preview, HFR, stelle), placeholder solver |
| **Guide** | RMS total/RA/DEC, grafico errori live (fl_chart), start/stop/dither/calibration |
| **Observatory** | Weather params, dome shutter open/close, dust cap, flat panel intensity |
| **INDI Panel** | Clone esatto della Control Panel di Ekos: tutti i driver, tutte le proprietà, set live di Switch/Number/Text |
| **Files** | Browser FITS recenti (ricorsivo), thumbnail, preview full-screen |
| **Logs** | Stream messaggi INDI/Ekos in tempo reale |

Tutte le schermate sono **clone live**: ogni cambiamento su Ekos è propagato all'app via WebSocket entro ~50 ms.

---

## Sviluppi futuri (non in v0.1)

- Sequencer multi-target (Ekos Scheduler integration via DBus)
- Plate solving trigger via app
- Analyze timeline sessione
- Notifiche push (frame finito, sequenza completata, weather alert)
- Donazione codice a [devDucks/astroarch](https://github.com/devDucks/astroarch) come integrazione ufficiale

---

🌙 **Buone osservazioni!**
— Astroarch Interface · Zarletti-Osservatorio Jupiter
