#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
command -v python3 >/dev/null || { echo 'Нужен Python 3.10+'; exit 1; }
command -v curl >/dev/null || { echo 'Нужен системный curl с SOCKS/TLS'; exit 1; }
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
printf '%s\n' 'Ок. Проверьте SOCKS командой python3 scripts/check-telegram.py и внесите номер Метрики в dist/config.js.'
printf '%s\n' 'Запуск: .venv/bin/gunicorn --bind 127.0.0.1:8080 --workers 2 --threads 4 --timeout 45 --access-logfile /dev/null backend.server:application'
