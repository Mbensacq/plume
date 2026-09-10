# -*- coding: utf-8 -*-
"""
Plume — dictée vocale locale (speech-to-text), 100 % hors ligne.

Pile : faster-whisper (CTranslate2) + soundcard (WASAPI) + numpy + tkinter.
Cible : Windows 11, NVIDIA RTX 4090 (CUDA 12 / cuDNN 9), repli CPU automatique.

Interface : rendu net (DPI-aware), 3 thèmes (Sombre / Clair / Océan), bouton
circulaire et boutons arrondis dessinés sur Canvas. Thème et sources mémorisés.

Capture : micro(s) et/ou son du PC (loopback), au choix, plusieurs sources
mixables simultanément, via un sélecteur de périphériques.

Usage :
    python plume.py             # lance l'interface graphique
    python plume.py --selftest  # auto-test : charge le modèle, transcrit un
                                #   buffer synthétique, affiche le backend, sort.
"""

import os
import re
import sys
import time
import json
import errno
import queue
import threading
import traceback
import importlib.util

__version__ = "1.0.0"

FROZEN = bool(getattr(sys, "frozen", False))   # exécutable PyInstaller ?


# ---------------------------------------------------------------------------
# Emplacement de l'application (portable : tout est ancré sur le dossier de
# l'exécutable, que l'on soit lancé comme script .py ou comme .exe PyInstaller)
# ---------------------------------------------------------------------------
def _app_dir():
    """Dossier de base servant d'ancre aux fichiers de l'app (config, corrections,
    journal, modèle en version portable) : dossier de l'exécutable si l'app est
    « figée » (PyInstaller), sinon dossier de ce script."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()


def _bundle_dir():
    """Dossier des ressources embarquées (PyInstaller : _internal), sinon APP_DIR."""
    return getattr(sys, "_MEIPASS", None) or APP_DIR


def _icon_path():
    """Chemin du .ico de l'app s'il est présent (bundle figé en priorité, sinon à
    côté du script). Retourne None si absent."""
    for base in (_bundle_dir(), APP_DIR):
        p = os.path.join(base, "plume.ico")
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Journal : stderr (si console) + fichier plume.log à côté de l'app. Lancée via
# pythonw / l'exécutable fenêtré, l'app n'a pas de console : sans ce fichier,
# aucune erreur ne serait visible.
# ---------------------------------------------------------------------------
LOG_PATH = os.path.join(APP_DIR, "plume.log")
_LOG_MAX_BYTES = 1_000_000
_log_lock = threading.Lock()


def log(msg):
    """Écrit `msg` sur stderr et dans plume.log (horodaté). Ne lève jamais."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:
        pass
    with _log_lock:
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > _LOG_MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".old")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _message_box(title, text):
    """Boîte de dialogue système (Windows) pour les erreurs FATALES survenant avant
    ou sans interface Tk ; ailleurs, simple affichage sur stderr."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    print(f"{title} : {text}", file=sys.stderr)


def fatal(text, exc=None):
    """Journalise une erreur bloquante, l'affiche à l'utilisateur, puis quitte."""
    log("FATAL : " + text)
    if exc is not None:
        log("".join(traceback.format_exception(exc)))
    _message_box("Plume — erreur", text + f"\n\nDétails : {LOG_PATH}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constantes éditables
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000        # Hz — format attendu par Whisper (16 kHz mono)
LANGUAGE = "fr"            # langue forcée

# Taille du modèle Whisper. Priorité : $PLUME_MODEL, puis le choix fait dans les
# Paramètres (plume_config.json), sinon un défaut qui dépend du matériel :
# large-v3 sur GPU, un modèle plus léger sur CPU (large-v3 y est très lent).
DEFAULT_GPU_MODEL = "large-v3"
DEFAULT_CPU_MODEL = "small"
MODEL_SIZE = os.environ.get("PLUME_MODEL", "").strip() or None   # None = auto
KNOWN_MODELS = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]

# Taille approximative des modèles téléchargés (Mo), pour informer l'utilisateur.
MODEL_SIZES_MB = {"tiny": 75, "base": 145, "small": 485, "medium": 1530,
                  "large-v3-turbo": 1620, "turbo": 1620, "large-v3": 3090,
                  "large-v2": 3090, "large": 3090}

# Dossier de stockage du modèle Whisper (téléchargé au 1er lancement).
# Priorité : $PLUME_MODEL_DIR ; en version portable (exécutable) : models\ à côté
# de l'exe ; sinon None -> cache HuggingFace partagé (~/.cache/huggingface).
MODEL_DIR = (os.environ.get("PLUME_MODEL_DIR")
             or (os.path.join(APP_DIR, "models") if FROZEN else None))

# Durée maximale d'un enregistrement (s) : au-delà, l'enregistrement est arrêté
# automatiquement et transcrit. Borne la mémoire (16 kHz float32 = ~230 Mo/h par
# source) et le temps de transcription finale en mode « à l'arrêt ».
MAX_RECORD_SECONDS = 2 * 3600

# Repli CPU : si le chargement/inférence CUDA échoue, on retente en CPU/int8.
GPU_DEVICE = "cuda"
GPU_COMPUTE_TYPE = "float16"
CPU_DEVICE = "cpu"
CPU_COMPUTE_TYPE = "int8"

# Forcer le CPU (ignorer le GPU) si $PLUME_DEVICE=cpu — utile sur une machine sans
# carte NVIDIA (évite une tentative GPU vouée à l'échec et son message d'erreur).
FORCE_CPU = os.environ.get("PLUME_DEVICE", "").strip().lower() == "cpu"

# Qualité de décodage (beam search). 5 = bon compromis qualité/vitesse.
BEAM_SIZE = 5

# Durée minimale d'un enregistrement (en secondes) pour tenter une transcription.
MIN_RECORD_SECONDS = 0.2

# --- Transcription en direct (streaming) ---
# On transcrit en continu une file audio BORNÉE : chaque passe ne traite que
# l'audio depuis le dernier segment « figé » -> charge CPU/GPU linéaire, sans gros
# pic en fin d'enregistrement, et le texte s'affiche/s'affine au fur et à mesure.
DEFAULT_LIVE_MODE = False
LIVE_BEAM_SIZE = BEAM_SIZE   # même qualité que le mode « à l'arrêt »
LIVE_STEP = 0.6             # intervalle mini entre deux passes (s)
LIVE_MAX_TAIL = 30.0        # fenêtre révisable LONGUE : on peut revenir loin en arrière
LIVE_MIN_COMMIT = 6.0       # ne fige pas de trop petits bouts
LIVE_SILENCE_SEC = 2.0      # durée de silence pour figer (vraie pause, pas une hésitation)
LIVE_SILENCE_RMS = 0.006    # niveau RMS sous lequel on considère que c'est un silence

# --- Ponctuation ---
# Amorce de style : un court texte BIEN ponctué « conditionne » Whisper à
# produire davantage de virgules, points, points d'interrogation, etc.
INITIAL_PROMPT = ("Voici une transcription en français, soigneusement ponctuée. "
                  "Elle contient des virgules, des points, des points "
                  "d'interrogation ? Et parfois des points d'exclamation !")

# Nettoyage léger en sortie : majuscule en début, point final si absent,
# espaces autour de la ponctuation corrigés. Mettre False pour le texte brut.
AUTO_PUNCTUATION = True

# Conditionne chaque segment sur le texte précédent -> ponctuation/contexte plus
# cohérents. (True = défaut de faster-whisper ; explicite ici.)
CONDITION_ON_PREVIOUS_TEXT = True

# --- Raccourci global (Windows) ---
# Démarre/arrête la dictée même quand Plume n'est pas au premier plan. Le raccourci
# est configurable dans les Paramètres (capture au clavier, ou bouton latéral de
# souris). Modificateurs RegisterHotKey : 1=ALT, 2=CTRL, 4=SHIFT, 8=WIN.
HOTKEY_ENABLED = True
_NR = 0x4000  # MOD_NOREPEAT
# Raccourci par défaut : Ctrl + Alt + D.
DEFAULT_HOTKEY = {"type": "key", "mods": 0x0002 | 0x0001, "vk": 0x44,
                  "label": "Ctrl + Alt + D"}

_VK_NAMES = {0x20: "Espace", 0x0D: "Entrée", 0x09: "Tab", 0x08: "Retour",
             0x2E: "Suppr", 0x2D: "Inser", 0x24: "Origine", 0x23: "Fin",
             0x21: "PgPréc", 0x22: "PgSuiv",
             0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓"}


def _vk_name(vk):
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x70 <= vk <= 0x87:            # F1..F24
        return "F" + str(vk - 0x6F)
    if 0x30 <= vk <= 0x5A:            # 0-9, A-Z
        return chr(vk)
    if 0x60 <= vk <= 0x69:            # pavé numérique 0-9
        return "Num" + str(vk - 0x60)
    return f"Touche {vk}"


def _hotkey_label(mods, vk):
    parts = []
    if mods & 0x0002:
        parts.append("Ctrl")
    if mods & 0x0001:
        parts.append("Alt")
    if mods & 0x0004:
        parts.append("Maj")
    if mods & 0x0008:
        parts.append("Win")
    parts.append(_vk_name(vk))
    return " + ".join(parts)

# --- Sortie après transcription ---
# "manual" : rien (clic « Copier »). "copy" : copie automatique.
# "type"   : insertion directe (tape le texte dans la fenêtre active).
DEFAULT_OUTPUT_MODE = "manual"

# --- Corrections personnalisées ---
# Fichier éditable (JSON) : { "mal transcrit": "correction", ... }.
# Insensible à la casse, sur mots entiers. Les clés commençant par "_" sont ignorées.
REPLACEMENTS_PATH = os.path.join(APP_DIR, "plume_replacements.json")


# ---------------------------------------------------------------------------
# Configuration des DLL CUDA (cuBLAS / cuDNN) installées via pip
# ---------------------------------------------------------------------------
def _setup_cuda_dll_path():
    """Ajoute les répertoires `bin` des paquets nvidia-*-cu12 au chemin de
    recherche des DLL, afin que CTranslate2 trouve cuBLAS et cuDNN.

    Doit être appelé AVANT d'importer faster_whisper / ctranslate2.
    Retourne la liste des répertoires ajoutés (vide si rien trouvé)."""
    added = []
    bases = []

    # Version portable (PyInstaller) : les DLL sont collectées dans le bundle sous
    # _internal/nvidia/<sous-paquet>/bin, un dossier que le bootloader n'ajoute
    # pas au chemin de recherche des DLL.
    if FROZEN:
        bases.append(os.path.join(_bundle_dir(), "nvidia"))

    # Installation pip : `nvidia` est un paquet d'espace de noms (PEP 420) : pas
    # de __file__ ; on parcourt __path__ pour localiser site-packages/nvidia.
    try:
        import nvidia  # paquets nvidia-cublas-cu12, nvidia-cudnn-cu12, ...
        bases += list(getattr(nvidia, "__path__", []))
    except ImportError:
        pass

    for base in bases:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            bindir = os.path.join(base, sub, "bin")
            if os.path.isdir(bindir) and bindir not in added:
                # add_dll_directory : nécessaire sous Windows depuis Python 3.8
                # pour que les DLL dépendantes des extensions C soient résolues.
                try:
                    os.add_dll_directory(bindir)
                except (OSError, AttributeError):
                    pass
                # Filet de sécurité : préfixer le PATH du process.
                os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
                added.append(bindir)
    return added


ADDED_DLL_DIRS = _setup_cuda_dll_path()

try:
    import numpy as np  # noqa: E402  (après _setup_cuda_dll_path, par cohérence)
except ImportError as _e:
    fatal("Dépendances Python manquantes (numpy). Installez-les avec :\n"
          "    python -m pip install -r requirements.txt\n"
          "ou lancez install.bat.", _e)


# ---------------------------------------------------------------------------
# Détection VAD (filtrage des silences pour limiter les hallucinations)
# ---------------------------------------------------------------------------
def _vad_available():
    """Le filtre VAD de faster-whisper (Silero) nécessite onnxruntime."""
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


VAD_FILTER = _vad_available()


def _capture_available():
    """La capture audio (micros et son système) repose sur 'soundcard'
    (loopback WASAPI). Disponible sous Windows si le paquet est installé."""
    return (sys.platform == "win32"
            and importlib.util.find_spec("soundcard") is not None)


CAPTURE_AVAILABLE = _capture_available()


# ---------------------------------------------------------------------------
# Chargement du modèle (GPU avec repli CPU)
# ---------------------------------------------------------------------------
class ModelError(Exception):
    """Échec de chargement du modèle, avec un message déjà compréhensible pour
    l'utilisateur (pas de trace brute)."""


