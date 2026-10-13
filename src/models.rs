//! Modèles Whisper : où les trouver, comment les télécharger.
//!
//! Les fichiers viennent du dépôt officiel de whisper.cpp sur Hugging Face.
//! Le téléchargement se fait dans un fichier `.part`, renommé seulement une fois
//! complet : un téléchargement interrompu ne laisse jamais de modèle abîmé.

use anyhow::{Context, Result, bail};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

const BASE_URL: &str = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ModelInfo {
    pub name: &'static str,
    pub file: &'static str,
    pub size_bytes: u64,
}

/// Modèles connus (tailles vérifiées sur Hugging Face).
pub const CATALOG: &[ModelInfo] = &[
    ModelInfo { name: "large-v3-turbo", file: "ggml-large-v3-turbo-q8_0.bin", size_bytes: 874_188_075 },
    ModelInfo { name: "large-v3", file: "ggml-large-v3.bin", size_bytes: 3_095_033_483 },
    ModelInfo { name: "medium", file: "ggml-medium-q8_0.bin", size_bytes: 823_369_779 },
    ModelInfo { name: "small", file: "ggml-small-q8_0.bin", size_bytes: 264_464_607 },
    ModelInfo { name: "base", file: "ggml-base.bin", size_bytes: 147_951_465 },
    ModelInfo { name: "tiny", file: "ggml-tiny.bin", size_bytes: 77_691_713 },
];

pub fn find(name: &str) -> Option<&'static ModelInfo> {
    let n = name.trim().to_lowercase();
    CATALOG.iter().find(|m| m.name == n || m.file == n)
}

/// Où se trouve (ou se trouvera) le modèle demandé dans la config : un nom du
/// catalogue, ou un chemin vers un fichier `.bin`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelSource {
    Catalog(&'static ModelInfo, PathBuf),
    File(PathBuf),
}

impl ModelSource {
    pub fn resolve(setting: &str, models_dir: &Path) -> Result<ModelSource> {
        if let Some(info) = find(setting) {
            return Ok(ModelSource::Catalog(info, models_dir.join(info.file)));
        }
        let path = PathBuf::from(setting.trim());
        if path.extension().is_some_and(|e| e.eq_ignore_ascii_case("bin")) {
            return Ok(ModelSource::File(path));
        }
        let names: Vec<&str> = CATALOG.iter().map(|m| m.name).collect();
        bail!("modèle inconnu « {setting} » ; choix possibles : {} ou un chemin vers un fichier .bin", names.join(", "))
    }

    pub fn path(&self) -> &Path {
        match self {
            ModelSource::Catalog(_, p) | ModelSource::File(p) => p,
        }
    }

    pub fn is_present(&self) -> bool {
        match self {
            ModelSource::Catalog(info, p) => fs::metadata(p).map(|m| m.len() == info.size_bytes).unwrap_or(false),
            ModelSource::File(p) => p.is_file(),
        }
    }

    pub fn label(&self) -> String {
        match self {
            ModelSource::Catalog(info, _) => info.name.to_string(),
            ModelSource::File(p) => p.file_name().map(|f| f.to_string_lossy().into_owned()).unwrap_or_default(),
        }
    }
}

/// Client HTTP : certificats du système (Windows) pour suivre les règles de
/// sécurité de la machine, délais raisonnables.
pub fn http_agent() -> ureq::Agent {
    use ureq::tls::{RootCerts, TlsConfig};
    ureq::Agent::config_builder()
        .tls_config(TlsConfig::builder().root_certs(RootCerts::PlatformVerifier).build())
        .timeout_connect(Some(Duration::from_secs(20)))
        // Pas de limite sur le corps : un modèle de 3 Go peut prendre longtemps.
        .timeout_recv_response(Some(Duration::from_secs(30)))
        .user_agent(concat!("plume/", env!("CARGO_PKG_VERSION")))
        .build()
        .into()
}

/// Télécharge `info` dans `dest`. `progress(reçus, total)` est appelé
/// régulièrement (à peu près tous les pourcents).
pub fn download(info: &ModelInfo, dest: &Path, mut progress: impl FnMut(u64, u64)) -> Result<()> {
    let url = format!("{BASE_URL}/{}", info.file);
    download_url(&url, dest, Some(info.size_bytes), &mut progress)
}

pub fn download_url(url: &str, dest: &Path, expected: Option<u64>, progress: &mut dyn FnMut(u64, u64)) -> Result<()> {
    if let Some(dir) = dest.parent() {
        fs::create_dir_all(dir).with_context(|| format!("création de {}", dir.display()))?;
    }
    let part = dest.with_extension("part");
    let agent = http_agent();
    let mut resp = agent.get(url).call().with_context(|| format!("connexion à {url}"))?;
    let total = resp
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .or(expected)
        .unwrap_or(0);
    if let (Some(exp), true) = (expected, total > 0)
        && exp != total
    {
        bail!("taille inattendue pour {url} : {total} octets au lieu de {exp}");
    }
    let mut reader = resp.body_mut().with_config().limit(u64::MAX).reader();
    let mut file = File::create(&part).with_context(|| format!("création de {}", part.display()))?;
    let mut buf = vec![0u8; 1 << 20];
    let mut received: u64 = 0;
    let mut last_report = 0u64;
    loop {
        let n = reader.read(&mut buf).context("téléchargement interrompu")?;
        if n == 0 {
            break;
        }
        file.write_all(&buf[..n]).context("écriture sur le disque (plus de place ?)")?;
        received += n as u64;
        if total == 0 || received - last_report >= total / 100 {
            progress(received, total);
            last_report = received;
        }
    }
    file.sync_all().ok();
    drop(file);
    if total > 0 && received != total {
        let _ = fs::remove_file(&part);
        bail!("téléchargement incomplet : {received} octets sur {total}");
    }
    progress(received, total.max(received));
    fs::rename(&part, dest).with_context(|| format!("renommage vers {}", dest.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_catalog_names_and_paths() {
        let dir = Path::new("/tmp/modeles");
        let s = ModelSource::resolve("Large-V3-Turbo", dir).unwrap();
        assert_eq!(s.path(), dir.join("ggml-large-v3-turbo-q8_0.bin"));
        assert_eq!(s.label(), "large-v3-turbo");
        let s = ModelSource::resolve("D:\\modeles\\perso.bin", dir).unwrap();
        assert!(matches!(s, ModelSource::File(_)));
        let e = ModelSource::resolve("gigantesque", dir).unwrap_err().to_string();
        assert!(e.contains("large-v3-turbo"), "{e}");
    }

    #[test]
    fn partial_files_are_not_present() {
        let dir = std::env::temp_dir().join("plume-test-models");
        fs::create_dir_all(&dir).unwrap();
        let s = ModelSource::resolve("tiny", &dir).unwrap();
        fs::write(s.path(), b"abime").unwrap();
        assert!(!s.is_present(), "un fichier de mauvaise taille n'est pas un modèle");
        let _ = fs::remove_dir_all(dir);
    }
}
