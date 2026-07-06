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
import queue
import threading
import importlib.util


# ---------------------------------------------------------------------------
# Emplacement de l'application (portable : tout est ancré sur le dossier de
# l'exécutable, que l'on soit lancé comme script .py ou comme .exe PyInstaller)
# ---------------------------------------------------------------------------
def _app_dir():
    """Dossier de base servant d'ancre aux fichiers de l'app (config, corrections,
    icône) : le dossier de ce script."""
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()


def _icon_path():
    """Chemin du .ico de l'app s'il est présent à côté du script, sinon None."""
    p = os.path.join(APP_DIR, "plume.ico")
    return p if os.path.exists(p) else None


# ---------------------------------------------------------------------------
# Constantes éditables
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000        # Hz — format attendu par Whisper (16 kHz mono)
# Taille du modèle. Surchargeable SANS recompiler via $PLUME_MODEL (ex.
# "medium" / "small" sur un PC sans GPU). Défaut : "large-v3".
MODEL_SIZE = os.environ.get("PLUME_MODEL", "large-v3")
LANGUAGE = "fr"            # langue forcée

# Dossier de stockage du modèle Whisper (téléchargé au 1er lancement). Par défaut
# None -> cache HuggingFace partagé. Surchargeable via $PLUME_MODEL_DIR.
MODEL_DIR = os.environ.get("PLUME_MODEL_DIR") or None

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

# --- Raccourci clavier global (Windows) ---
# Démarre/arrête la dictée même quand Plume n'est pas au premier plan.
# On essaie chaque candidat dans l'ordre ; le PREMIER qui s'enregistre est
# utilisé (un raccourci peut être déjà pris par une autre application).
# Modificateurs : 1=ALT, 2=CTRL, 4=SHIFT, 8=WIN ; 0x4000 = NOREPEAT.
HOTKEY_ENABLED = True
_NR = 0x4000
HOTKEY_CANDIDATES = [
    (0x0002 | 0x0001 | _NR, 0x44, "Ctrl + Alt + D"),
    (0x0002 | 0x0004 | _NR, 0x20, "Ctrl + Maj + Espace"),
    (0x0002 | 0x0001 | _NR, 0x20, "Ctrl + Alt + Espace"),
    (0x0002 | 0x0001 | _NR, 0x4A, "Ctrl + Alt + J"),
    (0x0002 | 0x0001 | _NR, 0x78, "Ctrl + Alt + F9"),
]

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

    # `nvidia` est un paquet d'espace de noms (PEP 420) : pas de __file__ ; on
    # parcourt __path__ pour localiser site-packages/nvidia.
    try:
        import nvidia  # paquets nvidia-cublas-cu12, nvidia-cudnn-cu12, ...
        bases = list(getattr(nvidia, "__path__", []))
    except ImportError:
        return added

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

import numpy as np  # noqa: E402  (après _setup_cuda_dll_path, par cohérence)


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
def _try_load(device, compute_type):
    """Construit le modèle puis exécute une petite passe de chauffe.

    La passe de chauffe force l'initialisation complète (allocation GPU,
    chargement de cuDNN, exécution des convolutions de l'encodeur) : un échec
    CUDA *réel* survient ici plutôt qu'à la première dictée de l'utilisateur.
    """
    from faster_whisper import WhisperModel

    # download_root : None (défaut) => cache HuggingFace partagé ; sinon dossier
    # imposé via $PLUME_MODEL_DIR.
    if MODEL_DIR:
        os.makedirs(MODEL_DIR, exist_ok=True)
    model = WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type,
                         download_root=MODEL_DIR)

    # Chauffe : 1 s de signal, VAD désactivé pour garantir le passage dans
    # l'encodeur (convolutions cuDNN) même si le buffer est quasi silencieux.
    warm = (0.01 * np.sin(2 * np.pi * 220 *
            np.linspace(0, 1, SAMPLE_RATE, endpoint=False))).astype(np.float32)
    segments, _ = model.transcribe(warm, language=LANGUAGE, beam_size=1,
                                   vad_filter=False)
    list(segments)  # consommer le générateur => exécute réellement l'inférence
    return model


def load_model(prefer_gpu=True):
    """Charge le modèle Whisper. Tente le GPU (CUDA/float16) puis se rabat sur
    le CPU (int8). Retourne (model, backend_str)."""
    if prefer_gpu and not FORCE_CPU:
        try:
            model = _try_load(GPU_DEVICE, GPU_COMPUTE_TYPE)
            return model, "GPU (CUDA)"
        except Exception as e:
            print("[Plume] Chargement CUDA échoué -> repli CPU.\n"
                  f"         Cause : {e}", file=sys.stderr)
    model = _try_load(CPU_DEVICE, CPU_COMPUTE_TYPE)
    return model, "CPU"


