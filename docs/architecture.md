# Architecture de Plume

Comment Plume transforme la voix en texte, module par module, et où modifier
quoi. Pour installer l'environnement et lancer les commandes, voir
[`developpement.md`](developpement.md).

## Vue d'ensemble

Plume est un seul programme Rust en deux couches :

| Couche | Dossier | Dépend de Windows ? | Rôle |
|---|---|---|---|
| **Cœur** (`plume_core`) | `src/` hors `app/` | Non | Réglages, découpage de la parole, moteur Whisper, nettoyage du texte, modèles, mises à jour |
| **Application** | `src/app/` | Oui | Raccourci global, micro, collage, icône et indicateur |

Le cœur se compile et se teste partout (Linux, WSL, Windows ; plus tard
Android). Le point d'entrée `src/main.rs` lance l'application de bureau sans
argument (Windows seulement), ou la ligne de commande (`src/cli.rs`) avec
`--transcrire`, `--telecharger-modele`, `--version`.

```text
├── Cargo.toml           Dépendances et variantes (moteur, vulkan, cuda)
├── build.rs             Icône et description intégrées à plume.exe
├── plume.toml           Réglages par défaut, copiés au premier lancement
├── assets/plume.ico
├── src/
│   ├── main.rs          Application de bureau ou ligne de commande
│   ├── cli.rs           --transcrire / --telecharger-modele / --version
│   ├── lib.rs           Le cœur :
│   ├── config.rs          lecture de plume.toml (erreurs signalées, jamais bloquantes)
│   ├── hotkey.rs          analyse du raccourci (« Ctrl+Alt+Espace », « F13 »…)
│   ├── trigger.rs         appui court / appui long / Échap
│   ├── resample.rs        micro (48 kHz…) → 16 kHz pour Whisper
│   ├── segmenter.rs       découpage de la parole aux pauses, pendant l'écoute
│   ├── engine.rs          moteur Whisper (carte graphique, repli processeur)
│   ├── pipeline.rs        une dictée : son → morceaux → moteur → texte assemblé
│   ├── text.rs            amorce Whisper et nettoyage du texte
│   ├── models.rs          catalogue et téléchargement des modèles
│   ├── update.rs          mise à jour depuis les Releases GitHub
│   ├── paths.rs           emplacement des fichiers (portable ou %LOCALAPPDATA%)
│   ├── logging.rs         journal plume.log
│   ├── audio_file.rs      lecture/écriture WAV (tests et ligne de commande)
│   └── app/             L'application Windows :
│       ├── mod.rs         démarrage, instance unique, fils d'exécution
│       ├── controller.rs  le chef d'orchestre : que faire de chaque événement
│       ├── hooks.rs       raccourci global (crochets clavier et souris)
│       ├── audio.rs       capture du micro (WASAPI)
│       ├── output.rs      collage / frappe / presse-papiers
│       ├── startup.rs     lancement à l'ouverture de session (clé « Run »)
│       ├── ui.rs          icône près de l'horloge, indicateur, menu
│       └── win.rs         petits utilitaires Win32
├── tests/e2e.rs         Test de bout en bout sur un extrait LibriVox en français
└── .github/workflows/ci.yml   Tests Linux, plume.exe Windows, Releases
```

## Parcours d'une dictée

```text
raccourci ─► hooks ─► controller ─► audio (micro) ─► Dictation::feed()
                                                       │ resample → segmenter
                                                       ▼
                                   moteur (fil dédié) ◄─ morceau terminé à chaque pause
                                                       │
raccourci (arrêt) ─► dernier morceau ─► texte assemblé ─► text::finalize ─► output (collage)
```

