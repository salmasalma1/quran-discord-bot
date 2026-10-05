#!/usr/bin/env bash
# Quran Bot - one-shot setup for Ubuntu servers (Oracle Cloud / any VPS)
# Run inside the quran-bot folder:   bash setup_server.sh
set -e
cd "$(dirname "$0")"

echo "==> Installing system packages (ffmpeg, python-venv)..."
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg python3-venv python3-pip > /dev/null

echo "==> Creating the Python environment and installing requirements..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Creating .env from the template (if missing)..."
[ -f .env ] || cp .env.example .env

echo "==> Creating the systemd service (auto-restart + starts on boot)..."
sudo tee /etc/systemd/system/quran-bot.service > /dev/null <<EOF
[Unit]
Description=Quran Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload

echo ""
echo "=========================================="
echo " Setup finished! Remaining steps:"
echo " 1) nano .env                  <- put your DISCORD_TOKEN"
echo "    (save with Ctrl+O then Enter, exit with Ctrl+X)"
echo " 2) sudo systemctl enable --now quran-bot"
echo " 3) sudo journalctl -u quran-bot -f    <- live logs (Ctrl+C to exit)"
echo "=========================================="
