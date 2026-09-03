#!/bin/sh
# Start the dnd-sim web server. Used by PM2 (ecosystem.config.js) and by hand.
#
#   ./run.sh                 # live mode, needs ANTHROPIC_API_KEY
#   DND_SIM_MOCK=1 ./run.sh  # mock mode, no API calls, no key needed
#
# Config is ./.env (gitignored, mode 600, written by `dndsim deploy`): it is
# sourced here, so pm2-managed and hand runs read the same file and the key
# never has to be in pm2's environment. Values in .env override the caller's.
set -eu

cd "$(dirname "$0")"

if [ -f ./.env ]; then
    set -a
    . ./.env
    set +a
fi

PORT="${PORT:-8071}"
export PORT
export PYTHONUNBUFFERED=1

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi

exec "$PY" -m web.app
