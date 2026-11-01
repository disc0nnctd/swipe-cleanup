#!/usr/bin/env bash
# Daily: index new mail -> classify new senders + subject patterns -> (if enabled) apply money-mail rules.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
$PY index.py
$PY classify.py
$PY classify_types.py --everyone
$PY ledger.py
$PY ledger_ai.py || true   # local model; skipped if not running
$PY jury.py
$PY autoclean.py
