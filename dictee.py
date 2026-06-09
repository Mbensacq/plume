# -*- coding: utf-8 -*-
"""
Dictée vocale locale (speech-to-text) — 100 % hors ligne.

Pile : faster-whisper (CTranslate2) + sounddevice (PortAudio) + numpy + tkinter.
Cible : Windows 11, NVIDIA RTX 4090 (CUDA 12 / cuDNN 9), repli CPU automatique.

Interface : rendu net (DPI-aware), 3 thèmes (Sombre / Clair / Océan), bouton
micro circulaire et boutons arrondis dessinés sur Canvas. Le thème est mémorisé.

Usage :
    python dictee.py             # lance l'interface graphique
    python dictee.py --selftest  # auto-test : charge le modèle, transcrit un
                                 #   buffer synthétique, affiche le backend, sort.
"""

import os
import sys
import time
import json
import queue
import threading
import importlib.util

# ---------------------------------------------------------------------------
# Constantes éditables
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000        # Hz — format attendu par Whisper (16 kHz mono)
MODEL_SIZE = "large-v3"    # ex. "large-v3", "medium", "small" (repli CPU : viser plus petit)
LANGUAGE = "fr"            # langue forcée

# Repli CPU : si le chargement/inférence CUDA échoue, on retente en CPU/int8.
GPU_DEVICE = "cuda"
GPU_COMPUTE_TYPE = "float16"
CPU_DEVICE = "cpu"
CPU_COMPUTE_TYPE = "int8"

# Qualité de décodage (beam search). 5 = bon compromis qualité/vitesse.
BEAM_SIZE = 5

# Durée minimale d'un enregistrement (en secondes) pour tenter une transcription.
MIN_RECORD_SECONDS = 0.2


# ---------------------------------------------------------------------------
# Configuration des DLL CUDA (cuBLAS / cuDNN) installées via pip
# ---------------------------------------------------------------------------
def _setup_cuda_dll_path():
    """Ajoute les répertoires `bin` des paquets nvidia-*-cu12 au chemin de
    recherche des DLL, afin que CTranslate2 trouve cuBLAS et cuDNN.

    Doit être appelé AVANT d'importer faster_whisper / ctranslate2.
    Retourne la liste des répertoires ajoutés (vide si rien trouvé)."""
    added = []
    try:
        import nvidia  # paquets nvidia-cublas-cu12, nvidia-cudnn-cu12, ...
    except ImportError:
        return added

    # `nvidia` est un paquet d'espace de noms (PEP 420) : pas de __file__ ;
    # on parcourt __path__ pour localiser le dossier site-packages/nvidia.
    for base in list(getattr(nvidia, "__path__", [])):
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            bindir = os.path.join(base, sub, "bin")
            if os.path.isdir(bindir):
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


def _system_audio_available():
    """Capture du son système (loopback WASAPI) via la bibliothèque 'soundcard'.
    Disponible uniquement sous Windows et si le paquet est installé."""
    return (sys.platform == "win32"
            and importlib.util.find_spec("soundcard") is not None)


SYSTEM_AUDIO_AVAILABLE = _system_audio_available()


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

    model = WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type)

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
    if prefer_gpu:
        try:
            model = _try_load(GPU_DEVICE, GPU_COMPUTE_TYPE)
            return model, "GPU (CUDA)"
        except Exception as e:
            print("[Dictée] Chargement CUDA échoué -> repli CPU.\n"
                  f"         Cause : {e}", file=sys.stderr)
    model = _try_load(CPU_DEVICE, CPU_COMPUTE_TYPE)
    return model, "CPU"