def _is_network_error(e):
    """Vrai si l'exception (ou une de ses causes) ressemble à un problème réseau
    ou à un modèle absent du cache hors ligne (huggingface_hub / requests)."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        name = type(e).__name__
        text = str(e).lower()
        if name in ("LocalEntryNotFoundError", "ConnectionError", "ConnectTimeout",
                    "ReadTimeout", "MaxRetryError", "NewConnectionError",
                    "OfflineModeIsEnabled", "HfHubHTTPError", "RepositoryNotFoundError",
                    "GatedRepoError", "SSLError", "ProxyError", "URLError",
                    "gaierror", "timeout"):
            return True
        if any(k in text for k in ("connection", "network", "resolve", "timed out",
                                    "offline", "huggingface.co", "name or service",
                                    "getaddrinfo", "unreachable")):
            return True
        e = e.__cause__ or e.__context__
    return False


def _is_disk_full(e):
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT):
            return True
        if "no space left" in str(e).lower():
            return True
        e = e.__cause__ or e.__context__
    return False


def describe_model_error(e, model_size, model_dir=None):
    """Traduit une exception de chargement en message clair (une phrase, avec la
    marche à suivre). Retourne str."""
    size_mb = MODEL_SIZES_MB.get(str(model_size), 0)
    size_txt = f" (~{size_mb / 1000:.1f} Go)" if size_mb >= 1000 else (
        f" (~{size_mb} Mo)" if size_mb else "")
    where = model_dir or os.path.join("~", ".cache", "huggingface", "hub")
    if isinstance(e, ModelError):
        return str(e)
    if isinstance(e, ImportError):
        return ("Dépendance manquante : " + str(e) + ". Installez les dépendances : "
                "python -m pip install -r requirements.txt (ou install.bat).")
    if _is_disk_full(e):
        return (f"Disque plein : impossible d'écrire le modèle « {model_size} »{size_txt} "
                f"dans {where}. Libérez de l'espace puis relancez.")
    if _is_network_error(e):
        return (f"Modèle « {model_size} »{size_txt} introuvable en local et pas de "
                "connexion internet. Connectez-vous une fois pour le télécharger "
                f"(il est ensuite conservé dans {where}), ou choisissez un modèle "
                "déjà téléchargé dans les Paramètres.")
    if isinstance(e, (PermissionError,)):
        return (f"Accès refusé en écriture dans {where}. Choisissez un autre dossier "
                "via PLUME_MODEL_DIR ou vérifiez les droits.")
    if isinstance(e, MemoryError) or "out of memory" in str(e).lower():
        return (f"Mémoire insuffisante pour le modèle « {model_size} ». Choisissez un "
                "modèle plus petit (Paramètres) : medium, small…")
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    if "invalid model" in text.lower() or "not a valid" in text.lower() \
            or "repo id" in text.lower():
        return (f"Modèle « {model_size} » inconnu. Choix possibles : "
                + ", ".join(KNOWN_MODELS) + ".")
    return f"Échec du chargement du modèle « {model_size} » : {text[:160]}"


def _try_load(device, compute_type, model_size):
    """Construit le modèle puis exécute une petite passe de chauffe.

    La passe de chauffe force l'initialisation complète (allocation GPU,
    chargement de cuDNN, exécution des convolutions de l'encodeur) : un échec
    CUDA *réel* survient ici plutôt qu'à la première dictée de l'utilisateur.
    """
    from faster_whisper import WhisperModel

    # download_root : None (défaut) => cache HuggingFace partagé ; sinon dossier
    # imposé via $PLUME_MODEL_DIR (ou models\ en version portable).
    if MODEL_DIR:
        os.makedirs(MODEL_DIR, exist_ok=True)
    model = WhisperModel(model_size, device=device, compute_type=compute_type,
                         download_root=MODEL_DIR)

    # Chauffe : 1 s de signal, VAD désactivé pour garantir le passage dans
    # l'encodeur (convolutions cuDNN) même si le buffer est quasi silencieux.
    warm = (0.01 * np.sin(2 * np.pi * 220 *
            np.linspace(0, 1, SAMPLE_RATE, endpoint=False))).astype(np.float32)
    segments, _ = model.transcribe(warm, language=LANGUAGE, beam_size=1,
                                   vad_filter=False)
    list(segments)  # consommer le générateur => exécute réellement l'inférence
    return model


def resolve_model_size(requested=None, backend_is_gpu=True):
    """Taille de modèle effective : $PLUME_MODEL > `requested` (Paramètres) >
    défaut selon le matériel (large-v3 sur GPU, plus léger sur CPU)."""
    if MODEL_SIZE:
        return MODEL_SIZE
    if requested:
        return str(requested).strip()
    return DEFAULT_GPU_MODEL if backend_is_gpu else DEFAULT_CPU_MODEL


def load_model(prefer_gpu=True, requested_size=None):
    """Charge le modèle Whisper. Tente le GPU (CUDA/float16) puis se rabat sur
    le CPU (int8). Retourne (model, backend_str, model_size).
    Lève ModelError avec un message compréhensible en cas d'échec définitif."""
    gpu_size = resolve_model_size(requested_size, backend_is_gpu=True)
    cpu_size = resolve_model_size(requested_size, backend_is_gpu=False)
    gpu_error = None
    if prefer_gpu and not FORCE_CPU:
        try:
            model = _try_load(GPU_DEVICE, GPU_COMPUTE_TYPE, gpu_size)
            return model, "GPU (CUDA)", gpu_size
        except Exception as e:
            gpu_error = e
            log("Chargement CUDA échoué -> repli CPU. Cause : "
                f"{type(e).__name__}: {str(e)[:300]}")
            if gpu_size == cpu_size and (_is_network_error(e) or _is_disk_full(e)):
                # Inutile de retenter en CPU : le modèle lui-même est indisponible.
                raise ModelError(describe_model_error(e, gpu_size, MODEL_DIR)) from e
    try:
        model = _try_load(CPU_DEVICE, CPU_COMPUTE_TYPE, cpu_size)
    except Exception as e:
        log("Chargement CPU échoué : " + "".join(traceback.format_exception(e)))
        raise ModelError(describe_model_error(e, cpu_size, MODEL_DIR)) from e
    backend = "CPU"
    if gpu_error is not None and ADDED_DLL_DIRS:
        # Les bibliothèques CUDA sont installées mais inutilisables (pilote,
        # carte…) : on le signale ; en installation CPU, c'est le cas normal.
        backend = "CPU (GPU indisponible)"
    return model, backend, cpu_size


def load_replacements(path=None):
    """Charge le dictionnaire de corrections personnalisées (si présent).
    Ignore les clés commençant par « _ » (commentaires/exemples).
    Un fichier absent ou invalide donne un dictionnaire vide (voir
    check_replacements_file pour le diagnostic)."""
    path = path or REPLACEMENTS_PATH
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items()
                if isinstance(k, str) and not k.startswith("_")
                and k.strip() and isinstance(v, str)}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"Corrections ignorées : {os.path.basename(path)} invalide ({e})")
        return {}


def check_replacements_file(path=None):
    """Diagnostic du fichier de corrections : None si tout va bien (ou absent),
    sinon un message d'erreur court destiné à l'utilisateur."""
    path = path or REPLACEMENTS_PATH
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        return (f"{os.path.basename(path)} : JSON invalide (ligne {e.lineno}, "
                f"colonne {e.colno}) — corrections désactivées.")
    except Exception as e:
        return f"{os.path.basename(path)} illisible ({type(e).__name__})."
    if not isinstance(raw, dict):
        return f"{os.path.basename(path)} : un objet {{ \"mot\": \"correction\" }} est attendu."
    return None


def apply_replacements(text, repl):
    """Applique les corrections (mots entiers, insensible à la casse).

    Les frontières sont « pas de caractère de mot juste avant/après » plutôt
    que \\b, pour que des clés finissant par un signe (« c++ », « .net »)
    fonctionnent aussi."""
    if not text or not repl:
        return text
    for wrong, right in repl.items():
        if not wrong:
            continue
        text = re.sub(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", right, text,
                      flags=re.IGNORECASE)
    return text


# Phrases « parasites » que Whisper hallucine sur du silence ou du son sans
# parole : ce sont des génériques de sous-titres de vidéos, vus à l'entraînement.
# On jette tout segment qui en contient (ex. « Sous-titrage… Amara.org »).
_HALLUCINATION_RE = re.compile(
    r"sous[\-\s]?titr"                 # sous-titrage / sous-titres…
    r"|amara\.org|soustitreur"
    r"|radio[\-\s]?canada"
    r"|merci d'avoir regard"           # merci d'avoir regardé (cette vidéo)
    r"|thanks for watching|subtitl"
    r"|[♪♫🎵]",
    re.IGNORECASE,
)


def _join_segments(segments):
    """Assemble le texte des segments en écartant les hallucinations connues
    (génériques de sous-titres) que Whisper produit sur du silence."""
    parts = []
    for seg in segments:
        t = seg.text.strip()
        if t and not _HALLUCINATION_RE.search(t):
            parts.append(t)
    return " ".join(parts).strip()


def _iter_with_progress(segments, total_seconds, progress):
    """Relaie l'avancement (secondes d'audio traitées / total) à `progress`
    au fil des segments produits par faster-whisper."""
    for seg in segments:
        if progress is not None:
            try:
                progress(min(float(seg.end), total_seconds), total_seconds)
            except Exception:
                pass
        yield seg


def transcribe_audio(model, audio, progress=None):
    """Transcrit un buffer (np.float32 16 kHz mono) en texte ponctué.
    Centralise les options qui favorisent la ponctuation + les corrections.
    `progress(done_s, total_s)` est appelé (thread appelant) à chaque segment."""
    segments, _info = model.transcribe(
        audio,
        language=LANGUAGE,
        beam_size=BEAM_SIZE,
        vad_filter=VAD_FILTER,
        initial_prompt=INITIAL_PROMPT or None,
        condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
    )
    total = float(audio.size) / SAMPLE_RATE
    text = _join_segments(_iter_with_progress(segments, total, progress))
    text = apply_replacements(text, load_replacements())
    return postprocess_text(text)


# --- Mode IA : reformulation par un LLM LOCAL (Ollama) ----------------------
# 100 % local : Plume dialogue avec le serveur Ollama sur la machine (localhost).
# Surchargeable via $PLUME_AI_URL / $PLUME_AI_MODEL.
AI_URL = os.environ.get("PLUME_AI_URL") or "http://localhost:11434"
AI_MODEL_DEFAULT = os.environ.get("PLUME_AI_MODEL") or "llama3.1:8b"
AI_SYSTEM = (
    "Tu es un assistant d'écriture en français. On te donne une dictée brute "
    "(un brouillon, parfois mal formulé, avec des fautes). Réécris-la en un texte "
    "clair, naturel et bien tourné : corrige l'orthographe, la grammaire et la "
    "ponctuation, structure les phrases proprement, MAIS conserve le sens, les "
    "informations et le ton d'origine, sans rien inventer ni ajouter de commentaire. "
    "Réponds UNIQUEMENT par le texte réécrit, sans préambule ni guillemets."
)


class AIError(Exception):
    """Erreur explicite du mode IA (message déjà prêt pour l'utilisateur)."""


def ai_reformulate(text, model, url=None, timeout=120):
    """Reformule `text` via un LLM local Ollama (endpoint /api/chat).
    Lève AIError avec un message clair si Ollama est injoignable ou le modèle absent."""
    import urllib.request
    import urllib.error
    base = (url or AI_URL).rstrip("/")
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": AI_SYSTEM},
                     {"role": "user", "content": text}],
        "stream": False,
        "options": {"temperature": 0.4},
    }).encode("utf-8")
    req = urllib.request.Request(base + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise AIError(f"Modèle IA « {model} » absent — faites : ollama pull {model}")
        raise AIError(f"IA : erreur Ollama ({e.code}).")
    except Exception:
        raise AIError("IA : Ollama injoignable — installez-le et lancez-le "
                      "(ollama.com), puis « ollama pull " + str(model) + " ».")
    return (data.get("message", {}) or {}).get("content", "").strip()


def _strip_ellipsis(text):
    """Retire les « … » / « ... » en tête et en fin — fréquents sur un extrait
    coupé en pleine phrase — pour un rendu propre en direct (sans les garder)."""
    text = re.sub(r"^\s*(?:\.{2,}|…)+\s*", "", text)
    text = re.sub(r"\s*(?:\.{2,}|…)+\s*$", "", text)
    return text.strip()


def transcribe_stream(model, audio, repl, prompt=None):
    """Transcription d'un extrait pour l'affichage en direct, à la MÊME qualité
    que le mode « à l'arrêt » (beam, VAD, contexte). `prompt` = texte déjà figé,
    redonné pour la continuité. Sans post-traitement (le texte est encore en
    cours) et sans « … » de fin (extrait volontairement coupé)."""
    segments, _info = model.transcribe(
        audio,
        language=LANGUAGE,
        beam_size=LIVE_BEAM_SIZE,
        vad_filter=VAD_FILTER,
        initial_prompt=(prompt or INITIAL_PROMPT) or None,
        condition_on_previous_text=True,
    )
    text = _join_segments(segments)
    return apply_replacements(_strip_ellipsis(text), repl)


def rms(audio):
    """Énergie RMS d'un buffer float32 (0.0 si vide)."""
    if audio is None or audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))


# Son neuf minimal (s) pour qu'une passe en direct vaille la peine.
LIVE_MIN_PASS_SEC = 0.4


def live_should_commit(tail, tail_dur, final=False):
    """Décide si la queue audio courante doit être FIGÉE (rendue non révisable).

    On ne fige que sur une VRAIE pause (silence prolongé en fin de queue) ou si
    la fenêtre révisable devient trop longue : une simple hésitation ne fige
    rien, tout reste révisable comme en mode « à l'arrêt »."""
    if final:
        return True
    if tail_dur < LIVE_MIN_COMMIT:
        return False
    if tail_dur >= LIVE_MAX_TAIL:
        return True
    pause = tail[-int(LIVE_SILENCE_SEC * SAMPLE_RATE):]
    return bool(pause.size) and rms(pause) < LIVE_SILENCE_RMS


class LiveSession:
    """État d'une dictée en direct : texte déjà figé et index audio figé.

    Indépendant de l'interface et du modèle : `step()` reçoit la queue audio
    non figée et une fonction de transcription, et retourne le texte à
    afficher. Testable sans matériel audio ni modèle."""

    def __init__(self, prefix=""):
        self.prefix = prefix          # texte déjà présent dans le panneau au départ
        self.committed = ""           # texte figé de la dictée en cours
        self.commit_sample = 0        # index audio (échantillons) figé

    def _prompt(self):
        if self.committed:
            return (INITIAL_PROMPT + " " + self.committed[-200:]).strip()
        return None

    def _join_prefix(self, body):
        if self.prefix and body:
            return (self.prefix + " " + body).strip()
        return (self.prefix or body).strip()

    def step(self, tail, total, transcribe, final=False):
        """Une passe. `tail` : audio depuis `commit_sample` ; `total` : nombre
        d'échantillons capturés au total ; `transcribe(audio, prompt) -> str`.

        Retourne (texte_affiché, texte_final_nettoyé_ou_None), ou None si la
        passe est inutile (pas assez de son neuf)."""
        tail = np.asarray(tail, dtype=np.float32).reshape(-1)
        tail_dur = tail.size / SAMPLE_RATE
        if not final and tail.size < int(LIVE_MIN_PASS_SEC * SAMPLE_RATE):
            return None
        # Queue quasi silencieuse -> on ne transcrit pas (évite les hallucinations).
        text = transcribe(tail, self._prompt()) if rms(tail) >= LIVE_SILENCE_RMS else ""
        text = (text or "").strip()
        if live_should_commit(tail, tail_dur, final):
            if text:
                self.committed = (self.committed + " " + text).strip()
            self.commit_sample = max(self.commit_sample, int(total))
            body = self.committed
        else:
            body = (self.committed + " " + text).strip()
        if final:
            clean = postprocess_text(self.committed)
            return self._join_prefix(clean), clean
        return self._join_prefix(body), None


def postprocess_text(text):
    """Nettoyage TRÈS léger : on préserve la ponctuation de Whisper (déjà
    correcte, espace fine à la française avant « ? ! ; : » incluse) et on se
    contente de corriger la forme. N'invente AUCUNE virgule."""
    if not text:
        return ""
    if not AUTO_PUNCTUATION:
        return text
    s = re.sub(r"\s+", " ", text).strip()
    # espace parasite avant virgule/point uniquement (en FR, on garde l'espace
    # avant ? ! ; : que Whisper produit déjà correctement)
    s = re.sub(r"\s+([,.])", r"\1", s)
    if not s:
        return ""
    # majuscule initiale
    s = s[0].upper() + s[1:]
    # point final si la phrase se termine par une lettre/chiffre (pas de doublon)
    if s[-1] not in ".!?…\"»)":
        s += "."
    return s


