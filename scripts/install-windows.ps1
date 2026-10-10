<#
.SYNOPSIS
    Installe puis verifie tout ce qu'il faut pour lancer Plume sous Windows.

.DESCRIPTION
    Appele par install-wsl.sh (depuis WSL), ou directement depuis Windows :
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-windows.ps1

    Etapes, toutes idempotentes (relancer le script = reparer et reverifier) :
      1. Python 3.13 x64 (winget, sinon installeur officiel python.org), sans droits admin.
      2. Environnement virtuel .venv + dependances de requirements.txt.
      3. Git for Windows, pour la mise a jour automatique de lancer.bat.   [-NoGit]
      4. Ollama + modele local du mode IA.                                 [-NoAI]
      5. Raccourcis "Plume" (menu Demarrer + Bureau) vers lancer.bat.      [-NoShortcuts]
      6. Verification complete via scripts\check_install.py : tests unitaires, audio,
         GPU, modele Whisper telecharge et teste, mode IA teste de bout en bout.

    Fichier volontairement en ASCII : Windows PowerShell 5.1 lit les .ps1 sans BOM
    en ANSI, et la sortie traverse un pipe WSL dont l'encodage n'est pas garanti.

.NOTES
    Compatible Windows PowerShell 5.1 (present sur tout Windows 10/11).
    Code de sortie : 0 si Plume est pret, 1 sinon.
#>
[CmdletBinding()]
param(
    [switch]$NoAI,
    [switch]$NoGit,
    [switch]$NoShortcuts
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest est tres lent avec sa barre de progression (PS 5.1)
Set-StrictMode -Version 2.0

$PythonTag = '3.13'                         # version testee (README / requirements.txt)
$RepoDir = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $RepoDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$CheckScript = Join-Path $RepoDir 'scripts\check_install.py'
$WingetCommon = @('--exact', '--source', 'winget', '--silent', '--accept-package-agreements', '--accept-source-agreements')

$script:Results = New-Object System.Collections.Generic.List[object]
$script:WingetPath = $null
$script:WingetSearched = $false


# ---------------------------------------------------------------------------
# Affichage et bilan
# ---------------------------------------------------------------------------
function Write-Step { param([string]$Message) Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Info { param([string]$Message) Write-Host "    $Message" }
function Write-Warn { param([string]$Message) Write-Host "    [ATTENTION] $Message" -ForegroundColor Yellow }

function Add-Result {
    param([string]$Status, [string]$Item, [string]$Detail = '')
    $script:Results.Add([pscustomobject]@{ Status = $Status; Item = $Item; Detail = $Detail })
}

function Show-Summary {
    Write-Host ''
    Write-Host '==================== Bilan de l''installation ====================' -ForegroundColor Cyan
    foreach ($r in $script:Results) {
        $color = switch ($r.Status) { 'OK' { 'Green' } 'INFO' { 'Gray' } 'ATTENTION' { 'Yellow' } default { 'Red' } }
        $line = '[{0,-9}] {1}' -f $r.Status, $r.Item
        if ($r.Detail) { $line += " : $($r.Detail)" }
        Write-Host $line -ForegroundColor $color
    }
}

function Stop-Install {
    # Echec bloquant : bilan, puis sortie en erreur.
    param([string]$Message)
    Add-Result 'ECHEC' 'Installation interrompue' $Message
    Write-Host ''
    Write-Host "[ECHEC] $Message" -ForegroundColor Red
    Show-Summary
    exit 1
}

trap {
    Write-Host ''
    Write-Host "[ECHEC] Erreur inattendue : $($_.Exception.Message)" -ForegroundColor Red
    Write-Host $_.InvocationInfo.PositionMessage
    exit 1
}


# ---------------------------------------------------------------------------
# Lancement de programmes externes
# ---------------------------------------------------------------------------
function ConvertTo-CommandLine {
    # Assemble des arguments en ligne de commande Windows (regles CommandLineToArgvW).
    param([string[]]$Arguments)
    $parts = foreach ($a in $Arguments) {
        if ($a -eq '') { '""' }
        elseif ($a -notmatch '[\s"]') { $a }
        else {
            $escaped = [regex]::Replace($a, '(\\*)"', { param($m) ($m.Groups[1].Value * 2) + '\"' })
            $escaped = [regex]::Replace($escaped, '(\\+)$', { param($m) $m.Groups[1].Value * 2 })
            '"' + $escaped + '"'
        }
    }
    return ($parts -join ' ')
}

function Invoke-Native {
    # Lance un executable en lui laissant la console (sortie et progression en direct).
    # Renvoie son code de sortie, ou -1 s'il n'a pas pu demarrer.
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$ArgumentList = @())
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ConvertTo-CommandLine $ArgumentList
    $psi.UseShellExecute = $false
    $psi.WorkingDirectory = $RepoDir
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        Write-Warn "Impossible de lancer $FilePath : $($_.Exception.Message)"
        return -1
    }
    $proc.WaitForExit()
    return $proc.ExitCode
}

