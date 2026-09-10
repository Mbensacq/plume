# -*- coding: utf-8 -*-
"""Messages d'erreur destinés à l'utilisateur (modèle absent, hors ligne,
disque plein…) et petites fonctions utilitaires (raccourci, thèmes, modèle)."""
import errno

import pytest

import plume


class LocalEntryNotFoundError(Exception):
    """Même nom que l'exception de huggingface_hub (modèle absent hors ligne)."""


def test_model_error_offline():
    msg = plume.describe_model_error(LocalEntryNotFoundError("Cannot find the requested files"),
                                     "large-v3", None)
    assert "large-v3" in msg and "connexion" in msg and "3.1 Go" in msg


def test_model_error_offline_from_cause_chain():
    inner = ConnectionError("Failed to resolve 'huggingface.co'")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert "connexion" in plume.describe_model_error(outer, "small", "D:/models")


def test_model_error_disk_full():
    e = OSError(errno.ENOSPC, "No space left on device")
    msg = plume.describe_model_error(e, "medium", "C:/plume/models")
    assert "Disque plein" in msg and "C:/plume/models" in msg


def test_model_error_unknown_model():
    e = ValueError("Invalid model size 'huge', expected one of: tiny, base")
    msg = plume.describe_model_error(e, "huge")
    assert "inconnu" in msg and "large-v3-turbo" in msg


def test_model_error_missing_dependency():
    msg = plume.describe_model_error(ImportError("No module named 'faster_whisper'"), "small")
    assert "requirements.txt" in msg


def test_model_error_memory():
    assert "plus petit" in plume.describe_model_error(MemoryError(), "large-v3")
    oom = RuntimeError("CUDA out of memory")
    assert "plus petit" in plume.describe_model_error(oom, "large-v3")


def test_model_error_generic_is_short_and_readable():
    e = RuntimeError("something odd happened\nwith a second line " + "x" * 500)
    msg = plume.describe_model_error(e, "small")
    assert msg.startswith("Échec du chargement du modèle « small »")
    assert "second line" not in msg and len(msg) < 260


def test_model_error_passthrough():
    assert plume.describe_model_error(plume.ModelError("déjà clair"), "x") == "déjà clair"


def test_enumeration_error_message(monkeypatch):
    monkeypatch.setattr(plume, "CAPTURE_AVAILABLE", False)
    assert "Windows" in plume.describe_enumeration_error(RuntimeError())
    monkeypatch.setattr(plume, "CAPTURE_AVAILABLE", True)
    msg = plume.describe_enumeration_error(OSError("boom"))
    assert "OSError" in msg and "Actualiser" in msg


# --- choix du modèle --------------------------------------------------------
def test_resolve_model_size(monkeypatch):
    monkeypatch.setattr(plume, "MODEL_SIZE", None)
    assert plume.resolve_model_size(None, True) == plume.DEFAULT_GPU_MODEL
    assert plume.resolve_model_size(None, False) == plume.DEFAULT_CPU_MODEL
    assert plume.resolve_model_size(" medium ", False) == "medium"
    monkeypatch.setattr(plume, "MODEL_SIZE", "tiny")
    assert plume.resolve_model_size("medium", True) == "tiny"     # $PLUME_MODEL prime


def test_load_model_reports_clear_error(monkeypatch):
    def boom(device, compute_type, model_size):
        raise LocalEntryNotFoundError("offline")
    monkeypatch.setattr(plume, "_try_load", boom)
    monkeypatch.setattr(plume, "MODEL_SIZE", None)
    with pytest.raises(plume.ModelError) as ei:
        plume.load_model(prefer_gpu=False)
    assert "connexion" in str(ei.value)


def test_load_model_falls_back_to_cpu(monkeypatch):
    calls = []

    def fake(device, compute_type, model_size):
        calls.append((device, model_size))
        if device == "cuda":
            raise RuntimeError("CUDA driver version is insufficient")
        return "MODEL"
    monkeypatch.setattr(plume, "_try_load", fake)
    monkeypatch.setattr(plume, "FORCE_CPU", False)
    monkeypatch.setattr(plume, "MODEL_SIZE", None)
    model, backend, size = plume.load_model(prefer_gpu=True)
    assert model == "MODEL" and backend.startswith("CPU") and size == plume.DEFAULT_CPU_MODEL
    assert calls == [("cuda", plume.DEFAULT_GPU_MODEL), ("cpu", plume.DEFAULT_CPU_MODEL)]


# --- utilitaires ------------------------------------------------------------
def test_hotkey_label():
    assert plume._hotkey_label(0x0002 | 0x0001, 0x44) == "Ctrl + Alt + D"
    assert plume._hotkey_label(0x0004 | 0x0008, 0x70) == "Maj + Win + F1"
    assert plume._hotkey_label(0, 0x20) == "Espace"
    assert plume._hotkey_label(0, 0x65) == "Num5"
    assert plume._hotkey_label(0, 0xFF) == "Touche 255"


def test_theme_derivation():
    t = plume._derive_theme("#4B3BE8", "dark")
    assert set(t) >= {"bg", "surface", "text", "accent", "accent_hi", "on_accent", "border"}
    assert t["accent"] == "#4B3BE8" and t["dark_titlebar"] is True
    light = plume._derive_theme("#0F9B87", "light")
    assert light["dark_titlebar"] is False
    assert plume._mix("#000000", "#ffffff", 0.5) == "#808080"
    assert 0.0 <= plume._lum("#808080") <= 1.0


def test_ai_reformulate_unreachable_gives_clear_error():
    with pytest.raises(plume.AIError) as ei:
        plume.ai_reformulate("texte", "llama3.1:8b", url="http://127.0.0.1:9", timeout=1)
    assert "Ollama" in str(ei.value)


def test_log_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(plume, "LOG_PATH", str(tmp_path / "plume.log"))
    plume.log("ligne de test")
    assert "ligne de test" in (tmp_path / "plume.log").read_text(encoding="utf-8")
    monkeypatch.setattr(plume, "LOG_PATH", str(tmp_path / "nope" / "x" / "plume.log"))
    plume.log("dossier absent : ne doit pas lever")
