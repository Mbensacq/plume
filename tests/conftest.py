# -*- coding: utf-8 -*-
"""Configuration pytest : rend `plume` importable depuis la racine du dépôt et
fournit quelques utilitaires audio partagés (signaux synthétiques).

Importer `plume` ne charge PAS le modèle Whisper (chargement paresseux) et ne
touche pas au matériel audio : ces tests sont instantanés et tournent sans
micro, sans GPU et sans réseau.
"""
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Aucune tentative GPU pendant les tests, quel que soit l'environnement.
os.environ.setdefault("PLUME_DEVICE", "cpu")

import plume  # noqa: E402


@pytest.fixture
def sr():
    return plume.SAMPLE_RATE


def tone(seconds, amplitude=0.1, freq=220.0):
    """Signal « parole » synthétique : sinusoïde d'amplitude nette (RMS bien
    au-dessus du seuil de silence)."""
    n = int(seconds * plume.SAMPLE_RATE)
    t = np.arange(n, dtype=np.float32) / plume.SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * plume.SAMPLE_RATE), dtype=np.float32)


@pytest.fixture
def audio():
    """Fabrique de signaux : audio.tone(s), audio.silence(s)."""
    class _Audio:
        tone = staticmethod(tone)
        silence = staticmethod(silence)
    return _Audio
