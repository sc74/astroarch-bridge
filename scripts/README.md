# scripts/ — utility per deploy e manutenzione del bridge

Script di supporto per l'installazione, manutenzione e backup del bridge
su Raspberry Pi (AstroArch).

## Contenuto

### `install_deps_pacman.sh`

Installa le dipendenze Python del bridge **via pacman** invece che pip.
Usalo quando il sistema ha Python troppo recente per i pacchetti
pinnati in `requirements.txt` (es. **AstroArch con Python 3.14**, dove
`pydantic-core 0.22.2` non compila).

```bash
bash scripts/install_deps_pacman.sh
sudo bash deploy/install.sh --user $(whoami) --no-pip
```

### `backup_rpi.sh`

Crea un `.tar.gz` con tutte le config Ekos/KStars, PHD2, INDI, bridge
+ Tailscale state + lista pacman, escludendo gli indici astrometry
(GB, si riscaricano).

```bash
bash scripts/backup_rpi.sh
# produce: /tmp/astroarch-backup-<hostname>-<timestamp>.tar.gz
```

Da Windows poi: `scp astronaut@<RPi>:/tmp/astroarch-backup-*.tar.gz .`

In caso di reflash, vedi le istruzioni di restore nel
`README.md` accanto ai backup salvati.
