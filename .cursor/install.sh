#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for Candle Craft Trading Agent.
# Prepares system packages, a local PostgreSQL instance, the Python
# virtualenv, and the database schema. Safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PG_VERSION=16
PG_HBA="/etc/postgresql/${PG_VERSION}/main/pg_hba.conf"

echo "[install] Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3.12-venv \
  postgresql \
  postgresql-contrib

# Local-only dev convenience: allow passwordless localhost connections so the
# default DATABASE_URL works without embedding a secret. Never use in production.
echo "[install] Configuring PostgreSQL trust auth for localhost"
sudo sed -i -E \
  's#^(host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1/32|::1/128)[[:space:]]+)[[:alnum:]-]+#\1trust#' \
  "$PG_HBA"

echo "[install] Starting PostgreSQL"
sudo pg_ctlcluster "$PG_VERSION" main start 2>/dev/null || sudo pg_ctlcluster "$PG_VERSION" main reload || true
for _ in $(seq 1 30); do
  sudo -u postgres pg_isready -q && break
  sleep 1
done

echo "[install] Ensuring 'candle' role and 'candle_craft' database"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='candle'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE candle LOGIN PASSWORD 'change-me'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='candle_craft'" | grep -q 1 \
  || sudo -u postgres createdb -O candle candle_craft

echo "[install] Creating Python virtualenv and installing dependencies"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "[install] Applying database migrations"
alembic upgrade head

echo "[install] Done"
