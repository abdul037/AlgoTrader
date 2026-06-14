#!/bin/sh
set -eu

python scripts/validate_railway_env.py
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:?PORT is required}"
