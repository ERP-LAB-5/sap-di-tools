#!/usr/bin/env bash
# Run the test suite, with a workstation's ROS 2 environment kept out of it.
# ROS puts /opt/ros on PYTHONPATH and registers pytest plugins there that fail
# to import without ROS's own dependencies; neither belongs in this project.
#
#   ./test.sh                     everything under tests/
#   ./test.sh tests/test_core.py -k skill
#
# Core-owned (D-LAB-5 tool template): `copier update` rewrites this file.
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

if [ ! -x .venv/bin/python ]; then
  echo "  creating .venv ..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
fi
cat requirements.txt requirements-dev.txt > .venv/.test-requirements.new
if ! cmp -s .venv/.test-requirements.new .venv/.test-requirements.txt; then
  echo "  installing requirements ..."
  .venv/bin/pip install --quiet -r requirements.txt -r requirements-dev.txt
  mv .venv/.test-requirements.new .venv/.test-requirements.txt
  # run.sh keeps its own stamp; the runtime set is now installed too
  cp requirements.txt .venv/.requirements.txt
else
  rm -f .venv/.test-requirements.new
fi
exec .venv/bin/python -m pytest "${@:-tests/}"
