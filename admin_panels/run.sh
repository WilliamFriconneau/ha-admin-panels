#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

bashio::log.info "Starting Admin Panels launcher..."

cd /usr/src/app
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --log-level info --no-access-log
