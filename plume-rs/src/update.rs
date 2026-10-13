//! Mise à jour automatique depuis les « Releases » GitHub du projet.
//!
//! Au démarrage (si `mise_a_jour_auto = true`), Plume regarde la dernière
//! version publiée. Si elle est plus récente, il télécharge le nouvel
//! exécutable à côté de l'actuel, puis échange les fichiers : Windows permet de
//! renommer un programme en cours d'exécution. La nouvelle version est active
//! au lancement suivant ; l'ancienne (`.old`) est supprimée à ce moment-là.

use crate::models;
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::fs;
use std::path::{Path, PathBuf};

const LATEST_URL: &str = "https://api.github.com/repos/Mbensacq/plume/releases/latest";

/// Nom du fichier publié pour cette variante de Plume.
pub fn asset_name() -> &'static str {
    if cfg!(any(feature = "vulkan", feature = "cuda")) { "plume-windows-x64.exe" } else { "plume-windows-x64-cpu.exe" }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Release {
    pub version: String,
    pub url: String,
    pub size: u64,
}

#[derive(Deserialize)]
struct GhRelease {
    tag_name: String,
    #[serde(default)]
    draft: bool,
    #[serde(default)]
    prerelease: bool,
    #[serde(default)]
    assets: Vec<GhAsset>,
}

#[derive(Deserialize)]
struct GhAsset {
    name: String,
    browser_download_url: String,
    size: u64,
}

/// « v2.1.0 », « plume-v2.1.0 », « 2.1 » → (2, 1, 0).
pub fn parse_version(tag: &str) -> Option<(u32, u32, u32)> {
    let start = tag.find(|c: char| c.is_ascii_digit())?;
    let core = tag[start..].split(['-', '+', ' ']).next()?;
    let mut it = core.split('.').map(|p| p.parse::<u32>());
    let major = it.next()?.ok()?;
    let minor = it.next().unwrap_or(Ok(0)).ok()?;
    let patch = it.next().unwrap_or(Ok(0)).ok()?;
    Some((major, minor, patch))
}

/// Choisit la mise à jour à installer dans la réponse de l'API GitHub.
fn pick(json: &str, current: &str, asset: &str) -> Result<Option<Release>> {
    let rel: GhRelease = serde_json::from_str(json).context("réponse GitHub illisible")?;
    if rel.draft || rel.prerelease {
        return Ok(None);
    }
    let (Some(latest), Some(mine)) = (parse_version(&rel.tag_name), parse_version(current)) else {
        return Ok(None);
    };
    if latest <= mine {
        return Ok(None);
    }
    Ok(rel.assets.into_iter().find(|a| a.name == asset).map(|a| Release {
        version: rel.tag_name.clone(),
        url: a.browser_download_url,
        size: a.size,
    }))
}

/// Interroge GitHub. `Ok(None)` : déjà à jour (ou rien de publié).
pub fn check() -> Result<Option<Release>> {
    let agent = models::http_agent();
    let resp = agent
        .get(LATEST_URL)
        .header("Accept", "application/vnd.github+json")
        .config()
        .http_status_as_error(false)
        .build()
        .call()
        .context("connexion à GitHub")?;
    let status = resp.status().as_u16();
    if status == 404 {
        return Ok(None);
    }
    if status != 200 {
        bail!("GitHub a répondu {status}");
    }
    let body = resp.into_body().read_to_string().context("lecture de la réponse GitHub")?;
    pick(&body, crate::VERSION, asset_name())
}

/// Télécharge et met en place la nouvelle version de `exe`.
pub fn install(release: &Release, exe: &Path) -> Result<()> {
    let new = with_suffix(exe, "new");
    let old = with_suffix(exe, "old");
    models::download_url(&release.url, &new, Some(release.size), &mut |_, _| {})?;
    let _ = fs::remove_file(&old);
    fs::rename(exe, &old).context("mise de côté de l'ancienne version")?;
    if let Err(e) = fs::rename(&new, exe) {
        // On remet l'ancienne version en place : Plume doit toujours pouvoir démarrer.
        let _ = fs::rename(&old, exe);
        return Err(e).context("installation de la nouvelle version");
    }
    Ok(())
}

/// Supprime la version précédente laissée par une mise à jour.
pub fn cleanup(exe: &Path) {
    let _ = fs::remove_file(with_suffix(exe, "old"));
    let _ = fs::remove_file(with_suffix(exe, "new"));
}

fn with_suffix(p: &Path, suffix: &str) -> PathBuf {
    let mut s = p.as_os_str().to_owned();
    s.push(".");
    s.push(suffix);
    PathBuf::from(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    const JSON: &str = r#"{
        "tag_name": "v2.1.0", "draft": false, "prerelease": false,
        "assets": [
            {"name": "plume-windows-x64.exe", "browser_download_url": "https://exemple/gpu.exe", "size": 10},
            {"name": "plume-windows-x64-cpu.exe", "browser_download_url": "https://exemple/cpu.exe", "size": 9}
        ]}"#;

    #[test]
    fn versions_are_compared_numerically() {
        assert_eq!(parse_version("v2.10.1"), Some((2, 10, 1)));
        assert_eq!(parse_version("plume-v2.1"), Some((2, 1, 0)));
        assert_eq!(parse_version("2.0.0-beta"), Some((2, 0, 0)));
        assert_eq!(parse_version("nightly"), None);
        assert!(parse_version("v2.10.0") > parse_version("v2.9.9"));
    }

    #[test]
    fn picks_newer_release_with_matching_asset() {
        let r = pick(JSON, "2.0.0", "plume-windows-x64-cpu.exe").unwrap().unwrap();
        assert_eq!(r.url, "https://exemple/cpu.exe");
        assert_eq!(r.size, 9);
        assert!(pick(JSON, "2.1.0", "plume-windows-x64.exe").unwrap().is_none(), "déjà à jour");
        assert!(pick(JSON, "3.0.0", "plume-windows-x64.exe").unwrap().is_none());
        assert!(pick(JSON, "2.0.0", "autre.exe").unwrap().is_none());
    }

    #[test]
    fn ignores_prereleases() {
        let json = JSON.replace("\"prerelease\": false", "\"prerelease\": true");
        assert!(pick(&json, "2.0.0", "plume-windows-x64.exe").unwrap().is_none());
    }

    #[test]
    fn suffix_keeps_extension() {
        assert_eq!(with_suffix(Path::new("C:/Plume/plume.exe"), "old"), PathBuf::from("C:/Plume/plume.exe.old"));
    }
}
