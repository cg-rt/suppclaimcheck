#!/usr/bin/env bash
# Sends one Telegram message from the VM (P1.14, D-024): ./notify.sh "text".
# Host scripts (deploy, backup, disk check) use this directly, so an alert still arrives when the app is down.
# Reads the bot token and chat ID from the Docker secret files beside compose.prod.yaml; needs root to read them.
# Never fails its caller: a missing secret or a Telegram error is printed and the exit code is still 0.
set -uo pipefail

dir="${SCC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
token_file="$dir/secrets/telegram_bot_token"
chat_file="$dir/secrets/telegram_chat_id"
text="SuppClaimCheck · $(hostname) · ${1:-no text}"

token="$(tr -d '[:space:]' 2> /dev/null < "$token_file" || true)"
chat="$(tr -d '[:space:]' 2> /dev/null < "$chat_file" || true)"
if [ -z "$token" ] || [ -z "$chat" ]; then
  echo "notify: Telegram is not configured; not sent: $text" >&2
  exit 0
fi

# The token goes in a curl config on stdin, so it never appears in the process list.
if ! printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
  | curl --silent --show-error --fail --max-time 10 --retry 2 --retry-delay 3 --config - \
      --data-urlencode "chat_id=$chat" \
      --data-urlencode "text=$text" \
      --output /dev/null; then
  echo "notify: Telegram did not accept the message: $text" >&2
fi
exit 0
