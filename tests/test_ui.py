# -*- coding: utf-8 -*-
"""Test de fumée de l'interface : construction de la fenêtre, passage à l'état
« Prêt » avec un faux modèle, et réactions aux messages d'erreur. Ignoré si
Tk ou un affichage sont indisponibles (serveur sans X, Python sans tcl/tk)."""
import pytest

import plume

if not plume.TK_AVAILABLE:
    pytest.skip("tkinter indisponible", allow_module_level=True)

import tkinter as tk  # noqa: E402


@pytest.fixture
def app(monkeypatch, tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"pas d'affichage : {e}")
    root.withdraw()
    monkeypatch.setattr(plume, "CONFIG_PATH", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(plume, "LOG_PATH", str(tmp_path / "plume.log"))
    monkeypatch.setattr(plume, "CAPTURE_AVAILABLE", False)
    monkeypatch.setattr(plume, "HOTKEY_ENABLED", False)
    monkeypatch.setattr(plume, "load_model",
                        lambda prefer_gpu=True, requested_size=None: ("FAKE", "CPU", "tiny"))
    # aucune boîte de dialogue bloquante pendant les tests
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: None)
    application = plume.PlumeApp(root)
    yield application
    application.on_close()


def _pump(app, seconds=3.0, until=None):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.root.update()
        if until is not None and until():
            return True
        time.sleep(0.05)
    return until is None


def _ready(app):
    """Modèle chargé ET message « ready » traité par le thread Tk."""
    return app.model is not None and not app._loading_model


def test_window_becomes_ready_with_fake_model(app):
    assert _pump(app, until=lambda: _ready(app))
    assert app.backend == "CPU" and app.model_size == "tiny"
    status = app.status_var.get()
    # Sans capture audio (Linux / paquet absent), le statut explique pourquoi ;
    # sinon il indique « Prêt ». Jamais de trace brute.
    assert status.startswith("Prêt — CPU") or status.startswith("Capture audio indisponible")
    assert "Error" not in status and "Traceback" not in status


def test_result_and_output_modes(app):
    assert _pump(app, until=lambda: _ready(app))
    app._handle("result", "Première phrase.")
    app._handle("result", "Deuxième.")
    assert app.text.get("1.0", "end-1c") == "Première phrase. Deuxième."
    assert app.text_revealed is True
    app._on_clear()
    assert app.text.get("1.0", "end-1c") == "" and app.text_revealed is False


def test_error_messages_reset_state(app):
    assert _pump(app, until=lambda: _ready(app))
    app.recording = True
    app._handle("live_abort", "Périphérique audio indisponible.")
    assert app.recording is False
    assert app.status_var.get().startswith("⚠ Périphérique")
    app._handle("error", "Erreur de transcription (X)")
    assert app._transcribing is False
    app._handle("progress", (65.0, 600.0))
    assert app.status_var.get() == "Transcription… 1:05 / 10:00"


def test_model_error_enables_retry(app):
    assert _pump(app, until=lambda: _ready(app))
    app.model = None
    app._loading_model = False
    app._handle("model_error", "Modèle introuvable et pas de connexion.")
    assert app._model_error and app.status_var.get().startswith("⚠ Modèle")
    assert app.mic_btn._enabled is True        # le bouton sert à réessayer
    app._on_mic()                               # relance le chargement (faux modèle)
    assert _pump(app, until=lambda: _ready(app))
    assert app._model_error is None


def test_callback_exception_is_logged_not_fatal(app, tmp_path):
    assert _pump(app, until=lambda: _ready(app))
    try:
        raise ValueError("exception simulée")
    except ValueError as e:
        app._report_callback_exception(type(e), e, e.__traceback__)
    assert "Erreur interne (ValueError)" in app.status_var.get()
    assert "exception simulée" in (tmp_path / "plume.log").read_text(encoding="utf-8")
