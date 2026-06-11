# -*- mode: python ; coding: utf-8 -*-
"""
Spec PyInstaller — build « portable » de Plume (exécutable Windows autonome).

Construire (depuis la racine du projet, avec le venv qui contient les
dépendances de requirements.txt + requirements-build.txt) :

    .venv\\Scripts\\python -m PyInstaller Plume.spec --noconfirm --clean

Résultat :
    dist\\Plume\\Plume.exe   -> dossier ENTIÈREMENT autonome (aucun Python à
    installer sur la machine cible). À copier tel quel sur une clé USB / un
    autre PC Windows. GPU NVIDIA utilisé si présent, repli CPU sinon.

Le modèle Whisper n'est PAS embarqué : il se télécharge au 1er lancement dans
« models\\ » à côté de l'exe (cf. MODEL_DIR dans plume.py). Le 1er démarrage
sur une nouvelle machine nécessite donc une connexion internet ; ensuite, tout
fonctionne hors ligne.

Astuce : poser PLUME_BUILD_CONSOLE=1 avant le build produit une variante AVEC
console (utile pour voir les logs / déboguer un 1er lancement).
"""
import os
import glob
from PyInstaller.utils.hooks import collect_all

# Application fenêtrée par défaut (comme pythonw). Console de debug si demandé.
CONSOLE = os.environ.get("PLUME_BUILD_CONSOLE", "0") == "1"
# Nom de l'exe et du dossier de sortie. Permet de produire plusieurs variantes
# CÔTE À CÔTE dans dist\ (ex. « Plume » fenêtré + « Plume-debug » avec console).
APP_NAME = os.environ.get("PLUME_BUILD_NAME", "Plume")
# Build « allégé » CPU : on n'embarque PAS les DLL CUDA (~1,3 Go) et on force le
# CPU + un modèle plus léger via un runtime hook. Destiné aux PC sans GPU NVIDIA.
CPU_ONLY = os.environ.get("PLUME_BUILD_CPU", "0") == "1"

datas, binaries, hiddenimports = [], [], []

# --- Paquets natifs / avec données embarquées : on collecte TOUT ---
# (DLL natives, .pyd, assets VAD de faster_whisper, ffmpeg de PyAV, etc.)
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av",
            "tokenizers", "soundcard"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# --- DLL CUDA (cuBLAS / cuDNN / nvrtc) des paquets nvidia-*-cu12 ---
# On les copie en préservant l'arborescence « nvidia/<sous-paquet>/bin » afin que
# _setup_cuda_dll_path() (branche « figé ») les retrouve sous
# sys._MEIPASS\nvidia\<sous-paquet>\bin et les ajoute au chemin de recherche.
# IGNORÉ pour le build CPU allégé (CPU_ONLY) : c'est là tout le gain de taille.
if not CPU_ONLY:
    try:
        import nvidia
        for base in nvidia.__path__:
            parent = os.path.dirname(base)              # ...\site-packages
            for dll in glob.glob(os.path.join(base, "*", "bin", "*.dll")):
                dest = os.path.relpath(os.path.dirname(dll), parent)  # nvidia\<sub>\bin
                binaries.append((dll, dest))
    except Exception:
        pass

# soundcard sélectionne son backend Windows dynamiquement (mediafoundation) et
# s'appuie sur cffi : on force ces imports que l'analyse statique peut rater.
hiddenimports += ["soundcard.mediafoundation", "_cffi_backend", "cffi"]

# --- Icône de l'application ---
# .ico à la racine du projet. On l'utilise pour l'icône de l'EXE (Explorateur) ET
# on l'embarque dans le paquet : l'icône de fenêtre / barre des tâches est posée au
# runtime via root.iconbitmap (l'icône de l'EXE ne s'y applique pas d'elle-même).
icon_file = os.path.join(SPECPATH, "plume.ico")
if not os.path.exists(icon_file):
    icon_file = None
if icon_file:
    datas.append((icon_file, "."))

# Plume n'utilise NI torch NI transformers : on les exclut pour
# éviter tout gonflement accidentel. Build CPU : on exclut aussi `nvidia` et on
# branche le runtime hook qui force CPU + modèle léger.
excludes = ["torch", "transformers"]
runtime_hooks = []
if CPU_ONLY:
    excludes.append("nvidia")
    rthook = os.path.join(SPECPATH, "pyi_rthook_cpu.py")
    if os.path.exists(rthook):
        runtime_hooks.append(rthook)

a = Analysis(
    ["plume.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=runtime_hooks,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=CONSOLE,
    disable_windowed_traceback=False,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
