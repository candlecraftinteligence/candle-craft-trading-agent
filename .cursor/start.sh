#!/usr/bin/env bash
# Per-boot startup for Candle Craft Trading Agent: bring up PostgreSQL and
# wait until it is ready to accept connections. Idempotent and restart-safe.
set -euo pipefail

PG_VERSION=16

echo "[start] Starting PostgreSQL"
sudo pg_ctlcluster "$PG_VERSION" main start 2>/dev/null || true

for _ in $(seq 1 30); do
  sudo -u postgres pg_isready -q && break
  sleep 1
done

sudo -u postgres pg_isready
echo "[start] PostgreSQL ready"
