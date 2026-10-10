# -*- coding: utf-8 -*-
"""
Vérification d'une installation de Plume, contrôle par contrôle.

Lancé par scripts/install-windows.ps1 en fin d'installation, mais utilisable seul
depuis la racine du dépôt :

    .venv\\Scripts\\python scripts\\check_install.py
    .venv\\Scripts\\python scripts\\check_install.py --skip-model
    .venv\\Scripts\\python scripts\\check_install.py --ai-model llama3.1:8b

Contrôles : Python, import de plume.py, dépendances, Tk, tests unitaires,
périphériques audio, GPU, modèle Whisper (téléchargement + chargement +
transcription via `plume.run_selftest`) et, sur demande, le mode IA (serveur
Ollama, présence du modèle, reformulation réelle via `plume.ai_reformulate`).

Messages volontairement en ASCII : la sortie traverse la console Windows puis,
depuis WSL, un pipe dont l'encodage n'est pas garanti.

Code de sortie : 0 si aucun contrôle n'est en ECHEC, 1 sinon.
"""
import argparse
import importlib
import importlib.metadata
import json
import os
import struct
import sys
import threading
import time
import urllib.request

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_DIR not in sys.path:
    sys.path.insert(0, REPO_DIR)

OK, INFO, WARN, FAIL = "OK", "INFO", "ATTENTION", "ECHEC"

# (module à importer, distribution pip pour la version, libellé)
DEPENDENCIES = [
    ("numpy", "numpy", "numpy"),
    ("PIL.ImageTk", "pillow", "pillow (rendu de l'interface)"),
    ("faster_whisper", "faster-whisper", "faster-whisper"),
    ("ctranslate2", "ctranslate2", "ctranslate2"),
    ("onnxruntime", "onnxruntime", "onnxruntime (filtre VAD)"),
    ("soundcard", "soundcard", "soundcard (capture WASAPI)"),
]

AI_TEST_TEXT = "bonjour ceci est un petit test de la dictee vocale"

_results = []


def report(status, label, detail=""):
    _results.append((status, label, detail))
    line = f"[{status:^9}] {label}"
    if detail:
        line += f" : {detail}"
    print(line, flush=True)


def section(title):
    print(f"\n--- {title} ---", flush=True)


def _err(e):
    return f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Contrôles
# ---------------------------------------------------------------------------
def check_python():
    """Plume ne fonctionne que sous Windows natif, en Python 64 bits."""
    v = sys.version_info
    bits = struct.calcsize("P") * 8
    desc = f"{v.major}.{v.minor}.{v.micro}, {bits} bits ({sys.executable})"
    if sys.platform != "win32":
        report(FAIL, "Systeme", f"{sys.platform} : Plume doit tourner sous "
                                "Windows natif (pas dans WSL)")
        return False
    if bits != 64:
        report(FAIL, "Python", desc + " - 64 bits requis")
        return False
    if (v.major, v.minor) != (3, 13):
        report(WARN, "Python", desc + " - version testee : 3.13")
    else:
        report(OK, "Python", desc)
    return True


def import_plume():
    """Importe plume.py comme le fait l'application (config des DLL CUDA,
    numpy, tkinter, Pillow). Ne charge PAS le modèle Whisper (paresseux)."""
    try:
        import plume
    except Exception as e:
        report(FAIL, "Import de plume.py", _err(e))
        return None
    report(OK, "Import de plume.py")
    return plume


def check_dependencies():
    ok = True
    for module, dist, label in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except Exception as e:
            report(FAIL, label, "import impossible (" + _err(e) + ")")
            ok = False
            continue
        try:
            version = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            version = "version inconnue"
        report(OK, label, version)
    return ok


def check_tk():
    """Ouvre puis détruit une fenêtre Tk masquée : valide les DLL Tcl/Tk."""
    import tkinter as tk
    try:
        root = tk.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
            patch = root.tk.call("info", "patchlevel")
        finally:
            root.destroy()
    except Exception as e:
        report(FAIL, "Interface Tk", _err(e))
        return False
    report(OK, "Interface Tk", f"Tcl/Tk {patch}")
    return True


def check_unit_tests():
    try:
        import test_plume
    except Exception as e:
        report(FAIL, "Tests unitaires", "import impossible (" + _err(e) + ")")
        return False
    passed = test_plume._run()
    report(OK if passed else FAIL, "Tests unitaires",
           "tous passent" if passed else "voir le detail ci-dessus")
    return passed


def check_audio(plume):
    """Énumère les périphériques comme l'application : dans un thread dédié,
    COM initialisé (cf. PlumeApp._enumerate_worker)."""
    out = {}

    def worker():
        try:
            import ctypes
            ctypes.windll.ole32.CoInitialize(None)  # COM requis sur ce thread
        except Exception:
            pass
        try:
            out["data"] = plume.enumerate_devices()
        except Exception as e:
            out["error"] = _err(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=30)
    label = "Peripheriques audio"
    if t.is_alive():
        report(WARN, label, "enumeration bloquee (> 30 s)")
        return
    if "error" in out:
        report(WARN, label, out["error"])
        return
    data = out["data"]
    n_in, n_out = len(data["inputs"]), len(data["outputs"])
    detail = f"{n_in} micro(s), {n_out} sortie(s) capturable(s) en loopback"
    if n_in + n_out == 0:
        report(WARN, label, detail + " - rien a capturer")
    elif n_in == 0:
        report(WARN, label, detail + " - aucun micro : seul le son du PC "
                                     "sera capturable")
    else:
        report(OK, label, detail)
    for dev in data["inputs"]:
        print(f"            micro  : {dev['name']}")
    for dev in data["outputs"]:
        print(f"            sortie : {dev['name']}")


