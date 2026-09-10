# -*- coding: utf-8 -*-
"""Mixage, instantanés bornés et détection de panne du Recorder — sans ouvrir
le moindre périphérique : les buffers sont injectés directement."""
import numpy as np

import plume
from plume import Recorder


def _chunks(values):
    return [np.asarray(v, dtype=np.float32) for v in values]


# --- rms / mixage -----------------------------------------------------------
def test_rms():
    assert plume.rms(None) == 0.0
    assert plume.rms(np.zeros(0, dtype=np.float32)) == 0.0
    assert plume.rms(np.zeros(100, dtype=np.float32)) == 0.0
    assert abs(plume.rms(np.full(100, 0.5, dtype=np.float32)) - 0.5) < 1e-6


def test_mix_empty_and_single():
    assert Recorder._mix({}).size == 0
    assert Recorder._mix({0: []}).size == 0
    single = Recorder._mix({0: _chunks([[0.1, 0.2], [0.3]])})
    np.testing.assert_allclose(single, [0.1, 0.2, 0.3])


def test_mix_aligns_pads_and_normalizes():
    mixed = Recorder._mix({0: _chunks([[0.6, 0.6, 0.6]]), 1: _chunks([[0.6]])})
    # somme alignée au début, zéros ensuite, puis normalisation du pic (1.2 -> 1.0)
    np.testing.assert_allclose(mixed, [1.0, 0.5, 0.5])


def test_mix_no_normalization_when_below_one():
    mixed = Recorder._mix({0: _chunks([[0.2, 0.2]]), 1: _chunks([[0.1]])})
    np.testing.assert_allclose(mixed, [0.3, 0.2], rtol=1e-6)


# --- queue bornée (snapshot) ------------------------------------------------
def test_tail_of_concatenates_only_needed_chunks():
    chunks = _chunks([[1, 2, 3], [4, 5], [6, 7, 8, 9]])
    np.testing.assert_array_equal(Recorder._tail_of(chunks, 9, 0), [1, 2, 3, 4, 5, 6, 7, 8, 9])
    np.testing.assert_array_equal(Recorder._tail_of(chunks, 9, 4), [5, 6, 7, 8, 9])
    np.testing.assert_array_equal(Recorder._tail_of(chunks, 9, 8), [9])
    assert Recorder._tail_of(chunks, 9, 9).size == 0
    assert Recorder._tail_of(chunks, 9, 50).size == 0      # since au-delà du capturé


def test_snapshot_since_matches_full_mix_suffix():
    r = Recorder()
    r.set_sources([{"id": "a", "name": "A", "loopback": False},
                   {"id": "b", "name": "B", "loopback": True}])
    a = _chunks([[0.1] * 5, [0.2] * 5, [0.3] * 5])
    b = _chunks([[0.4] * 4, [0.5] * 4])
    r._buffers = {0: a, 1: b}
    r._counts = {0: 15, 1: 8}
    full = Recorder._mix({0: a, 1: b})
    tail, total = r.snapshot(since=6)
    assert total == 15
    np.testing.assert_allclose(tail, full[6:])
    tail0, _ = r.snapshot(since=0)
    np.testing.assert_allclose(tail0, full)


def test_snapshot_when_nothing_captured():
    r = Recorder()
    tail, total = r.snapshot()
    assert tail.size == 0 and total == 0


# --- pannes de capture ------------------------------------------------------
def test_all_failed():
    r = Recorder()
    assert r.all_failed() is False                    # aucune source
    r.set_sources([{"id": "a"}, {"id": "b"}])
    r._failed = {0}
    assert r.all_failed() is False
    r._failed = {0, 1}
    assert r.all_failed() is True


def test_start_without_sources_raises():
    r = Recorder()
    try:
        r.start()
    except RuntimeError as e:
        assert "source" in str(e)
    else:
        raise AssertionError("start() doit refuser sans source")


def test_capture_failure_is_recorded(monkeypatch):
    """Un périphérique qui échoue à l'ouverture : erreur mémorisée, source
    marquée en panne, pas de trace non gérée dans le thread."""
    import sys
    import types
    fake_sc = types.ModuleType("soundcard")

    def get_microphone(dev_id, include_loopback=False):
        raise RuntimeError("Error 0x88890004: device invalidated")
    fake_sc.get_microphone = get_microphone
    monkeypatch.setitem(sys.modules, "soundcard", fake_sc)

    r = Recorder()
    r.set_sources([{"id": "x", "name": "Micro test", "loopback": False}])
    r.start()
    for t in r._threads:
        t.join(timeout=5)
    assert r.all_failed() is True
    errs = r.consume_errors()
    assert len(errs) == 1 and errs[0].startswith("Micro test")
    assert r.consume_errors() == []
    audio = r.stop()
    assert audio.size == 0


def test_describe_capture_error_messages():
    mic = plume.describe_capture_error(
        ["Micro — RuntimeError: Error 0x88890004: device invalidated"])
    assert "micro débranché" in mic and "son du PC" not in mic
    assert "son du PC" in plume.describe_capture_error(
        ["Casque — RuntimeError: loopback render endpoint failed"])
    assert "refusé" in plume.describe_capture_error(
        ["Micro — OSError: access denied 0x80070005"])
    generic = plume.describe_capture_error(["Micro — ValueError: bizarre"])
    assert "Micro" in generic and "bizarre" in generic
    assert plume.describe_capture_error([]) == "Capture impossible."
