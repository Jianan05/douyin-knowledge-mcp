@echo off
setlocal
cd /d "%~dp0"

set "PY=%~dp0runtime\python\python.exe"
set "PATH=%~dp0runtime\python;%~dp0runtime\python\Scripts;%PATH%"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\ms-playwright"
set "HF_HOME=%~dp0runtime\huggingface"
set "PYTHONUTF8=1"

if not exist "%PY%" (
    echo Private Python runtime is missing.
    pause
    exit /b 1
)

"%PY%" app.py
pause
