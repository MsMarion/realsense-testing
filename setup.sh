#!/usr/bin/env bash
# Create .venv and install dependencies for the RealSense hub.
#
#   ./setup.sh              install the pinned, verified versions (requirements.lock)
#   ./setup.sh --latest     install the newest allowed versions (requirements.txt)
#   ./setup.sh --recreate   delete .venv and start over
#   PYTHON=python3.11 ./setup.sh   use a specific interpreter
set -euo pipefail
cd "$(dirname "$0")"

REQS=requirements.lock
for arg in "$@"; do
  case "$arg" in
    --latest)   REQS=requirements.txt ;;
    --recreate) rm -rf .venv ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Pick an interpreter >= 3.8. Prefer the system one: a pyenv shim may point at
# an old version (pyrealsense2 has no wheels for 3.6/3.7).
pick_python() {
  for py in "${PYTHON:-}" /usr/bin/python3 python3; do
    [ -n "$py" ] || continue
    if command -v "$py" >/dev/null 2>&1 &&
       "$py" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
      echo "$py"; return
    fi
  done
  echo "error: need Python >= 3.8 (set PYTHON=/path/to/python3)" >&2; exit 1
}

if [ ! -x .venv/bin/python ]; then
  PY=$(pick_python)
  echo "==> creating .venv with $PY ($("$PY" --version))"
  "$PY" -m venv .venv || { echo "error: venv failed; try: sudo apt install python3-venv" >&2; exit 1; }
fi

echo "==> installing $REQS"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r "$REQS"

echo "==> librealsense $(.venv/bin/python -c 'import pyrealsense2 as rs; print(rs.__version__)')"
echo "==> connected cameras:"
.venv/bin/python - <<'PY'
import pyrealsense2 as rs
devs = list(rs.context().query_devices())
for d in devs:
    g = d.get_info
    print(f"    {g(rs.camera_info.name)}  serial {g(rs.camera_info.serial_number)}  "
          f"USB {g(rs.camera_info.usb_type_descriptor)}")
if not devs:
    print("    none found: check the cable / USB 3 port and permissions (see README > Troubleshooting)")
PY

cat <<'MSG'

Done. Next:
  ./run.sh check --together   # verify every camera delivers good data
  ./run.sh launch --open      # start the hub + dashboard
MSG
