@echo off
rem Portable JupyterLab for Windows (KB development/mcp-kb/setup#launch).
rem Only what must happen before Python starts; server settings - jupyter_data\config\jupyter_server_config.json,
rem token and printed addresses - kb_mcp plugin.
rem Python: py -3.11 by default; override with: set PY=path\to\python.exe
setlocal
cd /d "%~dp0"
set "ROOT=%CD%"
set "BIN=%ROOT%\jupyter_bin\win"
set "DATA=%ROOT%\jupyter_data"
if not defined PY set "PY=py -3.11"

if not exist "%BIN%\jupyterlab" (
  echo [setup] Installing JupyterLab into %BIN% ...
  %PY% -m pip install --target "%BIN%" jupyterlab dulwich || exit /b 1
)
if not exist "%BIN%\dulwich" %PY% -m pip install --target "%BIN%" dulwich || exit /b 1

set "PYTHONPATH=%ROOT%;%BIN%"
set "JUPYTERLAB_DIR=%BIN%\share\jupyter\lab"
set "JUPYTER_CONFIG_PATH=%BIN%\etc\jupyter"
set "JUPYTER_CONFIG_DIR=%DATA%\config"
set "JUPYTER_DATA_DIR=%DATA%\data"
set "JUPYTER_RUNTIME_DIR=%DATA%\runtime"

%PY% -m jupyterlab --notebook-dir "%DATA%" %*
