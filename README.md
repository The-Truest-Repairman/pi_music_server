# Pi Music Server Setup Guide

Raspberry Pi 5 music server with automatic CD ripping and streaming playback.

## Architecture

```
USB CD Drive -> ARM (Docker) -> /srv/music/*.flac -> LMS (Docker) -> Squeezelite -> USB DAC -> Amplifier
                                                                          ^
                                                              Web Browser (any device)
```

## Prerequisites

- Raspberry Pi 5 with NVMe HAT/SSD, Raspberry Pi OS Lite (64-bit)
- USB optical drive (externally powered recommended)
- USB DAC (Peachtree Nova 500)
- Ethernet connection

---

## 0. Credentials

| System | Username | Password |
|--------|----------|----------|
| Raspberry Pi | `<PI_USERNAME>` | `<PI_PASSWORD>` |
| ARM Web UI | admin | `<ARM_PASSWORD>` |

Hostname: `<PI_HOSTNAME>`

> **Note:** Set your actual values in the `.env` file (copy from `.env.example`).

---

## 1. Initial Pi Setup

Flash Raspberry Pi OS Lite (64-bit) using Raspberry Pi Imager with SSH enabled, hostname `<PI_HOSTNAME>`, username `<PI_USERNAME>`.

```bash
ssh <PI_USERNAME>@<PI_HOSTNAME>.local
sudo apt update && sudo apt upgrade -y
sudo dpkg-reconfigure tzdata
```

---

## 2. Create Directory Structure

```bash
sudo mkdir -p /srv/music /opt/arm /opt/lms/config
sudo useradd -m -s /bin/bash arm
sudo chown -R 1001:1001 /home/arm /srv/music /opt/arm /opt/lms
sudo chmod 755 /srv/music
```

---

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker <PI_USERNAME>
sudo usermod -aG docker arm
sudo apt install docker-compose-plugin -y
sudo reboot
```

Verify after reboot: `docker run hello-world`

---

## 4. Docker Compose Configuration

The `docker-compose.yml` is located at `/opt/arm/docker-compose.yml`:

```yaml
services:
  arm:
    image: automaticrippingmachine/automatic-ripping-machine:latest
    container_name: arm-ripper
    restart: unless-stopped
    dns:
      - 8.8.8.8
      - 1.1.1.1
    environment:
      - ARM_UID=1001
      - ARM_GID=1001
      - TZ=America/Chicago
    volumes:
      - /home/arm:/home/arm
      - /home/arm/config:/etc/arm/config
      - /srv/music:/home/arm/music
    devices:
      - /dev/sr0:/dev/sr0
      - /dev/sg0:/dev/sg0
    ports:
      - "8080:8080"
      - "5000:5000"
    privileged: true
    command: >
      bash -c "pip3 install --quiet pyacoustid mutagen musicbrainzngs flask pillow requests &&
               python3 /etc/arm/config/tagger-app/app.py &
               /sbin/my_init"

  lms:
    image: lmscommunity/lyrionmusicserver
    container_name: lms
    restart: unless-stopped
    dns:
      - 8.8.8.8
      - 1.1.1.1
    ports:
      - "9000:9000"
      - "9090:9090"
      - "3483:3483/tcp"
      - "3483:3483/udp"
    volumes:
      - /opt/lms/config:/config
      - /srv/music:/music:ro
    environment:
      - PUID=1001
      - PGID=1001
      - TZ=America/Chicago
```

**Notes:**
- `/home/arm:/home/arm` volume is required - ARM checks this on startup
- `/home/arm/config` is a **symlink** to `/opt/arm/config` (see Repository Structure)
- `dns` settings prevent Tailscale MagicDNS conflicts
- `command` installs Python packages and starts the tagger app on container start
- Ports: 8080 (ARM), 5000 (Tagger), 9000 (LMS web), 9090 (LMS CLI), 3483 (player discovery)

Find optical drive: `sudo apt install lsscsi -y && lsscsi -g`

Start services:
```bash
cd /opt/arm && docker compose up -d
```

---

## 5. Configure ARM

Access: `http://<PI_IP_ADDRESS>:8080` (admin/password)

Verify in Settings:
- `RIPMETHOD`: abcde
- `COMPLETED_PATH`: /home/arm/music
- `AUDIO_FORMAT`: flac

Or edit directly: `nano /opt/arm/config/arm.yaml`

### abcde.conf Metadata Settings