function Invoke-Capture {
    # Lance un executable sans console (stdin ferme : aucune invite ne peut bloquer)
    # et renvoie { ExitCode, Output, Error }. ExitCode = -1 s'il n'a pas pu demarrer.
    param([Parameter(Mandatory = $true)][string]$FilePath, [string[]]$ArgumentList = @(), [int]$TimeoutSec = 120)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ConvertTo-CommandLine $ArgumentList
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $psi.WorkingDirectory = $RepoDir
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Output = ''; Error = $_.Exception.Message }
    }
    $proc.StandardInput.Close()
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill() } catch { }
        return [pscustomobject]@{ ExitCode = -1; Output = ''; Error = "delai depasse ($TimeoutSec s)" }
    }
    $proc.WaitForExit()   # attend aussi la fin des lectures asynchrones
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; Output = $outTask.Result.Trim(); Error = $errTask.Result.Trim() }
}

function Update-SessionPath {
    # Recharge le PATH depuis le registre : rend visibles les outils fraichement installes.
    $parts = @()
    foreach ($scope in @('Machine', 'User')) {
        $value = [Environment]::GetEnvironmentVariable('Path', $scope)
        if ($value) { $parts += $value -split ';' }
    }
    $parts += $env:Path -split ';'
    $seen = @{}
    $clean = New-Object System.Collections.Generic.List[string]
    foreach ($p in $parts) {
        if (-not $p) { continue }
        $key = $p.TrimEnd('\').ToLowerInvariant()
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $clean.Add($p)
    }
    $env:Path = $clean -join ';'
}

function Get-Winget {
    # Chemin de winget.exe, ou $null. Sous Windows 11, App Installer est livre mais
    # parfois pas encore enregistre pour l'utilisateur : on tente de l'enregistrer.
    if ($script:WingetSearched) { return $script:WingetPath }
    $script:WingetSearched = $true
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop } catch { }
        Update-SessionPath
        $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    }
    if ($cmd) { $script:WingetPath = $cmd.Source }
    else {
        $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
        if (Test-Path -LiteralPath $alias) { $script:WingetPath = $alias }
    }
    return $script:WingetPath
}

function Invoke-WingetInstall {
    # Installe un paquet winget. Renvoie $false si winget est absent.
    param([string]$Id, [switch]$UserScope)
    $winget = Get-Winget
    if (-not $winget) { return $false }
    $wingetArgs = @('install', '--id', $Id) + $WingetCommon
    if ($UserScope) { $wingetArgs += @('--scope', 'user') }
    $code = Invoke-Native $winget $wingetArgs
    Write-Info "winget install $Id : code $code"
    Update-SessionPath
    return $true
}

function Get-SignedDownload {
    # Telecharge un installeur dans %TEMP% et verifie sa signature Authenticode.
    # Renvoie le chemin du fichier, ou $null (fichier supprime) en cas de probleme.
    param([string]$Url, [string]$FileName, [string]$ExpectedSigner = '')
    $dest = Join-Path $env:TEMP $FileName
    try {
        Invoke-WebRequest -Uri $Url -OutFile $dest -UseBasicParsing
    } catch {
        Write-Warn "Telechargement impossible ($Url) : $($_.Exception.Message)"
        return $null
    }
    $sig = Get-AuthenticodeSignature -FilePath $dest
    $subject = ''
    if ($sig.SignerCertificate) { $subject = $sig.SignerCertificate.Subject }
    if ($sig.Status -ne 'Valid' -or ($ExpectedSigner -and $subject -notlike "*$ExpectedSigner*")) {
        Write-Warn "Signature invalide pour $FileName ($($sig.Status), '$subject') : installation annulee."
        Remove-Item -LiteralPath $dest -Force -ErrorAction SilentlyContinue
        return $null
    }
    return $dest
}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
function Test-Python {
    # Vrai si $Path est un Python $PythonTag 64 bits qui fonctionne.
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $r = Invoke-Capture $Path @('-c', "import sys, struct; print(sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8)")
    return ($r.ExitCode -eq 0 -and $r.Output -eq "$($PythonTag.Replace('.', ' ')) 64")
}

