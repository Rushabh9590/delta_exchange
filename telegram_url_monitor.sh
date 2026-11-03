
#!/bin/bash
set -Eeuo pipefail

APP_DIR="$HOME/delta_exchange"
URL_FILE="$APP_DIR/logs/latest_tunnel_url.txt"
ENV_FILE="$APP_DIR/.env"

# Load existing .env
set -a
source "$ENV_FILE"
set +a

# Check URL
if [[ ! -s "$URL_FILE" ]]; then
    echo "Cloudflare URL not available"
    exit 1
fi

URL="$(cat "$URL_FILE")"

# Send ONLY URL to Telegram
curl -fsS -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${URL}" \
    >/dev/null

echo "Telegram URL notification sent successfully."
exit 0

