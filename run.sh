#!/bin/sh
# Start the dnd-sim web server. Used by PM2 (ecosystem.config.js) and by hand.
#
#   ./run.sh                 # live mode, needs ANTHROPIC_API_KEY
#   DND_SIM_MOCK=1 ./run.sh  # mock mode, no API calls, no key needed
set -eu

cd "$(dirname "$0")"

PORT="${PORT:-8045}"
export PORT
export PYTHONUNBUFFERED=1

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi

exec "$PY" -m web.app
