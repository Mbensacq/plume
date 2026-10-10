#!/usr/bin/env bash
# -----------------------------------------------------------------------------
#  Plume — installation automatique depuis WSL
#
#  Plume est une application Windows native (capture WASAPI, raccourci global,
#  saisie clavier Win32) : elle ne peut pas tourner DANS WSL. Ce script, lancé
#  depuis WSL, prépare donc le côté Windows :
#    1. vérifie WSL et l'interopérabilité avec Windows ;
#    2. place le dépôt sur le disque Windows : sur place s'il y est déjà
#       (/mnt/c/...), sinon clone dans %USERPROFILE%\Plume (ou --dest) ;
#    3. délègue à scripts/install-windows.ps1 : Python 3.13, .venv, dépendances,
#       Git for Windows, Ollama + modèle IA, raccourcis, puis vérification
#       complète (tests, audio, GPU, modèle Whisper, mode IA).
#
#  Usage : ./install-wsl.sh [--dest CHEMIN] [--no-ai] [--no-git] [--no-shortcuts]
#  Idempotent : le relancer met à jour la copie Windows et revérifie tout.
# -----------------------------------------------------------------------------
# Lancé par « sh install-wsl.sh » (dash) : on bascule sur bash.
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SCRIPT_DIR
readonly PS1_REL='scripts\install-windows.ps1'
# shellcheck disable=SC1003  # antislash littéral voulu
readonly WIN_DRIVE_RE='^[A-Za-z]:\\'

if [[ -t 1 ]]; then
    C_STEP=$'\e[1;36m' C_WARN=$'\e[1;33m' C_ERR=$'\e[1;31m' C_OK=$'\e[1;32m' C_OFF=$'\e[0m'
else
    C_STEP='' C_WARN='' C_ERR='' C_OK='' C_OFF=''
fi
WARNINGS=()