# ---------------------------------------------------------------------------
# Capture audio (soundcard / WASAPI) — micros et sorties (loopback)
# ---------------------------------------------------------------------------
def describe_capture_error(errors):
    """Message utilisateur à partir des erreurs de capture remontées par le
    Recorder (liste de str « nom — Exception: détail »)."""
    joined = " ; ".join(errors).lower()
    if not errors:
        return "Capture impossible."
    if "loopback" in joined or "render" in joined or "0x88890008" in joined:
        return ("Capture du son du PC impossible : la sortie audio choisie est "
                "indisponible (débranchée, désactivée ou en usage exclusif). "
                "Choisissez une autre source dans « Sources… ».")
    if "0x88890004" in joined or "device" in joined or "not found" in joined \
            or "invalidated" in joined or "0x88890026" in joined:
        return ("Périphérique audio indisponible (micro débranché ou désactivé). "
                "Rebranchez-le, puis « Sources… » → Actualiser.")
    if "access" in joined or "denied" in joined or "0x80070005" in joined:
        return ("Accès au micro refusé. Autorisez-le dans Windows : Paramètres → "
                "Confidentialité → Microphone.")
    first = errors[0].split(" — ", 1)
    return f"Capture impossible ({first[0]}) : {first[-1][:100]}"


def describe_enumeration_error(e):
    """Message utilisateur quand l'énumération des périphériques échoue."""
    if not CAPTURE_AVAILABLE:
        return ("Capture audio indisponible sur ce système (Windows + paquet "
                "soundcard requis).")
    return ("Impossible de lister les périphériques audio "
            f"({type(e).__name__}). Vérifiez qu'un micro ou une sortie audio est "
            "branché(e) et activé(e) dans Windows, puis « Sources… » → Actualiser.")


def enumerate_devices():
    """Énumère les périphériques de capture via soundcard.

    À appeler depuis un THREAD dédié (jamais le thread Tk) : COM doit être
    initialisé sur ce thread. Retourne un dict :
        {"inputs": [...], "outputs": [...], "default_mic_id": str|None}
    où chaque entrée est {"id", "name", "loopback"}.
    Les "inputs" sont les micros/entrées ; les "outputs" sont les sorties
    captées en loopback (le « son du PC »)."""
    import soundcard as sc
    inputs, outputs = [], []
    default_mic_id = None
    try:
        default_mic_id = sc.default_microphone().id
    except Exception:
        pass
    for m in sc.all_microphones(include_loopback=True):
        entry = {"id": m.id, "name": m.name, "loopback": bool(m.isloopback)}
        (outputs if m.isloopback else inputs).append(entry)
    return {"inputs": inputs, "outputs": outputs, "default_mic_id": default_mic_id}


class Recorder:
    """Capture audio en 16 kHz mono float32 depuis UNE OU PLUSIEURS sources
    (micros et/ou sorties en loopback), via soundcard.

    Chaque source est capturée dans son propre thread (record() bloquant +
    CoInitialize COM requis). À l'arrêt, les sources sont mélangées (mix) :
    alignées au début, complétées par des zéros à la longueur max, sommées,
    puis normalisées pour éviter la saturation.

    Une source est un dict {"id": str, "name": str, "loopback": bool}.
    """

    def __init__(self, samplerate=SAMPLE_RATE):
        self.samplerate = samplerate
        self.sources = []
        self._lock = threading.Lock()
        self._buffers = {}      # index source -> liste de np.ndarray
        self._counts = {}       # index source -> nb d'échantillons capturés
        self._failed = set()    # index des sources dont la capture a échoué
        self._threads = []
        self._running = False
        self._errors = []

    def set_sources(self, sources):
        self.sources = list(sources)

    def consume_errors(self):
        with self._lock:
            errs = self._errors
            self._errors = []
        return errs

    def all_failed(self):
        """Vrai si TOUTES les sources ont cessé de capturer sur erreur (micro
        débranché, périphérique de loopback indisponible…)."""
        with self._lock:
            return bool(self.sources) and len(self._failed) >= len(self.sources)

    def total_samples(self):
        """Longueur (échantillons) de l'audio capturé jusqu'ici (source la plus longue)."""
        with self._lock:
            return max(self._counts.values(), default=0)

    # ----- démarrage -----
    def start(self):
        """Démarre la capture de toutes les sources sélectionnées."""
        if not self.sources:
            raise RuntimeError("aucune source sélectionnée")
        with self._lock:
            self._buffers = {i: [] for i in range(len(self.sources))}
            self._counts = {i: 0 for i in range(len(self.sources))}
            self._failed = set()
            self._errors = []
        self._running = True
        self._threads = []
        for i, src in enumerate(self.sources):
            t = threading.Thread(target=self._capture_one, args=(i, src),
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def _capture_one(self, i, src):
        try:
            import ctypes
            ctypes.windll.ole32.CoInitialize(None)  # COM requis sur ce thread
        except Exception:
            pass
        try:
            import soundcard as sc
            mic = sc.get_microphone(src["id"],
                                    include_loopback=bool(src.get("loopback")))
            with mic.recorder(samplerate=self.samplerate, channels=1,
                              blocksize=1024) as rec:
                while self._running:
                    data = rec.record(numframes=1600)  # ~0,1 s ; zéros si silence
                    arr = np.asarray(data, dtype=np.float32).reshape(-1)
                    with self._lock:
                        self._buffers[i].append(arr)
                        self._counts[i] += arr.size
        except Exception as e:
            msg = f"{src.get('name', src.get('id'))} — {type(e).__name__}: {e}"
            with self._lock:
                self._errors.append(msg)
                self._failed.add(i)
            log(f"[audio] {msg}")
        # NB : pas de CoUninitialize (le thread se termine, COM est nettoyé) ;
        # un CoUninitialize ici pourrait casser le COM partagé de soundcard.

    @staticmethod
    def _mix(buffers):
        """Mixe les buffers (dict index -> liste d'ndarray) en un signal 1D :
        somme alignée au début, complétée par des zéros, puis normalisée."""
        arrays = []
        for i in sorted(buffers):
            if buffers[i]:
                a = np.concatenate(buffers[i]).reshape(-1)
                if a.size:
                    arrays.append(a)
        if not arrays:
            return np.zeros(0, dtype=np.float32)
        if len(arrays) == 1:
            return arrays[0]
        n = max(a.size for a in arrays)
        mix = np.zeros(n, dtype=np.float32)
        for a in arrays:
            mix[:a.size] += a
        peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        if peak > 1.0:
            mix /= peak
        return mix

    @staticmethod
    def _tail_of(chunks, count, since):
        """Échantillons [since, count) d'une source, en ne concaténant que les
        derniers morceaux nécessaires (coût borné par la taille de la queue, pas
        par la durée totale de l'enregistrement)."""
        need = count - since
        if need <= 0:
            return np.zeros(0, dtype=np.float32)
        parts, got = [], 0
        for a in reversed(chunks):
            parts.append(a)
            got += a.size
            if got >= need:
                break
        arr = np.concatenate(parts[::-1]).reshape(-1)
        return arr[-need:] if arr.size > need else arr

    def snapshot(self, since=0):
        """Retourne (audio, total) SANS arrêter la capture : `audio` est le mix
        des échantillons capturés depuis l'index `since`, `total` le nombre
        d'échantillons capturés jusqu'ici. Sert à la transcription en direct :
        seule la queue non figée est reconstruite à chaque passe."""
        with self._lock:
            chunks = {i: list(v) for i, v in self._buffers.items()}
            counts = dict(self._counts)
        total = max(counts.values(), default=0)
        tails = {i: [self._tail_of(chunks[i], counts[i], since)] for i in chunks}
        return self._mix(tails), total

    # ----- arrêt -----
    def stop(self):
        """Arrête toutes les captures et retourne l'audio mixé (np.float32, 1D)."""
        self._running = False
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        with self._lock:
            buffers = self._buffers
            self._buffers = {}
        return self._mix(buffers)

    def close(self):
        """Fermeture défensive (sortie de l'application)."""
        self._running = False
        for t in self._threads:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        self._threads = []


# ===========================================================================
# INTERFACE GRAPHIQUE
# ===========================================================================
try:
    import tkinter as tk  # noqa: E402
    TK_AVAILABLE = True
except ImportError:
    # Sans Tk (Python installé sans « tcl/tk », environnement serveur…), on garde
    # utilisables le cœur (transcription, --selftest, tests) : les classes de
    # l'interface ci-dessous héritent alors de substituts inertes, et main()
    # affiche un message clair au lieu d'une trace.
    TK_AVAILABLE = False

    class _TkStub:
        Canvas = object
        Frame = object

    tk = _TkStub()

# Pillow : rendu anti-aliasé des formes de l'UI (cercles, coins arrondis) via
# supersampling. Optionnel : si absent, on retombe sur le dessin Canvas natif.
from typing import TYPE_CHECKING
if TYPE_CHECKING:  # pour l'analyse de types : les noms sont vus comme définis
    from PIL import Image, ImageDraw, ImageTk, ImageFilter
try:
    from PIL import Image, ImageDraw, ImageTk, ImageFilter  # noqa: E402
    _PIL_OK = True
except Exception:
    _PIL_OK = False

_SS = 4  # facteur de supersampling (dessin en grand puis réduction lissée)

CONFIG_PATH = os.path.join(APP_DIR, "plume_config.json")

# --- Palettes de thèmes -----------------------------------------------------
THEME_ORDER = ["Sombre", "Clair", "Océan"]
DEFAULT_THEME = "Sombre"
THEMES = {
    "Sombre": {
        "bg": "#15151d", "surface": "#1e1e2a", "elevated": "#272736",
        "text": "#ECEDF5", "muted": "#8B8CA6",
        "accent": "#7B6CF6", "accent_hi": "#8E80FF", "on_accent": "#ffffff",
        "danger": "#F0566E", "danger_hi": "#FF6F86",
        "border": "#2B2B3C", "dark_titlebar": True,
    },
    "Clair": {
        "bg": "#F3F4F8", "surface": "#FFFFFF", "elevated": "#EBEDF4",
        "text": "#1B2233", "muted": "#697086",
        "accent": "#4F6CF7", "accent_hi": "#3F5CE6", "on_accent": "#FFFFFF",
        "danger": "#E5484D", "danger_hi": "#D23B40",
        "border": "#E2E5EE", "dark_titlebar": False,
    },
    "Océan": {
        "bg": "#0C2A2F", "surface": "#123A40", "elevated": "#18484F",
        "text": "#DEF5F1", "muted": "#82B2AE",
        "accent": "#2DD4BF", "accent_hi": "#46E7D3", "on_accent": "#06302C",
        "danger": "#FF6B6B", "danger_hi": "#FF8585",
        "border": "#1C525A", "dark_titlebar": True,
    },
}


def load_config():
    try:
        # utf-8-sig : tolère un BOM éventuel (fichier édité dans le Bloc-notes).
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception as e:
        # Non bloquant (disque plein, dossier en lecture seule…) : l'app
        # fonctionne, seules les préférences ne sont pas mémorisées.
        log(f"Préférences non enregistrées ({type(e).__name__}: {e})")


# --- DPI / barre de titre (Windows) -----------------------------------------
def _enable_dpi_awareness():
    """Rend le process conscient du DPI -> rendu net (pas d'étirement flou)."""
    if sys.platform != "win32":
        return
    import ctypes
    try:  # Per-Monitor v2 (le plus net), Windows 10 1703+
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _set_titlebar_dark(root, dark):
    """Assortit la barre de titre Windows au thème (sombre/clair)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20, ou 19 anciens builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        # Forcer le redessin de la zone non-cliente (barre de titre).
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
    except Exception:
        pass


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Dessine un rectangle à coins arrondis (polygone lissé)."""
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


# --- Rendu anti-aliasé (Pillow) : formes lisses aplaties sur le fond ---------
def _hex_rgb(c):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _mix(c1, c2, t):
    """Mélange deux couleurs hex (t=0 -> c1, t=1 -> c2)."""
    a, b = _hex_rgb(c1), _hex_rgb(c2)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _lum(c):
    """Luminance perçue (0..1) d'une couleur hex."""
    r, g, b = _hex_rgb(c)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _derive_theme(accent, base="dark"):
    """Dérive une palette complète et harmonieuse à partir d'une couleur d'accent
    et d'un fond (« dark »/« light »). Sert aux profils de couleurs personnalisés."""
    accent_hi = _mix(accent, "#ffffff", 0.16)
    on_accent = "#ffffff" if _lum(accent) < 0.62 else "#12121c"
    if base == "light":
        return {
            "bg": _mix("#F3F4F8", accent, 0.05), "surface": "#FFFFFF",
            "elevated": _mix("#EBEDF4", accent, 0.06),
            "text": "#1B2233", "muted": "#697086",
            "accent": accent, "accent_hi": _mix(accent, "#000000", 0.10),
            "on_accent": "#FFFFFF",
            "danger": "#E5484D", "danger_hi": "#D23B40",
            "border": _mix("#E2E5EE", accent, 0.10), "dark_titlebar": False,
        }
    return {
        "bg": _mix("#14141C", accent, 0.05), "surface": _mix("#1E1E2A", accent, 0.05),
        "elevated": _mix("#272736", accent, 0.06),
        "text": "#ECEDF5", "muted": "#8B8CA6",
        "accent": accent, "accent_hi": accent_hi, "on_accent": on_accent,
        "danger": "#F0566E", "danger_hi": "#FF6F86",
        "border": _mix("#2B2B3C", accent, 0.07), "dark_titlebar": True,
    }


def _aa_rounded_rect(w, h, radius, fill, bg, outline=None, outline_w=0):
    """PhotoImage d'un rectangle arrondi anti-aliasé, aplati sur la couleur `bg`
    (bords parfaitement fondus). Retourne None si Pillow indisponible."""
    if not _PIL_OK or w <= 0 or h <= 0:
        return None
    ss = _SS
    im = Image.new("RGB", (w * ss, h * ss), _hex_rgb(bg))
    d = ImageDraw.Draw(im)
    pad = ss
    box = [pad, pad, w * ss - pad, h * ss - pad]
    r = max(0, min(radius * ss, (w * ss) / 2 - pad, (h * ss) / 2 - pad))
    if outline and outline_w:
        d.rounded_rectangle(box, radius=r, fill=_hex_rgb(fill),
                            outline=_hex_rgb(outline), width=max(1, int(outline_w * ss)))
    else:
        d.rounded_rectangle(box, radius=r, fill=_hex_rgb(fill))
    return ImageTk.PhotoImage(im.resize((w, h), Image.Resampling.LANCZOS))


def _pil_mic(d, cx, cy, R, col):
    """Pictogramme micro, calé sur le rayon R du disque."""
    hw = R * 0.28
    lw = max(1, int(R * 0.06))
    d.rounded_rectangle([cx - hw, cy - R * 0.61, cx + hw, cy + R * 0.13], radius=hw, fill=col)
    d.arc([cx - R * 0.49, cy - R * 0.28, cx + R * 0.49, cy + R * 0.41],
          start=20, end=160, fill=col, width=lw)
    d.line([cx, cy + R * 0.41, cx, cy + R * 0.66], fill=col, width=lw)
    d.line([cx - R * 0.23, cy + R * 0.66, cx + R * 0.23, cy + R * 0.66], fill=col, width=lw)


def _pil_speaker(d, cx, cy, R, col):
    xl, xm, xr = cx - R * 0.49, cx - R * 0.08, cx + R * 0.08
    hs, hb = R * 0.18, R * 0.41
    d.polygon([(xl, cy - hs), (xm, cy - hs), (xr, cy - hb),
               (xr, cy + hb), (xm, cy + hs), (xl, cy + hs)], fill=col)
    lw = max(1, int(R * 0.05))
    for rr in (R * 0.31, R * 0.49):
        cxw = cx + R * 0.13
        d.arc([cxw - rr, cy - rr, cxw + rr, cy + rr], start=-52, end=52, fill=col, width=lw)


def _pil_stop(d, cx, cy, R, col):
    dd = R * 0.38
    d.rounded_rectangle([cx - dd, cy - dd, cx + dd, cy + dd], radius=R * 0.13, fill=col)


def _aa_mic_image(size, circle, glyph, bg, mode, recording, glow=None, glow_strength=130):
    """PhotoImage du bouton micro : halo lumineux DOUX (son flou s'estompe DANS
    l'image, donc jamais coupé -> pas de bord droit) + disque + pictogramme,
    le tout anti-aliasé. Retourne None si Pillow indisponible."""
    if not _PIL_OK or size <= 0:
        return None
    ss = _SS
    S = size * ss
    im = Image.new("RGB", (S, S), _hex_rgb(bg))
    cx = cy = S / 2.0
    R = S * 0.33  # disque nettement plus petit que l'image -> marge pour le halo
    if glow:
        mask = Image.new("L", (S, S), 0)
        gr = R * 1.05
        ImageDraw.Draw(mask).ellipse([cx - gr, cy - gr, cx + gr, cy + gr], fill=glow_strength)
        mask = mask.filter(ImageFilter.GaussianBlur(R * 0.14))
        im = Image.composite(Image.new("RGB", (S, S), _hex_rgb(glow)), im, mask)
    d = ImageDraw.Draw(im)
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=_hex_rgb(circle))
    col = _hex_rgb(glyph)
    if recording:
        _pil_stop(d, cx, cy, R, col)
    elif mode == "system":
        _pil_speaker(d, cx, cy, R, col)
    else:
        _pil_mic(d, cx, cy, R, col)
    return ImageTk.PhotoImage(im.resize((size, size), Image.Resampling.LANCZOS))