def load_replacements():
    """Charge le dictionnaire de corrections personnalisées (si présent).
    Ignore les clés commençant par « _ » (commentaires/exemples)."""
    try:
        with open(REPLACEMENTS_PATH, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items()
                if isinstance(k, str) and not k.startswith("_")
                and isinstance(v, str)}
    except Exception:
        return {}


def apply_replacements(text, repl):
    """Applique les corrections (mots entiers, insensible à la casse)."""
    for wrong, right in repl.items():
        text = re.sub(r"\b" + re.escape(wrong) + r"\b", right, text,
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


def transcribe_audio(model, audio):
    """Transcrit un buffer (np.float32 16 kHz mono) en texte ponctué.
    Centralise les options qui favorisent la ponctuation + les corrections."""
    segments, _info = model.transcribe(
        audio,
        language=LANGUAGE,
        beam_size=BEAM_SIZE,
        vad_filter=VAD_FILTER,
        initial_prompt=INITIAL_PROMPT or None,
        condition_on_previous_text=CONDITION_ON_PREVIOUS_TEXT,
    )
    text = _join_segments(segments)
    text = apply_replacements(text, load_replacements())
    return postprocess_text(text)


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

    # ----- démarrage -----
    def start(self):
        """Démarre la capture de toutes les sources sélectionnées."""
        if not self.sources:
            raise RuntimeError("aucune source sélectionnée")
        with self._lock:
            self._buffers = {i: [] for i in range(len(self.sources))}
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
        except Exception as e:
            msg = f"{src.get('name', src.get('id'))} — {type(e).__name__}: {e}"
            with self._lock:
                self._errors.append(msg)
            print(f"[audio] {msg}", file=sys.stderr)
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

    def snapshot(self):
        """Retourne l'audio mixé capturé JUSQU'ICI, sans arrêter la capture.
        Sert à la transcription en direct (streaming)."""
        with self._lock:
            buffers = {i: list(v) for i, v in self._buffers.items()}
        return self._mix(buffers)

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
import tkinter as tk  # noqa: E402

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
    except Exception:
        pass


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
        print(f"[Plume] insertion directe : SendInput {sent}/{n} (err {err})",
              file=sys.stderr)
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
            icon, glow, gs = "#ffffff", "#FFFFFF", 165
        else:
            circle = t["accent_hi"] if self._hover else t["accent"]
            icon, glow, gs = t["on_accent"], "#FFFFFF", (150 if self._hover else 100)

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
        self._live_committed = ""    # texte déjà figé pour la dictée en cours
        self._live_commit_sample = 0  # index audio figé (échantillons)
        self._live_prefix = ""       # texte déjà présent dans le panneau au départ

        # Raccourci clavier global
        self._hotkey_thread = None
        self._hotkey_thread_id = None
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
        self._compact_geom = f"{self.px(360)}x{self.px(384)}"
        self._expanded_geom = f"{self.px(360)}x{self.px(638)}"
        root.geometry(self._compact_geom)
        try:
            root.minsize(self.px(360), self.px(384))
        except Exception:
            pass

        self._build_ui()
        self.root.update_idletasks()
        self.apply_theme(self.theme_name, persist=False)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._pump_queue)
        threading.Thread(target=self._load_model_worker, daemon=True).start()
        threading.Thread(target=self._enumerate_worker, daemon=True).start()
        self._start_hotkey()

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
        self.clear_btn.set_theme(t, t["bg"])
        self.copy_btn.set_theme(t, t["bg"])
        _set_titlebar_dark(self.root, t.get("dark_titlebar", False))
        if self._sources_dialog is not None:
            self._rebuild_sources_dialog()
        if persist:
            self._save_prefs()

    def _save_prefs(self):
        save_config({
            "theme": self.theme_name,
            "output_mode": self.output_mode,
            "live_mode": self.live_mode,
            "sources": [{"id": s["id"], "loopback": s["loopback"]}
                        for s in self.selected_sources],
        })

    def _on_live_change(self, key):
        self.live_mode = (key == "live")
        self._save_prefs()
        self.status_var.set("Transcription en direct activée" if self.live_mode
                            else "Transcription à l'arrêt (fin d'enregistrement)")

    def _ready_status(self):
        s = f"Prêt — {self.backend}"
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
            data = enumerate_devices()
            self.q.put(("devices", data))
        except Exception as e:
            self.q.put(("devices_error", f"{type(e).__name__}: {e}"))

    def _on_pick_theme(self, name):
        if name != self.theme_name:
            self.apply_theme(name)

    # ----- Workers (threads secondaires) : ne touchent JAMAIS aux widgets -----
    def _load_model_worker(self):
        try:
            t0 = time.time()
            model, backend = load_model()
            dt = time.time() - t0
            self.model = model
            self.backend = backend
            self.q.put(("ready", (backend, dt)))
        except Exception as e:
            self.q.put(("error", f"Échec chargement modèle : {e}"))

    def _transcribe_worker(self, audio):
        try:
            if audio is None or audio.size < int(MIN_RECORD_SECONDS * SAMPLE_RATE):
                self.q.put(("empty", None))
                return
            text = transcribe_audio(self.model, audio)
            if not text:
                self.q.put(("empty", None))
            else:
                self.q.put(("result", text))
        except Exception as e:
            self.q.put(("error", f"Erreur de transcription : {e}"))

    # ----- Transcription en direct (streaming) -----
    def _stream_worker(self):
        """Boucle (thread) qui transcrit en continu la file audio bornée depuis
        le dernier segment figé, et poste des aperçus. À l'arrêt, fait la passe
        finale, fige tout, et poste le résultat définitif."""
        try:
            repl = load_replacements()
            while self.recording:
                self._process_live(self.recorder.snapshot(), repl, final=False)
                time.sleep(LIVE_STEP)
            final_audio = self.recorder.stop()
            errs = self.recorder.consume_errors()
            if final_audio.size == 0 and errs:
                self.q.put(("error", "Capture : " + " ; ".join(errs)[:120]))
                return
            self._process_live(final_audio, repl, final=True)
        except Exception as e:
            self.q.put(("error", f"Erreur transcription en direct : {e}"))

    def _process_live(self, snap, repl, final):
        """Transcrit la file audio courante (depuis le dernier point figé),
        décide s'il faut la figer, et poste l'aperçu (ou le résultat final)."""
        total = int(snap.size)
        tail = (snap[self._live_commit_sample:] if total > self._live_commit_sample
                else np.zeros(0, dtype=np.float32))
        tail_dur = tail.size / SAMPLE_RATE
        if not final and tail.size < int(0.4 * SAMPLE_RATE):
            return  # pas assez de son neuf pour une passe utile

        # Contexte : on redonne le texte déjà figé pour une bonne continuité.
        prompt = None
        if self._live_committed:
            prompt = (INITIAL_PROMPT + " " + self._live_committed[-200:]).strip()
        # File quasi silencieuse -> on ne transcrit pas (évite les hallucinations).
        text = (transcribe_stream(self.model, tail, repl, prompt)
                if rms(tail) >= LIVE_SILENCE_RMS else "")

        # On ne FIGE (rend non révisable) que sur une VRAIE pause (silence
        # prolongé) ou si la fenêtre devient trop longue : une simple hésitation
        # ne fige rien, tout reste révisable comme en mode « à l'arrêt ».
        commit = final
        if not commit and tail_dur >= LIVE_MIN_COMMIT:
            pause = tail[-int(LIVE_SILENCE_SEC * SAMPLE_RATE):]
            if tail_dur >= LIVE_MAX_TAIL or (pause.size and rms(pause) < LIVE_SILENCE_RMS):
                commit = True
        if commit:
            if text:
                self._live_committed = (self._live_committed + " " + text).strip()
            self._live_commit_sample = total
            body = self._live_committed
        else:
            body = (self._live_committed + " " + text).strip()

        if final:
            clean = postprocess_text(self._live_committed)
            self.q.put(("live", (self._join_prefix(clean), True, clean)))
        else:
            self.q.put(("live", (self._join_prefix(body), False, None)))

    def _join_prefix(self, body):
        """Assemble le texte déjà présent au départ (mode ajout) avec le nouveau."""
        if self._live_prefix and body:
            return (self._live_prefix + " " + body).strip()
        return (self._live_prefix or body).strip()

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
        can_record = (self.model is not None and len(self.selected_sources) > 0
                      and not self.recording)
        self.mic_btn.set_enabled(can_record)
        self.sources_btn.set_enabled(self.devices_loaded and not self.recording)
        self.output_sel.set_enabled(not self.recording)
        self.live_sel.set_enabled(not self.recording)

    def _handle(self, kind, payload):
        if kind == "hotkey":
            if self.recording or (self.model is not None and self.selected_sources):
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
            self.status_var.set(f"Périphériques audio : {payload}")
            print(f"[Plume] Énumération : {payload}", file=sys.stderr)
        elif kind == "ready":
            backend, dt = payload
            self._update_idle_controls()
            self.status_var.set(self._ready_status() if self.selected_sources
                                else f"Prêt — {backend}  ·  choisissez une source")
            print(f"[Plume] Modèle prêt en {dt:.1f}s — backend : {backend}",
                  file=sys.stderr)
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
        elif kind == "empty":
            self._update_idle_controls()
            self.status_var.set("Rien à transcrire (enregistrement vide ou trop court).")
        elif kind == "error":
            self._update_idle_controls()
            self.status_var.set(payload)
            print(f"[Plume] {payload}", file=sys.stderr)

    # ----- Actions UI (thread principal) -----
    def _on_mic(self):
        if not self.recording:
            if not self.selected_sources:
                self.status_var.set("Aucune source — ouvrez « Sources… ».")
                return
            try:
                self.recorder.start()
            except Exception as e:
                self.status_var.set(f"Capture indisponible : {e}")
                print(f"[Plume] Capture indisponible : {e}", file=sys.stderr)
                return
            # Nouvel enregistrement -> on repart d'une zone de texte VIDE
            # (tous les modes : Manuel / Auto-copie / Insérer).
            self.text.delete("1.0", "end")
            self.recording = True
            self.mic_btn.set_recording(True)
            self.sources_btn.set_enabled(False)
            self.output_sel.set_enabled(False)
            self.live_sel.set_enabled(False)
            self._record_start = time.monotonic()
            self._tick_timer()
            if self.live_mode:
                # Direct : la zone vient d'être vidée ; on affine au fil de la
                # parole (un thread dédié fait le streaming).
                self._live_prefix = ""
                self._live_committed = ""
                self._live_commit_sample = 0
                self._reveal_text_area()
                self._stream_thread = threading.Thread(target=self._stream_worker,
                                                        daemon=True)
                self._stream_thread.start()
        else:
            self.recording = False
            self.mic_btn.set_recording(False)
            if self.live_mode:
                # Le worker de streaming fait la passe finale (arrête le recorder,
                # fige le texte, applique la sortie) puis réactive les contrôles.
                self.mic_btn.set_enabled(False)
                self.status_var.set("Finalisation…")
                return
            audio = self.recorder.stop()
            errs = self.recorder.consume_errors()
            if errs and audio.size == 0:
                self.status_var.set("Capture : " + " ; ".join(errs)[:120])
                self._update_idle_controls()
                return
            if errs:
                print("[Plume] erreurs capture (partielles) :", errs, file=sys.stderr)
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
        """Affiche la durée d'enregistrement (mise à jour chaque 0,5 s)."""
        if not self.recording:
            return
        el = int(time.monotonic() - self._record_start)
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
        registered = None
        for mods, vk, label in HOTKEY_CANDIDATES:
            if user32.RegisterHotKey(None, 1, mods, vk):
                registered = label
                break
        if not registered:
            print("[Plume] Aucun raccourci global disponible (tous pris ?).",
                  file=sys.stderr)
            self.q.put(("hotkey_status", None))
            return
        self.q.put(("hotkey_status", registered))
        print(f"[Plume] Raccourci global actif : {registered}", file=sys.stderr)
        try:
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):       # WM_QUIT ou erreur
                    break
                if msg.message == 0x0312:   # WM_HOTKEY
                    self.q.put(("hotkey", None))
        finally:
            user32.UnregisterHotKey(None, 1)

    def _stop_hotkey(self):
        if self._hotkey_thread_id:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(
                    self._hotkey_thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass

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
    print("=== Auto-test Plume ===")
    print(f"Modèle : {MODEL_SIZE} | SR : {SAMPLE_RATE} Hz | Langue : {LANGUAGE}")
    print(f"VAD disponible (onnxruntime) : {VAD_FILTER}")
    if ADDED_DLL_DIRS:
        print("Répertoires DLL CUDA ajoutés au PATH :")
        for d in ADDED_DLL_DIRS:
            print("   -", d)
    else:
        print("Aucun répertoire DLL nvidia détecté (paquets CUDA pip absents ?).")

    t0 = time.time()
    model, backend = load_model()
    load_dt = time.time() - t0
    print(f"\nBackend obtenu          : {backend}")
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
    if "--selftest" in sys.argv:
        run_selftest()
        return

    _enable_dpi_awareness()
    root = tk.Tk()
    PlumeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
