# 📟 E-Paper HAT+ Applications for Raspberry Pi

A growing collection of Python applications for the **WaveShare 2.13" E-Paper HAT+** on Raspberry Pi. Each app is self-contained, uses partial refresh for smooth updates, and is designed to run as a systemd service.

> Maintained by **PP5KX** — Mafra, Santa Catarina, Brazil 🇧🇷

---

## Hardware

| Item | Details |
|---|---|
| Display | WaveShare 2.13" E-Paper HAT+ |
| Resolution | 250 × 122 px |
| Colors | Black & White |
| Interface | SPI |
| Partial refresh | ~0.3 s |
| Full refresh | ~2 s |
| Driver | `epd2in13_V4` |

> ⚠️ This repository targets the **HAT+** variant specifically. The older HAT (tricolor, `epd2in13b_V4`) uses a different driver and does **not** support partial refresh.

---

## Applications

### 🕐 Station Monitor — `station_monitor.py`

A real-time system dashboard showing clock, date, and live hardware metrics.

**Display layout:**
```
┌──────────────────────────────────────────┐
│ RasPi0W-105         Sat, 26 Apr 2026    │
├──────────────────────────────────────────┤
│                                          │
│              22:14:37                    │
│                                          │
├─────────────────┬────────────────────────┤
│ CPU [████░░] 72%│ Temp 54°C             │
│ RAM [███░░░] 45%│ Disk 23%              │
├─────────────────┴────────────────────────┤
│ 192.168.1.105                   PP5KX   │
└──────────────────────────────────────────┘
```

**Features:**
- Clock with seconds, updated every 1 s via partial refresh
- CPU usage, RAM usage, CPU temperature, disk usage
- System hostname, IP address and operator callsign
- Background thread for collecting metrics (non-blocking)
- `--black` flag to invert colors (white text on black background)
- Automatic color inversion every 30 min to prevent permanent ghosting
- Full refresh every 10 min for display health
- Graceful shutdown screen on exit

**Usage:**
```bash
sudo python3 station_monitor.py            # white background (default)
sudo python3 station_monitor.py --black    # black background
sudo python3 station_monitor.py --simulate # save preview to /tmp/epd_preview.png
sudo python3 station_monitor.py --once     # single refresh and exit
```

---

### 📡 XLX Reflector Dashboard — `e-paper_monitor.py`

A live dashboard for [xlxd](https://github.com/LX3JL/xlxd) D-Star/YSF/DMR reflectors. Parses the xlxd log file in real time and displays last heard stations and connected clients.

**Display layout:**
```
┌──────────────────────────────────────────┐
│ XLXBRA            Sat 26/04  22:14:37   │
├──────────────────────────────────────────┤
│ PP5KX-A   [D]  DCS   13:02:03       2s  │
│ PY2ABC-A  [D]  DCS   12:58:11      14s  │
│ PY3XYZ-B  [B]  YSF   12:44:59       5s  │
│ PP1DEF-A  [A]  DMR   12:30:22       8s  │
├──────────────────────────────────────────┤
│ PP5KX-A  PP5KX-B               2 online │
├──────────────────────────────────────────┤
│ 192.168.1.105                   PP5KX   │
└──────────────────────────────────────────┘
```

**Features:**
- Last 4 transmissions with callsign, module, protocol and TX duration
- Live count of connected radio clients (XLX peers/interlinks excluded)
- Parses `/var/log/xlx.log` efficiently via tail read (no full file load)
- Session-aware: resets state on xlxd service restart
- Handles connect/disconnect/reconnect events correctly
- Clock updated every 1 s via partial refresh
- Background log reader thread (non-blocking)
- `--black` flag for inverted color scheme
- Automatic color inversion every 30 min (anti-ghosting)
- Full refresh every 10 min for display health
- Graceful shutdown screen on exit

**Usage:**
```bash
sudo python3 e-paper_monitor.py            # default
sudo python3 e-paper_monitor.py --black    # inverted colors
sudo python3 e-paper_monitor.py --simulate # PNG preview without hardware
sudo python3 e-paper_monitor.py --once     # single refresh and exit
```

**Log file:** `/var/log/xlx.log` (configurable via `XLX_LOG` constant at the top of the file)

---

## Installation

### 1. Clone this repository

```bash
git clone https://github.com/pp5kx/epaper-apps.git /usr/local/bin/epaper-apps
cd /usr/local/bin/epaper-apps
```

### 2. Install the WaveShare driver library

```bash
git clone https://github.com/waveshare/e-Paper.git
cp -r e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd ./waveshare_epd
```

### 3. Install Python dependencies

```bash
pip install pillow psutil
```

### 4. Enable SPI on your Raspberry Pi

```bash
sudo raspi-config
# Interface Options → SPI → Enable
```

### 5. Run as a systemd service

Copy the desired service file and enable it:

```bash
sudo cp e-paper_monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable e-paper_monitor.service
sudo systemctl start e-paper_monitor.service

# Follow the logs
sudo journalctl -u e-paper_monitor -f
```

---

## Display Health & Longevity

E-paper displays can develop permanent ghosting if the same image is shown for extended periods. These apps implement a multi-layer protection strategy:

| Mechanism | Interval | Purpose |
|---|---|---|
| Partial refresh | 1 s | Clock update, minimal stress |
| Full refresh | 10 min | Clears partial refresh residue |
| Color inversion | 30 min | Alternates black/white background to equalize pixel wear |
| `epd.Clear()` | On every full refresh | Ensures clean slate before redraw |

---

## Project Structure

```
epaper-apps/
├── station_monitor.py        # System monitor (clock + hardware metrics)
├── e-paper_monitor.py        # XLX reflector dashboard
├── e-paper_monitor.service   # systemd service file
├── waveshare_epd/            # WaveShare driver library (copied from official repo)
│   ├── epd2in13_V4.py
│   ├── epdconfig.py
│   └── ...
└── README.md
```

---

## Compatibility

| Board | Tested |
|---|---|
| Raspberry Pi Zero W | ✅ |
| Raspberry Pi Zero 2 W | ✅ |
| Raspberry Pi 3B+ | ✅ |
| Raspberry Pi 4 | ✅ |

---

## Roadmap

Ideas for future applications in this repository:

- [ ] **APRS Tracker** — display latest APRS positions heard on a local iGate
- [ ] **Weather Station** — temperature, humidity and pressure from a local sensor
- [ ] **DMR Last Heard** — similar to the XLX dashboard but for BrandMeister/TGIF
- [ ] **Solar/Band conditions** — pull WWV solar flux and band conditions from the web
- [ ] **QSO Logger** — display the last logged contact from an ADIF file

Pull requests and suggestions are welcome.

---

## License

MIT License — feel free to use, modify and share.

---

*73 de PP5KX*
