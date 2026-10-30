#!/bin/bash
set -Eeuo pipefail

# Automatically detect current Linux user and home directory
CURRENT_USER="$(whoami)"
HOME_DIR="$HOME"

# Application directory (no hardcoded username)
APP_DIR="${APP_DIR:-$HOME_DIR/delta_exchange}"
LOG_DIR="$APP_DIR/logs"

PY_LOG="$LOG_DIR/python.log"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
STARTUP_LOG="$LOG_DIR/startup.log"
LATEST_URL="$LOG_DIR/latest_tunnel_url.txt"
URL_HISTORY="$LOG_DIR/tunnel_urls.log"

# Create log directory
mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$STARTUP_LOG"
}

log "Starting application for user: $CURRENT_USER"
log "Home directory: $HOME_DIR"
log "Application directory: $APP_DIR"

# Check prerequisites
for cmd in tmux cloudflared ss; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: $cmd is not installed or not in PATH" >&2
        exit 1
    fi
done

# Check application files
if [[ ! -f "$APP_DIR/server.py" ]]; then
    log "ERROR: server.py not found in $APP_DIR"
    exit 1
fi

if [[ ! -f "$APP_DIR/venv/bin/activate" ]]; then
    log "ERROR: Python virtual environment not found"
    exit 1
fi

# Start Python server
if ! tmux has-session -t pyserver 2>/dev/null; then

    if ss -lntH '( sport = :5000 )' | grep -q .; then
        log "ERROR: Port 5000 is already in use."
        echo "Check with: sudo ss -lntp | grep ':5000'"
        exit 1
    fi

    tmux new-session -d -s pyserver \
        "bash -lc 'cd \"$APP_DIR\" && source venv/bin/activate && python3 -u server.py 2>&1 | tee -a \"$PY_LOG\"'"

    log "Python server started"

else
    log "Python server already running"
fi

# Wait and verify Python server
sleep 3

if ! ss -lntH '( sport = :5000 )' | grep -q .; then
    log "ERROR: Python server is not listening on port 5000"
    echo "Check Python log: $PY_LOG"
    echo "Check session: tmux attach -t pyserver"
    exit 1
fi

log "Python server is listening on port 5000"

# Start Cloudflare Tunnel
if ! tmux has-session -t cloudflare 2>/dev/null; then

    : > "$LATEST_URL"

    tmux new-session -d -s cloudflare \
        "bash -lc 'set -o pipefail; cloudflared tunnel --url http://127.0.0.1:5000 2>&1 | while IFS= read -r line; do printf \"%s\\n\" \"\$line\" | tee -a \"$TUNNEL_LOG\"; if [[ \"\$line\" =~ (https://[a-zA-Z0-9-]+\\.trycloudflare\\.com) ]]; then url=\"\${BASH_REMATCH[1]}\"; printf \"%s\\n\" \"\$url\" > \"$LATEST_URL\"; printf \"[%s] %s\\n\" \"\$(date \"+%F %T\")\" \"\$url\" >> \"$URL_HISTORY\"; printf \"Latest URL saved: %s\\n\" \"\$url\"; fi; done'"

    log "Cloudflare Tunnel started"

else
    log "Cloudflare Tunnel already running"
fi

echo
echo "========== SYSTEM INFORMATION =========="
echo "Linux user:        $CURRENT_USER"
echo "Home directory:    $HOME_DIR"
echo "Application:       $APP_DIR"

echo
echo "========== TMUX SESSIONS =========="
tmux ls

echo
echo "========== LOG FILES =========="
echo "Python log:        $PY_LOG"
echo "Tunnel log:        $TUNNEL_LOG"
echo "Startup log:       $STARTUP_LOG"
echo "Latest tunnel URL: $LATEST_URL"
echo "URL history:       $URL_HISTORY"

echo
echo "========== COMMANDS =========="
echo "Show current URL:  cat '$LATEST_URL'"
echo "View Python:       tmux attach -t pyserver"
echo "View Cloudflare:   tmux attach -t cloudflare"

echo
echo "Detach from tmux: Ctrl+B, then D"
