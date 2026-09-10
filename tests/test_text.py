# -*- coding: utf-8 -*-
"""Post-traitement du texte, corrections personnalisées et filtrage des
hallucinations : les fonctions « pures » les plus faciles à casser par mégarde."""
import json
from types import SimpleNamespace

import pytest

import plume


# --- postprocess_text -------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("bonjour tout le monde", "Bonjour tout le monde."),
    ("Salut, comment vas-tu ?", "Salut, comment vas-tu ?"),          # espace FR conservée
    ("il a dit : oui ; puis non !", "Il a dit : oui ; puis non !"),
    ("ceci est un test , voilà .", "Ceci est un test, voilà."),      # espace parasite
    ("  plusieurs   espaces  ", "Plusieurs espaces."),
    ("c'est déjà fini.", "C'est déjà fini."),                        # pas de double point
    ("il a dit « bonjour »", "Il a dit « bonjour »"),                # guillemet fermant
    ("une phrase (entre parenthèses)", "Une phrase (entre parenthèses)"),
    ("et puis…", "Et puis…"),
    ("42 est la réponse", "42 est la réponse."),
    ("é", "É."),
])
def test_postprocess(raw, expected):
    assert plume.postprocess_text(raw) == expected


def test_postprocess_empty():
    assert plume.postprocess_text("") == ""
    assert plume.postprocess_text("   ") == ""
    assert plume.postprocess_text(None) == ""


def test_postprocess_disabled(monkeypatch):
    monkeypatch.setattr(plume, "AUTO_PUNCTUATION", False)
    assert plume.postprocess_text("brut sans point") == "brut sans point"


# --- _strip_ellipsis (aperçus en direct) ------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("... en cours de phrase", "en cours de phrase"),
    ("… en cours de phrase …", "en cours de phrase"),
    ("phrase coupée...", "phrase coupée"),
    ("rien à retirer.", "rien à retirer."),
    ("un point. pas deux", "un point. pas deux"),
    ("", ""),
])
def test_strip_ellipsis(raw, expected):
    assert plume._strip_ellipsis(raw) == expected


# --- corrections personnalisées --------------------------------------------
def test_apply_replacements_word_boundary_and_case():
    repl = {"discorde": "Discord"}
    assert plume.apply_replacements("J'adore Discorde le soir", repl) == "J'adore Discord le soir"
    assert plume.apply_replacements("DISCORDE !", repl) == "Discord !"
    # mot entier : ne touche pas une sous-chaîne
    assert plume.apply_replacements("discordement", repl) == "discordement"


def test_apply_replacements_multiword_and_regex_chars():
    repl = {"git eub": "GitHub", "c++": "C++"}
    assert plume.apply_replacements("sur git eub hier", repl) == "sur GitHub hier"
    # les métacaractères de la clé sont échappés
    assert plume.apply_replacements("du c++ moderne", repl) == "du C++ moderne"


def test_apply_replacements_empty():
    assert plume.apply_replacements("inchangé", {}) == "inchangé"
    assert plume.apply_replacements("", {"a": "b"}) == ""


def test_load_replacements_skips_underscore_and_non_strings(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"_aide": "commentaire", "discorde": "Discord",
                             "nombre": 3, "": "vide", " ": "blanc"}), encoding="utf-8")
    assert plume.load_replacements(str(p)) == {"discorde": "Discord"}


def test_load_replacements_accepts_bom(tmp_path):
    p = tmp_path / "r.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": "b"}).encode("utf-8"))
    assert plume.load_replacements(str(p)) == {"a": "b"}


def test_load_replacements_missing_or_invalid(tmp_path):
    assert plume.load_replacements(str(tmp_path / "absent.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text('{"a": "b",}', encoding="utf-8")          # virgule finale
    assert plume.load_replacements(str(bad)) == {}
    lst = tmp_path / "list.json"
    lst.write_text('["a", "b"]', encoding="utf-8")
    assert plume.load_replacements(str(lst)) == {}


def test_check_replacements_file(tmp_path):
    assert plume.check_replacements_file(str(tmp_path / "absent.json")) is None
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": "b"}', encoding="utf-8")
    assert plume.check_replacements_file(str(ok)) is None
    bad = tmp_path / "bad.json"
    bad.write_text('{\n  "a": "b",\n}', encoding="utf-8")
    msg = plume.check_replacements_file(str(bad))
    assert msg and "JSON invalide" in msg and "ligne " in msg   # n° selon la version de Python
    lst = tmp_path / "list.json"
    lst.write_text('[]', encoding="utf-8")
    assert "objet" in plume.check_replacements_file(str(lst))


def test_repository_replacements_file_is_valid():
    """Le fichier livré dans le dépôt doit rester valide et sans clé technique."""
    assert plume.check_replacements_file() is None
    repl = plume.load_replacements()
    assert all(not k.startswith("_") for k in repl)
    assert repl.get("discorde") == "Discord"


# --- filtrage des hallucinations -------------------------------------------
def _segs(*texts):
    return [SimpleNamespace(text=t) for t in texts]


def test_join_segments_filters_hallucinations():
    segs = _segs(" Bonjour à tous. ", "Sous-titrage ST' 501", " Amara.org ",
                 "Merci d'avoir regardé cette vidéo", "♪ musique ♪", "", "  ",
                 "La suite du texte.")
    assert plume._join_segments(segs) == "Bonjour à tous. La suite du texte."


def test_join_segments_keeps_normal_text():
    assert plume._join_segments(_segs("Un", "deux", "trois.")) == "Un deux trois."
    assert plume._join_segments([]) == ""


# --- pipeline complet avec un faux modèle ----------------------------------
class FakeModel:
    """Imite WhisperModel.transcribe : segments fixes, quelle que soit l'entrée."""

    def __init__(self, texts):
        self.texts = texts
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        segs = [SimpleNamespace(text=t, start=i * 10.0, end=(i + 1) * 10.0)
                for i, t in enumerate(self.texts)]
        return iter(segs), SimpleNamespace(language="fr", language_probability=1.0)


def test_transcribe_audio_pipeline(monkeypatch, audio):
    monkeypatch.setattr(plume, "load_replacements", lambda path=None: {"discorde": "Discord"})
    model = FakeModel(["bonjour sur discorde", "Sous-titrage Amara.org", "à bientôt"])
    progress = []
    out = plume.transcribe_audio(model, audio.tone(30),
                                 progress=lambda d, t: progress.append((d, t)))
    assert out == "Bonjour sur Discord à bientôt."
    assert model.calls[0]["language"] == "fr"
    assert model.calls[0]["beam_size"] == plume.BEAM_SIZE
    # progression : secondes traitées, bornées par la durée totale (30 s)
    assert progress == [(10.0, 30.0), (20.0, 30.0), (30.0, 30.0)]


def test_transcribe_stream_no_postprocess():
    model = FakeModel(["... en plein milieu", "de la phrase..."])
    out = plume.transcribe_stream(model, plume.np.zeros(16000, dtype="float32"),
                                  {"milieu": "MILIEU"}, prompt="contexte")
    # pas de majuscule/point ajoutés, « … » retirés, corrections appliquées
    assert out == "en plein MILIEU de la phrase"
    assert model.calls[0]["initial_prompt"] == "contexte"