def check_gpu():
    """Nombre de GPU CUDA vus par CTranslate2 (0 = transcription sur CPU)."""
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
    except Exception as e:
        report(WARN, "GPU CUDA", "detection impossible (" + _err(e) + ")")
        return 0
    if n > 0:
        report(OK, "GPU CUDA", f"{n} GPU utilisable(s)")
    else:
        report(INFO, "GPU CUDA", "aucun : transcription sur CPU (plus lente)")
    return n


def check_model(plume, gpu_count):
    """Auto-test officiel de Plume : télécharge le modèle au premier passage,
    le charge (GPU puis repli CPU), puis transcrit un buffer synthétique."""
    label = f"Modele Whisper {plume.MODEL_SIZE}"
    if gpu_count == 0:
        # Sans GPU CUDA, on évite une tentative GPU vouée à l'échec (et son
        # message d'erreur alarmant) : l'application fait la même bascule seule.
        plume.FORCE_CPU = True
    print("(premier passage : telechargement du modele, ~3 Go pour large-v3)",
          flush=True)
    t0 = time.time()
    try:
        backend = plume.run_selftest()
    except Exception as e:
        report(FAIL, label, _err(e))
        return
    detail = f"backend {backend}, {time.time() - t0:.0f} s"
    if gpu_count > 0 and not backend.startswith("GPU"):
        report(WARN, label, detail + " - GPU present mais CUDA en echec : "
                                     "repli CPU (voir le message ci-dessus)")
    else:
        report(OK, label, detail)
    if not backend.startswith("GPU") and plume.MODEL_SIZE.startswith("large"):
        report(INFO, "Performance", "large-v3 sur CPU est lent ; un modele plus "
                                    "leger se choisit via PLUME_MODEL=medium ou small")


def _ollama_get(url, path, timeout):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
        return json.load(r)


def check_ai(plume, model, url):
    """Mode IA de bout en bout : serveur joignable, modèle présent, puis une
    vraie reformulation via la fonction de l'application."""
    url = url or plume.AI_URL
    try:
        version = _ollama_get(url, "/api/version", 5).get("version", "?")
    except Exception as e:
        report(FAIL, "Serveur Ollama", f"injoignable sur {url} (" + _err(e) + ")")
        return
    report(OK, "Serveur Ollama", f"version {version} sur {url}")

    label = f"Modele IA {model}"
    try:
        tags = _ollama_get(url, "/api/tags", 10).get("models", [])
        names = {m.get("name") for m in tags}
    except Exception as e:
        report(FAIL, label, "liste des modeles illisible (" + _err(e) + ")")
        return
    wanted = model if ":" in model else model + ":latest"
    if wanted not in names and model not in names:
        report(FAIL, label, f"absent - a installer : ollama pull {model}")
        return

    print("(reformulation de test : le premier appel charge le modele en "
          "memoire, jusqu'a quelques minutes sur CPU)", flush=True)
    t0 = time.time()
    try:
        out = plume.ai_reformulate(AI_TEST_TEXT, model, url=url, timeout=600)
    except Exception as e:  # AIError (message clair) ou erreur inattendue
        report(FAIL, label, str(e) or _err(e))
        return
    if not out:
        report(FAIL, label, "reponse vide du modele")
        return
    preview = out if len(out) <= 60 else out[:57] + "..."
    report(OK, label, f"reformulation en {time.time() - t0:.0f} s : {preview!r}")


def print_ai_config():
    """Modèle et URL du mode IA tels que l'application les utilisera :
    plume_config.json, sinon $PLUME_AI_MODEL / $PLUME_AI_URL, sinon défauts."""
    import plume
    cfg = plume.load_config()
    print(json.dumps({"model": cfg.get("ai_model") or plume.AI_MODEL_DEFAULT,
                      "url": plume.AI_URL}))


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------
def summarize():
    counts = {s: sum(1 for r in _results if r[0] == s) for s in (OK, INFO, WARN, FAIL)}
    print(f"\n=== Bilan de la verification : {counts[OK]} OK, {counts[WARN]} "
          f"ATTENTION, {counts[FAIL]} ECHEC ===")
    for status, label, detail in _results:
        if status in (WARN, FAIL):
            print(f"[{status:^9}] {label}" + (f" : {detail}" if detail else ""))
    return 1 if counts[FAIL] else 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            # Ne jamais planter sur un nom de périphérique non encodable.
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="Verifie une installation de Plume.")
    p.add_argument("--skip-model", action="store_true",
                   help="ne pas telecharger/charger le modele Whisper")
    p.add_argument("--ai-model", metavar="NOM",
                   help="verifier aussi le mode IA avec ce modele Ollama")
    p.add_argument("--ai-url", metavar="URL",
                   help="URL du serveur Ollama (defaut : celle de Plume)")
    p.add_argument("--print-ai-config", action="store_true",
                   help="afficher en JSON le modele et l'URL du mode IA, puis sortir")
    args = p.parse_args(argv)

    if args.print_ai_config:
        print_ai_config()
        return 0

    section("Environnement")
    if not check_python():
        return summarize()
    plume = import_plume()
    if plume is None:
        return summarize()

    section("Dependances")
    check_dependencies()
    check_tk()

    section("Tests unitaires")
    check_unit_tests()

    section("Audio")
    check_audio(plume)

    section("GPU et modele Whisper")
    gpu_count = check_gpu()
    if args.skip_model:
        report(INFO, f"Modele Whisper {plume.MODEL_SIZE}", "non verifie (--skip-model)")
    else:
        check_model(plume, gpu_count)

    if args.ai_model:
        section("Mode IA (Ollama)")
        check_ai(plume, args.ai_model, args.ai_url)

    return summarize()


if __name__ == "__main__":
    sys.exit(main())
