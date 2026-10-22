#!/bin/bash
set -Eeuo pipefail

APP_DIR="/home/ubuntu/delta_exchange"
LOG_DIR="$APP_DIR/logs"
EMAIL="rushabh.9590@gmail.com"

mkdir -p "$LOG_DIR"

PY_LOG="$LOG_DIR/python.log"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
STARTUP_LOG="$LOG_DIR/startup.log"
LATEST_URL="$LOG_DIR/latest_tunnel_url.txt"
URL_HISTORY="$LOG_DIR/tunnel_urls.log"

# Check prerequisites
for cmd in tmux cloudflared; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: $cmd is not installed or not in PATH"
        exit 1
    fi
done

if [[ ! -f "$APP_DIR/server.py" || ! -f "$APP_DIR/venv/bin/activate" ]]; then
    echo "ERROR: server.py or Python virtual environment not found"
    exit 1
fi

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$STARTUP_LOG"
}

# Start Python session if it does not already exist
if ! tmux has-session -t pyserver 2>/dev/null; then
    tmux new-session -d -s pyserver \
      "cd '$APP_DIR' && source venv/bin/activate && exec python3 -u server.py 2>&1 | tee -a '$PY_LOG'"
    log "Python server tmux session started"
else
    log "Python server tmux session already exists"
fi

# Wait briefly for the application
sleep 5

# Start Cloudflare session if it does not already exist
if ! tmux has-session -t cloudflare 2>/dev/null; then
    # Clear stale current URL so it cannot be mistaken for the new URL
    : > "$LATEST_URL"

    tmux new-session -d -s cloudflare \
      "bash -c 'set -o pipefail; cloudflared tunnel --url http://127.0.0.1:5000 --allowed-mail \"$EMAIL\" 2>&1 | while IFS= read -r line; do echo \"\$line\" | tee -a \"$TUNNEL_LOG\"; if [[ \"\$line\" =~ (https://[a-zA-Z0-9-]+\\.trycloudflare\\.com) ]]; then url=\"\${BASH_REMATCH[1]}\"; printf \"%s\\n\" \"\$url\" > \"$LATEST_URL\"; printf \"[%s] %s\\n\" \"\$(date \"+%F %T\")\" \"\$url\" >> \"$URL_HISTORY\"; echo \"Latest URL saved: \$url\"; fi; done'"
    log "Cloudflare Tunnel tmux session started"
else
    log "Cloudflare Tunnel tmux session already exists"
fi

echo
echo "===== tmux sessions ====="
tmux ls

echo
echo "===== Log files ====="
echo "Python log:       $PY_LOG"
echo "Tunnel log:       $TUNNEL_LOG"
echo "Startup log:      $STARTUP_LOG"
echo "Latest tunnel URL: $LATEST_URL"
echo "URL history:      $URL_HISTORY"
echo
echo "Show current URL:"
echo "cat '$LATEST_URL'"
echo
echo "View Python:   tmux attach -t pyserver"
echo "View Cloudflare: tmux attach -t cloudflare"
