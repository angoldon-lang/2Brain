#!/usr/bin/env bash
set -euo pipefail
command -v brew >/dev/null || { echo "Installa Homebrew: https://brew.sh"; exit 1; }
brew install ollama libpst python@3.12
brew services start ollama
ollama pull qwen2.5:7b-instruct
ollama pull bge-m3
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
[ -f config.toml ] || cp config.example.toml config.toml
echo "Fatto. Modifica config.toml, poi: . .venv/bin/activate && secondbrain serve  → http://127.0.0.1:8765"
