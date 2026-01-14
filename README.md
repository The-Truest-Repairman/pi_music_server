# Pi Music Server

Raspberry Pi 5 music server with automatic CD ripping and streaming playback.

```
USB CD Drive -> ARM (Docker) -> /srv/music/*.flac -> LMS (Docker) -> Squeezelite -> USB DAC
                                                                          ^
                                                              Web Browser (any device)
```

## Features

- **Automatic CD ripping** to FLAC with metadata lookup
- **AcoustID fingerprinting** for unknown albums
- **Web-based tagger** for manual metadata editing
- **Lyrion Music Server** with Material UI for streaming
- **Squeezelite** for USB DAC output

## Prerequisites

- Raspberry Pi 5 with NVMe SSD
- USB optical drive (externally powered recommended)
- USB DAC
- Raspberry Pi OS Lite (64-bit)

---

## Quick Start

### 1. Clone and Configure

```bash
# Clone repository
cd /opt
sudo git clone https://github.com/YOUR_USERNAME/pi-music-server.git arm
sudo chown -R $(whoami):arm /opt/arm

# Create environment file
cp /opt/arm/.env.example /opt/arm/.env
nano /opt/arm/.env  # Fill in your values
```

### 2. Setup Directories

```bash
sudo mkdir -p /srv/music /opt/lms/config
sudo useradd -m -s /bin/bash arm
sudo chown -R 1001:1001 /home/arm /srv/music /opt/arm /opt/lms
sudo ln -s /opt/arm/config /home/arm/config
```

### 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $(whoami)
sudo usermod -aG docker arm
sudo apt install docker-compose-plugin -y
sudo reboot
```

### 4. Start Services

```bash
cd /opt/arm && docker compose up -d
```

### 5. Access Web UIs

| Service | URL | Purpose |
|---------|-----|---------|
| ARM | http://YOUR_IP:8080 | CD ripping management |
| Music Tagger | http://YOUR_IP:5000 | Manual metadata editor |
| LMS Material | http://YOUR_IP:9000/material/ | Music streaming |

---

## Configuration

### ARM Settings

Access ARM at `http://YOUR_IP:8080` (default: admin/password) and verify:
- `RIPMETHOD`: abcde
- `COMPLETED_PATH`: /home/arm/music
- `AUDIO_FORMAT`: flac

### LMS Setup

Access LMS at `http://YOUR_IP:9000`:
1. Set Media Folders to `/music`
2. Install plugins: **Material Skin**, **AutoRescan**

### Squeezelite (Optional - for USB DAC playback)

```bash
sudo apt install squeezelite flac -y
```

Edit `/etc/default/squeezelite`:
```bash
SL_NAME="YourDAC"
SL_SOUNDCARD="hw:CARD=yourcard,DEV=0"
SL_SERVERIP="127.0.0.1"
```

Find your device with `aplay -l` and `squeezelite -l`.

---

## Fingerprint Tagger

When CD metadata lookups fail, albums are saved as "Unknown Artist". The fingerprint tagger uses AcoustID to identify them.

**Get a free API key:** https://acoustid.org/new-application

Add to your `.env` file:
```
ACOUSTID_API_KEY=your-key-here
```

### Usage

```bash
# Preview changes (dry run)
docker exec arm-ripper python3 /etc/arm/config/fingerprint_tagger.py

# Apply changes
docker exec arm-ripper python3 /etc/arm/config/fingerprint_tagger.py --apply
```

### Automatic Mode

Enable in `config/arm.yaml`:
```yaml
BASH_SCRIPT: "/etc/arm/config/post_rip_fingerprint.sh"
```

---

## Music Tagger Web App

Web interface at `http://YOUR_IP:5000` for manually tagging unknown albums:

- MusicBrainz metadata lookup
- Album art via drag & drop or URL
- Audio preview in browser
- Edit artist, album, year, genre, disc number, track titles
- One-click save with automatic file organization

---

## Useful Commands

```bash
# Container management
docker compose ps                    # Check status
docker compose logs -f arm-ripper    # View ARM logs
docker compose restart               # Restart services

# Squeezelite
sudo systemctl status squeezelite
sudo systemctl restart squeezelite

# LMS rescan
echo "rescan" | nc localhost 9090

# ARM diagnostics
docker exec arm-ripper /etc/arm/config/arm-diagnostics.sh
docker exec -it arm-ripper /etc/arm/config/arm-diagnostics.sh --clean
```

---

## Repository Structure

```
/opt/arm/
├── .env                 <- Your secrets (gitignored)
├── .env.example         <- Template for .env
├── docker-compose.yml   <- Container orchestration
├── config/
│   ├── arm.yaml         <- ARM configuration
│   ├── abcde.conf       <- CD ripping settings
│   ├── apprise.yaml     <- Notifications (optional)
│   ├── fingerprint_tagger.py
│   ├── post_rip_fingerprint.sh
│   ├── arm-diagnostics.sh
│   └── tagger-app/
│       └── app.py       <- Web tagger application
└── README.md
```

The `/home/arm/config` directory is symlinked to `/opt/arm/config`.
