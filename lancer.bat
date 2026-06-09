@echo off
rem Lanceur de Plume (ferme la console immediatement).
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "plume.py"
