# -*- coding: utf-8 -*-
"""Découpage de la fenêtre de transcription en direct : quand figer, quoi
afficher, quel contexte redonner au modèle. Aucun modèle n'est chargé : la
fonction de transcription est un faux."""
import numpy as np

import plume
from plume import LiveSession, live_should_commit


def _concat(*parts):
    return np.concatenate(parts).astype(np.float32)


# --- live_should_commit -----------------------------------------------------
def test_no_commit_below_min_duration(audio):
    tail = _concat(audio.tone(2.0), audio.silence(3.0))     # 5 s < LIVE_MIN_COMMIT (6 s)
    assert live_should_commit(tail, tail.size / plume.SAMPLE_RATE) is False


def test_commit_on_real_pause(audio):
    tail = _concat(audio.tone(6.0), audio.silence(plume.LIVE_SILENCE_SEC + 0.5))
    assert live_should_commit(tail, tail.size / plume.SAMPLE_RATE) is True


def test_no_commit_on_short_hesitation(audio):
    # 0,5 s de blanc en fin de queue : ce n'est pas une vraie pause
    tail = _concat(audio.tone(7.0), audio.silence(0.5))
    assert live_should_commit(tail, tail.size / plume.SAMPLE_RATE) is False


def test_commit_when_window_too_long(audio):
    tail = audio.tone(plume.LIVE_MAX_TAIL + 1.0)             # parole continue, sans pause
    assert live_should_commit(tail, tail.size / plume.SAMPLE_RATE) is True


def test_final_always_commits(audio):
    tail = audio.tone(0.5)
    assert live_should_commit(tail, 0.5, final=True) is True


# --- LiveSession ------------------------------------------------------------
class Recorder:
    """Faux transcripteur : mémorise les appels, renvoie des textes préparés."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def __call__(self, tail, prompt):
        self.calls.append((tail.size, prompt))
        return self.texts.pop(0) if self.texts else ""


def test_step_skips_when_not_enough_new_audio(audio):
    s = LiveSession()
    tx = Recorder("jamais appelé")
    assert s.step(audio.tone(0.2), 3200, tx) is None
    assert tx.calls == []


def test_step_skips_transcription_on_silence(audio):
    s = LiveSession()
    tx = Recorder("jamais appelé")
    display, final = s.step(audio.silence(3.0), 48000, tx)
    assert display == "" and final is None
    assert tx.calls == []


def test_preview_is_revisable_until_commit(audio):
    s = LiveSession()
    tx = Recorder("bonjour à", "bonjour à tous")
    d1, _ = s.step(audio.tone(1.0), 16000, tx)
    d2, _ = s.step(audio.tone(2.0), 32000, tx)
    assert (d1, d2) == ("bonjour à", "bonjour à tous")
    assert s.committed == "" and s.commit_sample == 0
    # sans texte figé, le contexte est celui par défaut (None -> INITIAL_PROMPT)
    assert tx.calls[0][1] is None


def test_commit_on_pause_then_context_is_given(audio):
    s = LiveSession()
    tail = _concat(audio.tone(6.0), audio.silence(plume.LIVE_SILENCE_SEC + 0.5))
    tx = Recorder("première phrase", "suite")
    d1, _ = s.step(tail, tail.size, tx)
    assert d1 == "première phrase"
    assert s.committed == "première phrase"
    assert s.commit_sample == tail.size
    # passe suivante : la queue repart de zéro, le texte figé sert de contexte
    d2, _ = s.step(audio.tone(1.0), tail.size + 16000, tx)
    assert d2 == "première phrase suite"
    assert tx.calls[1][1].endswith("première phrase")
    assert tx.calls[1][1].startswith(plume.INITIAL_PROMPT)


def test_commit_sample_never_goes_backwards(audio):
    s = LiveSession()
    s.commit_sample = 100_000
    tail = audio.tone(plume.LIVE_MAX_TAIL + 1)
    s.step(tail, 50_000, Recorder("x"))      # total incohérent (plus petit)
    assert s.commit_sample == 100_000


def test_final_pass_applies_postprocess_and_prefix(audio):
    s = LiveSession(prefix="Texte déjà là.")
    tx = Recorder("nouvelle dictée", "et la fin")
    tail = _concat(audio.tone(6.0), audio.silence(plume.LIVE_SILENCE_SEC + 0.5))
    s.step(tail, tail.size, tx)                            # fige « nouvelle dictée »
    display, clean = s.step(audio.tone(1.0), tail.size + 16000, tx, final=True)
    assert clean == "Nouvelle dictée et la fin."
    assert display == "Texte déjà là. Nouvelle dictée et la fin."


def test_final_pass_on_empty_tail(audio):
    s = LiveSession()
    display, clean = s.step(audio.silence(0), 0, Recorder(), final=True)
    assert (display, clean) == ("", "")


def test_prompt_uses_only_recent_committed_text(audio):
    s = LiveSession()
    s.committed = "mot " * 200                              # texte figé très long
    tx = Recorder("suite")
    s.step(audio.tone(1.0), 16000, tx)
    prompt = tx.calls[0][1]
    assert len(prompt) <= len(plume.INITIAL_PROMPT) + 1 + 200
