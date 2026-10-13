#!/bin/zsh
set -e
FILMDUST_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$FILMDUST_ROOT"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python filmdust/server.py
