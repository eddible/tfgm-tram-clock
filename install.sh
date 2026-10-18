#!/bin/sh
# Install (or update) the tram clock as a systemd service.
# Run from the project directory on the Pi:  ./install.sh
set -e
cd "$(dirname "$0")"
DIR=$(pwd)

if ! python3 -c "import pygame, requests, hyperpixel2r" 2>/dev/null; then
    echo "Installing Python dependencies"
    command -v pip3 >/dev/null || sudo apt-get install -y python3-pip
    sudo pip3 install -r requirements.txt
fi

if [ ! -f config.json ]; then
    echo "Creating config.json"
    python3 - <<'EOF'
import configparser, json, os
cfg = {"views": []}
old = configparser.ConfigParser()
# Carry settings over from the original version's config.ini if present.
old.read(["config.ini", os.path.expanduser("~pi/tfgm-tram-clock/config.ini")])
if "TFGM-CONFIG" in old:
    sec = old["TFGM-CONFIG"]
    cfg["api_key"] = sec.get("api_key", "").strip()
    for opt in ("tram_line", "tram_line_2"):
        pid = sec.get(opt, "").strip()
        if pid:
            cfg["views"].append({"name": pid, "pids": [pid]})
with open("config.json", "w") as f:
    json.dump(cfg, f, indent=2)
EOF
    chmod 600 config.json
fi

sed "s#/home/pi/tram-clock#$DIR#g" tramclock.service | sudo tee /etc/systemd/system/tramclock.service >/dev/null

# Retire the old rc.local launcher.
if grep -q '^tram.sh' /etc/rc.local 2>/dev/null; then
    sudo sed -i 's/^tram.sh/# tram.sh  (replaced by tramclock.service)/' /etc/rc.local
fi
sudo pkill -f 'python3 tram_time.py' 2>/dev/null || true

# The login prompt on tty1 would otherwise draw over the clock.
sudo systemctl disable --now getty@tty1.service 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable tramclock.service
sudo systemctl restart tramclock.service
echo "Installed. Web UI: http://$(hostname -I | cut -d' ' -f1)/"