function Find-Python {
    # Cherche un Python $PythonTag 64 bits : registre (PEP 514), dossiers d'installation
    # standard, puis lanceur py.exe. Ne passe jamais par l'alias python.exe du Store.
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        $key = "$hive\Software\Python\PythonCore\$PythonTag\InstallPath"
        if (-not (Test-Path -LiteralPath $key)) { continue }
        $props = Get-ItemProperty -LiteralPath $key
        $exe = $props.PSObject.Properties['ExecutablePath']
        if ($exe -and $exe.Value) { $candidates.Add([string]$exe.Value) }
        $dir = $props.PSObject.Properties['(default)']
        if ($dir -and $dir.Value) { $candidates.Add((Join-Path ([string]$dir.Value) 'python.exe')) }
    }
    $short = $PythonTag.Replace('.', '')
    $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python$short\python.exe"))
    $candidates.Add((Join-Path $env:ProgramFiles "Python$short\python.exe"))
    foreach ($c in $candidates) { if (Test-Python $c) { return $c } }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $r = Invoke-Capture $launcher.Source @("-$PythonTag", '-c', 'import sys; print(sys.executable)')
        if ($r.ExitCode -eq 0 -and (Test-Python $r.Output)) { return $r.Output }
    }
    return $null
}

function Install-PythonFromPythonOrg {
    # Repli sans winget : dernier installeur 3.13.x x64 de python.org, signe par la PSF,
    # installe pour l'utilisateur courant (pas d'UAC), avec Tcl/Tk et le lanceur py.
    $base = 'https://www.python.org/ftp/python/'
    try {
        $listing = (Invoke-WebRequest -Uri $base -UseBasicParsing).Content
    } catch {
        Write-Warn "python.org injoignable : $($_.Exception.Message)"
        return $false
    }
    $pattern = 'href="(' + [regex]::Escape($PythonTag) + '\.\d+)/"'
    $versions = [regex]::Matches($listing, $pattern) | ForEach-Object { [version]$_.Groups[1].Value } | Sort-Object -Descending -Unique
    foreach ($v in $versions) {
        $file = "python-$v-amd64.exe"
        try { $null = Invoke-WebRequest -Uri "$base$v/$file" -Method Head -UseBasicParsing } catch { continue }
        Write-Info "Installation de Python $v depuis python.org..."
        $exe = Get-SignedDownload "$base$v/$file" $file 'Python Software Foundation'
        if (-not $exe) { return $false }
        $code = Invoke-Native $exe @('/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=1',
            'InstallLauncherAllUsers=0', 'Include_tcltk=1', 'Include_pip=1', 'Include_test=0', 'Shortcuts=0')
        Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
        Write-Info "Installeur Python : code $code"
        return ($code -eq 0)
    }
    Write-Warn "Aucun installeur Python $PythonTag x64 trouve sur python.org."
    return $false
}