def _aa_segmented(w, h, n, sel_pos, track_fill, border, hi_fill, bg, pad):
    """PhotoImage d'un contrôle segmenté : piste arrondie + pastille de sélection
    dessinée DANS la même image (coins arrondis corrects). `sel_pos` est un indice
    FRACTIONNAIRE (float) -> permet d'animer le glissement de la pastille."""
    if not _PIL_OK or w <= 0 or h <= 0 or n <= 0:
        return None
    ss = _SS
    im = Image.new("RGB", (w * ss, h * ss), _hex_rgb(bg))
    d = ImageDraw.Draw(im)
    p = ss
    d.rounded_rectangle([p, p, w * ss - p, h * ss - p], radius=(h * ss) / 2 - p,
                        fill=_hex_rgb(track_fill), outline=_hex_rgb(border),
                        width=max(1, ss))
    if sel_pos is not None:
        segw = (w * ss) / n
        pp = pad * ss
        box = [sel_pos * segw + pp, pp, (sel_pos + 1) * segw - pp, h * ss - pp]
        d.rounded_rectangle(box, radius=(h * ss - 2 * pp) / 2, fill=_hex_rgb(hi_fill))
    return ImageTk.PhotoImage(im.resize((w, h), Image.Resampling.LANCZOS))


def type_text(text, avoid_hwnd=None):
    """Tape `text` (Unicode) dans la fenêtre ayant le focus clavier, via
    SendInput. Sert à l'« insertion directe » (ex. écrire dans Discord).

    Retourne True si le texte a réellement été envoyé, False sinon : aucune
    fenêtre au premier plan, ou cette fenêtre est `avoid_hwnd` (typiquement Plume
    lui-même) — on évite ainsi de taper la dictée dans sa propre fenêtre.

    IMPORTANT : la structure INPUT doit avoir la MÊME taille que la structure
    Win32 (union complète mi/ki/hi). Sinon SendInput échoue silencieusement
    (cbSize invalide) et rien n'est tapé."""
    if sys.platform != "win32" or not text:
        return False
    import ctypes
    from ctypes import wintypes
    # Évite d'envoyer Entrée (qui enverrait le message Discord par accident).
    text = text.replace("\r", " ").replace("\n", " ")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # Cible interdite (Plume au premier plan) ou aucune fenêtre -> on n'écrit pas.
    user32.GetForegroundWindow.restype = wintypes.HWND
    fg = int(user32.GetForegroundWindow() or 0)
    if not fg or (avoid_hwnd and fg == int(avoid_hwnd)):
        return False
    ULONG_PTR = wintypes.WPARAM            # entier de la taille d'un pointeur
    INPUT_KEYBOARD = 1
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    events = []
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:      # hors BMP : ignoré (rare en dictée)
            continue
        for up in (0, KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | up, 0, 0)
            events.append(inp)
    if not events:
        return False
    n = len(events)
    arr = (INPUT * n)(*events)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        log(f"Insertion directe : SendInput {sent}/{n} (err {err})")
    return sent == n


# --- Contrôle segmenté (choix multi-options) --------------------------------
class SegmentedToggle(tk.Canvas):
    def __init__(self, parent, options, on_change, width, height, font, scale=1.0):
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0, bd=0)
        self.options = options          # [(clé, libellé), ...]
        self.on_change = on_change
        self.cw, self.ch = width, height
        self.font = font
        self.ui_scale = scale
        self.theme = None
        self.current = options[0][0]
        self._enabled = True
        self._pos = 0.0          # position FRACTIONNAIRE de la pastille (animée)
        self._anim_id = None
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)

    def set_theme(self, theme, page_bg):
        self.theme = theme
        self.configure(bg=page_bg)
        self._redraw()

    def _index(self, key):
        return next((i for i, (k, _) in enumerate(self.options) if k == key), 0)

    def set_current(self, key):
        self.current = key
        target = self._index(key)
        if self.theme is None:
            self._pos = float(target)   # pas encore affiché : on cale sans animer
        else:
            self._animate_to(target)

    def set_enabled(self, on):
        self._enabled = on
        self._redraw()

    def _seg(self, i):
        w = self.cw / len(self.options)
        return i * w, 0, (i + 1) * w, self.ch

    def _animate_to(self, target):
        if self._anim_id is not None:
            try:
                self.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None
        start, target = self._pos, float(target)
        steps = 9
        if abs(target - start) < 0.001:
            self._pos = target
            self._redraw()
            return

        def step(i):
            f = i / steps
            f = 1 - (1 - f) ** 3        # ease-out cubique (glissement doux)
            self._pos = start + (target - start) * f
            self._redraw()
            if i < steps:
                self._anim_id = self.after(16, lambda: step(i + 1))
            else:
                self._pos, self._anim_id = target, None
        step(1)

    def _redraw(self):
        if self.theme is None:
            return
        self.delete("all")
        t = self.theme
        pad = max(2, int(2 * self.ui_scale))
        hi_fill = t["accent"] if self._enabled else t["border"]
        img = _aa_segmented(self.cw, self.ch, len(self.options), self._pos,
                            t["elevated"], t["border"], hi_fill, self["bg"], pad)
        if img is not None:
            self._img = img  # référence à conserver
            self.create_image(0, 0, anchor="nw", image=img)
        else:
            _round_rect(self, 1, 1, self.cw - 1, self.ch - 1, self.ch / 2,
                        fill=t["elevated"], outline=t["border"])
            w = self.cw / len(self.options)
            _round_rect(self, self._pos * w + pad, pad, (self._pos + 1) * w - pad,
                        self.ch - pad, (self.ch - 2 * pad) / 2, fill=hi_fill, outline=hi_fill)
        for i, (key, label) in enumerate(self.options):
            x1, y1, x2, y2 = self._seg(i)
            fg = (t["on_accent"] if self._enabled else t["muted"]) if key == self.current \
                else t["muted"]
            self.create_text((x1 + x2) / 2, self.ch / 2, text=label,
                             fill=fg, font=self.font)

    def _click(self, e):
        if not self._enabled:
            return
        i = max(0, min(len(self.options) - 1, int(e.x // (self.cw / len(self.options)))))
        key = self.options[i][0]
        if key != self.current:
            self.current = key
            self._animate_to(i)
            if self.on_change:
                self.on_change(key)

    def _motion(self, _e):
        self.configure(cursor="hand2" if self._enabled else "")


# --- Bouton arrondi réutilisable -------------------------------------------
class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, width, height, radius,
                 font, kind="accent", scale=1.0):
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0, bd=0)
        self.command = command
        self.text = text
        self.kind = kind            # "accent" ou "ghost"
        self.cw, self.ch = width, height  # NB: ne pas utiliser self._w (réservé tkinter)
        self.radius = radius
        self.font = font
        self.ui_scale = scale
        self.theme = None
        self._enabled = True
        self._hover = False
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def set_theme(self, theme, page_bg):
        self.theme = theme
        self.configure(bg=page_bg)
        self._redraw()

    def set_enabled(self, on):
        self._enabled = on
        self._redraw()

    def set_text(self, text):
        self.text = text
        self._redraw()

    def _palette(self, t):
        if not self._enabled:
            return t["border"], t["muted"]
        if self.kind == "accent":
            return (t["accent_hi"] if self._hover else t["accent"]), t["on_accent"]
        return (t["border"] if self._hover else t["elevated"]), t["text"]

    def _redraw(self):
        t = self.theme
        if t is None:
            return
        self.delete("all")
        fill, fg = self._palette(t)
        img = _aa_rounded_rect(self.cw, self.ch, self.radius, fill, self["bg"])
        if img is not None:
            self._img = img  # référence à conserver (sinon GC -> bouton vide)
            self.create_image(0, 0, anchor="nw", image=img)
        else:
            _round_rect(self, 1, 1, self.cw - 1, self.ch - 1, self.radius,
                        fill=fill, outline=fill)
        self.create_text(self.cw / 2, self.ch / 2, text=self.text,
                         fill=fg, font=self.font)

    def _click(self, _e):
        if self._enabled and self.command:
            self.command()

    def _enter(self, _e):
        self._hover = True
        if self._enabled:
            self.configure(cursor="hand2")
        self._redraw()

    def _leave(self, _e):
        self._hover = False
        self.configure(cursor="")
        self._redraw()


# --- Bouton micro circulaire ------------------------------------------------
class MicButton(tk.Canvas):
    def __init__(self, parent, command, size, scale=1.0):
        super().__init__(parent, width=size, height=size,
                         highlightthickness=0, bd=0)
        self.command = command
        self.size = size
        self.ui_scale = scale
        self.theme = None
        self._enabled = False
        self._hover = False
        self._recording = False
        self.mode = "mic"            # "mic" ou "system"
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def set_theme(self, theme, page_bg):
        self.theme = theme
        self.configure(bg=page_bg)
        self._redraw()

    def set_enabled(self, on):
        self._enabled = on
        self._redraw()

    def set_recording(self, on):
        self._recording = on
        self._redraw()

    def set_mode(self, mode):
        self.mode = mode
        self._redraw()

    def _redraw(self):
        if self.theme is None:
            return
        self.delete("all")
        t = self.theme
        if not self._enabled:
            circle, icon, glow, gs = t["surface"], t["muted"], None, 0
        elif self._recording:
            circle = t["danger_hi"] if self._hover else t["danger"]
            icon, glow, gs = "#ffffff", t["danger"], (185 if self._hover else 165)
        else:
            circle = t["accent_hi"] if self._hover else t["accent"]
            icon, glow, gs = t["on_accent"], t["accent"], (150 if self._hover else 110)

        img = _aa_mic_image(self.size, circle, icon, self["bg"], self.mode,
                            self._recording, glow, gs)
        if img is not None:
            self._img = img  # référence à conserver
            self.create_image(0, 0, anchor="nw", image=img)
            return

        # Repli sans Pillow : dessin Canvas natif.
        s = self.size
        cx = cy = s / 2
        r = s / 2 - max(2, int(3 * self.ui_scale))
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=circle, outline=circle)
        if self._recording:
            self._draw_stop(cx, cy, icon)
        elif self.mode == "system":
            self._draw_speaker(cx, cy, icon)
        else:
            self._draw_mic(cx, cy, icon)

    def _lw(self, factor):
        return max(2, int(factor * self.ui_scale))

    def _draw_mic(self, cx, cy, color):
        s = self.size
        # corps (capsule arrondie)
        hw = s * 0.12
        _round_rect(self, cx - hw, cy - s * 0.26, cx + hw, cy + s * 0.06, hw,
                    fill=color, outline=color)
        # support (arc en U)
        aw = s * 0.20
        self.create_arc(cx - aw, cy - s * 0.10, cx + aw, cy + s * 0.18,
                        start=200, extent=140, style="arc",
                        outline=color, width=self._lw(2.4))
        # pied + base
        self.create_line(cx, cy + s * 0.18, cx, cy + s * 0.28,
                         fill=color, width=self._lw(2.4))
        self.create_line(cx - s * 0.10, cy + s * 0.28, cx + s * 0.10, cy + s * 0.28,
                         fill=color, width=self._lw(2.4))

    def _draw_speaker(self, cx, cy, color):
        s = self.size
        xl = cx - s * 0.20      # arrière du haut-parleur
        xm = cx - s * 0.04      # jonction corps / cône
        xr = cx + s * 0.04      # bord du cône
        hs = s * 0.07           # demi-hauteur arrière
        hb = s * 0.17           # demi-hauteur cône (évasé)
        self.create_polygon(
            xl, cy - hs, xm, cy - hs, xr, cy - hb,
            xr, cy + hb, xm, cy + hs, xl, cy + hs,
            fill=color, outline=color,
        )
        # ondes sonores (deux arcs à droite)
        for r in (0.13, 0.21):
            rr = s * r
            cxw = cx + s * 0.06
            self.create_arc(cxw - rr, cy - rr, cxw + rr, cy + rr,
                            start=-52, extent=104, style="arc",
                            outline=color, width=self._lw(2.0))

    def _draw_stop(self, cx, cy, color):
        s = self.size
        d = s * 0.15
        _round_rect(self, cx - d, cy - d, cx + d, cy + d, s * 0.045,
                    fill=color, outline=color)

    def _click(self, _e):
        if self._enabled and self.command:
            self.command()

    def _enter(self, _e):
        self._hover = True
        if self._enabled:
            self.configure(cursor="hand2")
        self._redraw()

    def _leave(self, _e):
        self._hover = False
        self.configure(cursor="")
        self._redraw()


