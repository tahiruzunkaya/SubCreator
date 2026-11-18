@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m subcreator %*
if errorlevel 1 pause
