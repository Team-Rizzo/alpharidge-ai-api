#!/usr/bin/env bash
# Alpharidge coordination API — local launcher.
# Runs from the repo-local .venv, matching the live pm2 `alpharidge.api` process, so the
# Prisma client resolves to .venv (NOT the miniconda install). Prisma CLI usage should
# likewise prefer .venv: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m prisma <cmd>`.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python main.py
