@echo off
REM Runs TwNewsMonitor in the foreground from this repo (Windows-native). Ctrl+C stops it gracefully.
cd /d "%~dp0\..\.."
".venv\Scripts\python.exe" main.py --config config.yaml %*