# ---------------------------------------------------------------------------
# Capture audio (sounddevice / PortAudio)
# ---------------------------------------------------------------------------
class Recorder:
    """Capture audio en 16 kHz mono float32.

    Deux sources possibles :
      - "mic"    : microphone via sounddevice (PortAudio), callback non bloquant.
      - "system" : son système (loopback WASAPI) via soundcard, dans un thread
                   dédié (record() bloquant + CoInitialize COM requis).
    """

    def __init__(self, samplerate=SAMPLE_RATE, source="mic"):
        self.samplerate = samplerate
        self.source = source
        self._frames = []
        self._lock = threading.Lock()
        # micro (sounddevice)
        self._stream = None
        # système (soundcard)
        self._sys_thread = None
        self._sys_running = False
        self._sys_error = None
        self._sys_spk_name = None

    def set_source(self, source):
        self.source = source
        self._sys_error = None

    def consume_error(self):
        """Retourne la dernière erreur de capture système (et la remet à zéro)."""
        err, self._sys_error = self._sys_error, None
        return err

    # ----- démarrage -----
    def start(self):
        """Démarre la capture selon la source. Lève si la source est indisponible."""
        with self._lock:
            self._frames = []
        if self.source == "system":
            self._start_system()
        else:
            self._start_mic()

    def _start_mic(self):
        import sounddevice as sd  # import tardif : évite de toucher PortAudio en --selftest
        self._stream = sd.InputStream(
            samplerate=self.samplerate, channels=1, dtype="float32",
            callback=self._mic_callback,
        )
        self._stream.start()

    def _mic_callback(self, indata, frames, time_info, status):
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        # indata : (frames, 1) ; copie obligatoire (le buffer est réutilisé).
        with self._lock:
            self._frames.append(indata.copy())

    def _start_system(self):
        import soundcard as sc  # peut lever ImportError si non installé
        spk = sc.default_speaker()
        if spk is None:
            raise RuntimeError("aucun périphérique de sortie détecté")
        self._sys_spk_name = spk.name
        self._sys_error = None
        self._sys_running = True
        self._sys_thread = threading.Thread(target=self._system_loop, daemon=True)
        self._sys_thread.start()
        # Laisser le thread tenter l'ouverture et remonter une erreur immédiate.
        time.sleep(0.15)
        if self._sys_error is not None:
            self._sys_running = False
            raise RuntimeError(self._sys_error)

    def _system_loop(self):
        try:
            import ctypes
            ctypes.windll.ole32.CoInitialize(None)  # COM requis dans ce thread
        except Exception:
            pass
        try:
            import soundcard as sc
            mic = sc.get_microphone(self._sys_spk_name, include_loopback=True)
            with mic.recorder(samplerate=self.samplerate, channels=1,
                              blocksize=1024) as rec:
                while self._sys_running:
                    data = rec.record(numframes=1600)  # ~0,1 s ; zéros si silence
                    with self._lock:
                        self._frames.append(np.asarray(data, dtype=np.float32))
        except Exception as e:
            self._sys_error = f"{type(e).__name__}: {e}"
            print(f"[audio système] {self._sys_error}", file=sys.stderr)
        finally:
            try:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    # ----- arrêt -----
    def stop(self):
        """Arrête la capture, retourne l'audio capturé (np.float32, 1D)."""
        if self.source == "system":
            self._sys_running = False
            if self._sys_thread is not None:
                self._sys_thread.join(timeout=2.0)
                self._sys_thread = None
        else:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                finally:
                    self._stream = None
        with self._lock:
            frames = self._frames
            self._frames = []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames, axis=0).reshape(-1)

    def close(self):
        """Fermeture défensive (sortie de l'application)."""
        self._sys_running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._sys_thread is not None:
            try:
                self._sys_thread.join(timeout=1.0)
            except Exception:
                pass
            self._sys_thread = None


# ===========================================================================
# INTERFACE GRAPHIQUE
# ===========================================================================
import tkinter as tk  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dictee_config.json")

