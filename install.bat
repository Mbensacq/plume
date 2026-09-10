@echo off
setlocal enabledelayedexpansion
rem ---------------------------------------------------------------------------
rem  Installation de Plume (Windows) : cree l'environnement virtuel .venv,
rem  installe les dependances epinglees, puis lance l'auto-test (telechargement
rem  du modele Whisper inclus). Relancable sans risque.
rem
rem  Usage :  install.bat            (detection automatique GPU NVIDIA / CPU)
rem           install.bat --cpu      (forcer l'installation CPU, ~600 Mo)
rem           install.bat --gpu      (forcer l'installation GPU, +1,3 Go de CUDA)
rem           install.bat --no-test  (ne pas lancer l'auto-test a la fin)
rem ---------------------------------------------------------------------------
cd /d "%~dp0"
set "MODE=auto"
set "RUNTEST=1"
for %%a in (%*) do (
    if /i "%%a"=="--cpu" set "MODE=cpu"
    if /i "%%a"=="--gpu" set "MODE=gpu"
    if /i "%%a"=="--no-test" set "RUNTEST=0"
)

echo.
echo == Plume : installation ==
echo.

rem -- 1. Trouver un Python 3.11, 3.12 ou 3.13 (lanceur "py" d'abord, puis PATH) --
set "PY="
for %%v in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%v -c "import sys" >nul 2>&1
        if !errorlevel! equ 0 set "PY=py -%%v"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PY=python"
)
if not defined PY (
    echo [ERREUR] Python 3.11, 3.12 ou 3.13 introuvable.
    echo   Installez-le depuis https://www.python.org/downloads/windows/
    echo   en cochant "Add python.exe to PATH" et "tcl/tk and IDLE", puis relancez.
    goto :fail
)
for /f "delims=" %%i in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%i"
echo Python detecte : %PYVER% (%PY%)

%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Ce Python n'inclut pas tkinter ^(interface graphique^).
    echo   Reinstallez Python en cochant "tcl/tk and IDLE" ^(python.org^).
    goto :fail
)

rem -- 2. Environnement virtuel --
if not exist ".venv\Scripts\python.exe" (
    echo Creation de l'environnement virtuel .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERREUR] Impossible de creer .venv
        goto :fail
    )
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip --quiet

rem -- 3. CPU ou GPU ? (nvidia-smi est installe avec le pilote NVIDIA) --
set "REQ=requirements.txt"
if "%MODE%"=="gpu" set "REQ=requirements-gpu.txt"
if "%MODE%"=="auto" (
    where nvidia-smi >nul 2>&1
    if !errorlevel! equ 0 (
        nvidia-smi -L >nul 2>&1
        if !errorlevel! equ 0 (
            echo Carte NVIDIA detectee : installation GPU ^(CUDA 12 / cuDNN 9, ~1,3 Go en plus^).
            set "REQ=requirements-gpu.txt"
        )
    )
)
if "%REQ%"=="requirements.txt" echo Installation CPU ^(pas de carte NVIDIA detectee, ou --cpu^).

echo Installation des dependances ^(%REQ%^) ...
"%VPY%" -m pip install -r "%REQ%"
if errorlevel 1 (
    echo [ERREUR] L'installation des dependances a echoue ^(voir les messages ci-dessus^).
    echo   Verifiez la connexion internet et l'espace disque, puis relancez install.bat.
    goto :fail
)

rem -- 4. Auto-test : charge le modele (telecharge au premier passage), verifie le backend --
if "%RUNTEST%"=="1" (
    echo.
    echo Auto-test ^(telechargement du modele Whisper au premier passage^) ...
    "%VPY%" plume.py --selftest
    if errorlevel 1 (
        echo [ERREUR] L'auto-test a echoue. Consultez plume.log et le README ^(Troubleshooting^).
        goto :fail
    )
)

echo.
echo == Installation terminee. Lancez Plume avec Plume.vbs ou lancer.bat ==
echo.
pause
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