Key settings in `/opt/arm/config/abcde.conf`:
```bash
CDDBMETHOD=cddb,cdtext           # CDDB + CD-Text (ARM handles MusicBrainz separately)
CDDBURL="http://gnudb.gnudb.org/~cddb/cddb.cgi"
HELLOINFO="`whoami`@`hostname`"  # Required by gnudb
CDDBPROTO=6                      # UTF-8 encoding
ACTIONS=musicbrainz,read,encode,tag,move,clean,getalbumart,embedalbumart  # No playlist
```

Reset to defaults if needed:
```bash
docker exec arm-ripper cat /etc/abcde.conf > /opt/arm/config/abcde.conf
```

---

## 6. Configure LMS

Access: `http://<PI_IP_ADDRESS>:9000`

1. Set Media Folders: `/music`
2. Install plugins (Settings -> Plugins):
   - **Material Skin** - Modern web UI
   - **AutoRescan** - Auto-detects new files
   - **Rescan Music Library** - Scheduled daily rescan

Material UI: `http://<PI_IP_ADDRESS>:9000/material/`

---

## 7. Configure Squeezelite

```bash
sudo apt install squeezelite flac -y
aplay -l        # Find USB DAC
squeezelite -l  # List devices
```

Edit `/etc/default/squeezelite`:
```bash
SL_NAME="Peachtree"
SL_SOUNDCARD="hw:CARD=nova500,DEV=0"
SL_SERVERIP="127.0.0.1"  # Required - LMS in Docker can't be auto-discovered
```

Auto-restart on DAC power cycle:
```bash
sudo mkdir -p /etc/systemd/system/squeezelite.service.d
cat << 'CONF' | sudo tee /etc/systemd/system/squeezelite.service.d/restart.conf
[Service]
Restart=always
RestartSec=10
CONF

sudo systemctl daemon-reload
sudo systemctl enable squeezelite
sudo systemctl restart squeezelite
```

Set DAC volume:
```bash
amixer -c nova500 set 'Peachtree nova500',0 100%
amixer -c nova500 set 'Peachtree nova500',1 100%
```

---

## 8. Set Static IP

```bash
ip route | grep default  # Note gateway
sudo nano /etc/dhcpcd.conf
```

Add:
```
interface eth0
static ip_address=<PI_IP_ADDRESS>/24
static routers=<ROUTER_IP>
static domain_name_servers=<ROUTER_IP> 8.8.8.8
```

---

## 9. Fingerprint Tagger (Unknown Album Recovery)

When CD database lookups fail, ARM saves rips as "Unknown Artist/Unknown Album". The fingerprint tagger identifies these using AcoustID acoustic fingerprinting.

### Files
| File | Purpose |
|------|---------|
| `/opt/arm/config/fingerprint_tagger.py` | Main identification script |
| `/opt/arm/config/post_rip_fingerprint.sh` | Post-rip automation hook |
| `/home/arm/logs/fingerprint_tagger.log` | Log file |

### Manual Usage
```bash
# Preview (dry run)
docker exec arm-ripper python3 /etc/arm/config/fingerprint_tagger.py

# Apply changes
docker exec arm-ripper python3 /etc/arm/config/fingerprint_tagger.py --apply

# Specific folder
docker exec arm-ripper python3 /etc/arm/config/fingerprint_tagger.py "/home/arm/music/Unknown Artist/Unknown Album_abc123" --apply
```

### Automatic Mode
Enabled via `BASH_SCRIPT` in `arm.yaml`:
```yaml
BASH_SCRIPT: "/etc/arm/config/post_rip_fingerprint.sh"
```

Runs automatically after each rip. Check logs:
```bash
docker exec arm-ripper tail -50 /home/arm/logs/fingerprint_tagger.log
```

### Safety Thresholds
- 80% confidence per track
- 70% of tracks must be identified
- 70% agreement on artist