1. **Raccourci** (`app/hooks.rs`) : les crochets « bas niveau » voient chaque
   touche et chaque clic. Quand la combinaison est reconnue, la touche
   principale est **avalée** (l'application active ne la reçoit pas) et un
   événement part vers le contrôleur. Le traitement doit être instantané :
   Windows retire un crochet trop lent.
2. **Décision** (`trigger.rs`, `app/controller.rs`) : appui court, appui
   long, relâchement, Échap. `trigger.rs` est une logique pure, le temps lui
   est passé en paramètre, ce qui la rend testable.
3. **Micro** (`app/audio.rs`) : le son arrive par petits blocs, passe en mono,
   et repart aussitôt (le fil audio de Windows doit rester léger).
4. **Rééchantillonnage** (`resample.rs`) : 48 kHz (ou autre) → 16 kHz, avec
   un filtre à sinus cardinal fenêtré, en flux.
5. **Découpage** (`segmenter.rs`) : la parole est coupée aux pauses
   **pendant** l'écoute. Chaque morceau terminé part au moteur.
6. **Moteur** (`engine.rs`, dans son propre fil) : transcrit les morceaux
   dans l'ordre, pendant que tu parles encore.
7. **Arrêt** : il ne reste que le dernier morceau à transcrire. Le
   collecteur (`pipeline.rs`) assemble les textes, `text::finalize` les
   nettoie, `app/output.rs` colle le résultat.

À chaque morceau reçu, le texte en cours est écrit dans
`derniere-dictee.txt` : rien n'est perdu si Plume s'arrête en pleine dictée.

## Fils d'exécution (application Windows)

| Fil | Rôle |
|---|---|
| Principal | Boucle de messages Windows : icône, indicateur, menu |
| « raccourci » | Crochets clavier et souris |
| « controleur » | Reçoit tous les événements et décide |
| « moteur » | Transcription, un morceau après l'autre |
| Collecteur (un par dictée) | Rassemble les textes d'une dictée |
| « fin-dictee » (un par dictée) | Assemblage final puis collage, un seul collage à la fois |
| « mise-a-jour » | Vérification au démarrage, si activée |

Les fils communiquent par canaux (`std::sync::mpsc`). Le contrôleur ne bloque
jamais : une dictée peut commencer pendant que la précédente se termine.

## Découpage de la parole (`segmenter.rs`)

Le son à 16 kHz est analysé par trames de 30 ms. Une trame est de la parole
si elle dépasse le **bruit de fond**, estimé en continu, de 6 dB.

| Réglage (`SegmenterConfig`) | Valeur | Effet |
|---|---|---|
| `min_chunk_ms` | 6 000 | Un morceau d'au moins 6 s est coupé à la première pause… |
| `pause_ms` | 600 | … d'au moins 0,6 s |
| `long_silence_ms` | 3 000 | Un silence de 3 s coupe même un morceau court (une hésitation, plus courte, ne coupe pas la phrase) |
| `max_chunk_ms` | 25 000 | Sans pause pendant 25 s : coupe à l'endroit le plus calme des dernières secondes |
| `max_inner_silence_ms` | 400 | Les silences internes plus longs sont ramenés à 0,4 s : Whisper invente du texte sur les silences et prend une longue pause pour une fin de phrase |
| `keep_pad_ms` | 400 | Silence gardé autour de la parole |
| `min_speech_ms` | 200 | En dessous, le morceau est ignoré (clic, souffle) |
| `speech_margin_db` | 6 | Marge au-dessus du bruit de fond |

Ces valeurs se modifient dans `impl Default for SegmenterConfig`. Pour mesurer
l'effet d'un changement : `plume --transcrire fichier.wav --temps-reel` affiche
le nombre de morceaux et le délai après l'arrêt.

## Moteur Whisper (`engine.rs`)

- **whisper.cpp** via la crate `whisper-rs`. Variantes de compilation :
  `moteur` (processeur), `vulkan` (toute carte graphique récente), `cuda`
  (NVIDIA, non distribuée).
- Au chargement : essai sur la carte graphique, **repli automatique sur le
  processeur** en cas d'échec.
- `precision` (1 à 8) = largeur du faisceau de recherche (1 = glouton, le plus
  rapide).
- Température 0, avec relance à température plus haute si le résultat est
  douteux ; segments jugés sans parole écartés ; génériques de sous-titres
  inventés sur le silence filtrés (`text::is_hallucination`).
- Le contexte interne de Whisper est désactivé : c'est Plume qui lui donne
  l'**amorce** (ci-dessous). Cela évite que des erreurs se propagent d'un
  morceau à l'autre.

### L'amorce (`text::build_prompt`)

Avant chaque morceau, Whisper reçoit un texte d'amorce, dans cet ordre :

1. `Vocabulaire : …` : les mots de `[transcription] vocabulaire`, dans la
   limite de 300 caractères (les premiers de la liste sont prioritaires) ;
2. une phrase de style, longue et ponctuée, que Whisper imite : des virgules
   aux respirations, un point seulement en fin de phrase ;
3. la fin du texte déjà dicté, **sans son point final**, pour que la suite
   enchaîne naturellement : Whisper décide lui-même si le morceau continue la
   phrase (il commence alors en minuscule) ou en commence une nouvelle.

L'ensemble est limité à 600 caractères, car whisper.cpp ne garde que les
223 derniers jetons de l'amorce et couperait **le début**, donc le
vocabulaire. Les réglages signalent les mots qui dépassent.

## Nettoyage du texte (`text.rs`)

| Étape | Fonction | Ce qu'elle fait |
|---|---|---|
| Par morceau | `clean_chunk` | Espaces, « … » de début et de fin (morceau coupé en pleine phrase), répétitions en boucle |
| Assemblage | `join_chunk` | Raccorde les morceaux. Whisper finit chaque morceau par un point, même coupé sur une hésitation : si le morceau suivant commence en minuscule, la phrase continuait et ce point est retiré |
| Final | `finalize` | Corrections, répétitions, espaces avant `,` `.`, majuscule initiale, point final |

Les **corrections** (`[corrections]` de `plume.toml`) portent sur des mots
entiers, sans tenir compte des majuscules, les expressions les plus longues
d'abord.

## Sortie du texte (`app/output.rs`)

- **coller** : Plume attend que Ctrl/Alt/Maj/Win soient relâchés (sinon Ctrl+V
  deviendrait Ctrl+Alt+V ; au-delà de 1,5 s, il les relâche virtuellement),
  met le texte dans le presse-papiers, simule Ctrl+V, puis remet l'ancien
  contenu 600 ms plus tard.
- **taper** : caractère par caractère, pour les applications qui refusent le
  collage.
- **presse-papiers** : copie seulement.

Les touches simulées portent une marque (`PLUME_MARK`) pour que les crochets
de Plume les ignorent.

## Réglages (`config.rs`)

`plume.toml` est lu avec `serde` et `toml`. Une clé inconnue ou une valeur
invalide ne bloque jamais le démarrage : la valeur par défaut est utilisée et
un avertissement en français s'affiche. Le modèle commenté `plume.toml` est
intégré à l'exécutable (`include_str!`) et écrit au premier lancement.

## Mise à jour automatique (`update.rs`)

Au démarrage, si `mise_a_jour_auto = true`, Plume interroge la dernière
Release GitHub de `Mbensacq/plume`. Si elle est plus récente et contient
l'exécutable de la même variante (`plume-windows-x64.exe` ou `-cpu.exe`), il
la télécharge et la met à la place de l'exécutable actuel. Windows interdit de
remplacer un programme en cours, mais pas de le renommer : l'ancien est
renommé puis supprimé au lancement suivant. La nouvelle version est active au
lancement d'après. En cas d'échec, l'ancienne version est remise en place.

## Où modifier quoi

| Je veux… | Fichier |
|---|---|
| Ajouter ou changer un réglage | `src/config.rs` + `plume.toml` |
| Accepter une nouvelle touche ou un nouveau bouton dans le raccourci | `src/hotkey.rs` (noms) et `src/app/hooks.rs` (détection) |
| Changer le comportement appui court / appui long | `src/trigger.rs` |
| Couper la parole plus tôt ou plus tard | `src/segmenter.rs` (`SegmenterConfig`) |
| Filtrer une nouvelle hallucination de Whisper | `src/text.rs` (`hallucination_re`) |
| Changer la ponctuation ou l'assemblage final | `src/text.rs` (`finalize`, `join_chunk`) |
| Changer les paramètres de Whisper | `src/engine.rs` (`transcribe`) |
| Ajouter un modèle au catalogue | `src/models.rs` |
| Changer l'icône, l'indicateur ou le menu | `src/app/ui.rs` |
| Changer la façon d'écrire le texte | `src/app/output.rs` |
| Changer le lancement à l'ouverture de session | `src/app/startup.rs` |
| Changer la compilation Windows ou les Releases | `.github/workflows/ci.yml` |
