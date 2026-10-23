#!/bin/bash
set -Eeuo pipefail

APP_DIR="/home/ubuntu/delta_exchange"
LOG_DIR="$APP_DIR/logs"

PY_LOG="$LOG_DIR/python.log"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
STARTUP_LOG="$LOG_DIR/startup.log"
LATEST_URL="$LOG_DIR/latest_tunnel_url.txt"
URL_HISTORY="$LOG_DIR/tunnel_urls.log"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$STARTUP_LOG"
}

# Check prerequisites
for cmd in tmux cloudflared; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: $cmd is not installed or not in PATH" >&2
        exit 1
    fi
done

if [[ ! -f "$APP_DIR/server.py" || ! -f "$APP_DIR/venv/bin/activate" ]]; then
    echo "ERROR: server.py or Python virtual environment not found in $APP_DIR" >&2
    exit 1
fi

# Start Python only if its tmux session does not already exist.
if ! tmux has-session -t pyserver 2>/dev/null; then
    if ss -lnt 2>/dev/null | grep -qE '(^|[[:space:]])[^[:space:]]*:5000[[:space:]]'; then
        log "WARNING: Port 5000 is already listening. Checking whether an existing pyserver session owns it."
        echo "Port 5000 is already in use. Run: sudo ss -lntp | grep ':5000'"
        echo "Identify the process before stopping it. No process has been killed."
        exit 1
    fi

    tmux new-session -d -s pyserver \
        "bash -lc 'cd \"$APP_DIR\" && source venv/bin/activate && python3 -u server.py 2>&1 | tee -a \"$PY_LOG\"'"
    log "Python server tmux session started"
else
    log "Python server tmux session already exists"
fi

# Give the application a moment to start, then verify port 5000.
sleep 3
if ! ss -lnt 2>/dev/null | grep -qE '(^|[[:space:]])[^[:space:]]*:5000[[:space:]]'; then
    log "ERROR: Nothing is listening on port 5000. Check $PY_LOG and tmux attach -t pyserver"
    echo "Python did not open port 5000. See: $PY_LOG"
    exit 1
fi

# Start an unprotected Quick Tunnel only if its tmux session is absent.
# Note: without Access/email protection, anyone with the URL may reach the app.
if ! tmux has-session -t cloudflare 2>/dev/null; then
    : > "$LATEST_URL"

    tmux new-session -d -s cloudflare \
        "bash -lc 'set -o pipefail; cloudflared tunnel --url http://127.0.0.1:5000 2>&1 | while IFS= read -r line; do printf \"%s\\n\" \"\$line\" | tee -a \"$TUNNEL_LOG\"; if [[ \"\$line\" =~ (https://[a-zA-Z0-9-]+\\.trycloudflare\\.com) ]]; then url=\"\${BASH_REMATCH[1]}\"; printf \"%s\\n\" \"\$url\" > \"$LATEST_URL\"; printf \"[%s] %s\\n\" \"\$(date \"+%F %T\")\" \"\$url\" >> \"$URL_HISTORY\"; printf \"Latest URL saved: %s\\n\" \"\$url\"; fi; done'"
    log "Cloudflare Tunnel tmux session started"
else
    log "Cloudflare Tunnel tmux session already exists"
fi

echo
echo "===== tmux sessions ====="
tmux ls

echo
echo "===== Log files ====="
echo "Python log:        $PY_LOG"
echo "Tunnel log:        $TUNNEL_LOG"
echo "Startup log:       $STARTUP_LOG"
echo "Latest tunnel URL: $LATEST_URL"
echo "URL history:       $URL_HISTORY"
echo
echo "Show current URL:  cat '$LATEST_URL'"
echo "View Python:       tmux attach -t pyserver"
echo "View Cloudflare:   tmux attach -t cloudflare"
echo
echo "Detach from tmux without stopping it: Ctrl+B, then D"