step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_OFF"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    %s[ATTENTION]%s %s\n' "$C_WARN" "$C_OFF" "$*"; WARNINGS+=("$*"); }
die()  { printf '\n%s[ECHEC]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }
trap 'die "erreur inattendue ligne $LINENO (commande : $BASH_COMMAND)"' ERR

usage() {
    cat <<'EOF'
Usage : ./install-wsl.sh [options]

Installe et vérifie tout ce qu'il faut pour lancer Plume sous Windows, depuis WSL.
Ensuite : menu Démarrer > Plume (ou double-clic sur lancer.bat).

Options :
  --dest CHEMIN    Dossier Windows où placer Plume si le dépôt est dans le
                   système de fichiers Linux (défaut : %USERPROFILE%\Plume).
                   Accepte C:\chemin ou /mnt/c/chemin.
  --no-ai          Ne pas installer Ollama ni le modèle du mode IA.
  --no-git         Ne pas installer Git for Windows (pas de mise à jour auto).
  --no-shortcuts   Ne pas créer les raccourcis menu Démarrer / Bureau.
  -h, --help       Afficher cette aide.

Journal complet : ./install-wsl.sh 2>&1 | tee install.log
EOF
}

# --- Arguments ----------------------------------------------------------------
DEST_ARG='' NO_AI=0 NO_GIT=0 NO_SHORTCUTS=0
while (($#)); do
    case $1 in
        --dest)         (($# >= 2)) || die "--dest attend un chemin"; DEST_ARG=$2; shift 2 ;;
        --dest=*)       DEST_ARG=${1#*=}; shift ;;
        --no-ai)        NO_AI=1; shift ;;
        --no-git)       NO_GIT=1; shift ;;
        --no-shortcuts) NO_SHORTCUTS=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "option inconnue : $1 (voir --help)" ;;
    esac
done

# --- Outils -------------------------------------------------------------------
is_wsl() {
    [[ -n ${WSL_DISTRO_NAME:-} ]] || grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null
}

find_powershell() {
    local p
    p=$(command -v powershell.exe 2>/dev/null || true)
    if [[ -z $p ]]; then   # PATH Windows non importé (appendWindowsPath=false)
        # shellcheck disable=SC1003  # antislash littéral voulu
        p="$(wslpath -u 'C:\' 2>/dev/null || echo /mnt/c/)"
        p="${p%/}/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    fi
    [[ -f $p ]] && printf '%s\n' "$p"
}

# Exécute du code PowerShell. -EncodedCommand (UTF-16LE en base64) évite tout
# problème de guillemets entre bash, l'interop WSL et PowerShell. Le répertoire
# courant est placé sur le disque Windows (pas de chemin UNC \\wsl$).
ps_run() {
    local encoded
    encoded=$(printf '%s' "$1" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
    (cd "$WIN_CWD" && "$PS_EXE" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass \
        -EncodedCommand "$encoded" </dev/null)
}

# Valeur d'une expression PowerShell, transportée en base64 : les chemins
# accentués (C:\Users\Héloïse) survivent à l'encodage de la console Windows.
win_eval() {
    local b64
    b64=$(ps_run "\$ProgressPreference='SilentlyContinue'; [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]($1)))" | tr -d '\r\n') || return 1
    [[ -n $b64 ]] && printf '%s' "$b64" | base64 -d
}

# Chemin Windows d'un chemin Linux, même s'il n'existe pas encore (parent requis).
win_path_of() {
    local p=$1 parent
    if [[ -e $p ]]; then wslpath -w "$p"; return; fi
    [[ -d $(dirname -- "$p") ]] || return 1
    parent=$(wslpath -w "$(dirname -- "$p")")
    printf '%s\\%s\n' "${parent%\\}" "$(basename -- "$p")"   # « C:\ » -> « C: »
}

# URL SSH -> HTTPS : Git for Windows n'a en général pas de clé SSH, et une
# invite de clé d'hôte bloquerait lancer.bat (console réduite).
to_https() {
    local url=$1
    if [[ $url =~ ^git@([^:]+):(.+)$ ]]; then
        url="https://${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
    elif [[ $url =~ ^ssh://git@([^/:]+)(:[0-9]+)?/(.+)$ ]]; then
        url="https://${BASH_REMATCH[1]}/${BASH_REMATCH[3]}"
    fi
    printf '%s\n' "$url"
}

root_commits() { git -C "$1" rev-list --max-parents=0 HEAD 2>/dev/null | sort; }

# Copie Windows = clone git du dépôt WSL, avec l'origin réelle (GitHub) pour que
# lancer.bat se mette à jour seul. core.autocrlf=true : git WSL et Git for Windows
# voient alors les mêmes fins de ligne, sans faux fichiers modifiés.
deploy_clone() {
    local origin
    info "Clonage vers $TARGET_WIN"
    git clone --quiet -c core.autocrlf=true -- "$SCRIPT_DIR" "$TARGET_UNIX"
    origin=$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || true)
    if [[ -n $origin ]]; then
        git -C "$TARGET_UNIX" remote set-url origin "$(to_https "$origin")"
    else
        git -C "$TARGET_UNIX" remote remove origin
        warn "Le dépôt WSL n'a pas de remote origin : la copie Windows ne pourra pas se mettre à jour seule."
    fi
}

deploy_update() {
    local src_branch dest_branch
    src_branch=$(git -C "$SCRIPT_DIR" symbolic-ref --quiet --short HEAD || true)
    dest_branch=$(git -C "$TARGET_UNIX" symbolic-ref --quiet --short HEAD || true)
    if [[ -z $src_branch || $src_branch != "$dest_branch" ]]; then
        warn "Copie Windows sur la branche « ${dest_branch:-détachée} », dépôt WSL sur « ${src_branch:-détachée} » : copie Windows laissée telle quelle."
        return
    fi
    git -C "$TARGET_UNIX" config core.autocrlf true
    git -C "$TARGET_UNIX" update-index -q --refresh >/dev/null 2>&1 || true
    # --ff-only : jamais de retour en arrière si lancer.bat a déjà tiré des
    # commits plus récents depuis GitHub, jamais de modification locale écrasée.
    if git -C "$TARGET_UNIX" fetch --quiet -- "$SCRIPT_DIR" HEAD \
        && git -C "$TARGET_UNIX" merge --ff-only --quiet FETCH_HEAD >/dev/null; then
        info "Copie Windows à jour ($(git -C "$TARGET_UNIX" rev-parse --short HEAD), branche $dest_branch)."
    else
        warn "Copie Windows non mise à jour (modifications locales ou historiques divergents) : elle est conservée telle quelle."
    fi
}

deploy() {
    command -v git >/dev/null || die "git est requis dans WSL pour copier Plume côté Windows : sudo apt install git"
    git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1 \
        || die "$SCRIPT_DIR n'est pas un dépôt git : clonez Plume avec « git clone », puis relancez."
    if [[ -n $(git -C "$SCRIPT_DIR" status --porcelain 2>/dev/null) ]]; then
        warn "Modifications non commitées dans $SCRIPT_DIR : seul le dernier commit est copié côté Windows."
    fi

    if [[ ! -e $TARGET_UNIX ]] || { [[ -d $TARGET_UNIX ]] && [[ -z $(ls -A -- "$TARGET_UNIX") ]]; }; then
        deploy_clone
    elif [[ -d $TARGET_UNIX/.git && "$(root_commits "$TARGET_UNIX")" == "$(root_commits "$SCRIPT_DIR")" ]]; then
        deploy_update
    else
        die "$TARGET_WIN existe déjà et n'est pas une copie de Plume : choisissez un autre dossier avec --dest."
    fi
}

# =============================================================================
step "Vérification de WSL"
is_wsl || die "Ce script se lance depuis WSL (Windows Subsystem for Linux)."
for cmd in wslpath iconv base64 tr; do
    command -v "$cmd" >/dev/null || die "commande requise absente : $cmd"
done
PS_EXE=$(find_powershell) || die "powershell.exe introuvable côté Windows."
WIN_CWD=$(dirname -- "$PS_EXE")
readonly PS_EXE WIN_CWD
ps_run 'exit 0' >/dev/null 2>&1 \
    || die "Impossible d'exécuter powershell.exe : l'interopérabilité WSL → Windows est désactivée. Mettez « [interop] enabled=true » dans /etc/wsl.conf, faites « wsl --shutdown » depuis Windows, puis relancez."
info "WSL : ${WSL_DISTRO_NAME:-distribution inconnue}, interopérabilité Windows OK."

step "Emplacement de Plume côté Windows"
SRC_WIN=$(wslpath -w "$SCRIPT_DIR")
if [[ -n $DEST_ARG ]]; then
    if [[ $DEST_ARG =~ ^[A-Za-z]:[\\/] ]]; then
        TARGET_WIN=${DEST_ARG//\//\\}
        TARGET_UNIX=$(wslpath -u "$TARGET_WIN")
    else
        TARGET_UNIX=$(realpath -m -- "$DEST_ARG")
        TARGET_WIN=$(win_path_of "$TARGET_UNIX") || die "--dest : le dossier parent de $DEST_ARG n'existe pas."
    fi
    TARGET_UNIX=${TARGET_UNIX%/}
    [[ $TARGET_WIN =~ $WIN_DRIVE_RE ]] || die "--dest doit désigner un dossier sur un disque Windows (ex. C:\\Apps\\Plume)."
    [[ -d $(dirname -- "$TARGET_UNIX") ]] || die "--dest : le dossier parent de $TARGET_WIN n'existe pas."
elif [[ $SRC_WIN =~ $WIN_DRIVE_RE ]]; then
    TARGET_UNIX=$SCRIPT_DIR
    TARGET_WIN=$SRC_WIN
else
    WIN_HOME=$(win_eval "[Environment]::GetFolderPath('UserProfile')") || true
    [[ $WIN_HOME =~ $WIN_DRIVE_RE ]] || die "Dossier utilisateur Windows introuvable : précisez la destination avec --dest C:\\chemin\\Plume."
    TARGET_WIN="${WIN_HOME%\\}\\Plume"
    TARGET_UNIX="$(wslpath -u "$WIN_HOME")"
    TARGET_UNIX="${TARGET_UNIX%/}/Plume"
fi

if [[ $TARGET_UNIX == "$SCRIPT_DIR" ]]; then
    info "Dépôt déjà sur le disque Windows : installation sur place ($TARGET_WIN)."
else
    info "Dépôt dans le système de fichiers Linux ($SCRIPT_DIR) :"
    info "Windows ne peut pas l'exécuter efficacement depuis \\\\wsl\$, Plume est donc placé dans $TARGET_WIN."
    deploy
fi
[[ -f $TARGET_UNIX/scripts/install-windows.ps1 ]] \
    || die "scripts/install-windows.ps1 absent de $TARGET_WIN : commitez les scripts d'installation, puis relancez."

step "Installation côté Windows (PowerShell)"
ps_args=()
((NO_AI)) && ps_args+=(-NoAI)
((NO_GIT)) && ps_args+=(-NoGit)
((NO_SHORTCUTS)) && ps_args+=(-NoShortcuts)
rc=0
# Chemin relatif au dossier courant (Windows) : aucun espace à protéger.
(cd "$TARGET_UNIX" && "$PS_EXE" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$PS1_REL" "${ps_args[@]}") || rc=$?

echo
if ((rc == 0)); then
    printf '%sPlume est installé et vérifié.%s\n' "$C_OK" "$C_OFF"
    info "Lancement : menu Démarrer > Plume, ou double-clic sur lancer.bat dans $TARGET_WIN"
    if [[ $TARGET_UNIX != "$SCRIPT_DIR" ]]; then
        info "Windows utilise la copie $TARGET_WIN : lancer.bat la met à jour depuis GitHub à chaque"
        info "démarrage ; relancer ce script la resynchronise avec ce dépôt WSL."
    fi
else
    printf '%s[ECHEC]%s Installation incomplète (code %s) : voir le bilan ci-dessus, corrigez, puis relancez ./install-wsl.sh\n' \
        "$C_ERR" "$C_OFF" "$rc"
fi
if ((${#WARNINGS[@]})); then
    echo
    printf '%sPoints d'"'"'attention côté WSL :%s\n' "$C_WARN" "$C_OFF"
    printf '    - %s\n' "${WARNINGS[@]}"
fi
exit "$rc"
