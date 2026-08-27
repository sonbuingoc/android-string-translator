#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN=""
for candidate in python3.11 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "Python 3.11 is required for reliable Argos/NLLB installation on macOS."
  echo "Install it with: brew install python@3.11"
  exit 2
fi

echo "Using: $PYTHON_BIN"
"$PYTHON_BIN" -m venv .venv
.venv/bin/python3 -m pip install --upgrade pip setuptools wheel
.venv/bin/python3 -m pip install --only-binary=:all: "spacy>=3.8,<3.9"
.venv/bin/python3 -m pip install -r requirements.txt

echo
echo "Local translation dependencies installed in $SCRIPT_DIR/.venv"
echo "GUI will automatically use .venv/bin/python3 for translation."
echo "Argos models are downloaded automatically per language pair on first use."
echo "NLLB model facebook/nllb-200-distilled-600M is downloaded automatically on first use."
