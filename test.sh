#!/usr/bin/env bash
# Run the test suite, with the workstation's ROS 2 environment kept out of it.
# ROS puts /opt/ros on PYTHONPATH and registers pytest plugins there that fail
# to import without ROS's own dependencies; neither belongs in this project.
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

if [ ! -x .venv/bin/python ]; then
  echo "  creating .venv ..."
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt -r requirements-dev.txt
fi
exec .venv/bin/python -m pytest "${@:-tests/}"
