#!/bin/sh
set -eu
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN:-}"
export TIBBER_TOKEN="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["tibber_token"])')"
export APP_PASSWORD="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["admin_password"])')"
export FAMILY_PASSWORD="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["family_password"])')"
export DATA_DIR=/data
export PORT=8099
exec python3 /app/app.py
