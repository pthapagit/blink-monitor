#!/usr/bin/env bash
# scripts/lock_requirements.sh
# Regenerate the fully resolved, HASH-LOCKED dependency file from
# requirements.in, then run a CVE scan. Run this on Python 3.12 (the Lambda
# runtime) so resolved wheels match the deploy target.
#
# Supply-chain note: --generate-hashes pins every package AND sub-package to a
# sha256. `pip install --require-hashes` then refuses anything whose hash does
# not match, so a tampered/yanked PyPI release cannot enter the Lambda zip.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERE="${ROOT}/lambda/poller"
cd "$HERE"

# Keep pip-tools / pip-audit caches inside the repo (avoids permission issues
# in sandboxes and CI).
export PIP_TOOLS_CACHE_DIR="${ROOT}/.pip-tools-cache"
export XDG_CACHE_HOME="${ROOT}/.cache"
mkdir -p "$PIP_TOOLS_CACHE_DIR" "$XDG_CACHE_HOME"

# Prefer the project venv when present.
if [[ -x "${ROOT}/.venv/bin/pip-compile" ]]; then
  PIP_COMPILE="${ROOT}/.venv/bin/pip-compile"
  PIP_AUDIT="${ROOT}/.venv/bin/pip-audit"
else
  PIP_COMPILE="pip-compile"
  PIP_AUDIT="pip-audit"
fi

echo "==> Compiling requirements.in -> requirements.lock (with hashes)"
"$PIP_COMPILE" \
  --generate-hashes \
  --output-file=requirements.lock \
  requirements.in

echo "==> Auditing locked dependencies for known CVEs"
"$PIP_AUDIT" -r requirements.lock --strict

echo "==> Done. Commit requirements.lock and deploy with:"
echo "    pip install --require-hashes -r requirements.lock -t ./package"