AcoustID API key: Set `ACOUSTID_API_KEY` in your `.env` file (get a free key at https://acoustid.org/new-application)

---

## 10. Music Tagger Web App

Web interface for manually tagging unknown albums with MusicBrainz lookup.

**Access:** `http://<PI_IP_ADDRESS>:5000`

### Features
- Lists all albums in "Unknown Artist" folder
- **MusicBrainz search** with multiple candidates and track count matching
- Edit artist, album, year, genre, **disc number**, track titles
- **Album art preview** - shows existing embedded art
- Add album art via drag & drop or URL
- **Audio preview** - play tracks in browser
- **Delete album** with optional backup
- Edit history with undo support
- Search entire music library
- One-click save: tags files, creates folders, moves files, triggers LMS rescan

### Usage
1. Open `http://<PI_IP_ADDRESS>:5000`
2. Click an unknown album
3. (Optional) Use "Fetch Metadata from Web" to search MusicBrainz
4. Fill in Artist & Album name
5. (Optional) Add track names, year, genre, disc number
6. Drag album cover image or paste URL
7. Click **Save & Organize**

### Files
- `/opt/arm/config/tagger-app/app.py` - Flask web application
- Port 5000 exposed in docker-compose.yml

---

## 11. Recovering from Failed/Interrupted Rips

If the Pi shuts down or crashes during a rip, you may end up with corrupted files, incomplete albums, or stale state that causes problems on subsequent rips.

### How ARM and abcde Work Together

1. **ARM** (Python) handles disc detection, database tracking, and job management
2. **abcde** (shell script) does the actual ripping, encoding, tagging, and file moving
3. They run somewhat independently - ARM can crash while abcde continues

### Key Locations

| Location | Purpose |
|----------|---------|
| `/home/arm/abcde.*` | Temp folders for in-progress rips (inside container) |
| `/home/arm/db/arm.db` | ARM's SQLite database tracking all jobs |
| `/srv/music/` | Final destination for completed rips |

### The Resume Problem

abcde creates a temp folder like `/home/arm/abcde.73083708/` containing:
- `status` file tracking which tracks are done
- `.wav` files (raw ripped audio)
- `.flac` files (encoded, before moving)

**If a rip is interrupted and you re-insert the CD**, abcde finds the old temp folder and **resumes** from where it left off. This causes problems if:
- You deleted files from `/srv/music/` (abcde thinks they exist)
- Some tracks were partially written (corrupted files)

### Signs of a Failed Rip

1. **Abnormally small files** - A FLAC track should be 20-35MB. If one is 1-2MB, it's truncated.
2. **Missing tracks** - Album has fewer files than expected
3. **ARM shows error but abcde continued** - Check logs for "ARM has encountered an error and stopping"
4. **Stale "active job" in ARM UI** - Job stuck but no ripping happening

### How to Check for Problems

```bash
# List any leftover abcde temp folders
docker exec arm-ripper ls -la /home/arm/ | grep abcde

# Check temp folder contents (replace ID with actual folder name)
docker exec arm-ripper ls -la /home/arm/abcde.*/
docker exec arm-ripper cat /home/arm/abcde.*/status

# Check file sizes for an album (look for outliers)
du -h "/srv/music/Artist Name/Album Name/"*.flac | sort -h

# Check ARM database for stuck jobs
docker exec arm-ripper python3 -c "
import sqlite3
conn = sqlite3.connect('/home/arm/db/arm.db')
cur = conn.cursor()
cur.execute(\"SELECT job_id, title, status FROM job ORDER BY job_id DESC LIMIT 10\")
for row in cur.fetchall(): print(row)
"
```

### Clean Slate Recovery

When in doubt, delete everything related to the failed rip and start fresh:

```bash
# 1. Delete abcde temp folder(s)
docker exec arm-ripper rm -rf /home/arm/abcde.*

# 2. Delete partial/corrupted album from music library
sudo rm -rf "/srv/music/Artist Name/Album Name"

# 3. Re-insert CD and let ARM rip from scratch
```

The ARM database will create a new job entry - old entries marked "success" are harmless.

### Diagnostics Script

Run the diagnostics script to check ARM health and optionally clean up:

```bash
# Check status (read-only)
docker exec arm-ripper /etc/arm/config/arm-diagnostics.sh

# Check and clean up stale files (with confirmation prompts)
docker exec -it arm-ripper /etc/arm/config/arm-diagnostics.sh --clean
```

The script checks:
- abcde temp folders
- Active rip processes (warns before cleaning if rip in progress)
- Leftover WAV files
- Stuck database jobs
- Lock files
- CD drive status
- Raw/transcode directories

### Preventing Issues

- Use an **externally powered USB CD drive** to avoid power fluctuations
- Avoid shutting down the Pi while ripping
- If you must restart, wait for the current rip to complete or manually clean up first

---

## Quick Reference

| Service | URL |
|---------|-----|
| ARM | http://\<PI_IP_ADDRESS\>:8080 |
| Music Tagger | http://\<PI_IP_ADDRESS\>:5000 |
| LMS Material | http://\<PI_IP_ADDRESS\>:9000/material/ |

| Command | Purpose |
|---------|---------|
| `cd /opt/arm && docker compose ps` | Check containers |
| `cd /opt/arm && docker compose logs -f arm-ripper` | ARM logs |
| `cd /opt/arm && docker compose logs -f lms` | LMS logs |
| `cd /opt/arm && docker compose restart` | Restart services |
| `cd /opt/arm && docker compose down && docker compose up -d` | Full restart |
| `sudo systemctl status squeezelite` | Player status |
| `sudo systemctl restart squeezelite` | Restart player |
| `echo "rescan" \| nc localhost 9090` | Trigger LMS rescan |
| `ls /srv/music` | List ripped albums |
| `lsscsi -g` | List CD drives |
| `aplay -l` | List audio devices |
| `docker exec arm-ripper /etc/arm/config/arm-diagnostics.sh` | ARM health check |
| `docker exec -it arm-ripper /etc/arm/config/arm-diagnostics.sh --clean` | Clean stale files |

---

## Troubleshooting

**Docker permission denied:** Full reboot required after `usermod -aG docker <PI_USERNAME>`. Logout/login is NOT enough.

**ARM permission error:** `sudo chown -R 1001:1001 /home/arm /srv/music` then restart containers.

**"Mounting failed" for audio CDs:** Normal - audio CDs have no filesystem. ARM still rips correctly.

**Player not showing after DAC power cycle:** Wait 20-30 seconds for auto-restart, or: `sudo systemctl restart squeezelite`

**No sound:** 1) Verify DAC is on USB input, 2) Check `aplay -l`, 3) Restart squeezelite.

