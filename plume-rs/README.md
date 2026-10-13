# Plume 2

Dictée vocale **100 % locale** pour Windows, réécrite en Rust. Un raccourci
clavier (ou souris) lance l'écoute ; le texte, ponctué, est **collé là où se
trouve le curseur**, dans n'importe quelle application. La reconnaissance utilise
Whisper d'OpenAI (via [whisper.cpp](https://github.com/ggml-org/whisper.cpp)),
sur la carte graphique quand c'est possible.

Plume 1 (Python, dossier racine du dépôt) reste disponible et n'est pas modifié.

## Ce qui change par rapport à Plume 1

| | Plume 1 | Plume 2 |
|---|---|---|
| Longues dictées | Tout est transcrit à l'arrêt : l'attente grandit avec la durée | Transcription **pendant** que tu parles, découpée aux pauses : à l'arrêt, il ne reste que le dernier morceau |
| Phrases en boucle, texte inventé | Contexte Whisper qui se propage, silences envoyés au modèle | Contexte maîtrisé, silences et bruits de clavier écartés, boucles supprimées |
| Installation | Python, environnement virtuel, ~1,5 Go de paquets, WSL | Un seul `plume.exe` ; le modèle se télécharge au premier lancement |
| Démarrage | Python + CUDA + modèle | Programme instantané ; modèle chargé en quelques secondes |
| Interface | Fenêtre avec thèmes | Icône près de l'horloge + petit indicateur pendant l'écoute |
| Déclenchement | Appui pour démarrer, appui pour arrêter | Les deux : **appui court** = démarrer/arrêter, **appui long** = parler en maintenant |
| Sortie | Frappe caractère par caractère | **Collage** instantané (frappe toujours possible) |
| Réglages | Fenêtre | Fichier texte commenté `plume.toml` |
| Sécurité des longues dictées | Un plantage perd tout | Le texte est écrit sur le disque morceau par morceau |

Le mode IA (Ollama), les thèmes, le mode « en direct » et le mélange de
plusieurs sources ont été retirés, d'après tes réponses au formulaire.

## Installation

1. Récupère `plume-windows-x64.exe` :
   - dans l'onglet **Actions** du dépôt GitHub, dernière exécution réussie de
     « Plume 2 (Rust) », section **Artifacts** ;
   - ou dans **Releases** quand une version y est publiée.
   `plume-windows-x64-cpu.exe` est la variante sans carte graphique.
