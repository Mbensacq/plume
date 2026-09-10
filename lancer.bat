@echo off
setlocal enabledelayedexpansion
rem ---------------------------------------------------------------------------
rem  Lanceur de Plume : met a jour le depot (git pull) puis lance l'application.
rem  => a chaque lancement, l'application se met a jour automatiquement.
rem  Si git est absent, hors-ligne, ou la maj est impossible : on lance quand meme.
rem  Si .venv n'existe pas encore : lance install.bat d'abord.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Environnement absent : installation...
    call install.bat --no-test
    if errorlevel 1 exit /b 1
)

rem -- git dispo ET on est bien dans un depot git ? --
set "GIT_OK="
where git >nul 2>&1 && set "GIT_OK=1"
if defined GIT_OK git rev-parse --git-dir >nul 2>&1 || set "GIT_OK="

if defined GIT_OK (
    echo Mise a jour de Plume...
    rem empreinte des requirements avant maj (pour reinstaller si les deps changent)
    set "REQ_BEFORE="
    for /f "delims=" %%i in ('git rev-parse HEAD:requirements.txt HEAD:requirements-gpu.txt 2^>nul') do set "REQ_BEFORE=!REQ_BEFORE!%%i"

    git pull --ff-only

    set "REQ_AFTER="
    for /f "delims=" %%i in ('git rev-parse HEAD:requirements.txt HEAD:requirements-gpu.txt 2^>nul') do set "REQ_AFTER=!REQ_AFTER!%%i"
    if not "!REQ_BEFORE!"=="!REQ_AFTER!" (
        echo Dependances modifiees : mise a jour...
        rem Le fichier GPU est utilise si les bibliotheques CUDA sont deja installees.
        set "REQ=requirements.txt"
        if exist ".venv\Lib\site-packages\nvidia\cudnn" set "REQ=requirements-gpu.txt"
        ".venv\Scripts\python.exe" -m pip install -r !REQ!
    )
)

rem -- Lancement sans fenetre console (l'app tourne, la console du lanceur se ferme) --
start "" ".venv\Scripts\pythonw.exe" "plume.py"
endlocal
