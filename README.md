# Plume — reconnaissance vocale locale (français)

Application de dictée vocale **100 % locale et hors ligne** (après installation).
Vous parlez au micro **— ou vous transcrivez le son joué par le PC —** et le texte
français apparaît dans une fenêtre, copiable en un clic (pratique pour Discord, etc.).

- **Transcription** : [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (Whisper via CTranslate2)
- **Capture micro** : [sounddevice](https://python-sounddevice.readthedocs.io) (PortAudio)
- **Capture son système** : [soundcard](https://github.com/bastibe/SoundCard) (loopback WASAPI, Windows)
- **Interface** : `tkinter` (bibliothèque standard de Python), 3 thèmes, rendu DPI-aware
- **Modèle par défaut** : `large-v3`, GPU NVIDIA (CUDA/float16) avec **repli CPU (int8) automatique**

---

## 1. Prérequis

- **Windows 11** (testé), Windows 10 probablement compatible.
- **Python 3.10 ou plus** (développé et testé sur **3.13.13**). Cochez « Add Python to PATH » à l'installation.
- Un **microphone**.
- **Pour le GPU (recommandé)** : carte **NVIDIA** avec pilote récent (testé sur RTX 4090,
  pilote 596.49). Les bibliothèques CUDA 12 / cuDNN 9 sont installées via `pip` (voir ci-dessous),
  **aucun CUDA Toolkit système n'est requis**.
- Sans GPU NVIDIA : l'application fonctionne quand même en **CPU** (plus lent ; voir §6).

> Premier lancement : le modèle `large-v3` (~1,5 Go) est téléchargé **une seule fois**
> dans `C:\Users\<vous>\.cache\huggingface`. Ensuite, tout est hors ligne.

---

## 2. Installation

Dans le dossier du projet (`C:\Users\firer\Documents\voicetotext`), ouvrez PowerShell :

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
| **Double-clic** | `Dictee.vbs` | Non (recommandé) |
| **Batch** | `lancer.bat` | Non |
| **Terminal** | `.venv\Scripts\python dictee.py` | Oui (utile pour voir les logs) |

Pour lancer sans console depuis un terminal :

```powershell
.venv\Scripts\pythonw dictee.py
```

---

## 4. Utilisation

1. Au démarrage, la fenêtre « Dictée » affiche **« Chargement du modèle… »**
   puis **« Prêt — GPU (CUDA) »** (ou **« Prêt — CPU »** en repli).
2. Cliquez sur **🎙 Micro** pour démarrer l'enregistrement (le bouton devient **⏹ Arrêter**).
3. Cliquez de nouveau pour arrêter : la transcription se lance, la fenêtre s'agrandit
   et le texte apparaît dans une zone défilante.
4. **📋 Copier** place tout le texte dans le presse-papiers (collez-le ensuite avec `Ctrl+V`).
5. **Mode ajout** : chaque nouvelle dictée s'**ajoute à la suite** de la précédente
   (séparée par une espace). Pour repartir de zéro, effacez la zone de texte manuellement.

### Source : micro ou son système

Sous le bouton, un sélecteur permet de choisir **ce qui est transcrit** :

- **🎙 Micro** : votre microphone (par défaut).
- **🔊 Système** : le **son joué par le PC** (loopback WASAPI) — utile pour transcrire
  une vidéo, un appel, ce que dit une personne dans un vocal Discord, etc. Le gros
  bouton affiche alors une icône de haut-parleur.

Marche à suivre pour le son système : sélectionnez **Système**, lancez la lecture du
son à transcrire, cliquez sur le bouton pour démarrer, puis cliquez à nouveau pour
arrêter et transcrire. Le choix de source est **mémorisé** entre les lancements.

> Notes :
> - La capture système est **Windows uniquement** (WASAPI) et capte la **sortie par
>   défaut** de Windows (Paramètres → Son → Sortie). Changez la sortie par défaut pour
>   capter un autre périphérique.
> - Seul ce qui est **réellement audible** est capté : si une appli a son propre volume
>   à zéro (ou est en sourdine), rien n'est transcrit.

### Thèmes & apparence

- Trois thèmes au choix via les **3 pastilles colorées** en haut à droite :
  **Sombre** (indigo), **Clair** (bleu) et **Océan** (turquoise). La barre de titre
  Windows s'assortit automatiquement (sombre/clair).
- Le thème choisi est **mémorisé** entre les lancements (fichier `dictee_config.json`,
  créé automatiquement à côté de `dictee.py`). Supprimez-le pour revenir au thème par défaut.
- L'interface est **DPI-aware** : rendu net (non pixelisé) même sur écran haute résolution
  ou avec une mise à l'échelle Windows à 125 %/150 %/200 %.

---

## 5. Auto-test

Vérifie le chargement du modèle, le backend (GPU/CPU) et la transcription, **sans ouvrir l'interface** :

```powershell
.venv\Scripts\python dictee.py --selftest
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

En haut de [`dictee.py`](dictee.py) :

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

---

## 7. Dépannage CUDA (le point fragile)

Le statut affiche **« Prêt — CPU »** alors que vous avez une carte NVIDIA ?
Lancez depuis un terminal (`.venv\Scripts\python dictee.py`) pour voir la **cause** affichée
en console (`[Dictée] Chargement CUDA échoué -> repli CPU. Cause : …`).

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

## 8. Limites connues

- **Noms propres, jargon, anglais, bruit de fond** : sources d'erreurs inhérentes à tout
  moteur STT. Parlez clairement, micro proche.
- **Hallucinations sur les silences** : limitées par le filtre VAD (activé automatiquement
  si `onnxruntime` est disponible), pas totalement éliminées.
- **Mode ajout uniquement** : pas de bouton « Effacer » dans l'interface (effacez à la main
  dans la zone de texte).
- **Barre de titre Océan** : si Windows a l'option « Afficher la couleur d'accentuation
  sur les barres de titre » activée, la barre peut prendre votre couleur d'accent système
  (purement cosmétique, le mode sombre reste appliqué).
- **Capture son système** : Windows uniquement (loopback WASAPI via `soundcard`). Si le
  paquet `soundcard` est absent, l'option « Système » est signalée indisponible et seul le
  micro fonctionne. La capture suit la **sortie audio par défaut** de Windows.
- **Latence du 1er lancement** : téléchargement du modèle (~1,5 Go), une seule fois.

---

## 9. Fichiers du projet

| Fichier | Rôle |
|---|---|
| `dictee.py` | Application (UI + thèmes + transcription + auto-test `--selftest`). Constantes en haut de fichier. |
| `requirements.txt` | Dépendances Python (cœur + GPU). |
| `Dictee.vbs` | Lanceur double-clic sans console. |
| `lancer.bat` | Lanceur batch sans console. |
| `dictee_config.json` | Préférences (thème choisi). Créé automatiquement ; suppressible sans risque. |
| `README.md` | Ce fichier. |
| `.venv\` | Environnement virtuel (créé à l'installation). |