# ---------------------------------------------------------------------------
# Git, Ollama
# ---------------------------------------------------------------------------
function Find-Git {
    $cmd = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @((Join-Path $env:ProgramFiles 'Git\cmd\git.exe'), (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd\git.exe'))) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Find-Ollama {
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @((Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'), (Join-Path $env:ProgramFiles 'Ollama\ollama.exe'))) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Test-OllamaServer {
    param([string]$Url)
    try {
        $null = Invoke-RestMethod -Uri ($Url.TrimEnd('/') + '/api/version') -TimeoutSec 3 -UseBasicParsing
        return $true
    } catch { return $false }
}

function Wait-OllamaServer {
    param([string]$Url, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-OllamaServer $Url) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Test-OllamaServer $Url)
}

function Test-OllamaModel {
    param([string]$Url, [string]$Model)
    $wanted = if ($Model -match ':') { $Model } else { "${Model}:latest" }
    try {
        $tags = Invoke-RestMethod -Uri ($Url.TrimEnd('/') + '/api/tags') -TimeoutSec 10 -UseBasicParsing
        foreach ($m in @($tags.models)) {
            if ($m.name -eq $wanted -or $m.name -eq $Model) { return $true }
        }
    } catch { }
    return $false
}


# ===========================================================================
# Installation
# ===========================================================================
if ($env:OS -ne 'Windows_NT') {
    Write-Host '[ECHEC] Ce script doit etre execute par PowerShell sous Windows.' -ForegroundColor Red
    exit 1
}
try {
    # Sortie UTF-8 sans BOM (chemins accentues, sortie de Python).
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
} catch { }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
Set-Location -LiteralPath $RepoDir

Write-Host '==================== Installation de Plume (Windows) ====================' -ForegroundColor Cyan
Write-Info "Dossier : $RepoDir"
Write-Info "Systeme : $([Environment]::OSVersion.VersionString)"

foreach ($required in @('plume.py', 'requirements.txt', 'lancer.bat')) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoDir $required))) {
        Stop-Install "$required introuvable dans $RepoDir : lancez ce script depuis le depot Plume."
    }
}
$arch = $env:PROCESSOR_ARCHITEW6432
if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
if ($arch -ne 'AMD64') {
    Stop-Install "Architecture $arch non prise en charge : Plume requiert Windows x64 (pas de paquets ARM64 pour CTranslate2/CUDA)."
}
$running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase) } catch { $false }
    })
if ($running.Count -gt 0) {
    Stop-Install 'Plume est en cours d''execution : fermez-le, puis relancez l''installation.'
}

# --- 1. Python --------------------------------------------------------------
Write-Step "Python $PythonTag (64 bits)"
$Python = Find-Python
if (-not $Python) {
    Write-Info "Python $PythonTag absent : installation via winget (sans droits admin)..."
    if (Invoke-WingetInstall "Python.Python.$PythonTag" -UserScope) { $Python = Find-Python }
}
if (-not $Python) {
    Write-Warn 'winget absent ou en echec : repli sur l''installeur officiel python.org.'
    if (Install-PythonFromPythonOrg) { $Python = Find-Python }
}
if (-not $Python) {
    Stop-Install "Python $PythonTag introuvable apres installation. Installez-le depuis https://www.python.org/downloads/ (option tcl/tk cochee), puis relancez."
}
$tk = Invoke-Capture $Python @('-c', 'import tkinter; print(tkinter.TkVersion)')
if ($tk.ExitCode -ne 0) {
    Stop-Install "Tkinter absent de $Python : Parametres > Applications > Python $PythonTag > Modifier, cochez 'tcl/tk and IDLE', puis relancez."
}
Write-Info "Python : $Python (Tk $($tk.Output))"
Add-Result 'OK' "Python $PythonTag" $Python

# --- 2. Environnement virtuel + dependances ----------------------------------
Write-Step 'Environnement virtuel (.venv)'
if (Test-Path -LiteralPath $VenvDir) {
    if (Test-Python $VenvPython) {
        Write-Info "Reutilise : $VenvDir"
    } else {
        Write-Info '.venv casse, incomplet ou cree par un autre Python (ex. depuis WSL) : recreation.'
        try { Remove-Item -LiteralPath $VenvDir -Recurse -Force }
        catch { Stop-Install "Suppression de $VenvDir impossible ($($_.Exception.Message)) : supprimez-le a la main, puis relancez." }
    }
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    $code = Invoke-Native $Python @('-m', 'venv', $VenvDir)
    if ($code -ne 0 -or -not (Test-Python $VenvPython)) { Stop-Install "Creation de .venv impossible (code $code)." }
    Write-Info "Cree : $VenvDir"
}
Add-Result 'OK' 'Environnement virtuel' $VenvDir

Write-Step 'Dependances Python (requirements.txt)'
Write-Info 'Premiere installation : ~1,5 Go a telecharger (bibliotheques CUDA incluses).'
$code = Invoke-Native $VenvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
if ($code -ne 0) { Write-Warn "Mise a jour de pip en echec (code $code) : on continue avec la version actuelle." }
$code = Invoke-Native $VenvPython @('-m', 'pip', 'install', '-r', (Join-Path $RepoDir 'requirements.txt'))
if ($code -ne 0) {
    Stop-Install "pip install -r requirements.txt a echoue (code $code) : verifiez la connexion et l'espace disque, puis relancez."
}
$code = Invoke-Native $VenvPython @('-m', 'pip', 'check')
if ($code -eq 0) { Add-Result 'OK' 'Dependances Python' 'requirements.txt installe, pip check sans conflit' }
else { Add-Result 'ATTENTION' 'Dependances Python' 'pip check signale des incoherences (voir ci-dessus)' }

