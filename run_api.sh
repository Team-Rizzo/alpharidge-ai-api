#!/usr/bin/env bash
# Talisman coordination API — local
set -euo pipefail
cd /home/rizzo/talisman/talisman-ai-api
exec /home/rizzo/miniconda3/envs/talisman_ai/bin/python main.py