# --- Sélecteur de thème (3 pastilles colorées) ------------------------------
class ThemeDots(tk.Frame):
    def __init__(self, parent, on_select, scale=1.0):
        super().__init__(parent)
        self.on_select = on_select
        self.scale = scale
        self.size = int(20 * scale)
        self.dots = {}
        for name in THEME_ORDER:
            c = tk.Canvas(self, width=self.size, height=self.size,
                          highlightthickness=0, bd=0)
            c.pack(side="left", padx=int(3 * scale))
            c.bind("<Button-1>", lambda _e, n=name: self.on_select(n))
            c.bind("<Enter>", lambda e: e.widget.configure(cursor="hand2"))
            self.dots[name] = c

    def render(self, active_name, page_bg):
        self.configure(bg=page_bg)
        d = self.size
        for name, c in self.dots.items():
            c.configure(bg=page_bg)
            c.delete("all")
            accent = THEMES[name]["accent"]
            if name == active_name:
                c.create_oval(1, 1, d - 1, d - 1,
                              outline=THEMES[active_name]["text"],
                              width=max(1, int(1.5 * self.scale)))
            pad = max(3, int(3.5 * self.scale))
            c.create_oval(pad, pad, d - pad, d - pad, fill=accent, outline=accent)


# --- Application ------------------------------------------------------------
class PlumeApp:
    def __init__(self, root):
        self.root = root

        # Échelle DPI (la fenêtre est déjà DPI-aware via _enable_dpi_awareness).
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        if not dpi or dpi < 72:
            dpi = 96.0
        self.scale = dpi / 96.0
        try:
            root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        cfg = load_config()

        # Profils de couleurs personnalisés (accent + fond) : dérivés en palettes
        # complètes et ajoutés aux thèmes AVANT de résoudre le thème courant / l'UI.
        self._custom = {}
        self._settings_dialog = None
        for prof in cfg.get("custom_profiles", []):
            self._register_profile(prof.get("name"), prof.get("accent"),
                                   prof.get("base"), save=False)

        self.theme_name = os.environ.get("PLUME_THEME") or cfg.get("theme", DEFAULT_THEME)
        if self.theme_name not in THEMES:
            self.theme_name = DEFAULT_THEME
        self.theme = THEMES[self.theme_name]

        # Sélection de sources mémorisée : liste de {"id", "loopback"}.
        # Les noms réels sont résolus après l'énumération des périphériques.
        self._pending_sources = cfg.get("sources")  # None ou liste

        self.q = queue.Queue()
        self.recorder = Recorder(SAMPLE_RATE)
        self.devices = {"inputs": [], "outputs": [], "default_mic_id": None}
        self.selected_sources = []     # liste de {"id","name","loopback"}
        self.devices_loaded = False
        self._sources_dialog = None
        self.model = None
        self.backend = None
        self.recording = False
        self.text_revealed = False
        self._record_start = 0.0

        # Mode de sortie mémorisé (manual / copy / type)
        self.output_mode = cfg.get("output_mode", DEFAULT_OUTPUT_MODE)
        if self.output_mode not in ("manual", "copy", "type"):
            self.output_mode = DEFAULT_OUTPUT_MODE

        # Transcription en direct (streaming)
        self.live_mode = bool(cfg.get("live_mode", DEFAULT_LIVE_MODE))
        self._stream_thread = None
        self._live = LiveSession()   # état de la dictée en direct en cours

        # Modèle choisi dans les Paramètres (None = automatique selon GPU/CPU).
        # $PLUME_MODEL, s'il est défini, prime toujours (cf. resolve_model_size).
        self.model_choice = (cfg.get("model") or "").strip() or None
        self.model_size = None        # taille effectivement chargée
        self._model_error = None      # message si le chargement a échoué
        self._loading_model = False
        self._transcribing = False    # transcription « à l'arrêt » en cours
        self._capture_warning = None  # erreur de capture partielle à afficher après coup

        # Mode IA : reformulation par un LLM local (Ollama) après la dictée
        self.ai_mode = bool(cfg.get("ai_mode", False))
        self.ai_model = cfg.get("ai_model") or AI_MODEL_DEFAULT

        # Raccourci global (configurable : clavier ou bouton latéral de souris)
        self.hotkey = cfg.get("hotkey") or dict(DEFAULT_HOTKEY)
        if not isinstance(self.hotkey, dict) or "type" not in self.hotkey:
            self.hotkey = dict(DEFAULT_HOTKEY)
        self._hotkey_thread = None
        self._hotkey_thread_id = None
        self._hook_proc = None
        self.active_hotkey_label = None

        # Insertion directe : texte en attente tant que le curseur n'est pas sur
        # une fenêtre cible (garde-fou) + id du sondage de relance.
        self._pending_insert = ""
        self._insert_poll_id = None

        # Polices (tailles en points -> mises à l'échelle par tk scaling).
        self.f_title = ("Segoe UI Semibold", 16)
        self.f_subtitle = ("Segoe UI", 9)
        self.f_status = ("Segoe UI", 9)
        self.f_btn = ("Segoe UI Semibold", 10)
        self.f_seg = ("Segoe UI Semibold", 9)
        self.f_text = ("Segoe UI", 11)

        root.title("  Plume")
        ico = _icon_path()
        if ico:
            try:                       # icône de fenêtre / barre des tâches
                root.iconbitmap(default=ico)
            except Exception:
                pass
        root.resizable(False, False)
        self._compact_geom = f"{self.px(360)}x{self.px(422)}"
        self._expanded_geom = f"{self.px(360)}x{self.px(676)}"
        root.geometry(self._compact_geom)
        try:
            root.minsize(self.px(360), self.px(422))
        except Exception:
            pass

        self._build_ui()
        self.root.update_idletasks()
        self.apply_theme(self.theme_name, persist=False)

        # Une exception dans un callback Tk ne doit ni planter l'app ni rester
        # invisible (pas de console sous pythonw) : journal + statut.
        root.report_callback_exception = self._report_callback_exception

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._pump_queue)
        self._start_model_load()
        threading.Thread(target=self._enumerate_worker, daemon=True).start()
        self._start_hotkey()
        problem = check_replacements_file()
        if problem:
            log(problem)
            self.root.after(1500, lambda: self.status_var.set("⚠ " + problem))

    def _report_callback_exception(self, exc_type, exc, tb):
        log("Erreur dans l'interface : "
            + "".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            self.status_var.set(f"Erreur interne ({exc_type.__name__}) — "
                                f"détails dans {os.path.basename(LOG_PATH)}")
        except Exception:
            pass

    def px(self, n):
        return int(round(n * self.scale))

    # ----- Construction de l'interface -----
    def _build_ui(self):
        pad = self.px(18)
        self.container = tk.Frame(self.root)
        self.container.pack(fill="both", expand=True)

        # En-tête : titre + sous-titre + sélecteur de thème
        self.header = tk.Frame(self.container)
        self.header.pack(fill="x", padx=pad, pady=(self.px(16), 0))
        self.title_box = tk.Frame(self.header)
        self.title_box.pack(side="left")
        self.title_lbl = tk.Label(self.title_box, text="Plume", font=self.f_title)
        self.title_lbl.pack(side="top", anchor="w")
        self.subtitle_lbl = tk.Label(self.title_box, text="dictée vocale locale",
                                     font=self.f_subtitle)
        self.subtitle_lbl.pack(side="top", anchor="w")
        self.dots = ThemeDots(self.header, self._on_pick_theme, self.scale)
        self.dots.pack(side="right", pady=(self.px(5), 0))
        self.gear_lbl = tk.Label(self.header, text="⚙", font=("Segoe UI", 14),
                                 cursor="hand2")
        self.gear_lbl.pack(side="right", padx=(0, self.px(10)), pady=(self.px(4), 0))
        self.gear_lbl.bind("<Button-1>", lambda _e: self._open_settings())

        # Bouton micro circulaire (centré)
        self.mic_area = tk.Frame(self.container)
        self.mic_area.pack(fill="x", pady=(self.px(20), self.px(2)))
        self.mic_btn = MicButton(self.mic_area, self._on_mic,
                                 size=self.px(108), scale=self.scale)
        self.mic_btn.pack()

        # Bouton d'accès au sélecteur de sources (micros / son du PC, mix possible)
        self.sources_btn = RoundedButton(
            self.container, "🎛  Sources…", self._open_sources_dialog,
            width=self.px(252), height=self.px(34), radius=self.px(10),
            font=self.f_seg, kind="ghost", scale=self.scale,
        )
        self.sources_btn.pack(pady=(self.px(16), 0))
        self.sources_btn.set_enabled(False)  # activé après énumération des périphériques

        # Mode de sortie : manuel / auto-copie / insertion directe
        self.output_sel = SegmentedToggle(
            self.container,
            options=[("manual", "Manuel"), ("copy", "Auto-copie"), ("type", "Insérer")],
            on_change=self._on_output_change,
            width=self.px(252), height=self.px(32),
            font=self.f_seg, scale=self.scale,
        )
        self.output_sel.pack(pady=(self.px(8), 0))
        self.output_sel.set_current(self.output_mode)

        # Moment de la transcription : à l'arrêt (fin d'enreg.) ou en direct
        self.live_sel = SegmentedToggle(
            self.container,
            options=[("batch", "À l'arrêt"), ("live", "En direct")],
            on_change=self._on_live_change,
            width=self.px(252), height=self.px(32),
            font=self.f_seg, scale=self.scale,
        )
        self.live_sel.pack(pady=(self.px(6), 0))
        self.live_sel.set_current("live" if self.live_mode else "batch")

        # Traitement : dictée brute, ou reformulation par l'IA locale
        self.ai_sel = SegmentedToggle(
            self.container,
            options=[("raw", "Dictée"), ("ai", "IA ✨")],
            on_change=self._on_ai_change,
            width=self.px(252), height=self.px(32),
            font=self.f_seg, scale=self.scale,
        )
        self.ai_sel.pack(pady=(self.px(6), 0))
        self.ai_sel.set_current("ai" if self.ai_mode else "raw")

        # Barre d'état
        self.status_var = tk.StringVar(value="Chargement du modèle…")
        self.status_lbl = tk.Label(self.container, textvariable=self.status_var,
                                   font=self.f_status, anchor="center")
        self.status_lbl.pack(fill="x", pady=(self.px(14), self.px(14)))

        # Panneau de texte (caché jusqu'au premier résultat)
        self.text_frame = tk.Frame(self.container)
        self.text = tk.Text(self.text_frame, wrap="word", font=self.f_text,
                            bd=0, relief="flat", highlightthickness=1,
                            padx=self.px(12), pady=self.px(10), height=8)
        self.text.pack(fill="both", expand=True, padx=pad, pady=(0, self.px(10)))
        # Ligne de boutons : Effacer (ghost) + Copier (accent)
        self.btn_row = tk.Frame(self.text_frame)
        self.btn_row.pack(fill="x", padx=pad, pady=(0, self.px(14)))
        self.clear_btn = RoundedButton(self.btn_row, "Effacer", self._on_clear,
                                       width=self.px(120), height=self.px(40),
                                       radius=self.px(12), font=self.f_btn,
                                       kind="ghost", scale=self.scale)
        self.clear_btn.pack(side="left")
        self.copy_btn = RoundedButton(self.btn_row, "Copier", self._on_copy,
                                      width=self.px(120), height=self.px(40),
                                      radius=self.px(12), font=self.f_btn,
                                      kind="accent", scale=self.scale)
        self.copy_btn.pack(side="right")

    # ----- Thèmes -----
    def apply_theme(self, name, persist=True):
        self.theme_name = name
        self.theme = t = THEMES[name]
        self.root.configure(bg=t["bg"])
        self.container.configure(bg=t["bg"])
        self.header.configure(bg=t["bg"])
        self.title_box.configure(bg=t["bg"])
        self.title_lbl.configure(bg=t["bg"], fg=t["text"])
        self.subtitle_lbl.configure(bg=t["bg"], fg=t["muted"])
        self.gear_lbl.configure(bg=t["bg"], fg=t["muted"])
        self.mic_area.configure(bg=t["bg"])
        self.status_lbl.configure(bg=t["bg"], fg=t["muted"])
        self.text_frame.configure(bg=t["bg"])
        self.btn_row.configure(bg=t["bg"])
        self.text.configure(bg=t["surface"], fg=t["text"],
                            insertbackground=t["accent"],
                            selectbackground=t["accent"],
                            selectforeground=t["on_accent"],
                            highlightbackground=t["border"],
                            highlightcolor=t["border"])
        self.dots.render(name, t["bg"])
        self.mic_btn.set_theme(t, t["bg"])
        self.sources_btn.set_theme(t, t["bg"])
        self.output_sel.set_theme(t, t["bg"])
        self.live_sel.set_theme(t, t["bg"])
        self.ai_sel.set_theme(t, t["bg"])
        self.clear_btn.set_theme(t, t["bg"])
        self.copy_btn.set_theme(t, t["bg"])
        _set_titlebar_dark(self.root, t.get("dark_titlebar", False))
        if self._sources_dialog is not None:
            self._rebuild_sources_dialog()
        if self._settings_dialog is not None:
            self._build_settings()
        if persist:
            self._save_prefs()

    def _save_prefs(self):
        save_config({
            "theme": self.theme_name,
            "output_mode": self.output_mode,
            "live_mode": self.live_mode,
            "ai_mode": self.ai_mode,
            "ai_model": self.ai_model,
            "model": self.model_choice,
            "hotkey": self.hotkey,
            "custom_profiles": list(self._custom.values()),
            "sources": [{"id": s["id"], "loopback": s["loopback"]}
                        for s in self.selected_sources],
        })

    # ----- Profils de couleurs personnalisés -----
    def _register_profile(self, name, accent, base, save=True):
        """Dérive et enregistre un profil (accent + fond) dans les thèmes."""
        name = (name or "").strip()
        base = base if base in ("dark", "light") else "dark"
        if not name or not isinstance(accent, str) or not accent.startswith("#"):
            return False
        try:
            THEMES[name] = _derive_theme(accent, base)
        except Exception:
            return False
        if name not in THEME_ORDER:
            THEME_ORDER.append(name)
        self._custom[name] = {"name": name, "accent": accent, "base": base}
        if save:
            self._save_prefs()
        return True

    def _rebuild_dots(self):
        """Reconstruit les pastilles de thème (après ajout/suppression de profil)."""
        try:
            self.dots.destroy()
        except Exception:
            pass
        self.dots = ThemeDots(self.header, self._on_pick_theme, self.scale)
        self.dots.pack(side="right", pady=(self.px(5), 0))
        self.dots.render(self.theme_name, self.theme["bg"])

    # ----- Fenêtre de paramètres -----
    def _open_settings(self):
        if self._settings_dialog is not None:
            try:
                self._settings_dialog.lift()
            except Exception:
                pass
            return
        t = self.theme
        dlg = tk.Toplevel(self.root)
        dlg.title("Paramètres")
        dlg.configure(bg=t["bg"])
        dlg.resizable(False, False)
        dlg.transient(self.root)
        try:
            dlg.geometry(f"+{self.root.winfo_rootx() + self.px(30)}"
                         f"+{self.root.winfo_rooty() + self.px(40)}")
        except Exception:
            pass
        dlg.protocol("WM_DELETE_WINDOW", self._close_settings)
        self._settings_dialog = dlg
        self._new_accent = self.theme["accent"]
        self.root.update_idletasks()
        _set_titlebar_dark(dlg, t.get("dark_titlebar", False))
        self._build_settings()
        try:
            dlg.grab_set()
        except Exception:
            pass

    def _build_settings(self):
        dlg = self._settings_dialog
        if dlg is None:
            return
        t = self.theme
        dlg.configure(bg=t["bg"])
        for w in dlg.winfo_children():
            w.destroy()
        self._settings_imgs = []   # garder les références d'images (sinon GC)
        pad = self.px(16)
        tk.Label(dlg, text="Paramètres", bg=t["bg"], fg=t["text"],
                 font=self.f_title).pack(anchor="w", padx=pad, pady=(self.px(14), 0))

        tk.Label(dlg, text="Nouveau profil de couleurs", bg=t["bg"], fg=t["muted"],
                 font=self.f_seg, anchor="w").pack(fill="x", padx=pad,
                                                   pady=(self.px(12), self.px(6)))
        row = tk.Frame(dlg, bg=t["bg"])
        row.pack(fill="x", padx=pad)
        self._swatch = tk.Canvas(row, width=self.px(30), height=self.px(30),
                                 highlightthickness=0, bd=0, bg=t["bg"], cursor="hand2")
        self._swatch.pack(side="left")
        self._swatch.bind("<Button-1>", lambda _e: self._pick_accent())
        self._paint_swatch()
        pick = RoundedButton(row, "Couleur d'accent…", self._pick_accent,
                             width=self.px(154), height=self.px(30), radius=self.px(9),
                             font=self.f_seg, kind="ghost", scale=self.scale)
        pick.pack(side="left", padx=(self.px(8), 0))
        pick.set_theme(t, t["bg"])

        self._base_sel = SegmentedToggle(
            dlg, options=[("dark", "Sombre"), ("light", "Clair")], on_change=None,
            width=self.px(200), height=self.px(30), font=self.f_seg, scale=self.scale)
        self._base_sel.pack(anchor="w", padx=pad, pady=(self.px(8), 0))
        self._base_sel.set_theme(t, t["bg"])
        self._base_sel.set_current("light" if _lum(t["bg"]) > 0.5 else "dark")

        nrow = tk.Frame(dlg, bg=t["bg"])
        nrow.pack(fill="x", padx=pad, pady=(self.px(8), 0))
        self._name_var = tk.StringVar(value="Mon profil")
        ent = tk.Entry(nrow, textvariable=self._name_var, font=self.f_status,
                       bg=t["surface"], fg=t["text"], insertbackground=t["accent"],
                       relief="flat", highlightthickness=1,
                       highlightbackground=t["border"], highlightcolor=t["accent"])
        ent.pack(side="left", fill="x", expand=True, ipady=self.px(4))
        create = RoundedButton(nrow, "Créer", self._settings_create, width=self.px(90),
                               height=self.px(30), radius=self.px(9), font=self.f_btn,
                               kind="accent", scale=self.scale)
        create.pack(side="right", padx=(self.px(8), 0))
        create.set_theme(t, t["bg"])

        if self._custom:
            tk.Label(dlg, text="Profils personnalisés", bg=t["bg"], fg=t["muted"],
                     font=self.f_seg, anchor="w").pack(fill="x", padx=pad,
                                                       pady=(self.px(14), self.px(4)))
            for pname in list(self._custom):
                pr = tk.Frame(dlg, bg=t["bg"])
                pr.pack(fill="x", padx=pad, pady=(0, self.px(4)))
                sw = tk.Canvas(pr, width=self.px(18), height=self.px(18),
                               highlightthickness=0, bd=0, bg=t["bg"])
                sw.pack(side="left")
                img = _aa_rounded_rect(self.px(18), self.px(18), self.px(9),
                                       self._custom[pname]["accent"], t["bg"])
                if img is not None:
                    self._settings_imgs.append(img)
                    sw.create_image(0, 0, anchor="nw", image=img)
                tk.Label(pr, text="  " + pname, bg=t["bg"], fg=t["text"],
                         font=self.f_status, anchor="w").pack(side="left")
                dele = RoundedButton(pr, "Supprimer",
                                     lambda n=pname: self._delete_profile(n),
                                     width=self.px(92), height=self.px(26),
                                     radius=self.px(8), font=self.f_seg, kind="ghost",
                                     scale=self.scale)
                dele.pack(side="right")
                dele.set_theme(t, t["bg"])

        # --- Raccourci global ---
        tk.Label(dlg, text="Raccourci global", bg=t["bg"], fg=t["muted"],
                 font=self.f_seg, anchor="w").pack(fill="x", padx=pad,
                                                   pady=(self.px(16), self.px(4)))
        cur = self.active_hotkey_label or self.hotkey.get("label", "—")
        self._hotkey_lbl = tk.Label(dlg, text="Actuel : " + cur, bg=t["bg"],
                                    fg=t["text"], font=self.f_status, anchor="w")
        self._hotkey_lbl.pack(fill="x", padx=pad)
        hkrow = tk.Frame(dlg, bg=t["bg"])
        hkrow.pack(fill="x", padx=pad, pady=(self.px(6), 0))
        cap = RoundedButton(hkrow, "⌨  Capturer une touche", self._capture_hotkey_key,
                            width=self.px(190), height=self.px(30), radius=self.px(9),
                            font=self.f_seg, kind="ghost", scale=self.scale)
        cap.pack(side="left")
        cap.set_theme(t, t["bg"])
        m1 = RoundedButton(hkrow, "Souris ◄", lambda: self._set_mouse_hotkey("x1"),
                           width=self.px(78), height=self.px(30), radius=self.px(9),
                           font=self.f_seg, kind="ghost", scale=self.scale)
        m1.pack(side="right")
        m1.set_theme(t, t["bg"])
        m2 = RoundedButton(hkrow, "Souris ►", lambda: self._set_mouse_hotkey("x2"),
                           width=self.px(78), height=self.px(30), radius=self.px(9),
                           font=self.f_seg, kind="ghost", scale=self.scale)
        m2.pack(side="right", padx=(0, self.px(6)))
        m2.set_theme(t, t["bg"])

        # --- Modèle Whisper ---
        tk.Label(dlg, text="Modèle Whisper", bg=t["bg"], fg=t["muted"],
                 font=self.f_seg, anchor="w").pack(fill="x", padx=pad,
                                                   pady=(self.px(16), self.px(4)))
        mrow = tk.Frame(dlg, bg=t["bg"])
        mrow.pack(fill="x", padx=pad)
        self._model_var = tk.StringVar(value=self.model_choice or "")
        ment = tk.Entry(mrow, textvariable=self._model_var, font=self.f_status,
                        bg=t["surface"], fg=t["text"], insertbackground=t["accent"],
                        relief="flat", highlightthickness=1,
                        highlightbackground=t["border"], highlightcolor=t["accent"])
        ment.pack(side="left", fill="x", expand=True, ipady=self.px(4))
        mapply = RoundedButton(mrow, "Appliquer", self._apply_model_choice,
                               width=self.px(90), height=self.px(30), radius=self.px(9),
                               font=self.f_btn, kind="accent", scale=self.scale)
        mapply.pack(side="right", padx=(self.px(8), 0))
        mapply.set_theme(t, t["bg"])
        cur = self.model_size or "chargement…"
        if MODEL_SIZE:
            mhelp = f"Imposé par PLUME_MODEL : {MODEL_SIZE}."
        else:
            mhelp = (f"Actuel : {cur}. Vide = automatique ({DEFAULT_GPU_MODEL} sur GPU, "
                     f"{DEFAULT_CPU_MODEL} sur CPU). Choix : " + ", ".join(KNOWN_MODELS)
                     + ". Un nouveau modèle est téléchargé au premier chargement.")
        tk.Label(dlg, text=mhelp, bg=t["bg"], fg=t["muted"], font=self.f_status,
                 anchor="w", wraplength=self.px(340), justify="left").pack(
                     fill="x", padx=pad, pady=(self.px(4), 0))

        # --- Mode IA (reformulation locale) ---
        tk.Label(dlg, text="Mode IA — modèle Ollama (local)", bg=t["bg"], fg=t["muted"],
                 font=self.f_seg, anchor="w").pack(fill="x", padx=pad,
                                                   pady=(self.px(16), self.px(4)))
        airow = tk.Frame(dlg, bg=t["bg"])
        airow.pack(fill="x", padx=pad)
        self._ai_model_var = tk.StringVar(value=self.ai_model)
        aient = tk.Entry(airow, textvariable=self._ai_model_var, font=self.f_status,
                         bg=t["surface"], fg=t["text"], insertbackground=t["accent"],
                         relief="flat", highlightthickness=1,
                         highlightbackground=t["border"], highlightcolor=t["accent"])
        aient.pack(side="left", fill="x", expand=True, ipady=self.px(4))
        aisave = RoundedButton(airow, "OK", self._save_ai_model, width=self.px(70),
                               height=self.px(30), radius=self.px(9), font=self.f_btn,
                               kind="accent", scale=self.scale)
        aisave.pack(side="right", padx=(self.px(8), 0))
        aisave.set_theme(t, t["bg"])
        tk.Label(dlg, text="Nécessite Ollama (ollama.com) lancé + « ollama pull <modèle> ».",
                 bg=t["bg"], fg=t["muted"], font=self.f_status, anchor="w",
                 wraplength=self.px(320), justify="left").pack(fill="x", padx=pad,
                                                               pady=(self.px(4), 0))

        self._settings_status = tk.StringVar(value="")
        tk.Label(dlg, textvariable=self._settings_status, bg=t["bg"], fg=t["muted"],
                 font=self.f_status, anchor="w").pack(fill="x", padx=pad,
                                                      pady=(self.px(10), 0))
        bar = tk.Frame(dlg, bg=t["bg"])
        bar.pack(fill="x", padx=pad, pady=(self.px(8), self.px(14)))
        close = RoundedButton(bar, "Fermer", self._close_settings, width=self.px(110),
                              height=self.px(34), radius=self.px(10), font=self.f_btn,
                              kind="ghost", scale=self.scale)
        close.pack(side="right")
        close.set_theme(t, t["bg"])

    def _paint_swatch(self):
        c = self._swatch
        c.delete("all")
        w = self.px(30)
        img = _aa_rounded_rect(w, w, self.px(9), self._new_accent, self.theme["bg"])
        if img is not None:
            self._swatch_img = img
            c.create_image(0, 0, anchor="nw", image=img)
        else:
            _round_rect(c, 1, 1, w - 1, w - 1, self.px(9),
                        fill=self._new_accent, outline=self._new_accent)

    def _pick_accent(self):
        from tkinter import colorchooser
        if self._settings_dialog is None:
            return
        res = colorchooser.askcolor(color=self._new_accent, parent=self._settings_dialog,
                                    title="Couleur d'accent")
        if res and res[1]:
            self._new_accent = res[1]
            self._paint_swatch()

    def _settings_create(self):
        name = self._name_var.get().strip()
        if not name:
            self._settings_status.set("Donnez un nom au profil.")
            return
        if self._register_profile(name, self._new_accent, self._base_sel.current):
            self._rebuild_dots()
            self.apply_theme(name)        # applique + persiste + reconstruit le dialogue
            self._settings_status.set(f"Profil « {name} » créé et appliqué.")
        else:
            self._settings_status.set("Profil invalide.")

    def _delete_profile(self, name):
        self._custom.pop(name, None)
        if name in THEME_ORDER and name not in ("Sombre", "Clair", "Océan"):
            THEME_ORDER.remove(name)
        THEMES.pop(name, None)
        self._rebuild_dots()
        self._save_prefs()
        if self.theme_name == name:
            self.apply_theme(DEFAULT_THEME)
        else:
            self._build_settings()

    def _close_settings(self):
        dlg = self._settings_dialog
        self._settings_dialog = None
        if dlg is not None:
            try:
                dlg.grab_release()
            except Exception:
                pass
            dlg.destroy()

    def _apply_model_choice(self):
        """Change de modèle Whisper (Paramètres) et le recharge en arrière-plan."""
        choice = self._model_var.get().strip() or None
        if self.recording or self._transcribing:
            self._settings_status.set("Attendez la fin de la dictée en cours.")
            return
        if MODEL_SIZE:
            self._settings_status.set(f"PLUME_MODEL={MODEL_SIZE} prime sur ce réglage.")
            return
        if choice and not re.fullmatch(r"[\w.\-/]+", choice):
            self._settings_status.set("Nom de modèle invalide.")
            return
        self.model_choice = choice
        self._save_prefs()
        self._settings_status.set(f"Chargement de « {choice or 'auto'} »…")
        self._start_model_load()

    def _save_ai_model(self):
        name = self._ai_model_var.get().strip()
        if name:
            self.ai_model = name
            self._save_prefs()
            if getattr(self, "_settings_status", None) is not None:
                self._settings_status.set(f"Modèle IA : {name}")

    # ----- Capture / réglage du raccourci -----
    def _current_mods(self):
        """Modificateurs physiquement enfoncés -> flags RegisterHotKey (via Win32)."""
        mods = 0
        try:
            import ctypes
            g = ctypes.windll.user32.GetAsyncKeyState
            if g(0x11) & 0x8000:                          # Ctrl
                mods |= 0x0002
            if g(0x12) & 0x8000:                          # Alt
                mods |= 0x0001
            if g(0x10) & 0x8000:                          # Maj
                mods |= 0x0004
            if (g(0x5B) & 0x8000) or (g(0x5C) & 0x8000):  # Win
                mods |= 0x0008
        except Exception:
            pass
        return mods

    def _capture_hotkey_key(self):
        """Capture la prochaine combinaison clavier appuyée comme raccourci."""
        dlg = self._settings_dialog
        if dlg is None:
            return
        self._settings_status.set("Appuyez sur la combinaison… (Échap pour annuler)")

        def on_key(e):
            vk = int(getattr(e, "keycode", 0))
            if vk in (16, 17, 18, 20, 91, 92, 160, 161, 162, 163, 164, 165):
                return  # modificateur seul : on attend une vraie touche
            dlg.unbind("<KeyPress>")
            if vk == 27:  # Échap
                self._settings_status.set("Capture annulée.")
                return
            mods = self._current_mods()
            if mods == 0 and not (0x70 <= vk <= 0x87):
                self._settings_status.set("Ajoutez un modificateur (Ctrl / Alt / Maj / Win).")
                dlg.bind("<KeyPress>", on_key)
                return
            self._set_hotkey({"type": "key", "mods": mods, "vk": vk,
                              "label": _hotkey_label(mods, vk)})

        dlg.bind("<KeyPress>", on_key)
        dlg.focus_set()

    def _set_mouse_hotkey(self, button):
        label = "Souris (Précédent)" if button == "x1" else "Souris (Suivant)"
        self._set_hotkey({"type": "mouse", "button": button, "label": label})

    def _set_hotkey(self, hk):
        self.hotkey = hk
        self._save_prefs()
        self._restart_hotkey()
        if self._settings_dialog is not None:
            try:
                self._hotkey_lbl.configure(text="Actuel : " + hk["label"])
                self._settings_status.set(f"Raccourci défini : {hk['label']}")
            except Exception:
                pass

    def _on_live_change(self, key):
        self.live_mode = (key == "live")
        self._save_prefs()
        self.status_var.set("Transcription en direct activée" if self.live_mode
                            else "Transcription à l'arrêt (fin d'enregistrement)")

    def _on_ai_change(self, key):
        self.ai_mode = (key == "ai")
        self._save_prefs()
        self._update_idle_controls()   # (dés)active le direct selon le mode IA
        if self.ai_mode:
            self.status_var.set(f"Mode IA : reformulation locale ({self.ai_model})")
        else:
            self.status_var.set("Mode dictée (texte brut)")

    def _ready_status(self):
        s = f"Prêt — {self.backend}"
        if self.model_size:
            s += f" · {self.model_size}"
        if self.active_hotkey_label:
            s += f"  ·  ⌨ {self.active_hotkey_label}"
        return s

    def _on_output_change(self, key):
        self.output_mode = key
        self._save_prefs()
        msg = {"manual": "Sortie : manuelle (bouton Copier)",
               "copy": "Sortie : copie automatique",
               "type": "Sortie : insertion directe dans la fenêtre active"}.get(key, "")
        self.status_var.set(msg)

    def _emit_output(self, new_text):
        """Applique le mode de sortie après une transcription."""
        if self.output_mode == "copy":
            self.root.clipboard_clear()
            self.root.clipboard_append(self.text.get("1.0", "end-1c"))
        elif self.output_mode == "type":
            self._queue_insert(new_text + " ")

    def _own_hwnd(self):
        """HWND de la fenêtre principale de Plume (pour ne pas s'insérer soi-même)."""
        if sys.platform != "win32":
            return 0
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.GetAncestor.argtypes = (wintypes.HWND, ctypes.c_uint)
            u.GetAncestor.restype = wintypes.HWND
            return int(u.GetAncestor(self.root.winfo_id(), 2) or 0)  # GA_ROOT
        except Exception:
            return 0

    def _queue_insert(self, text):
        """Met `text` en file pour insertion directe, puis tente de l'écrire."""
        if sys.platform != "win32" or not text:
            return
        self._pending_insert += text
        self._flush_insert()

    def _flush_insert(self):
        """Écrit le texte en attente dans la fenêtre cible. Sans cible valide
        (Plume au premier plan, aucune fenêtre…), on garde le texte et on
        réessaie dès qu'une fenêtre cible reçoit le focus (garde-fou)."""
        if not self._pending_insert:
            return
        if type_text(self._pending_insert, avoid_hwnd=self._own_hwnd()):
            self._pending_insert = ""
            if self.model is not None and not self.recording:
                self.status_var.set(self._ready_status())
        else:
            self.status_var.set("⏳ Texte prêt — placez le curseur dans la fenêtre cible")
            self._schedule_insert_poll()

    def _schedule_insert_poll(self):
        if self._insert_poll_id is None:
            self._insert_poll_id = self.root.after(400, self._poll_insert)

    def _poll_insert(self):
        self._insert_poll_id = None
        if self._pending_insert:
            self._flush_insert()

    # ----- Sources audio -----
    def _source_summary(self, maxlen=30):
        n = len(self.selected_sources)
        if n == 0:
            return "aucune source"
        if n == 1:
            name = self.selected_sources[0]["name"]
            return name if len(name) <= maxlen else name[:maxlen - 1] + "…"
        return f"{n} sources (mix)"

    def _refresh_source_ui(self):
        """Met à jour le bouton Sources et l'icône du gros bouton."""
        self.sources_btn.set_text("🎛  " + self._source_summary(26))
        all_loop = (len(self.selected_sources) > 0
                    and all(s["loopback"] for s in self.selected_sources))
        self.mic_btn.set_mode("system" if all_loop else "mic")
        if self.model is not None and not self.recording:
            self.status_var.set(self._ready_status())

    def _apply_initial_selection(self):
        """Choisit la sélection initiale après énumération : sélection mémorisée
        (filtrée sur les périphériques présents), sinon le micro par défaut."""
        available = {d["id"]: d for d in
                     (self.devices["inputs"] + self.devices["outputs"])}
        chosen = []
        if self._pending_sources:
            for s in self._pending_sources:
                d = available.get(s.get("id"))
                if d:
                    chosen.append(d)
        if not chosen:
            dft = self.devices.get("default_mic_id")
            if dft and dft in available:
                chosen = [available[dft]]
            elif self.devices["inputs"]:
                chosen = [self.devices["inputs"][0]]
            elif self.devices["outputs"]:
                chosen = [self.devices["outputs"][0]]
        self.selected_sources = chosen
        self.recorder.set_sources(chosen)
        self._save_prefs()

    def _enumerate_worker(self):
        try:
            import ctypes
            ctypes.windll.ole32.CoInitialize(None)  # COM requis sur ce thread
        except Exception:
            pass
        try:
            if not CAPTURE_AVAILABLE:
                raise RuntimeError("capture audio indisponible")
            data = enumerate_devices()
            self.q.put(("devices", data))
        except Exception as e:
            log(f"Énumération des périphériques : {type(e).__name__}: {e}")
            self.q.put(("devices_error", describe_enumeration_error(e)))

    def _on_pick_theme(self, name):
        if name != self.theme_name:
            self.apply_theme(name)

    # ----- Workers (threads secondaires) : ne touchent JAMAIS aux widgets -----
    def _start_model_load(self):
        """(Re)lance le chargement du modèle en arrière-plan."""
        if self._loading_model or self.recording:
            return
        self._loading_model = True
        self._model_error = None
        self.model = None
        self.backend = None
        wanted = resolve_model_size(self.model_choice, backend_is_gpu=not FORCE_CPU)
        size_mb = MODEL_SIZES_MB.get(wanted, 0)
        hint = f" (téléchargé au 1er lancement, ~{size_mb} Mo)" if size_mb else ""
        self.status_var.set(f"Chargement du modèle « {wanted} »…{hint}")
        self._update_idle_controls()
        threading.Thread(target=self._load_model_worker, daemon=True).start()

    def _load_model_worker(self):
        try:
            t0 = time.time()
            model, backend, size = load_model(requested_size=self.model_choice)
            dt = time.time() - t0
            self.model = model
            self.backend = backend
            self.model_size = size
            self.q.put(("ready", (backend, dt)))
        except ModelError as e:
            self.q.put(("model_error", str(e)))
        except Exception as e:
            log("Chargement du modèle : " + "".join(traceback.format_exception(e)))
            self.q.put(("model_error", describe_model_error(
                e, resolve_model_size(self.model_choice, not FORCE_CPU), MODEL_DIR)))

    def _transcribe_worker(self, audio):
        try:
            if audio is None or audio.size < int(MIN_RECORD_SECONDS * SAMPLE_RATE):
                self.q.put(("empty", None))
                return

            def progress(done, total):
                if total >= 20:   # inutile d'afficher une progression sur un court extrait
                    self.q.put(("progress", (done, total)))

            text = transcribe_audio(self.model, audio, progress=progress)
            if not text:
                self.q.put(("empty", None))
                return
            if self.ai_mode:
                self.q.put(("ai_working", None))   # -> statut « Reformulation (IA)… »
                try:
                    out = ai_reformulate(text, self.ai_model)
                except AIError as e:
                    self.q.put(("ai_error", str(e)))
                    return
                except Exception as e:
                    self.q.put(("ai_error", f"IA : {e}"))
                    return
                self.q.put(("result", out or text))
            else:
                self.q.put(("result", text))
        except MemoryError:
            log("Transcription : mémoire insuffisante")
            self.q.put(("error", "Mémoire insuffisante pour transcrire cet enregistrement. "
                                 "Essayez un enregistrement plus court ou un modèle plus petit."))
        except Exception as e:
            log("Erreur de transcription : " + "".join(traceback.format_exception(e)))
            self.q.put(("error", f"Erreur de transcription ({type(e).__name__}) — "
                                 f"détails dans {os.path.basename(LOG_PATH)}"))

    # ----- Transcription en direct (streaming) -----
    def _stream_worker(self):
        """Boucle (thread) qui transcrit en continu la file audio bornée depuis
        le dernier segment figé, et poste des aperçus. À l'arrêt, fait la passe
        finale, fige tout, et poste le résultat définitif."""
        sess = self._live
        repl = load_replacements()
        model = self.model

        def tx(audio, prompt):
            return transcribe_stream(model, audio, repl, prompt)

        failures = 0
        try:
            while self.recording:
                tail, total = self.recorder.snapshot(sess.commit_sample)
                try:
                    res = sess.step(tail, total, tx, final=False)
                    failures = 0
                except Exception as e:
                    # Une passe qui échoue n'annule pas la dictée : on réessaie
                    # à la passe suivante ; on abandonne après 3 échecs d'affilée.
                    failures += 1
                    log("Passe en direct échouée : "
                        + "".join(traceback.format_exception(e)))
                    if failures >= 3:
                        raise
                    res = None
                if res is not None:
                    self.q.put(("live", (res[0], False, None)))
                time.sleep(LIVE_STEP)
            final_audio = self.recorder.stop()
            errs = self.recorder.consume_errors()
            if final_audio.size == 0 and errs:
                self.q.put(("live_abort", describe_capture_error(errs)))
                return
            if errs:
                log("Erreurs de capture (partielles) : " + " ; ".join(errs))
            tail = (final_audio[sess.commit_sample:]
                    if final_audio.size > sess.commit_sample
                    else np.zeros(0, dtype=np.float32))
            display, clean = sess.step(tail, final_audio.size, tx, final=True)
            self.q.put(("live", (display, True, clean)))
        except Exception as e:
            log("Transcription en direct interrompue : "
                + "".join(traceback.format_exception(e)))
            try:
                self.recorder.stop()
            except Exception:
                pass
            self.q.put(("live_abort",
                        f"Transcription en direct interrompue ({type(e).__name__}). "
                        f"Détails : {os.path.basename(LOG_PATH)}"))

    def _set_text(self, s):
        """Remplace tout le contenu du panneau (utilisé par la transcription en direct)."""
        self.text.delete("1.0", "end")
        self.text.insert("end", s)
        self.text.see("end")

    # ----- Boucle de file (thread principal) -----
    def _pump_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._pump_queue)

    def _update_idle_controls(self):
        """État des boutons hors enregistrement/transcription."""
        busy = self.recording or self._transcribing
        can_record = (self.model is not None and len(self.selected_sources) > 0
                      and not busy)
        # Modèle en échec : le bouton sert à réessayer le chargement.
        can_retry = (self.model is None and self._model_error is not None
                     and not self._loading_model)
        self.mic_btn.set_enabled(can_record or can_retry)
        self.sources_btn.set_enabled(self.devices_loaded and not self.recording)
        self.output_sel.set_enabled(not self.recording)
        # En mode IA, le direct est désactivé (l'IA reformule à la fin).
        self.live_sel.set_enabled(not self.recording and not self.ai_mode)
        self.ai_sel.set_enabled(not self.recording)

    def _handle(self, kind, payload):
        if kind == "hotkey":
            if self.recording or (self.model is not None and self.selected_sources
                                  and not self._transcribing):
                self._on_mic()
            return
        if kind == "hotkey_status":
            self.active_hotkey_label = payload
            if self.model is not None and not self.recording:
                self.status_var.set(self._ready_status())
            return
        if kind == "devices":
            self.devices = payload
            self.devices_loaded = True
            if not self.selected_sources:
                self._apply_initial_selection()
            else:  # Actualiser : garder la sélection encore présente
                avail = {d["id"]: d for d in payload["inputs"] + payload["outputs"]}
                self.selected_sources = [avail[s["id"]] for s in self.selected_sources
                                         if s["id"] in avail]
                self.recorder.set_sources(self.selected_sources)
            self._refresh_source_ui()
            self._update_idle_controls()
            if self._sources_dialog is not None:
                self._rebuild_sources_dialog()
        elif kind == "devices_error":
            self.devices_loaded = True      # permet « Sources… » -> Actualiser
            self._update_idle_controls()
            self.status_var.set(payload)
        elif kind == "ready":
            backend, dt = payload
            self._loading_model = False
            self._update_idle_controls()
            if self.selected_sources:
                self.status_var.set(self._ready_status())
            elif self.devices_loaded:
                self.status_var.set(f"Prêt — {backend}  ·  aucune source : "
                                    "branchez un micro, puis « Sources… »")
            else:
                self.status_var.set(f"Prêt — {backend}  ·  recherche des périphériques…")
            log(f"Modèle « {self.model_size} » prêt en {dt:.1f}s — backend : {backend}")
        elif kind == "model_error":
            self._loading_model = False
            self._model_error = payload
            self._update_idle_controls()
            self.status_var.set("⚠ " + payload)
            log("Modèle : " + payload)
            try:
                from tkinter import messagebox
                messagebox.showerror("Plume — modèle indisponible",
                                     payload + "\n\nCliquez sur le bouton central pour "
                                     "réessayer.", parent=self.root)
            except Exception:
                pass
        elif kind == "progress":
            done, total = payload
            self.status_var.set(f"Transcription… {int(done // 60)}:{int(done % 60):02d} / "
                                f"{int(total // 60)}:{int(total % 60):02d}")
        elif kind == "live_abort":
            # La dictée en direct s'est arrêtée sur erreur : on remet l'interface
            # dans l'état « prêt » (sinon le bouton resterait sur « Finalisation… »).
            self.recording = False
            self.mic_btn.set_recording(False)
            self._update_idle_controls()
            self.status_var.set("⚠ " + payload)
        elif kind == "live":
            display, final, out_text = payload
            self._reveal_text_area()
            self._set_text(display)
            if final:
                self._update_idle_controls()
                if out_text:
                    self._emit_output(out_text)
                self.status_var.set(self._ready_status())
        elif kind == "result":
            self._transcribing = False
            self._update_idle_controls()
            if self.output_mode == "type":
                # Insertion directe : on n'écrit PAS dans la fenêtre de Plume
                # (le texte va dans la fenêtre active ; statut géré par _flush_insert).
                self._emit_output(payload)
            else:
                self._append_text(payload)
                self._reveal_text_area()
                self._emit_output(payload)
                self.status_var.set(self._ready_status())
            self._show_capture_warning()
        elif kind == "ai_working":
            self.status_var.set("Reformulation par l'IA…")
        elif kind == "ai_error":
            self._transcribing = False
            self._update_idle_controls()
            self.status_var.set(payload)
            log(payload)
        elif kind == "empty":
            self._transcribing = False
            self._update_idle_controls()
            self.status_var.set("Rien à transcrire (enregistrement vide ou trop court).")
            self._show_capture_warning()
        elif kind == "error":
            self._transcribing = False
            self._update_idle_controls()
            self.status_var.set("⚠ " + payload)
            log(payload)

    def _show_capture_warning(self):
        """Affiche (une fois) l'erreur de capture survenue pendant l'enregistrement
        qui vient d'être transcrit (ex. micro débranché en cours de dictée)."""
        if self._capture_warning:
            self.status_var.set("⚠ " + self._capture_warning)
            self._capture_warning = None

    # ----- Actions UI (thread principal) -----
    def _on_mic(self):
        if not self.recording:
            if self.model is None:
                if self._model_error is not None:
                    self._start_model_load()     # le bouton sert de « Réessayer »
                return
            if self._transcribing:
                return
            if not self.selected_sources:
                self.status_var.set("Aucune source — ouvrez « Sources… ».")
                return
            try:
                self.recorder.start()
            except Exception as e:
                self.status_var.set(f"⚠ Capture indisponible : {e}")
                log(f"Capture indisponible : {e}")
                return
            # Nouvel enregistrement -> on repart d'une zone de texte VIDE
            # (tous les modes : Manuel / Auto-copie / Insérer).
            self.text.delete("1.0", "end")
            self.recording = True
            self.mic_btn.set_recording(True)
            self.sources_btn.set_enabled(False)
            self.output_sel.set_enabled(False)
            self.live_sel.set_enabled(False)
            self.ai_sel.set_enabled(False)
            self._record_start = time.monotonic()
            self._tick_timer()
            if self.live_mode and not self.ai_mode:
                # Direct : la zone vient d'être vidée ; on affine au fil de la
                # parole (un thread dédié fait le streaming).
                self._live = LiveSession(prefix="")
                self._reveal_text_area()
                self._stream_thread = threading.Thread(target=self._stream_worker,
                                                        daemon=True)
                self._stream_thread.start()
        else:
            self.recording = False
            self.mic_btn.set_recording(False)
            if self.live_mode and not self.ai_mode:
                # Le worker de streaming fait la passe finale (arrête le recorder,
                # fige le texte, applique la sortie) puis réactive les contrôles.
                self.mic_btn.set_enabled(False)
                self.status_var.set("Finalisation…")
                return
            audio = self.recorder.stop()
            errs = self.recorder.consume_errors()
            if errs and audio.size == 0:
                self.status_var.set("⚠ " + describe_capture_error(errs))
                self._update_idle_controls()
                return
            if errs:
                log("Erreurs de capture (partielles) : " + " ; ".join(errs))
                self._capture_warning = describe_capture_error(errs)
            self._transcribing = True
            self.mic_btn.set_enabled(False)
            self.status_var.set("Transcription…")
            threading.Thread(target=self._transcribe_worker, args=(audio,),
                             daemon=True).start()

    def _on_copy(self):
        text = self.text.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Copié dans le presse-papiers ✓")

    def _on_clear(self):
        self.text.delete("1.0", "end")
        self._collapse_text_area()   # replie complètement la zone de texte
        self.status_var.set("Texte effacé.")

    def _tick_timer(self):
        """Affiche la durée d'enregistrement (mise à jour chaque 0,5 s) et
        surveille la capture : arrêt automatique si toutes les sources ont
        échoué (micro débranché…) ou si la durée maximale est atteinte."""
        if not self.recording:
            return
        el = int(time.monotonic() - self._record_start)
        if self.recorder.all_failed():
            # Arrêt propre : le chemin d'arrêt affiche l'erreur de capture (ou
            # transcrit ce qui a pu être capté avant la panne, puis l'affiche).
            self._on_mic()
            return
        if el >= MAX_RECORD_SECONDS:
            log(f"Durée maximale atteinte ({MAX_RECORD_SECONDS} s) : arrêt automatique")
            self._on_mic()
            return
        self.status_var.set(
            f"● Enregistrement… ({self._source_summary(18)})  {el // 60}:{el % 60:02d}")
        self.root.after(500, self._tick_timer)

    # ----- Raccourci clavier global (Windows) -----
    def _start_hotkey(self):
        if not (HOTKEY_ENABLED and sys.platform == "win32"):
            return
        self._hotkey_thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        self._hotkey_thread.start()

    def _hotkey_loop(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._hotkey_thread_id = kernel32.GetCurrentThreadId()
        hk = self.hotkey
        label = hk.get("label")
        hook = None

        if hk.get("type") == "mouse":
            # Bouton latéral de souris : hook bas niveau (le message loop ci-dessous
            # le fait vivre). XBUTTON1=1 (Précédent), XBUTTON2=2 (Suivant).
            target = {"x1": 1, "x2": 2}.get(str(hk.get("button", "")), 0)

            class _MSLL(ctypes.Structure):
                _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_void_p)]

            HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM)
            # argtypes/restype OBLIGATOIRES en 64 bits (sinon les pointeurs
            # callback/handle sont tronqués -> SetWindowsHookExW échoue).
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC,
                                                 wintypes.HINSTANCE, wintypes.DWORD)
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int,
                                              wintypes.WPARAM, wintypes.LPARAM)
            user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)

            def _proc(n_code, w_param, l_param):
                if n_code == 0 and w_param == 0x020B:   # HC_ACTION, WM_XBUTTONDOWN
                    ms = ctypes.cast(l_param, ctypes.POINTER(_MSLL)).contents
                    if (ms.mouseData >> 16) & 0xFFFF == target:
                        self.q.put(("hotkey", None))
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            self._hook_proc = HOOKPROC(_proc)   # garder la référence (sinon crash)
            hook = user32.SetWindowsHookExW(14, self._hook_proc,        # WH_MOUSE_LL
                                            kernel32.GetModuleHandleW(None), 0)
            if not hook:
                label = None
        else:
            mods = int(hk.get("mods", 0)) | _NR
            if not user32.RegisterHotKey(None, 1, mods, int(hk.get("vk", 0))):
                label = None

        if not label:
            log("Raccourci global indisponible (déjà pris par une autre application ?).")
            self.q.put(("hotkey_status", None))
            return
        self.q.put(("hotkey_status", label))
        log(f"Raccourci global actif : {label}")
        try:
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):       # WM_QUIT ou erreur
                    break
                if msg.message == 0x0312:   # WM_HOTKEY
                    self.q.put(("hotkey", None))
        finally:
            if hook:
                user32.UnhookWindowsHookEx(hook)
            else:
                user32.UnregisterHotKey(None, 1)

    def _stop_hotkey(self):
        if self._hotkey_thread_id:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(
                    self._hotkey_thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass

    def _restart_hotkey(self):
        """Ré-enregistre le raccourci global après un changement (à chaud)."""
        self._stop_hotkey()
        if self._hotkey_thread is not None:
            try:
                self._hotkey_thread.join(timeout=1.0)
            except Exception:
                pass
        self._hotkey_thread_id = None
        self._start_hotkey()

    def _append_text(self, new):
        """Mode ajout : ajoute le nouveau texte à la suite (séparé par une espace)."""
        existing = self.text.get("1.0", "end-1c")
        if existing.strip():
            self.text.insert("end", " " + new)
        else:
            self.text.insert("end", new)
        self.text.see("end")

    def _reveal_text_area(self):
        if not self.text_revealed:
            self.text_frame.pack(fill="both", expand=True)
            self.root.geometry(self._expanded_geom)
            self.text_revealed = True

    def _collapse_text_area(self):
        """Replie complètement la zone de texte (retour à la fenêtre compacte)."""
        if self.text_revealed:
            self.text_frame.pack_forget()
            self.root.geometry(self._compact_geom)
            self.text_revealed = False

    # ----- Dialogue de sélection des sources -----
    def _refresh_devices(self):
        threading.Thread(target=self._enumerate_worker, daemon=True).start()

    def _open_sources_dialog(self):
        if not self.devices_loaded:
            self.status_var.set("Énumération des périphériques en cours…")
            return
        if self._sources_dialog is not None:
            try:
                self._sources_dialog.lift()
            except Exception:
                pass
            return
        t = self.theme
        dlg = tk.Toplevel(self.root)
        dlg.title("Sources audio")
        dlg.configure(bg=t["bg"])
        dlg.resizable(False, False)
        dlg.transient(self.root)
        try:
            dlg.geometry(f"+{self.root.winfo_rootx() + self.px(24)}"
                         f"+{self.root.winfo_rooty() + self.px(44)}")
        except Exception:
            pass
        dlg.protocol("WM_DELETE_WINDOW", self._close_sources_dialog)
        self._sources_dialog = dlg
        self._dialog_vars = {}
        self.root.update_idletasks()
        _set_titlebar_dark(dlg, t.get("dark_titlebar", False))
        self._rebuild_sources_dialog()
        try:
            dlg.grab_set()
        except Exception:
            pass

    def _rebuild_sources_dialog(self):
        dlg = self._sources_dialog
        if dlg is None:
            return
        t = self.theme
        dlg.configure(bg=t["bg"])
        for w in dlg.winfo_children():
            w.destroy()
        self._dialog_vars = {}
        selected_ids = {s["id"] for s in self.selected_sources}
        pad = self.px(14)

        tk.Label(dlg, text="Cochez une ou plusieurs sources — mix possible (molette pour défiler)",
                 bg=t["bg"], fg=t["text"], font=self.f_seg, anchor="w",
                 wraplength=self.px(360), justify="left").pack(
                     fill="x", padx=pad, pady=(self.px(12), self.px(4)))

        body = tk.Frame(dlg, bg=t["bg"])
        body.pack(fill="both", expand=True, padx=pad)
        canvas = tk.Canvas(body, bg=t["bg"], highlightthickness=0, bd=0,
                           width=self.px(372), height=self.px(300))
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=t["bg"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_config(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win, width=canvas.winfo_width())
        inner.bind("<Configure>", _on_config)
        canvas.bind("<Enter>", lambda _e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        def section(title):
            tk.Label(inner, text=title, bg=t["bg"], fg=t["muted"], font=self.f_seg,
                     anchor="w").pack(fill="x", pady=(self.px(8), self.px(2)))

        def add_device(d):
            var = tk.IntVar(value=1 if d["id"] in selected_ids else 0)
            self._dialog_vars[d["id"]] = (var, d)
            tk.Checkbutton(
                inner, text="  " + d["name"], variable=var,
                bg=t["bg"], fg=t["text"], selectcolor=t["surface"],
                activebackground=t["bg"], activeforeground=t["text"],
                anchor="w", font=self.f_status, bd=0, highlightthickness=0,
                takefocus=0,
            ).pack(fill="x")

        def empty():
            tk.Label(inner, text="   (aucun)", bg=t["bg"], fg=t["muted"],
                     font=self.f_status, anchor="w").pack(fill="x")

        section("🎙  Micros / entrées")
        if self.devices["inputs"]:
            for d in self.devices["inputs"]:
                add_device(d)
        else:
            empty()
        section("🔊  Son du PC (sorties, captées en loopback)")
        if self.devices["outputs"]:
            for d in self.devices["outputs"]:
                add_device(d)
        else:
            empty()

        bar = tk.Frame(dlg, bg=t["bg"])
        bar.pack(fill="x", padx=pad, pady=(self.px(8), self.px(12)))
        refresh = RoundedButton(bar, "↻  Actualiser", self._refresh_devices,
                                width=self.px(120), height=self.px(36),
                                radius=self.px(10), font=self.f_seg, kind="ghost",
                                scale=self.scale)
        refresh.pack(side="left")
        refresh.set_theme(t, t["bg"])
        ok = RoundedButton(bar, "Valider", self._apply_sources_dialog,
                           width=self.px(120), height=self.px(36),
                           radius=self.px(10), font=self.f_btn, kind="accent",
                           scale=self.scale)
        ok.pack(side="right")
        ok.set_theme(t, t["bg"])

    def _apply_sources_dialog(self):
        chosen = [d for (var, d) in self._dialog_vars.values() if var.get()]
        self.selected_sources = chosen
        self.recorder.set_sources(chosen)
        self._save_prefs()
        self._refresh_source_ui()
        self._update_idle_controls()
        self._close_sources_dialog()

    def _close_sources_dialog(self):
        dlg = self._sources_dialog
        self._sources_dialog = None
        if dlg is not None:
            try:
                self.root.unbind_all("<MouseWheel>")
                dlg.grab_release()
            except Exception:
                pass
            dlg.destroy()

    def on_close(self):
        if self._insert_poll_id is not None:
            try:
                self.root.after_cancel(self._insert_poll_id)
            except Exception:
                pass
        try:
            self._stop_hotkey()
        except Exception:
            pass
        try:
            self.recorder.close()
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# Auto-test
# ---------------------------------------------------------------------------
def run_selftest():
    print(f"=== Auto-test Plume {__version__} ===")
    print(f"Python {sys.version.split()[0]} | SR : {SAMPLE_RATE} Hz | Langue : {LANGUAGE}")
    print(f"Modèle demandé : {MODEL_SIZE or 'auto'} "
          f"(GPU : {DEFAULT_GPU_MODEL}, CPU : {DEFAULT_CPU_MODEL}) | dossier : "
          f"{MODEL_DIR or 'cache HuggingFace'}")
    print(f"VAD disponible (onnxruntime) : {VAD_FILTER}")
    print(f"Capture audio disponible (soundcard, Windows) : {CAPTURE_AVAILABLE}")
    print(f"Interface Tk disponible : {TK_AVAILABLE}")
    if ADDED_DLL_DIRS:
        print("Répertoires DLL CUDA ajoutés au PATH :")
        for d in ADDED_DLL_DIRS:
            print("   -", d)
    else:
        print("Aucun répertoire DLL nvidia détecté (paquets GPU non installés : "
              "normal en installation CPU).")

    t0 = time.time()
    try:
        model, backend, size = load_model()
    except ModelError as e:
        print(f"\nÉCHEC : {e}")
        print("=== Auto-test terminé : ÉCHEC ===")
        return None
    load_dt = time.time() - t0
    print(f"\nBackend obtenu          : {backend}")
    print(f"Modèle chargé           : {size}")
    print(f"Temps de chargement     : {load_dt:.1f}s (incl. passe de chauffe)")

    # Buffer synthétique : 2 s de ton sinusoïdal (signal non vocal -> texte
    # probablement vide, ce qui est attendu ; on valide le pipeline + backend).
    duration = 2.0
    t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
    audio = (0.05 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    t1 = time.time()
    segments, info = model.transcribe(audio, language=LANGUAGE, beam_size=1,
                                      vad_filter=False)
    text = " ".join(s.text for s in segments).strip()
    transcribe_dt = time.time() - t1

    print(f"Transcription ({duration:.0f}s) : {transcribe_dt:.2f}s")
    print(f"Langue détectée/forcée  : {info.language} (p={info.language_probability:.2f})")
    print(f"Texte (signal non vocal -> vide attendu) : {text!r}")
    print("=== Auto-test terminé : OK ===")
    return backend


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def main():
    if "--version" in sys.argv:
        print(f"Plume {__version__}")
        return 0
    if "--selftest" in sys.argv:
        return 0 if run_selftest() else 1

    if not TK_AVAILABLE:
        fatal("L'interface graphique nécessite tkinter, absent de cette installation "
              "de Python. Réinstallez Python (python.org) en cochant « tcl/tk and "
              "IDLE », ou utilisez la version portable (exécutable).")
    _enable_dpi_awareness()
    try:
        root = tk.Tk()
    except Exception as e:
        fatal("Impossible d'ouvrir une fenêtre (Tk). " + str(e), e)
    try:
        PlumeApp(root)
    except Exception as e:
        fatal(f"Erreur au démarrage de l'interface ({type(e).__name__}) : {e}", e)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
