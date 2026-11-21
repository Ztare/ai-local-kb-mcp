#!/usr/bin/env sh
# Portable JupyterLab for Android (Termux) (KB development/mcp-kb/setup#launch).
# Only what must happen before Python starts; server settings - jupyter_data/config/jupyter_server_config.json,
# token and printed addresses - kb_mcp plugin.
# Python: "python" by default; override with: PY=python3.12 sh start_jupyter.sh
# Build deps for first install (once): pkg install python rust clang make libzmq
set -e
cd "$(dirname "$0")"
ROOT=$(pwd)
BIN="$ROOT/jupyter_bin/android"
DATA="$ROOT/jupyter_data"
PY=${PY:-python}

if [ ! -d "$BIN/jupyterlab" ]; then
  echo "[setup] Installing JupyterLab into $BIN ..."
  "$PY" -m pip install --target "$BIN" jupyterlab dulwich
fi
[ -d "$BIN/dulwich" ] || "$PY" -m pip install --target "$BIN" dulwich

export PYTHONPATH="$ROOT:$BIN"
export JUPYTERLAB_DIR="$BIN/share/jupyter/lab"
export JUPYTER_CONFIG_PATH="$BIN/etc/jupyter"
export JUPYTER_CONFIG_DIR="$DATA/config"
export JUPYTER_DATA_DIR="$DATA/data"
export JUPYTER_RUNTIME_DIR="$DATA/runtime"

# keep Termux alive with screen off (if termux-api is installed)
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true

exec "$PY" -m jupyterlab --notebook-dir "$DATA" "$@"