2. Range-le dans un dossier à toi, par exemple `C:\Plume\`, et renomme-le
   `plume.exe` si tu veux.
3. Double-clic. Windows SmartScreen peut prévenir que l'éditeur est inconnu
   (le programme n'est pas signé) : « Informations complémentaires » puis
   « Exécuter quand même ».
4. Au premier lancement, Plume télécharge le modèle (874 Mo pour
   `large-v3-turbo`). L'avancement s'affiche à l'écran ; tu peux déjà dicter,
   le texte arrivera dès que le modèle sera prêt.

Pour lancer Plume avec Windows : `Win + R`, tape `shell:startup`, puis place
dans ce dossier un raccourci vers `plume.exe`.

Configuration requise : Windows 10 ou 11 (64 bits). Pour la carte graphique,
un pilote à jour (NVIDIA, AMD ou Intel, via Vulkan). Sans carte graphique
utilisable, Plume passe tout seul sur le processeur, en plus lent.

## Utilisation

| Action | Effet |
|---|---|
| Appui **court** sur le raccourci | Démarre l'écoute ; un nouvel appui l'arrête |
| Appui **long** (plus de 0,35 s) | Écoute tant que le raccourci est maintenu |
| **Échap** pendant l'écoute | Annule : rien n'est écrit |
| Clic sur l'icône | Menu : copier la dernière dictée, réglages, dossier, quitter |

Raccourci par défaut : **Ctrl + Alt + Espace**.

Couleur de l'icône : gris = chargement, violet = prêt, rouge = écoute,
orange = transcription, rouge foncé = problème (survole-la pour le détail).

L'indicateur en bas de l'écran affiche « Écoute 0:12 » puis
« Transcription… ». Il ne prend jamais le focus : le texte va bien dans ta
fenêtre.

## Réglages

Le fichier `plume.toml` est créé à côté de `plume.exe` au premier lancement,
avec une explication pour chaque ligne. Après modification : clic sur l'icône,
puis « Recharger les réglages ».

| Section | Ce qu'on y règle |
|---|---|
| `[raccourci]` | Touches (`"Ctrl+Alt+Espace"`, `"F13"`, `"Ctrl+Souris4"`…), mode (`auto`, `maintenir`, `basculer`), Échap |
| `[transcription]` | Modèle, langue, carte graphique, précision, **vocabulaire** (noms propres et jargon à reconnaître) |
| `[sortie]` | `coller`, `taper` ou `presse-papiers` ; espace finale ; restauration du presse-papiers |
| `[interface]` | Indicateur affiché ou non, en haut ou en bas |
| `[audio]` | Micro à utiliser (vide = micro par défaut de Windows) |
| `[divers]` | Historique des dictées, mise à jour automatique |
| `[corrections]` | `"mot mal reconnu" = "correction"` (comme `plume_replacements.json` de Plume 1) |

Modèles disponibles : `large-v3-turbo` (conseillé), `large-v3` (le plus précis,
3,1 Go), `medium`, `small`, `base`, `tiny`, ou le chemin d'un fichier `.bin`.

## Fichiers

Tous dans le dossier de `plume.exe` (ou `%LOCALAPPDATA%\Plume` si ce dossier
n'est pas modifiable).

| Fichier | Contenu |
|---|---|
| `plume.toml` | Réglages |
| `modeles\` | Modèles Whisper téléchargés |
| `historique.txt` | Toutes les dictées, horodatées (désactivable) |
| `derniere-dictee.txt` | La dictée en cours ou la dernière, écrite au fil de l'eau |
| `plume.log` | Journal : à joindre en cas de problème |

## Limites connues

- **Fenêtres lancées en administrateur** : Windows interdit à un programme
  normal d'y envoyer des touches. Le raccourci et le collage n'y fonctionnent
  que si Plume est lui aussi lancé en administrateur.
- **Jeux** : certains jeux refusent Ctrl+V dans leur chat ; passe alors
  `[sortie] mode = "taper"`. En plein écran exclusif, l'indicateur peut ne pas
  être visible (l'icône change quand même de couleur).
- **Presse-papiers** : après un collage, Plume remet le texte, l'image ou les
  fichiers qui s'y trouvaient. Les formats enrichis (texte mis en forme) ne
  sont restaurés qu'en texte simple. Les dictées apparaissent dans
  l'historique du presse-papiers de Windows (`Win + V`).
- **Téléphone** : pas encore. Le cœur (`src/` hors `app/`) ne dépend pas de
  Windows pour pouvoir servir plus tard à une version Android.

## Comprendre et modifier le code

```text
plume-rs/
├── plume.toml          Réglages par défaut (copiés au premier lancement)
├── src/
│   ├── main.rs         Point d'entrée : application de bureau ou ligne de commande
│   ├── cli.rs          plume --transcrire / --telecharger-modele / --version
│   ├── lib.rs          Le cœur, indépendant de Windows :
│   ├── config.rs         lecture de plume.toml (erreurs signalées, jamais bloquantes)
│   ├── hotkey.rs         analyse du raccourci écrit dans les réglages
│   ├── trigger.rs        appui court / appui long / Échap
│   ├── resample.rs       micro (48 kHz…) → 16 kHz pour Whisper
│   ├── segmenter.rs      découpage de la parole aux pauses, pendant l'écoute
│   ├── engine.rs         moteur Whisper (carte graphique, repli processeur)
│   ├── pipeline.rs       une dictée : son → morceaux → moteur → texte assemblé
│   ├── text.rs           nettoyage : hallucinations, boucles, corrections, ponctuation
│   ├── models.rs         catalogue et téléchargement des modèles
│   ├── update.rs         mise à jour depuis les Releases GitHub
│   ├── paths.rs, logging.rs, audio_file.rs
│   └── app/            La partie Windows :
│       ├── mod.rs        démarrage, instance unique, fils d'exécution
│       ├── controller.rs le chef d'orchestre : que faire de chaque événement
│       ├── hooks.rs      raccourci global (crochets clavier et souris)
│       ├── audio.rs      capture du micro
│       ├── output.rs     collage / frappe / presse-papiers
│       └── ui.rs         icône près de l'horloge, indicateur, menu
└── tests/e2e.rs        Test de bout en bout sur un extrait LibriVox en français
```

Parcours d'une dictée :

```text
raccourci ─► hooks ─► controller ─► audio (micro) ─► pipeline.feed()
                                                       │ resample → segmenter
                                                       ▼
                                   moteur (fil dédié) ◄─ morceau terminé à chaque pause
                                                       │
raccourci (arrêt) ─► dernier morceau ─► texte assemblé ─► text::finalize ─► output (collage)
```

## Compiler soi-même

Prérequis Windows : [Rust](https://rustup.rs), Visual Studio Build Tools
(C++), CMake, LLVM (pour bindgen) et, pour la carte graphique, le
[Vulkan SDK](https://vulkan.lunarg.com/sdk/home).

```powershell
cd plume-rs
cargo build --release --features vulkan     # carte graphique (Vulkan)
cargo build --release                       # processeur seulement
cargo test --release                        # tests
```

Transcrire un fichier avec exactement le même traitement qu'une dictée, et
mesurer le délai après l'arrêt :

```powershell
.\target\release\plume.exe --transcrire dictee.wav --temps-reel
```

Test de bout en bout avec un vrai modèle :

```powershell
$env:PLUME_TEST_MODELE = "C:\Plume\modeles\ggml-tiny.bin"
cargo test --release --test e2e
```

Publier une version (déclenche la mise à jour automatique chez tous les
utilisateurs) : mettre à jour `version` dans `Cargo.toml`, puis pousser un tag
`v2.0.1`. La CI compile, teste et crée la Release.
