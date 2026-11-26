@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "ARB_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%ARB_PY%" set "ARB_PY=%USERPROFILE%\Desktop\申赎套利回测\.venv\Scripts\python.exe"
if not exist "%ARB_PY%" set "ARB_PY=python"
"%ARB_PY%" -X utf8 "%~dp0run_backtest.py" %*
exit /b %errorlevel%
