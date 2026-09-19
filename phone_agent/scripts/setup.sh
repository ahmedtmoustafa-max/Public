#!/usr/bin/env bash
# Interactive first-run setup. Writes .env, which is gitignored -- your phone
# number and API keys never enter the repository.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=".env"

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }

if [[ -f "$ENV_FILE" ]]; then
  say "$ENV_FILE already exists."
  read -r -p "Overwrite it? [y/N] " reply
  # Not ${reply,,}: stock macOS bash is 3.2 and does not have it.
  case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
    y|yes) ;;
    *) echo "Left it alone."; exit 0 ;;
  esac
  cp "$ENV_FILE" "$ENV_FILE.backup.$(date +%Y%m%d%H%M%S)"
  echo "Backed up the old one."
fi

# ask <var-name> <prompt> [default] [required]
# Locals are prefixed because bash has dynamic scope: a local named `value`
# here would shadow -- and silently swallow -- the caller's variable of the
# same name.
ask() {
  local __ask_var=$1 __ask_prompt=$2 __ask_default=${3:-} __ask_req=${4:-no}
  local __ask_reply=""
  while true; do
    if [[ -n "$__ask_default" ]]; then
      read -r -p "$__ask_prompt [$__ask_default]: " __ask_reply
      __ask_reply="${__ask_reply:-$__ask_default}"
    else
      read -r -p "$__ask_prompt: " __ask_reply
    fi
    if [[ -z "$__ask_reply" && "$__ask_req" == "yes" ]]; then
      echo "  (required)"
      continue
    fi
    break
  done
  printf -v "$__ask_var" '%s' "$__ask_reply"
}

ask_number() {
  local __num_var=$1 __num_prompt=$2 __num_reply=""
  while true; do
    ask __num_reply "$__num_prompt" "" yes
    if [[ "$__num_reply" =~ ^\+[1-9][0-9]{7,14}$ ]]; then break; fi
    echo "  Needs to be E.164: a plus, country code, then digits. e.g. +15195551234"
  done
  printf -v "$__num_var" '%s' "$__num_reply"
}

say "phone-agent setup"
cat <<'TXT'
Three things are needed before the first call: a Twilio account, the phone
you want rung when a human answers, and a public HTTPS address Twilio can
reach. Press enter to accept anything shown in brackets.
TXT

say "1. Your phone"
echo "The number that rings when a human picks up. Not your Twilio number."
ask_number MY_PHONE "  Your phone"

say "2. Twilio"
echo "From console.twilio.com. Leave blank to fill in later."
ask SID   "  Account SID (starts AC)" "" no
ask TOKEN "  Auth token" "" no
ask FROM  "  Twilio number to dial out from (e.g. +15195551234)" "" no

say "3. Where Twilio reaches this server"
echo "Local testing:  cloudflared tunnel --url http://localhost:8080"
echo "Deployed:       https://your-app.fly.dev"
ask BASE_URL "  Public HTTPS base URL" "http://localhost:8080" no

say "4. Optional: reading unfamiliar menus"
echo "Without these, script and watch modes still work fully."
ask ANTHROPIC_KEY "  Anthropic API key" "" no
ask DEEPGRAM_KEY  "  Deepgram API key" "" no
ASR_PROVIDER="none"; [[ -n "$DEEPGRAM_KEY" ]] && ASR_PROVIDER="deepgram"

say "5. Optional: push notification"
echo "Install the ntfy app and pick a topic nobody could guess."
ask NTFY_TOPIC "  ntfy topic" "" no

umask 077
cat > "$ENV_FILE" <<ENVEOF
# Written by scripts/setup.sh on $(date +%Y-%m-%d).
# Gitignored -- keep it that way.

PUBLIC_BASE_URL=$BASE_URL

TWILIO_ACCOUNT_SID=$SID
TWILIO_AUTH_TOKEN=$TOKEN
TWILIO_FROM_NUMBER=$FROM

MY_PHONE_NUMBER=$MY_PHONE

ANTHROPIC_API_KEY=$ANTHROPIC_KEY
ANTHROPIC_MODEL=claude-opus-5
ASR_PROVIDER=$ASR_PROVIDER
DEEPGRAM_API_KEY=$DEEPGRAM_KEY

NOTIFY_SMS=true
NOTIFY_NTFY_TOPIC=$NTFY_TOPIC
NOTIFY_PUSHOVER_TOKEN=
NOTIFY_PUSHOVER_USER=

ALLOWED_DESTINATIONS=[]
MAX_CALL_SECONDS=3600
MAX_CONCURRENT_CALLS=3
DETECTOR_STRICTNESS=0.55
VALIDATE_TWILIO_SIGNATURES=true
ENVEOF
chmod 600 "$ENV_FILE"

if [[ ! -f config/profile.yaml ]]; then
  cp config/profile.example.yaml config/profile.yaml
  echo
  echo "Created config/profile.yaml -- edit it to say what the agent may"
  echo "disclose to a phone menu. It says nothing by default."
fi

say "Checking it over"
PYTHON="python3"
[[ -x .venv/bin/python ]] && PYTHON=".venv/bin/python"
PYTHONPATH=src "$PYTHON" -m phone_agent.cli doctor || true

cat <<'TXT'

Next:
  .venv/bin/python -m phone_agent                      # start the server
  PYTHONPATH=src .venv/bin/python -m phone_agent.cli doctor --live
TXT
