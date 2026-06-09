# -*- coding: utf-8 -*-
"""
Tests unitaires légers des fonctions « pures » de Plume (sans GPU, sans audio,
sans interface). Couvre le post-traitement de ponctuation et les corrections
personnalisées — les parties les plus faciles à casser par mégarde.

Lancement :
    .venv\\Scripts\\python test_plume.py      # exécuteur intégré
    .venv\\Scripts\\python -m pytest test_plume.py   # si pytest est installé

NB : importer `plume` ne charge PAS le modèle Whisper (chargement paresseux) ;
ces tests sont donc instantanés.
"""
import plume


# --- postprocess_text -------------------------------------------------------
def test_postprocess_basic():
    assert plume.postprocess_text("bonjour tout le monde") == "Bonjour tout le monde."


def test_postprocess_preserve_french_spacing():
    # En français, l'espace avant « ? ! ; : » produit par Whisper est conservé.
    assert plume.postprocess_text("Salut, comment vas-tu ?") == "Salut, comment vas-tu ?"
    assert plume.postprocess_text("il a dit : oui ; puis non !") == "Il a dit : oui ; puis non !"


def test_postprocess_strip_space_before_comma_period():
    assert plume.postprocess_text("ceci est un test , voilà .") == "Ceci est un test, voilà."


def test_postprocess_collapse_spaces():
    assert plume.postprocess_text("  plusieurs   espaces  ") == "Plusieurs espaces."


def test_postprocess_no_double_final_point():
    assert plume.postprocess_text("c'est déjà fini.") == "C'est déjà fini."


def test_postprocess_empty():
    assert plume.postprocess_text("") == ""
    assert plume.postprocess_text("   ") == ""


def test_postprocess_disabled(monkeypatch=None):
    old = plume.AUTO_PUNCTUATION
    try:
        plume.AUTO_PUNCTUATION = False
        assert plume.postprocess_text("brut sans point") == "brut sans point"
    finally:
        plume.AUTO_PUNCTUATION = old


# --- corrections personnalisées --------------------------------------------
def test_apply_replacements_word_boundary_and_case():
    repl = {"discorde": "Discord"}
    assert plume.apply_replacements("J'adore Discorde le soir", repl) == "J'adore Discord le soir"
    # mot entier : ne touche pas une sous-chaîne
    assert plume.apply_replacements("discordement", repl) == "discordement"


def test_load_replacements_skips_underscore_keys():
    repl = plume.load_replacements()   # lit plume_replacements.json
    assert all(not k.startswith("_") for k in repl), "les clés _ doivent être ignorées"


# --- exécuteur intégré (sans dépendance pytest) -----------------------------
def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__} : {e}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {t.__name__} : {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests OK")
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _run() else 1)