# --- Carte graphique (information) --------------------------------------------
Write-Step 'Carte graphique'
$nvidia = @()
try { $nvidia = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match 'NVIDIA' }) } catch { }
if ($nvidia.Count -gt 0) {
    if (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
        Add-Result 'OK' 'GPU NVIDIA' "$($nvidia[0].Name) (pilote $($nvidia[0].DriverVersion))"
    } else {
        Add-Result 'ATTENTION' 'GPU NVIDIA' "$($nvidia[0].Name) sans pilote NVIDIA : installez-le (nvidia.com/drivers) pour l'acceleration GPU"
    }
} else {
    Add-Result 'INFO' 'GPU NVIDIA' 'aucun : transcription sur CPU (plus lente)'
}
Write-Info (($script:Results | Select-Object -Last 1).Detail)

# --- 3. Git for Windows (mise a jour automatique) ------------------------------
if (-not $NoGit) {
    Write-Step 'Git for Windows (mise a jour automatique par lancer.bat)'
    $Git = Find-Git
    if (-not $Git) {
        Write-Info 'Git absent : installation via winget...'
        if (Invoke-WingetInstall 'Git.Git' -UserScope) {
            $Git = Find-Git
            if (-not $Git) {
                Write-Info 'Installation utilisateur impossible : installation machine (une fenetre UAC peut s''ouvrir).'
                $null = Invoke-WingetInstall 'Git.Git'
                $Git = Find-Git
            }
        }
    }
    if (-not $Git) {
        Add-Result 'ATTENTION' 'Git for Windows' 'absent : lancer.bat demarrera Plume sans se mettre a jour'
    } else {
        $repo = Invoke-Capture $Git @('-C', $RepoDir, 'rev-parse', '--git-dir')
        if ($repo.ExitCode -ne 0 -and $repo.Error -match 'dubious ownership') {
            # Depot cree par un autre compte (ex. session admin) : on le declare sur.
            $null = Invoke-Capture $Git @('config', '--global', '--add', 'safe.directory', $RepoDir.Replace('\', '/'))
            $repo = Invoke-Capture $Git @('-C', $RepoDir, 'rev-parse', '--git-dir')
        }
        if ($repo.ExitCode -ne 0) {
            Add-Result 'ATTENTION' 'Mise a jour automatique' "dossier non gere par git ($($repo.Error)) : lancer.bat ne pourra pas se mettre a jour"
        } else {
            $origin = Invoke-Capture $Git @('-C', $RepoDir, 'remote', 'get-url', 'origin')
            if ($origin.ExitCode -ne 0) {
                Add-Result 'ATTENTION' 'Mise a jour automatique' 'pas de remote origin : lancer.bat ne pourra pas se mettre a jour'
            } elseif ($origin.Output -match '^(git@|ssh://)') {
                Add-Result 'ATTENTION' 'Mise a jour automatique' "origin en SSH ($($origin.Output)) : une cle SSH est requise cote Windows, sinon passez l'URL en HTTPS"
            } else {
                Add-Result 'OK' 'Git for Windows' "$Git (origin : $($origin.Output))"
            }
        }
    }
}

# --- 4. Mode IA : Ollama + modele ----------------------------------------------
$AIModel = $null
$AIUrl = $null
if (-not $NoAI) {
    Write-Step 'Mode IA : Ollama et modele local'
    $cfg = Invoke-Capture $VenvPython @($CheckScript, '--print-ai-config')
    if ($cfg.ExitCode -ne 0) { Stop-Install "Lecture de la configuration IA impossible : $($cfg.Error)" }
    $ai = ($cfg.Output -split "`n")[-1] | ConvertFrom-Json   # derniere ligne = JSON
    $AIModel = [string]$ai.model
    $AIUrl = [string]$ai.url
    Write-Info "Modele : $AIModel | Serveur : $AIUrl"

    if ($AIUrl -notmatch '^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?/?$') {
        Write-Info 'Serveur IA distant (PLUME_AI_URL) : pas d''installation locale d''Ollama.'
    } else {
        $Ollama = Find-Ollama
        if (-not $Ollama) {
            Write-Info 'Ollama absent : installation via winget...'
            $null = Invoke-WingetInstall 'Ollama.Ollama'
            $Ollama = Find-Ollama
        }
        if (-not $Ollama) {
            Write-Info 'Repli : installeur officiel ollama.com...'
            $setup = Get-SignedDownload 'https://ollama.com/download/OllamaSetup.exe' 'OllamaSetup.exe'
            if ($setup) {
                $code = Invoke-Native $setup @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-')
                Remove-Item -LiteralPath $setup -Force -ErrorAction SilentlyContinue
                Write-Info "Installeur Ollama : code $code"
                Update-SessionPath
                $Ollama = Find-Ollama
            }
        }
        if (-not $Ollama) {
            Add-Result 'ECHEC' 'Ollama' 'installation impossible : installez-le depuis https://ollama.com puis relancez'
        } else {
            Write-Info "Ollama : $Ollama"
            # L'installeur demarre souvent le serveur lui-meme : on lui laisse un instant.
            if (-not (Wait-OllamaServer $AIUrl 10)) {
                Write-Info 'Demarrage du serveur Ollama en arriere-plan...'
                Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden
                $null = Wait-OllamaServer $AIUrl 30
            }
            if (-not (Test-OllamaServer $AIUrl)) {
                Add-Result 'ECHEC' 'Ollama' "serveur injoignable sur $AIUrl"
            } else {
                if (Test-OllamaModel $AIUrl $AIModel) {
                    Write-Info "Modele $AIModel deja present."
                } else {
                    Write-Info "Telechargement du modele $AIModel (plusieurs Go)..."
                    $code = Invoke-Native $Ollama @('pull', $AIModel)
                    if ($code -ne 0) { Write-Warn "ollama pull $AIModel : code $code" }
                }
                Add-Result 'OK' 'Ollama' $Ollama
            }
        }
    }
}

# --- 5. Raccourcis ----------------------------------------------------------------
if (-not $NoShortcuts) {
    Write-Step 'Raccourcis Plume'
    $launcher = Join-Path $RepoDir 'lancer.bat'
    $icon = Join-Path $RepoDir 'plume.ico'
    $created = @()
    foreach ($folder in @('Programs', 'Desktop')) {
        try {
            $lnkPath = Join-Path ([Environment]::GetFolderPath($folder)) 'Plume.lnk'
            $shell = New-Object -ComObject WScript.Shell
            $lnk = $shell.CreateShortcut($lnkPath)
            $lnk.TargetPath = $launcher
            $lnk.WorkingDirectory = $RepoDir
            $lnk.IconLocation = "$icon,0"
            $lnk.WindowStyle = 7          # console de lancer.bat reduite ; elle se ferme seule
            $lnk.Description = 'Plume - dictee vocale locale'
            $lnk.Save()
            $created += $lnkPath
            Write-Info "Cree : $lnkPath"
        } catch {
            Write-Warn "Raccourci ($folder) impossible : $($_.Exception.Message)"
        }
    }
    if ($created.Count -eq 2) { Add-Result 'OK' 'Raccourcis' 'menu Demarrer + Bureau -> lancer.bat' }
    else { Add-Result 'ATTENTION' 'Raccourcis' 'creation partielle (voir ci-dessus)' }
}

# --- 6. Verification complete ------------------------------------------------------
Write-Step 'Verification complete (scripts\check_install.py)'
$checkArgs = @($CheckScript)
if ($AIModel) { $checkArgs += @('--ai-model', $AIModel, '--ai-url', $AIUrl) }
$code = Invoke-Native $VenvPython $checkArgs
if ($code -eq 0) { Add-Result 'OK' 'Verification' 'tous les controles bloquants passent (detail ci-dessus)' }
else { Add-Result 'ECHEC' 'Verification' 'au moins un controle en ECHEC (detail ci-dessus)' }

# --- Bilan -------------------------------------------------------------------------
Show-Summary
Write-Host ''
if (@($script:Results | Where-Object { $_.Status -eq 'ECHEC' }).Count -gt 0) {
    Write-Host 'Plume n''est pas pret : corrigez les points en ECHEC, puis relancez l''installation.' -ForegroundColor Red
    exit 1
}
Write-Host 'Plume est pret.' -ForegroundColor Green
Write-Host "Lancement : menu Demarrer > Plume, ou double-clic sur lancer.bat dans $RepoDir"
exit 0
