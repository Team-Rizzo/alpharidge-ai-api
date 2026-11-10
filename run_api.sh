#!/usr/bin/env bash
# Alpharidge coordination API — local launcher (runs `app.main:app` from the repo .venv).
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python -m app
