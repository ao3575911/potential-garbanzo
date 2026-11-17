#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12 or newer is required"'
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock -r build-requirements.lock
.venv/bin/python -m pip install --disable-pip-version-check --no-deps --no-build-isolation .
printf '%s\n' 'Installed. Next: .venv/bin/circuit-bot setup' 'Then: .venv/bin/circuit-bot doctor' 'Start: .venv/bin/circuit-bot run'
