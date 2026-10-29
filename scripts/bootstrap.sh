#!/usr/bin/env bash
# One-command local start on synthetic data (milestone M1: "app starts locally with one command").
#
# Copies .env.example to .env (with a generated SECRET_KEY), creates the SQLite schema with Alembic,
# creates a clinic and a reviewer account (password prompt + TOTP QR code), generates a synthetic
# batch, imports claims/notes/documents/835s, runs the pipeline and prints the URL to open after
# `denial-agent serve`.
#
# Usage: scripts/bootstrap.sh [reviewer-email] [n-claims] [seed]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

EMAIL="${1:-}"
CLAIMS="${2:-200}"
SEED="${3:-20261028}"
OUT_DIR="data/synthetic"

find_python() {
  if [ -x ".venv/bin/python" ]; then echo ".venv/bin/python"
  elif [ -x ".venv/Scripts/python.exe" ]; then echo ".venv/Scripts/python.exe"
  else echo ""; fi
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "Creating the virtual environment with uv sync ..."
    uv sync
    PY="$(find_python)"
  fi
fi
if [ -z "$PY" ]; then
  echo "No .venv found and uv is not installed. Install uv (https://docs.astral.sh/uv/) and run 'uv sync'." >&2
  exit 1
fi

agent() { "$PY" -m app.cli "$@"; }

if [ ! -f ".env" ]; then
  cp .env.example .env
  SECRET="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  sed -i.bak "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env && rm -f .env.bak
  echo "Wrote .env with a generated SECRET_KEY (edit it later to enable the real model)."
fi

mkdir -p data

echo "== Database schema (alembic upgrade head)"
agent init-db --migrate

echo "== Clinic"
CLINIC_ID="$(agent create-clinic --name "Synthetic Pilot Clinic" --pm-system synthetic \
  | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["clinic_id"])')"

echo "== Reviewer account (you will be asked for a password; scan the QR code with an authenticator app)"
if [ -z "$EMAIL" ]; then
  read -r -p "Reviewer email: " EMAIL
fi
agent create-user --email "$EMAIL" --role admin

echo "== Synthetic data ($CLAIMS claims, seed $SEED) -> $OUT_DIR"
agent generate-synthetic --out "$OUT_DIR" --seed "$SEED" --claims "$CLAIMS"

echo "== Imports"
agent import-claims "$OUT_DIR/claims_export.csv" --clinic-id "$CLINIC_ID"
if [ -f "$OUT_DIR/notes.csv" ]; then
  agent import-notes "$OUT_DIR/notes.csv" --clinic-id "$CLINIC_ID"
fi
if [ -f "$OUT_DIR/payer_documents.csv" ]; then
  agent import-documents "$OUT_DIR/payer_documents.csv" --clinic-id "$CLINIC_ID"
fi
for remit in "$OUT_DIR"/remit_*.835; do
  if [ -e "$remit" ]; then
    agent import-835 "$remit"
  fi
done

echo "== Pipeline"
agent process

echo
echo "Done. Start the app with:  $PY -m app.cli serve"
echo "Then open: http://127.0.0.1:8000"
