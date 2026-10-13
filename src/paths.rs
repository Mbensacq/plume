//! Emplacement des fichiers de Plume : configuration, modèles, journal, historique.
//!
//! Par défaut tout est rangé à côté de l'exécutable (installation « portable » :
//! on copie le dossier, tout suit). Si ce dossier n'est pas inscriptible (par
//! exemple `C:\Program Files`), on se replie sur `%LOCALAPPDATA%\Plume`.
//! La variable d'environnement `PLUME_DOSSIER` force un autre dossier.

use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct Paths {
    root: PathBuf,
}

impl Paths {
    /// Détermine le dossier de données selon les règles ci-dessus et le crée.
    pub fn detect() -> Paths {
        if let Some(dir) = std::env::var_os("PLUME_DOSSIER").filter(|v| !v.is_empty()) {
            return Paths::at(PathBuf::from(dir));
        }
        if let Some(exe_dir) = std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf))
            && is_writable(&exe_dir)
        {
            return Paths::at(exe_dir);
        }
        Paths::at(fallback_dir())
    }

    /// Utilise `root` tel quel (créé s'il n'existe pas).
    pub fn at(root: PathBuf) -> Paths {
        let _ = fs::create_dir_all(&root);
        Paths { root }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn config(&self) -> PathBuf {
        self.root.join("plume.toml")
    }

    pub fn models_dir(&self) -> PathBuf {
        self.root.join("modeles")
    }

    pub fn log_file(&self) -> PathBuf {
        self.root.join("plume.log")
    }

    /// Toutes les dictées, horodatées (si `historique = true`).
    pub fn history_file(&self) -> PathBuf {
        self.root.join("historique.txt")
    }

    /// Texte de la dictée en cours, écrit morceau par morceau : rien n'est perdu
    /// si Plume s'arrête en plein milieu d'une longue dictée.
    pub fn last_dictation_file(&self) -> PathBuf {
        self.root.join("derniere-dictee.txt")
    }
}

fn is_writable(dir: &Path) -> bool {
    let probe = dir.join(".plume-test-ecriture");
    match fs::write(&probe, b"ok") {
        Ok(()) => {
            let _ = fs::remove_file(&probe);
            true
        }
        Err(_) => false,
    }
}

fn fallback_dir() -> PathBuf {
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        return PathBuf::from(local).join("Plume");
    }
    if let Some(home) = std::env::var_os("HOME") {
        return PathBuf::from(home).join(".local").join("share").join("plume");
    }
    std::env::temp_dir().join("plume")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn files_live_under_root() {
        let dir = std::env::temp_dir().join("plume-test-paths");
        let p = Paths::at(dir.clone());
        assert!(dir.is_dir());
        assert_eq!(p.config(), dir.join("plume.toml"));
        assert_eq!(p.models_dir(), dir.join("modeles"));
        assert!(is_writable(&dir));
        let _ = fs::remove_dir_all(dir);
    }
}
