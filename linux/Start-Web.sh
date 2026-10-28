#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f "../venv/bin/activate" ]; then
  echo "Сначала установите зависимости по README.md"
  read -p "Press any key to continue..."
  exit 1
fi
if [ ! -f "../.env" ]; then
  cp "../.env.example" "../.env"
fi
source "../venv/bin/activate" && PYTHONPATH=.. exec python3 -X utf8 -m iacpaas_mcp.launcher --start

