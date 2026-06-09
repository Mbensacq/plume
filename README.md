# Plume — reconnaissance vocale locale (français)

**Plume** est une application de dictée vocale **100 % locale et hors ligne** (après
installation). Vous parlez au micro **— ou vous transcrivez le son joué par le PC, ou
un mélange des deux —** et le texte français apparaît dans une fenêtre, copiable en un
clic (pratique pour Discord, etc.).

- **Transcription** : [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper via CTranslate2)
- **Capture audio** : [soundcard](https://github.com/bastibe/SoundCard) — micros **et** sorties (loopback WASAPI)
- **Interface** : `tkinter` (standard), 3 thèmes, rendu DPI-aware, sélecteur de périphériques
- **Modèle par défaut** : `large-v3`, GPU NVIDIA (CUDA/float16) avec **repli CPU (int8) automatique**

---

## 1. Prérequis

- **Windows 10/11** (la capture audio utilise WASAPI, donc Windows uniquement).
- **Python 3.10 ou plus** (développé et testé sur **3.13.13**). Cochez « Add Python to PATH » à l'installation.
- Au moins un **microphone** et/ou une **sortie audio** (pour la capture du son du PC).
- **Pour le GPU (recommandé)** : carte **NVIDIA** avec pilote récent (testé sur RTX 4090,
  pilote 596.49). Les bibliothèques CUDA 12 / cuDNN 9 sont installées via `pip` (voir ci-dessous),
  **aucun CUDA Toolkit système n'est requis**.
- Sans GPU NVIDIA : l'application fonctionne quand même en **CPU** (plus lent ; voir §6).

> Premier lancement : le modèle `large-v3` (~1,5 Go) est téléchargé **une seule fois**
> dans `C:\Users\<vous>\.cache\huggingface`. Ensuite, tout est hors ligne.

---

## 2. Installation

Dans le dossier du projet, ouvrez PowerShell :

```powershell
# 1. Créer l'environnement virtuel
python -m venv .venv

# 2. Installer les dépendances (cœur + bibliothèques GPU CUDA 12 / cuDNN 9)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

> ⚠️ Les paquets GPU `nvidia-cublas-cu12` et `nvidia-cudnn-cu12` pèsent **~1,3 Go** :
> le téléchargement peut être long. Ils sont indispensables pour `device="cuda"`.
> En cas d'absence ou d'échec, l'application bascule automatiquement sur le CPU.

L'environnement (`.venv`) contient déjà tout ; l'installation est déjà faite si vous
avez reçu le projet monté.

---

## 3. Lancement

Trois façons, au choix :

| Méthode | Commande / action | Console visible ? |
|---|---|---|
| **Double-clic** | `Plume.vbs` | Non (recommandé) |
| **Batch** | `lancer.bat` | Non |
| **Terminal** | `.venv\Scripts\python plume.py` | Oui (utile pour voir les logs) |

Pour lancer sans console depuis un terminal :

```powershell
.venv\Scripts\pythonw plume.py
```

---

## 4. Utilisation

1. Au démarrage, la fenêtre « Plume » affiche **« Chargement du modèle… »**
   puis **« Prêt — GPU (CUDA) · <source> »** (ou **CPU** en repli).
2. Cliquez sur le **gros bouton** pour démarrer l'enregistrement (il devient rouge ⏹).
3. Cliquez de nouveau pour arrêter : la transcription se lance, la fenêtre s'agrandit
   et le texte apparaît dans une zone défilante.
4. **📋 Copier** place tout le texte dans le presse-papiers (collez-le avec `Ctrl+V`).
5. **Mode ajout** : chaque nouvelle dictée s'**ajoute à la suite** de la précédente
   (séparée par une espace). Pour repartir de zéro, effacez la zone de texte manuellement.

### Choisir les sources audio (micro, son du PC, ou mix)

Cliquez sur le bouton **🎛 Sources…** : une fenêtre liste tous les périphériques :

- **🎙 Micros / entrées** — tous vos micros et entrées ligne.
- **🔊 Son du PC (sorties, captées en loopback)** — chaque sortie audio ; la capturer
  revient à transcrire **ce que joue le PC** sur ce périphérique (vidéo, appel, vocal
  Discord…).

**Cochez une ou plusieurs sources**, puis **Valider**. Plusieurs sources cochées sont
**enregistrées en même temps et mélangées** (mix) avant transcription — par exemple
votre micro **+** le son d'un appel. Le bouton **↻ Actualiser** rafraîchit la liste
si vous branchez/débranchez un périphérique.

- Le gros bouton affiche une icône **micro** ou **haut-parleur** selon la sélection.
- Le bouton **🎛 Sources…** et la barre d'état rappellent la sélection courante
  (nom du périphérique, ou « N sources (mix) »).
- La sélection est **mémorisée** entre les lancements.

> Notes :
> - Pour le « son du PC », **lancez d'abord la lecture** puis enregistrez. Seul ce qui
>   est **réellement audible** est capté (une appli en sourdine ne produit rien).
> - Capturer une sortie casque/HP en loopback **ne coupe pas** votre écoute : vous
>   continuez d'entendre normalement.
> - Tout est ramené en **16 kHz mono** (format de Whisper) automatiquement.

### Mode de sortie (Manuel / Auto-copie / Insérer)

Sous le bouton, un sélecteur choisit ce qui se passe **après** chaque transcription :

- **Manuel** — rien d'automatique ; vous cliquez **Copier** quand vous voulez.
- **Auto-copie** — le texte complet est **copié automatiquement** dans le presse-papiers.
- **Insérer** — le nouveau texte est **tapé directement** dans la fenêtre active
  (ex. le champ de message Discord), comme si vous l'écriviez au clavier.

> L'« Insérer » est surtout pensé pour le **raccourci global** : vous restez dans
> Discord, vous dictez, et le texte s'écrit dans Discord. Si vous déclenchez depuis la
> fenêtre de Plume (qui a alors le focus), le texte irait dans Plume.

### Raccourci clavier global

Vous pouvez **démarrer/arrêter la dictée sans cliquer la fenêtre** (même quand Plume
est en arrière-plan). Plume essaie plusieurs combinaisons et active la **première
disponible** ; le raccourci actif est affiché dans la barre d'état (`⌨ …`).

Ordre essayé : `Ctrl+Alt+Espace`, `Ctrl+Maj+Espace`, `Ctrl+Alt+D`, `Ctrl+Alt+J`,
`Ctrl+Alt+F9`. Modifiable via `HOTKEY_CANDIDATES` en haut de `plume.py`
(`HOTKEY_ENABLED = False` pour le désactiver).

> Workflow type pour Discord : mode **Insérer** + raccourci global → placez le curseur
> dans le champ de message, pressez le raccourci, parlez, re-pressez : le texte
> s'écrit dans Discord.

### Effacer & minuterie

- Bouton **Effacer** (à côté de Copier) : vide la zone de texte.
- Pendant l'enregistrement, la barre d'état affiche un **compteur de durée**
  (`● Enregistrement… (source)  0:07`).

### Corrections personnalisées

Le fichier **`plume_replacements.json`** (à côté de `plume.py`) corrige automatiquement
des mots récurrents mal transcrits — utile pour les **pseudos, le jargon, les noms
propres**. Format :

```json
{
  "discorde": "Discord",
  "git eub": "GitHub",
  "mon pseudo mal entendu": "MonPseudo"
}
```

Insensible à la casse, sur mots entiers. Les clés commençant par `_` sont ignorées
(pour vos commentaires). Les modifications sont prises en compte à la dictée suivante.

### Thèmes & apparence

- Trois thèmes via les **3 pastilles colorées** en haut à droite : **Sombre** (indigo),
  **Clair** (bleu) et **Océan** (turquoise). La barre de titre Windows s'assortit.
- Thème **et** sélection de sources sont **mémorisés** (fichier `plume_config.json`,
  créé automatiquement à côté de `plume.py`). Supprimez-le pour tout réinitialiser.
- L'interface est **DPI-aware** : rendu net (non pixelisé) même en mise à l'échelle
  Windows à 125 %/150 %/200 %.

---

## 5. Auto-test

Vérifie le chargement du modèle, le backend (GPU/CPU) et la transcription, **sans ouvrir l'interface** :

```powershell
.venv\Scripts\python plume.py --selftest
```

Sortie attendue (extrait) :

```
Backend obtenu          : GPU (CUDA)
Temps de chargement     : ~12 s (incl. passe de chauffe)
Transcription (2s)      : ~0.2 s
=== Auto-test terminé : OK ===
```

> Le texte renvoyé par l'auto-test sur un signal sinusoïdal peut être vide ou farfelu
> (ex. « Sous-titrage… ») : c'est normal, l'audio synthétique n'est pas de la parole.
> Le but est de confirmer le **backend** et le **pipeline**, pas la qualité.

---

## 6. Réglages — changer le modèle (`MODEL_SIZE`)

En haut de [`plume.py`](plume.py) :

```python
SAMPLE_RATE = 16000        # ne pas changer (format attendu par Whisper)
MODEL_SIZE  = "large-v3"   # voir tableau ci-dessous
LANGUAGE    = "fr"
```

**Si le repli CPU est trop lent**, choisissez un modèle plus léger : remplacez
`large-v3` par, dans l'ordre du plus précis au plus rapide :

| `MODEL_SIZE` | Qualité | Vitesse CPU | VRAM (GPU) |
|---|---|---|---|
| `large-v3` | ★★★★★ | lent | ~4–5 Go |
| `medium`   | ★★★★ | moyen | ~2 Go |
| `small`    | ★★★ | rapide | ~1 Go |
| `base`     | ★★ | très rapide | <1 Go |

Sur **RTX 4090**, `large-v3` tourne **plus vite que le temps réel** : gardez-le.

Autres constantes utiles : `BEAM_SIZE` (qualité du décodage), `VAD_FILTER`
(filtrage des silences, activé si `onnxruntime` est présent).

**Ponctuation** (en haut de `plume.py`) :

- `INITIAL_PROMPT` — court texte bien ponctué qui « conditionne » Whisper à mettre
  davantage de virgules/points/points d'interrogation. Modifiable.
- `AUTO_PUNCTUATION` — nettoyage léger en sortie : majuscule initiale, **point final
  si absent**, espaces corrigés (la ponctuation à la française, ex. « mot ? », est
  préservée). Mettre `False` pour le texte brut du modèle.
- `CONDITION_ON_PREVIOUS_TEXT` — cohérence de ponctuation entre segments.

> La ponctuation reste **inférée par le modèle** d'après l'intonation et les pauses :
> parler en marquant les fins de phrases améliore le résultat. Pour une restauration
> de ponctuation plus poussée, il faudrait un modèle dédié (lourd) — non inclus pour
> rester léger.

---

## 7. Dépannage CUDA (le point fragile)

Le statut affiche **« Prêt — CPU »** alors que vous avez une carte NVIDIA ?
Lancez depuis un terminal (`.venv\Scripts\python plume.py`) pour voir la **cause** affichée
en console (`[Plume] Chargement CUDA échoué -> repli CPU. Cause : …`).

Pistes :

1. **Pilote NVIDIA** présent et à jour :
   ```powershell
   nvidia-smi
   ```
   doit afficher votre GPU. Sinon, installez/mettez à jour le pilote NVIDIA (GeForce/Studio).

2. **DLL CUDA introuvables** (`Could not load cublas64_12.dll` / `cudnn…`) :
   vérifiez que les paquets sont installés :
   ```powershell
   .venv\Scripts\python -m pip show nvidia-cublas-cu12 nvidia-cudnn-cu12
   ```
   L'application ajoute automatiquement leurs dossiers `…\site-packages\nvidia\*\bin`
   au chemin de recherche des DLL au démarrage. Si besoin, réinstallez :
   ```powershell
   .venv\Scripts\python -m pip install --force-reinstall nvidia-cublas-cu12 nvidia-cudnn-cu12
   ```

3. **Version de cuDNN** : CTranslate2 4.5+ exige **cuDNN 9** (fourni par `nvidia-cudnn-cu12`
   ≥ 9.x). Ne mélangez pas avec une vieille cuDNN 8 sur le `PATH` système.

4. **Mémoire GPU saturée** par une autre application : fermez-la, ou passez à un
   `MODEL_SIZE` plus petit.

Dans tous les cas, **le repli CPU reste fonctionnel** : l'application ne plante pas.

---

## 8. Dépannage audio

- **« Aucune source — ouvrez Sources… »** : aucune source n'est cochée. Ouvrez 🎛 Sources…
  et cochez au moins un périphérique.
- **Le son du PC ne se transcrit pas** : vérifiez que la lecture est lancée et audible,
  et que vous avez coché la **bonne sortie** (celle réellement utilisée) dans 🎛 Sources….
  Utilisez **↻ Actualiser** après un changement de périphérique.
- **Un périphérique a disparu** de la liste : il a été débranché ; **↻ Actualiser**.
  Une source mémorisée mais absente est simplement ignorée au démarrage.
- **Mix déséquilibré** : les sources sont mélangées telles quelles (puis normalisées).
  Ajustez les volumes Windows de chaque source si l'une couvre l'autre.

---

## 9. Limites connues

- **Noms propres, jargon, anglais, bruit de fond** : sources d'erreurs inhérentes à tout
  moteur STT. Parlez clairement, micro proche.
- **Hallucinations sur les silences** : limitées par le filtre VAD (activé automatiquement
  si `onnxruntime` est disponible), pas totalement éliminées.
- **Mode ajout uniquement** : pas de bouton « Effacer » dans l'interface (effacez à la main).
- **Mix = alignement au début** : les sources sont synchronisées sur l'instant de départ,
  pas échantillon par échantillon. Un léger décalage (quelques dizaines de ms) entre
  sources est sans incidence pour la transcription.
- **Capture = Windows/WASAPI** via `soundcard`. Sans ce paquet, aucune capture (l'app le
  signale au démarrage).
- **Barre de titre Océan** : si Windows affiche la couleur d'accentuation sur les barres
  de titre, la barre peut prendre cette couleur (cosmétique ; le mode sombre reste actif).
- **Latence du 1er lancement** : téléchargement du modèle (~1,5 Go), une seule fois.

---

## 10. Fichiers du projet

| Fichier | Rôle |
|---|---|
| `plume.py` | Application (UI + thèmes + sources + transcription + auto-test `--selftest`). Constantes en haut de fichier. |
| `requirements.txt` | Dépendances Python (cœur + GPU). |
| `Plume.vbs` | Lanceur double-clic sans console. |
| `lancer.bat` | Lanceur batch sans console. |
| `plume_config.json` | Préférences (thème, sources, mode de sortie). Créé automatiquement ; suppressible sans risque. |
| `plume_replacements.json` | Corrections personnalisées (pseudos, jargon). Éditable. |
| `README.md` | Ce fichier. |
| `.venv\` | Environnement virtuel (créé à l'installation). |