**Music not appearing:** Check `ls /srv/music`, then rescan: `echo "rescan" | nc localhost 9090`

**DNS/network errors in ARM:** Verify dns settings in docker-compose.yml. Test: `docker exec arm-ripper getent hosts musicbrainz.org`

**Fingerprint tagger not running:** Check `BASH_SCRIPT` in arm.yaml, verify packages: `docker exec arm-ripper python3 -c "import acoustid; print('ok')"`

**Tagger app not loading:** Check container logs: `docker compose logs arm-ripper | grep -i flask`

**Interrupted/failed rip (missing tracks, tiny files):** Run `docker exec -it arm-ripper /etc/arm/config/arm-diagnostics.sh --clean` then `sudo rm -rf "/srv/music/Artist/Album"` and re-rip. See Section 11 for details.

**abcde resuming old rip incorrectly:** Run diagnostics with cleanup: `docker exec -it arm-ripper /etc/arm/config/arm-diagnostics.sh --clean`

---

## Repository Structure

This repository lives at `/opt/arm/` and is the **single source of truth** for all configuration.

```
/opt/arm/                        <- Git repository root
├── config/                      <- Symlinked from /home/arm/config
│   ├── arm.yaml                 <- ARM main configuration
│   ├── abcde.conf               <- CD ripping settings
│   ├── apprise.yaml             <- Notification settings
│   ├── fingerprint_tagger.py    <- Auto-identification script
│   ├── post_rip_fingerprint.sh  <- Post-rip hook
│   ├── arm-diagnostics.sh       <- Health check and cleanup script
│   └── tagger-app/
│       └── app.py               <- Music Tagger web app
├── docker-compose.yml           <- Container orchestration
├── README.md                    <- This file
├── docs/                        <- Additional documentation
├── logs/                        <- Local logs (gitignored)
└── media/                       <- Local media (gitignored)
```

### Symlink Setup

The `/home/arm/config` directory is a symlink to `/opt/arm/config`:
```
/home/arm/config -> /opt/arm/config
```

This means:
- Edit files in `/opt/arm/config/` directly
- Changes take effect immediately (restart container if needed)
- Git tracks all configuration changes
- No need to copy files between locations

### Making Changes

```bash
# Edit a config file
nano /opt/arm/config/arm.yaml

# Commit changes
cd /opt/arm && git add -A && git commit -m "Description of change"

# Restart containers if needed
docker compose restart
```

### Fresh Installation

To set up on a new Pi from this repository:

```bash
# Clone repository
cd /opt
sudo git clone <repo-url> arm
sudo chown -R <PI_USERNAME>:arm /opt/arm
sudo chmod -R g+w /opt/arm

# Create symlink
sudo ln -s /opt/arm/config /home/arm/config

# Start services
cd /opt/arm && docker compose up -d
```
