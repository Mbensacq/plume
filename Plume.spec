# -*- mode: python ; coding: utf-8 -*-
"""
Spec PyInstaller — version portable de Plume (dossier Windows autonome, sans
Python à installer sur la machine cible).

Construire (avec un venv contenant requirements.txt + requirements-dev.txt) :

    .venv\\Scripts\\python -m PyInstaller Plume.spec --noconfirm --clean

Résultat : dist\\Plume\\
    Plume.exe            application fenêtrée (usage normal)
    Plume-console.exe    même application AVEC console (logs visibles, --selftest)
    _internal\\           bibliothèques (partagées par les deux exécutables)

Le modèle Whisper n'est PAS embarqué (jusqu'à 3 Go) : il est téléchargé au
premier lancement dans models\\ à côté de l'exe, puis tout fonctionne hors ligne.

Variante : PLUME_BUILD_GPU=1 embarque aussi les DLL CUDA/cuDNN (~1,3 Go) si les
paquets nvidia-*-cu12 sont installés dans le venv (requirements-gpu.txt). Par
défaut, la version portable est CPU (légère) ; les utilisateurs GPU installent
depuis les sources.
"""
import glob
import os

from PyInstaller.utils.hooks import collect_all

GPU_BUILD = os.environ.get("PLUME_BUILD_GPU", "0") == "1"

datas, binaries, hiddenimports = [], [], []

# Paquets natifs / avec données embarquées : on collecte TOUT (DLL, .pyd,
# assets VAD Silero de faster_whisper, ffmpeg de PyAV, etc.).
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
            "soundcard", "huggingface_hub"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# DLL CUDA (cuBLAS / cuDNN / nvrtc) des paquets nvidia-*-cu12, en préservant
# l'arborescence nvidia/<sous-paquet>/bin que _setup_cuda_dll_path() (branche
# « figé ») ajoute au chemin de recherche des DLL.
if GPU_BUILD:
    try:
        import nvidia
        for base in nvidia.__path__:
            parent = os.path.dirname(base)              # ...\site-packages
            for dll in glob.glob(os.path.join(base, "*", "bin", "*.dll")):
                dest = os.path.relpath(os.path.dirname(dll), parent)  # nvidia\<sub>\bin
                binaries.append((dll, dest))
    except ImportError:
        print("PLUME_BUILD_GPU=1 mais paquets nvidia-*-cu12 absents : build CPU.")

# soundcard choisit son backend Windows dynamiquement et s'appuie sur cffi :
# on force ces imports que l'analyse statique peut rater.
hiddenimports += ["soundcard.mediafoundation", "_cffi_backend", "cffi"]

icon_file = os.path.join(SPECPATH, "plume.ico")
if os.path.exists(icon_file):
    datas.append((icon_file, "."))       # icône de fenêtre posée au runtime
else:
    icon_file = None

# Plume n'utilise ni torch ni transformers : exclus pour éviter tout gonflement.
excludes = ["torch", "torchaudio", "transformers", "tkinter.test", "unittest",
            "pydoc_data", "IPython", "matplotlib"]
if not GPU_BUILD:
    excludes.append("nvidia")

a = Analysis(
    ["plume.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, icon=icon_file)

exe = EXE(pyz, a.scripts, [], name="Plume", console=False,
          disable_windowed_traceback=True, **common)
exe_console = EXE(pyz, a.scripts, [], name="Plume-console", console=True, **common)

coll = COLLECT(
    exe, exe_console,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Plume",
)
