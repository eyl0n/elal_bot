#!/usr/bin/env bash
# setup.sh — Run this once on a fresh Oracle Cloud Always Free Ubuntu VM
# to install and start the El Al flight monitor as a background service.
#
# Usage:
#   1. SSH into your VM:       ssh ubuntu@<your-vm-ip>
#   2. Clone your repo:        git clone https://github.com/YOUR_USER/elal_bot.git
#   3. cd elal_bot
#   4. bash setup.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="elal-monitor"

echo "==> Updating apt..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-venv python3-pip

echo "==> Creating virtual environment..."
python3 -m venv "$REPO_DIR/venv"

echo "==> Installing Python dependencies..."
"$REPO_DIR/venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

echo "==> Installing Playwright + Chromium (this downloads ~300MB, once only)..."
"$REPO_DIR/venv/bin/playwright" install chromium
"$REPO_DIR/venv/bin/playwright" install-deps chromium

# ---- .env setup ----
if [ ! -f "$REPO_DIR/.env" ]; then
    echo ""
    echo "==> No .env file found. Let's create one."
    read -rp "  Paste your TELEGRAM_TOKEN: " tg_token
    read -rp "  Paste your TELEGRAM_CHAT_ID: " tg_chat_id
    printf "TELEGRAM_TOKEN=%s\nTELEGRAM_CHAT_ID=%s\n" "$tg_token" "$tg_chat_id" > "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
    echo "  .env written and permissions set to 600."
else
    echo "==> Found existing .env, skipping."
fi

# ---- systemd service ----
echo "==> Installing systemd service..."

# Patch the service file to point to this repo dir and current user
CURRENT_USER="$(whoami)"
sed -e "s|/home/ubuntu/elal_bot|$REPO_DIR|g" \
    -e "s|User=ubuntu|User=$CURRENT_USER|g" \
    "$REPO_DIR/elal-monitor.service" \
    | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "========================================="
echo " El Al monitor is running as a service!"
echo " Useful commands:"
echo "   sudo systemctl status $SERVICE_NAME"
echo "   sudo journalctl -u $SERVICE_NAME -f"
echo "   tail -f $REPO_DIR/monitor.log"
echo "========================================="
