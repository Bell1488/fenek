#!/bin/sh
set -eu
cd "$(dirname "$0")"
export ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-http://127.0.0.1:8080,http://localhost:8080}"
export LEAD_DB_PATH="${LEAD_DB_PATH:-$PWD/private/leads.sqlite3}"
export TRUST_REVERSE_PROXY=0
exec python3 backend/server.py
