<#
  Construit la (les) version(s) PORTABLE(s) de Plume : exécutable(s) Windows
  autonome(s) dans dist\, SANS Python à installer sur la machine cible. GPU NVIDIA
  utilisé si présent, repli CPU automatique sinon.

  Le modèle Whisper n'est pas embarqué : il se télécharge au 1er lancement, dans
  models\ à côté de l'exe (internet requis cette première fois, puis hors ligne).

  Prérequis : un venv .venv\ à la racine, avec requirements.txt déjà installé.

  Usage :
      .\build_portable.ps1            # fenêtré -> dist\Plume\Plume.exe (usage normal)
      .\build_portable.ps1 -Console   # debug   -> dist\Plume-debug\Plume-debug.exe
      .\build_portable.ps1 -Both      # les DEUX (fenêtré + debug), côte à côte
      .\build_portable.ps1 -Cpu       # allégé SANS CUDA (PC sans GPU NVIDIA),
                                      #   modèle « medium » par défaut -> dist\Plume-CPU\
#>
param([switch]$Console, [switch]$Both, [switch]$Cpu)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Warning "Aucun .venv\ trouvé à la racine — utilisation du 'python' du PATH."
    $py = "python"
}

Write-Host "== Installation de PyInstaller (dépendance de build) =="
& $py -m pip install -r (Join-Path $root "requirements-build.txt")

function Build-Variant([string]$Name, [string]$ConsoleFlag, [string]$CpuFlag = "0") {
    $env:PLUME_BUILD_NAME = $Name
    $env:PLUME_BUILD_CONSOLE = $ConsoleFlag
    $env:PLUME_BUILD_CPU = $CpuFlag
    $label = if ($CpuFlag -eq "1") { "CPU allégé, sans CUDA" }
             elseif ($ConsoleFlag -eq "1") { "avec console (debug)" }
             else { "fenêtré" }
    Write-Host "== Construction de '$Name' ($label) — quelques minutes =="
    & $py -m PyInstaller (Join-Path $root "Plume.spec") --noconfirm --clean `
        --distpath (Join-Path $root "dist") `
        --workpath (Join-Path $root "build")
}

if ($Both) {
    Build-Variant "Plume" "0"
    Build-Variant "Plume-debug" "1"
} elseif ($Cpu) {
    Build-Variant "Plume-CPU" "0" "1"
} elseif ($Console) {
    Build-Variant "Plume-debug" "1"
} else {
    Build-Variant "Plume" "0"
}

Write-Host ""
Write-Host "Terminé. Dans dist\ :"
if (Test-Path (Join-Path $root "dist\Plume\Plume.exe")) {
    Write-Host "  - dist\Plume\Plume.exe              (fenêtré, GPU+CPU, usage normal)"
}
if (Test-Path (Join-Path $root "dist\Plume-debug\Plume-debug.exe")) {
    Write-Host "  - dist\Plume-debug\Plume-debug.exe  (console, pour diagnostiquer)"
}
if (Test-Path (Join-Path $root "dist\Plume-CPU\Plume-CPU.exe")) {
    Write-Host "  - dist\Plume-CPU\Plume-CPU.exe      (allégé, CPU, sans GPU NVIDIA)"
}
Write-Host "Copiez le DOSSIER voulu EN ENTIER sur la machine cible."
