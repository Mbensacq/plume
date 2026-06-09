@echo off
rem Lanceur de l'application de dictee (ferme la console immediatement).
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "dictee.py"
