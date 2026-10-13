# Développer Plume

Guide pour modifier Plume depuis **WSL (Ubuntu)**, le tester, obtenir un
`plume.exe` et publier une version. Pour comprendre le code lui-même, voir
[`architecture.md`](architecture.md).

## Ce qu'on peut faire, et où

Plume a deux couches : le **cœur** (`src/` hors `src/app/`), qui ne dépend
pas de Windows, et l'**application Windows** (`src/app/` : raccourci, micro,
collage, icône).

| Tâche | WSL (Ubuntu) | Windows | CI GitHub |
|---|---|---|---|
| Compiler et tester le cœur (découpage, texte, réglages, moteur Whisper) | Oui | Oui | Oui, à chaque push |
| Transcrire un fichier WAV avec le vrai traitement (`--transcrire`) | Oui | Oui | Oui (essai réel) |
| Vérifier que le code Windows compile et passe clippy | Oui (voir [plus bas](#vérifier-le-code-windows-depuis-wsl)) | Oui | Oui |
| Compiler le vrai `plume.exe` (carte graphique, Vulkan) | Non | Oui, avec les outils listés [plus bas](#compiler-sous-windows) | **Oui, recommandé** |
| Lancer l'application (raccourci, micro, collage) | Non | Oui | Non |

En pratique : on code et on teste sous WSL, on pousse, la CI fabrique
`plume.exe`, et on l'essaie sous Windows.

## Installer l'environnement (Ubuntu 24.04 sous WSL)

### 1. Outils de compilation

whisper.cpp est compilé en C++ pendant la compilation de Plume : il faut un
compilateur, CMake et libclang (pour générer les liaisons Rust).

```bash
sudo apt update
sudo apt install -y build-essential cmake clang libclang-dev git curl
```

### 2. Rust

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
rustup component add clippy rustfmt
```

Version minimale : Rust 1.85 (édition 2024). `rustup update` met à jour.

### 3. Le dépôt

Clone-le **dans le système de fichiers Linux** (`~/…`), pas sous `/mnt/c/…` :
sous `/mnt/c`, la compilation est plusieurs fois plus lente.

```bash
git clone https://github.com/Mbensacq/plume.git ~/plume
cd ~/plume
```

Pour ouvrir le dossier dans VS Code sous Windows : `code .` depuis WSL
(extension « WSL » de VS Code). L'extension **rust-analyzer** donne
l'autocomplétion et les erreurs en direct.

### 4. Dossier de données pour les essais

Plume range réglages, modèles et journal à côté de l'exécutable. Lancé avec
`cargo run`, ce serait dans `target/release/`, effacé par `cargo clean`. Fixe
plutôt un dossier stable (à ajouter à `~/.bashrc`) :

```bash
export PLUME_DOSSIER="$HOME/.local/share/plume"
```

### 5. Première compilation et premier essai

```bash
cargo test --release
cargo run --release -- --telecharger-modele tiny
cargo run --release -- --transcrire tests/data/librivox-fr-12s.wav --modele tiny
```

La première compilation prend plusieurs minutes (whisper.cpp) et
`target/` occupe environ 1 Go ; les suivantes sont rapides. Utilise toujours
`--release` : en mode debug, la transcription est des dizaines de fois plus
lente.

## Commandes du quotidien

| Commande | Rôle |
|---|---|
| `cargo build --release` | Compiler |
| `cargo test --release` | Tous les tests (le test de bout en bout est ignoré sans `PLUME_TEST_MODELE`) |
| `cargo fmt` | Mettre en forme le code (largeur 120, voir `rustfmt.toml`) |
| `cargo clippy --release --all-targets -- -D warnings` | Analyse du code ; la CI refuse le moindre avertissement |
| `cargo run --release -- --aide` | Aide de la ligne de commande |
| `cargo run --release -- --transcrire FICHIER.wav` | Transcrire un fichier avec exactement le traitement d'une dictée |
| `cargo run --release -- --transcrire FICHIER.wav --temps-reel` | Simuler une dictée en direct et mesurer le délai après l'arrêt |
| `cargo run --release -- --version` | Version, version de whisper.cpp, capacités du processeur |

Options de `--transcrire` : `--modele NOM`, `--precision 1..8`,
`--processeur` (sans carte graphique). Sous WSL, le moteur tourne sur le
processeur : la compilation par défaut n'inclut pas la carte graphique (c'est
la variante `--features vulkan`, celle de `plume.exe`).

Lancé sans argument hors de Windows, `plume` affiche simplement que
l'application de bureau ne fonctionne que sous Windows : c'est normal.

### Test de bout en bout

Il fait passer un extrait LibriVox en français (12 s, domaine public) par le
découpage, le moteur et le nettoyage :

```bash
PLUME_TEST_MODELE="$PLUME_DOSSIER/modeles/ggml-tiny.bin" cargo test --release --test e2e
```

### Essayer sur ta propre voix

`--transcrire` lit les fichiers **WAV** (toute fréquence, mono ou stéréo).
Tes fichiers Windows sont visibles sous `/mnt/c/Users/<nom>/…`. Pour
convertir un enregistrement du Magnétophone de Windows (`.m4a`) :

```bash
sudo apt install -y ffmpeg
ffmpeg -i /mnt/c/Users/<nom>/Documents/Enregistrement.m4a -ac 1 -ar 16000 dictee.wav
cargo run --release -- --transcrire dictee.wav --temps-reel
```

### Avant de pousser

Les trois vérifications de la CI, dans l'ordre :

```bash
cargo fmt --check
cargo clippy --release --all-targets -- -D warnings
cargo test --release
```

Si tu as modifié `src/app/` (code Windows), ajoute la vérification de la
section suivante.

## Vérifier le code Windows depuis WSL

Le code de `src/app/` n'est compilé que pour Windows. Pour savoir s'il
compile et passe clippy sans quitter WSL, une fois pour toutes :

```bash
sudo apt install -y gcc-mingw-w64-x86-64
rustup target add x86_64-pc-windows-gnu
```

Puis :

```bash
cargo clippy --target x86_64-pc-windows-gnu --no-default-features --all-targets -- -D warnings
```

`--no-default-features` laisse de côté le moteur Whisper (whisper.cpp ne se
compile pas vers Windows depuis Linux sans réglages supplémentaires) : on
vérifie tout le reste, dont toute la partie Windows. Le vrai `plume.exe` est
fabriqué par la CI.

## Obtenir un plume.exe

### Par la CI (recommandé)

1. Pousse ta branche (`git push`).
2. Sur GitHub, onglet **Actions**, exécution « CI » de ton commit
   (environ 10 minutes).
3. Section **Artifacts** en bas de la page : `plume-windows-x64.exe` (carte
   graphique) ou `plume-windows-x64-cpu.exe`, chacun dans un zip.
4. Quitte Plume (clic sur l'icône, « Quitter »), remplace l'ancien
   `plume.exe` par le nouveau, relance.

La CI vérifie aussi que l'exécutable démarre et transcrit un vrai extrait en
français avant de le proposer.

### Compiler sous Windows

Prérequis : [Rust](https://rustup.rs), Visual Studio Build Tools (charge de
travail « Développement Desktop en C++ »), CMake, LLVM (pour libclang) et le
[Vulkan SDK](https://vulkan.lunarg.com/sdk/home) pour la carte graphique.

```powershell
# Dossier de compilation court : la compilation Vulkan dépasse sinon la
# limite de 260 caractères des chemins Windows.
$env:CARGO_TARGET_DIR = "C:\t"
cargo build --release --features vulkan     # carte graphique (Vulkan)
cargo build --release                       # processeur seulement
```

L'exécutable se trouve dans `C:\t\release\plume.exe`.

## Organisation du travail

### Branches

- `main` : la version de référence. Toujours compilable, CI verte.
- `feature/<sujet>` : une nouveauté (`feature/raccourci-double-appui`).
- `fix/<sujet>` : une correction (`fix/collage-teams`).
- `archive/…` : anciennes versions, conservées telles quelles
  (`archive/plume-1-python`).

La CI tourne sur `main`, `feature/**`, `fix/**` et les pull requests. Les
modifications de documentation seule (`*.md`, `docs/`) ne relancent pas la
compilation.

```bash
git switch main && git pull --ff-only
git switch -c fix/collage-teams
# … modifications, vérifications …
git add -A && git commit -m "fix: paste into Teams chat"
git push -u origin fix/collage-teams
# CI verte et essai concluant : on ramène la branche dans main
git switch main && git merge --ff-only fix/collage-teams && git push
git push origin --delete fix/collage-teams
```

Une pull request sur GitHub fait la même chose avec une page de revue, si tu
préfères.

### Messages de commit

En anglais, au format déjà utilisé dans l'historique :

| Préfixe | Pour |
|---|---|
| `feat:` | Nouvelle fonctionnalité |
| `fix:` | Correction |
| `ci:` | Workflow GitHub Actions |
| `docs:` | Documentation |
| `refactor:` | Réorganisation sans changement de comportement |
| `test:` | Tests seulement |

Le code (commentaires, messages affichés, réglages) et la documentation sont
en français.

## Publier une version

Une Release déclenche la **mise à jour automatique** chez tous ceux qui ont
`mise_a_jour_auto = true` : au lancement suivant, Plume télécharge le nouvel
exécutable, qui devient actif au lancement d'après.

1. Mets à jour `version` dans `Cargo.toml` (par exemple `2.0.1`).
2. `cargo build --release` (met `Cargo.lock` à jour), puis commit sur `main`.
3. Crée et pousse le tag :

   ```bash
   git tag v2.0.1
   git push origin v2.0.1
   ```

4. La CI compile, teste et crée la Release avec les deux exécutables.

Les versions sont comparées numériquement (`v2.10.0` > `v2.9.9`). Une
version marquée « pre-release » sur GitHub est ignorée par la mise à jour
automatique : pratique pour faire essayer une version avant de la diffuser.

## Règles du code

- **Le cœur reste indépendant de Windows.** Tout ce qui touche à Windows va
  dans `src/app/` (ou derrière `#[cfg(windows)]`). C'est ce qui permettra une
  version Android plus tard.
- **Un nouveau réglage** = un champ avec sa valeur par défaut dans
  `src/config.rs` + sa ligne commentée dans `plume.toml` (le modèle copié au
  premier lancement). Le test `template_matches_defaults_except_examples`
  vérifie que les deux restent d'accord.
- **Un réglage invalide n'empêche jamais Plume de démarrer** : valeur par
  défaut et avertissement lisible, en français.
- **Tests** à côté du code (`#[cfg(test)] mod tests` en bas de chaque
  fichier) ; le test de bout en bout est dans `tests/e2e.rs`.
- `cargo fmt` et zéro avertissement clippy.

## Dépannage

| Symptôme | Solution |
|---|---|
| `Unable to find libclang` | `sudo apt install libclang-dev` |
| `is cmake not installed?` / `failed to execute command: cmake` | `sudo apt install cmake` |
| Compilation très lente | Le dépôt est sous `/mnt/c` : clone-le dans `~/` |
| Transcription très lente | Oubli de `--release` |
| `failed to find tool "x86_64-w64-mingw32-gcc"` (vérification Windows) | `sudo apt install gcc-mingw-w64-x86-64` |
| Un problème sous Windows | Lire `plume.log` dans le dossier de Plume |
