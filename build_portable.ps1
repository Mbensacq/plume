<#
  Construit la version PORTABLE de Plume : dist\Plume\ (dossier Windows autonome,
  sans Python à installer sur la machine cible), puis l'archive en zip.

  Prérequis : .venv\ à la racine avec requirements.txt installé (install.bat).
  PyInstaller est installé automatiquement (requirements-dev.txt).

  Usage :
      .\build_portable.ps1            # build CPU (léger) -> dist\Plume\ + dist\Plume-<version>-windows-x64-cpu.zip
      .\build_portable.ps1 -Gpu       # embarque aussi CUDA/cuDNN si présents dans le venv (~1,3 Go)
      .\build_portable.ps1 -NoTest    # ne lance pas l'auto-test de l'exécutable produit
#>
param([switch]$Gpu, [switch]$NoTest)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Warning "Aucun .venv\ trouvé — lancez install.bat d'abord. Utilisation du 'python' du PATH."
    $py = "python"
}

Write-Host "== Dépendances de build (PyInstaller) =="
& $py -m pip install -r (Join-Path $root "requirements-dev.txt") --quiet

$version = (& $py -c "import plume; print(plume.__version__)")
$variant = if ($Gpu) { "gpu" } else { "cpu" }
$env:PLUME_BUILD_GPU = if ($Gpu) { "1" } else { "0" }

Write-Host "== Construction de Plume $version ($variant) — quelques minutes =="
& $py -m PyInstaller (Join-Path $root "Plume.spec") --noconfirm --clean `
    --distpath (Join-Path $root "dist") --workpath (Join-Path $root "build")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller a échoué" }

$out = Join-Path $root "dist\Plume"
Copy-Item (Join-Path $root "plume_replacements.json") $out
Copy-Item (Join-Path $root "LICENSE") $out
Copy-Item (Join-Path $root "docs\LISEZMOI-portable.txt") (Join-Path $out "LISEZMOI.txt")

if (-not $NoTest) {
    Write-Host "== Auto-test de l'exécutable (modèle tiny, CPU) =="
    $env:PLUME_MODEL = "tiny"; $env:PLUME_DEVICE = "cpu"
    & (Join-Path $out "Plume-console.exe") --selftest
    if ($LASTEXITCODE -ne 0) { throw "L'auto-test de l'exécutable a échoué" }
    Remove-Item Env:PLUME_MODEL; Remove-Item Env:PLUME_DEVICE
    # le modèle de test ne doit pas partir dans l'archive
    Remove-Item -Recurse -Force (Join-Path $out "models") -ErrorAction SilentlyContinue
}

$zip = Join-Path $root "dist\Plume-$version-windows-x64-$variant.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $out -DestinationPath $zip
Write-Host ""
Write-Host "Terminé :"
Write-Host "  $out\Plume.exe"
Write-Host "  $zip"
