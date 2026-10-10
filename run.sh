#!/usr/bin/env bash
# Run a project script with the .venv Python, no activation needed.
# Creates the venv first if it doesn't exist yet.
#
#   ./run.sh check [args]    tests/check_cameras.py   (e.g. ./run.sh check --together)
#   ./run.sh launch [args]   launch.py                (e.g. ./run.sh launch --open)
#   ./run.sh info [args]     demos/device_info.py     (e.g. ./run.sh info --profiles)
#   ./run.sh board [args]    calib/make_board.py
#   ./run.sh <file.py> [args]  any other script
set -euo pipefail
cd "$(dirname "$0")"

[ -x .venv/bin/python ] || ./setup.sh

case "${1:-}" in
  check)  script=tests/check_cameras.py ;;
  launch) script=launch.py ;;
  info)   script=demos/device_info.py ;;
  board)  script=calib/make_board.py ;;
  ""|-h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *)      script=$1 ;;
esac
shift
exec .venv/bin/python "$script" "$@"
