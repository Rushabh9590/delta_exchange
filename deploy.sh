#!/bin/bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-$HOME/delta_exchange}"
cd "$APP_DIR"

echo "=========================================="
echo "Delta Exchange Production Deployment"
echo "Timestamp: $(date '+%F %T')"
echo "=========================================="

# 1. Pull latest master from git
echo "[1/4] Pulling latest code from origin/master..."
git fetch origin master
git reset --hard origin/master

# 2. Restart Python server tmux session
echo "[2/4] Stopping existing Python server..."
if tmux has-session -t pyserver 2>/dev/null; then
    tmux kill-session -t pyserver
    echo "Killed existing pyserver tmux session."
fi

# Ensure port 5000 is freed
fuser -k 5000/tcp 2>/dev/null || true
sleep 1

# 3. Start Python server & Cloudflare tunnel
echo "[3/4] Launching application with start_delta.sh..."
bash ./start_delta.sh

echo "[4/4] Deployment finished successfully!"
echo "Current Git Commit: $(git rev-parse --short HEAD)"
