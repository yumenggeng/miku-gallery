@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY_EXE=..\..\python_embeded\python.exe"
if not exist "%PY_EXE%" (
    set "PY_EXE=python"
)

"%PY_EXE%" -u sync_gallery.py %*

echo.
if "%~1"=="" pause
