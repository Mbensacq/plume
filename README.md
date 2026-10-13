# Plume

Dictée vocale **100 % locale** pour Windows, écrite en Rust. Un raccourci
clavier (ou souris) lance l'écoute ; le texte, ponctué, est **collé là où se
trouve le curseur**, dans n'importe quelle application. La reconnaissance utilise
Whisper d'OpenAI (via [whisper.cpp](https://github.com/ggml-org/whisper.cpp)),
sur la carte graphique quand c'est possible. Rien ne quitte l'ordinateur.

- Transcription **pendant** que tu parles : à l'arrêt, il ne reste que la fin à traiter.
- Un seul `plume.exe`, sans installation ; le modèle se télécharge au premier lancement.
- Réglages dans un fichier texte commenté, `plume.toml`.

> Plume 1 (Python) est archivé sur la branche
> [`archive/plume-1-python`](https://github.com/Mbensacq/plume/tree/archive/plume-1-python).

## Installation

1. Récupère `plume-windows-x64.exe` :
   - dans **Releases** quand une version y est publiée ;
   - sinon dans l'onglet **Actions** du dépôt GitHub : dernière exécution
     réussie de « CI » sur `main`, section **Artifacts** (fichier zip à
     décompresser).

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
| `[transcription]` | Modèle, langue, carte graphique, précision, **vocabulaire** (noms propres et jargon à reconnaître, environ 300 caractères au plus) |
| `[sortie]` | `coller`, `taper` ou `presse-papiers` ; espace finale ; restauration du presse-papiers |
| `[interface]` | Indicateur affiché ou non, en haut ou en bas |
| `[audio]` | Micro à utiliser (vide = micro par défaut de Windows) |
| `[divers]` | Historique des dictées, mise à jour automatique |
| `[corrections]` | `"mot mal reconnu" = "correction"`, appliqué au texte final |

Modèles disponibles : `large-v3-turbo` (conseillé), `large-v3` (le plus précis,
3,1 Go), `medium`, `small`, `base`, `tiny`, ou le chemin d'un fichier `.bin`.

Vocabulaire ou correction ? Un mot **mal reconnu** (Whisper entend autre
chose) va dans `vocabulaire`. Un mot **bien reconnu mais mal écrit** (« git
hub » au lieu de « GitHub ») va dans `[corrections]`, qui ne coûte rien en
vitesse. Ne corrige jamais un vrai mot français : il serait remplacé partout.

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

## Différences avec Plume 1

| | Plume 1 (Python) | Plume (Rust) |
|---|---|---|
| Longues dictées | Tout est transcrit à l'arrêt : l'attente grandit avec la durée | Transcription **pendant** que tu parles, découpée aux pauses |
| Phrases en boucle, texte inventé | Contexte Whisper qui se propage, silences envoyés au modèle | Contexte maîtrisé, silences et bruits de clavier écartés, boucles supprimées |
| Installation | Python, environnement virtuel, ~1,5 Go de paquets, WSL | Un seul `plume.exe` |
| Interface | Fenêtre avec thèmes | Icône près de l'horloge + petit indicateur pendant l'écoute |
| Déclenchement | Appui pour démarrer, appui pour arrêter | **Appui court** = démarrer/arrêter, **appui long** = parler en maintenant |
| Sortie | Frappe caractère par caractère | **Collage** instantané (frappe toujours possible) |
| Réglages | Fenêtre | Fichier texte commenté `plume.toml` |
| Longues dictées interrompues | Un plantage perd tout | Le texte est écrit sur le disque morceau par morceau |

Le mode IA (Ollama), les thèmes, le mode « en direct » et le mélange de
plusieurs sources n'ont pas été repris.

## Développement

| Document | Contenu |
|---|---|
| [`docs/developpement.md`](docs/developpement.md) | Installer l'environnement (WSL Ubuntu), compiler, tester, obtenir un `plume.exe`, branches, publier une version |
| [`docs/architecture.md`](docs/architecture.md) | Comment Plume fonctionne, module par module, et où modifier quoi |

Démarrage rapide sous WSL Ubuntu (prérequis détaillés dans
[`docs/developpement.md`](docs/developpement.md)) :

```bash
git clone https://github.com/Mbensacq/plume.git ~/plume && cd ~/plume
cargo test --release
cargo run --release -- --transcrire tests/data/librivox-fr-12s.wav --modele tiny
```