# --- Palettes de thèmes -----------------------------------------------------
THEME_ORDER = ["Sombre", "Clair", "Océan"]
DEFAULT_THEME = "Sombre"
THEMES = {
    "Sombre": {
        "bg": "#1b1b2b", "surface": "#262639", "elevated": "#2e2e46",
        "text": "#e9e9f4", "muted": "#9a9ab8",
        "accent": "#7c6cf0", "accent_hi": "#9183ff", "on_accent": "#ffffff",
        "danger": "#f0566e", "danger_hi": "#ff6f86",
        "border": "#363651", "dark_titlebar": True,
    },
    "Clair": {
        "bg": "#eceef4", "surface": "#ffffff", "elevated": "#f5f6fa",
        "text": "#1e2430", "muted": "#6c7384",
        "accent": "#4f6cf7", "accent_hi": "#3f5ce6", "on_accent": "#ffffff",
        "danger": "#e5484d", "danger_hi": "#d23b40",
        "border": "#dde0e9", "dark_titlebar": False,
    },
    "Océan": {
        "bg": "#0e2a2f", "surface": "#14393f", "elevated": "#1a474e",
        "text": "#dff5f1", "muted": "#82b2ae",
        "accent": "#2dd4bf", "accent_hi": "#46e7d3", "on_accent": "#06302c",
        "danger": "#ff6b6b", "danger_hi": "#ff8585",
        "border": "#1e545a", "dark_titlebar": True,
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
        self.scale = scale
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

    def _palette(self):
        t = self.theme
        if not self._enabled:
            return t["border"], t["muted"]
        if self.kind == "accent":
            return (t["accent_hi"] if self._hover else t["accent"]), t["on_accent"]
        return (t["border"] if self._hover else t["elevated"]), t["text"]

    def _redraw(self):
        if self.theme is None:
            return
        self.delete("all")
        fill, fg = self._palette()
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
        self.scale = scale
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
        s = self.size
        cx = cy = s / 2
        r = s / 2 - max(2, int(3 * self.scale))

        if not self._enabled:
            circle, ring, icon = t["surface"], t["border"], t["muted"]
        elif self._recording:
            circle = t["danger_hi"] if self._hover else t["danger"]
            ring, icon = circle, "#ffffff"
        else:
            circle = t["accent_hi"] if self._hover else t["accent"]
            ring, icon = circle, t["on_accent"]

        # halo discret derrière le bouton
        halo = max(1, int(3 * self.scale))
        self.create_oval(cx - r - halo, cy - r - halo, cx + r + halo, cy + r + halo,
                         outline=t["border"], width=1)
        self.create_oval(cx - r, cy - r, cx + r, cy + r,
                         fill=circle, outline=ring)

        if self._recording:
            self._draw_stop(cx, cy, icon)
        elif self.mode == "system":
            self._draw_speaker(cx, cy, icon)
        else:
            self._draw_mic(cx, cy, icon)

    def _lw(self, factor):
        return max(2, int(factor * self.scale))

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


# --- Contrôle segmenté (choix de la source : micro / système) ---------------
class SegmentedToggle(tk.Canvas):
    def __init__(self, parent, options, on_change, width, height, font, scale=1.0):
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0, bd=0)
        self.options = options          # [(clé, libellé), ...]
        self.on_change = on_change
        self.cw, self.ch = width, height
        self.font = font
        self.scale = scale
        self.theme = None
        self.current = options[0][0]
        self._enabled = True
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)

    def set_theme(self, theme, page_bg):
        self.theme = theme
        self.configure(bg=page_bg)
        self._redraw()

    def set_current(self, key):
        self.current = key
        self._redraw()

    def set_enabled(self, on):
        self._enabled = on
        self._redraw()

    def _seg(self, i):
        w = self.cw / len(self.options)
        return i * w, 0, (i + 1) * w, self.ch

    def _redraw(self):
        if self.theme is None:
            return
        self.delete("all")
        t = self.theme
        _round_rect(self, 1, 1, self.cw - 1, self.ch - 1, self.ch / 2,
                    fill=t["elevated"], outline=t["border"])
        pad = max(2, int(2 * self.scale))
        for i, (key, label) in enumerate(self.options):
            x1, y1, x2, y2 = self._seg(i)
            if key == self.current:
                fill = t["accent"] if self._enabled else t["border"]
                _round_rect(self, x1 + pad, y1 + pad, x2 - pad, y2 - pad,
                            (self.ch - 2 * pad) / 2, fill=fill, outline=fill)
                fg = t["on_accent"] if self._enabled else t["muted"]
            else:
                fg = t["muted"]
            self.create_text((x1 + x2) / 2, self.ch / 2, text=label,
                             fill=fg, font=self.font)

    def _click(self, e):
        if not self._enabled:
            return
        i = max(0, min(len(self.options) - 1, int(e.x // (self.cw / len(self.options)))))
        key = self.options[i][0]
        if key != self.current:
            self.current = key
            self._redraw()
            if self.on_change:
                self.on_change(key)

    def _motion(self, _e):
        self.configure(cursor="hand2" if self._enabled else "")


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
class DicteeApp:
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
        self.theme_name = cfg.get("theme", DEFAULT_THEME)
        if self.theme_name not in THEMES:
            self.theme_name = DEFAULT_THEME
        self.theme = THEMES[self.theme_name]

        # Source de capture mémorisée (micro / système). Repli sur micro si la
        # capture système n'est pas disponible.
        self.source = cfg.get("source", "mic")
        if self.source not in ("mic", "system") or not SYSTEM_AUDIO_AVAILABLE:
            self.source = "mic"

        self.q = queue.Queue()
        self.recorder = Recorder(SAMPLE_RATE, source=self.source)
        self.model = None
        self.backend = None
        self.recording = False
        self.text_revealed = False

        # Polices (tailles en points -> mises à l'échelle par tk scaling).
        self.f_title = ("Segoe UI", 15, "bold")
        self.f_status = ("Segoe UI", 9)
        self.f_btn = ("Segoe UI", 10, "bold")
        self.f_seg = ("Segoe UI", 9, "bold")
        self.f_text = ("Segoe UI", 11)

        root.title("  Dictée")
        root.resizable(False, False)
        self._compact_geom = f"{self.px(360)}x{self.px(252)}"
        self._expanded_geom = f"{self.px(360)}x{self.px(514)}"
        root.geometry(self._compact_geom)
        try:
            root.minsize(self.px(360), self.px(252))
        except Exception:
            pass

        self._build_ui()
        self.root.update_idletasks()
        self.apply_theme(self.theme_name, persist=False)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._pump_queue)
        threading.Thread(target=self._load_model_worker, daemon=True).start()

    def px(self, n):
        return int(round(n * self.scale))

    # ----- Construction de l'interface -----
    def _build_ui(self):
        pad = self.px(18)
        self.container = tk.Frame(self.root)
        self.container.pack(fill="both", expand=True)

        # En-tête : titre + sélecteur de thème
        self.header = tk.Frame(self.container)
        self.header.pack(fill="x", padx=pad, pady=(self.px(12), 0))
        self.title_lbl = tk.Label(self.header, text="Dictée", font=self.f_title)
        self.title_lbl.pack(side="left")
        self.dots = ThemeDots(self.header, self._on_pick_theme, self.scale)
        self.dots.pack(side="right")

        # Bouton micro circulaire (centré)
        self.mic_area = tk.Frame(self.container)
        self.mic_area.pack(fill="x", pady=(self.px(10), 0))
        self.mic_btn = MicButton(self.mic_area, self._on_mic,
                                 size=self.px(86), scale=self.scale)
        self.mic_btn.pack()

        # Sélecteur de source : micro ou son système (loopback)
        sys_label = "🔊  Système" if SYSTEM_AUDIO_AVAILABLE else "🔊  Système (indispo.)"
        self.source_sel = SegmentedToggle(
            self.container,
            options=[("mic", "🎙  Micro"), ("system", sys_label)],
            on_change=self._on_source_change,
            width=self.px(232), height=self.px(34),
            font=self.f_seg, scale=self.scale,
        )
        self.source_sel.pack(pady=(self.px(8), 0))
        self.source_sel.set_current(self.source)
        self.mic_btn.set_mode(self.source)

        # Barre d'état
        self.status_var = tk.StringVar(value="Chargement du modèle…")
        self.status_lbl = tk.Label(self.container, textvariable=self.status_var,
                                   font=self.f_status, anchor="center")
        self.status_lbl.pack(fill="x", pady=(self.px(10), self.px(12)))

        # Panneau de texte (caché jusqu'au premier résultat)
        self.text_frame = tk.Frame(self.container)
        self.text = tk.Text(self.text_frame, wrap="word", font=self.f_text,
                            bd=0, relief="flat", highlightthickness=1,
                            padx=self.px(12), pady=self.px(10), height=8)
        self.text.pack(fill="both", expand=True, padx=pad, pady=(0, self.px(10)))
        self.copy_btn = RoundedButton(self.text_frame, "Copier", self._on_copy,
                                      width=self.px(130), height=self.px(40),
                                      radius=self.px(12), font=self.f_btn,
                                      kind="accent", scale=self.scale)
        self.copy_btn.pack(pady=(0, self.px(14)))

    # ----- Thèmes -----
    def apply_theme(self, name, persist=True):
        self.theme_name = name
        self.theme = t = THEMES[name]
        self.root.configure(bg=t["bg"])
        self.container.configure(bg=t["bg"])
        self.header.configure(bg=t["bg"])
        self.title_lbl.configure(bg=t["bg"], fg=t["text"])
        self.mic_area.configure(bg=t["bg"])
        self.status_lbl.configure(bg=t["bg"], fg=t["muted"])
        self.text_frame.configure(bg=t["bg"])
        self.text.configure(bg=t["surface"], fg=t["text"],
                            insertbackground=t["accent"],
                            selectbackground=t["accent"],
                            selectforeground=t["on_accent"],
                            highlightbackground=t["border"],
                            highlightcolor=t["border"])
        self.dots.render(name, t["bg"])
        self.mic_btn.set_theme(t, t["bg"])
        self.source_sel.set_theme(t, t["bg"])
        self.copy_btn.set_theme(t, t["bg"])
        _set_titlebar_dark(self.root, t.get("dark_titlebar", False))
        if persist:
            self._save_prefs()

    def _save_prefs(self):
        save_config({"theme": self.theme_name, "source": self.source})

    def _ready_status(self):
        src = "son système" if self.source == "system" else "micro"
        return f"Prêt — {self.backend}  ·  {src}"

    def _on_pick_theme(self, name):
        if name != self.theme_name:
            self.apply_theme(name)

    def _on_source_change(self, key):
        if key == "system" and not SYSTEM_AUDIO_AVAILABLE:
            self.source_sel.set_current("mic")
            self.status_var.set("Capture système indisponible (paquet 'soundcard' absent).")
            return
        self.source = key
        self.recorder.set_source(key)
        self.mic_btn.set_mode(key)
        self._save_prefs()
        if self.model is not None and not self.recording:
            self.status_var.set(self._ready_status())

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
            segments, _info = self.model.transcribe(
                audio, language=LANGUAGE, beam_size=BEAM_SIZE,
                vad_filter=VAD_FILTER,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if not text:
                self.q.put(("empty", None))
            else:
                self.q.put(("result", text))
        except Exception as e:
            self.q.put(("error", f"Erreur de transcription : {e}"))

    # ----- Boucle de file (thread principal) -----
    def _pump_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._pump_queue)

    def _handle(self, kind, payload):
        if kind == "ready":
            backend, dt = payload
            self.status_var.set(self._ready_status())
            self.mic_btn.set_enabled(True)
            self.source_sel.set_enabled(True)
            print(f"[Dictée] Modèle prêt en {dt:.1f}s — backend : {backend}",
                  file=sys.stderr)
        elif kind == "result":
            self._append_text(payload)
            self._reveal_text_area()
            self.mic_btn.set_enabled(True)
            self.source_sel.set_enabled(True)
            self.status_var.set(self._ready_status())
        elif kind == "empty":
            self.mic_btn.set_enabled(True)
            self.source_sel.set_enabled(True)
            self.status_var.set("Rien à transcrire (enregistrement vide ou trop court).")
        elif kind == "error":
            self.mic_btn.set_enabled(self.model is not None)
            self.source_sel.set_enabled(self.model is not None)
            self.status_var.set(payload)
            print(f"[Dictée] {payload}", file=sys.stderr)

    # ----- Actions UI (thread principal) -----
    def _on_mic(self):
        if not self.recording:
            try:
                self.recorder.start()
            except Exception as e:
                src = "Capture système" if self.source == "system" else "Micro"
                self.status_var.set(f"{src} indisponible : {e}")
                print(f"[Dictée] {src} indisponible : {e}", file=sys.stderr)
                return
            self.recording = True
            self.mic_btn.set_recording(True)
            self.source_sel.set_enabled(False)
            label = "son système" if self.source == "system" else "micro"
            self.status_var.set(f"Enregistrement… ({label})")
        else:
            self.recording = False
            audio = self.recorder.stop()
            self.mic_btn.set_recording(False)
            err = self.recorder.consume_error()
            if err and audio.size == 0:
                self.status_var.set(f"Capture système : {err}")
                self.mic_btn.set_enabled(True)
                self.source_sel.set_enabled(True)
                return
            self.mic_btn.set_enabled(False)
            self.status_var.set("Transcription…")
            threading.Thread(target=self._transcribe_worker, args=(audio,),
                             daemon=True).start()

    def _on_copy(self):
        text = self.text.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Copié dans le presse-papiers ✓")

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

    def on_close(self):
        try:
            self.recorder.close()
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# Auto-test
# ---------------------------------------------------------------------------
def run_selftest():
    print("=== Auto-test Dictée ===")
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
    DicteeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
