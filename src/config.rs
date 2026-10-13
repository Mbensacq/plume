//! Réglages de Plume, lus dans `plume.toml`.
//!
//! Le fichier est créé avec des commentaires au premier lancement. Une erreur
//! dans le fichier n'empêche jamais Plume de démarrer : les réglages concernés
//! reprennent leur valeur par défaut et un avertissement lisible est renvoyé.

use crate::hotkey::Hotkey;
use crate::trigger::TriggerMode;
use serde::Deserialize;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

/// Modèle de fichier écrit au premier lancement (avec ses commentaires).
pub const DEFAULT_CONFIG_TOML: &str = include_str!("../plume.toml");

#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub raccourci: ShortcutConfig,
    pub transcription: TranscriptionConfig,
    pub sortie: OutputConfig,
    pub interface: UiConfig,
    pub audio: AudioConfig,
    pub divers: MiscConfig,
    pub corrections: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ShortcutConfig {
    pub touches: String,
    pub mode: TriggerMode,
    pub annuler_avec_echap: bool,
    /// Durée (ms) à partir de laquelle un appui est « long » en mode auto.
    pub appui_long_ms: u64,
}

impl Default for ShortcutConfig {
    fn default() -> Self {
        ShortcutConfig {
            touches: "Ctrl+Alt+Espace".into(),
            mode: TriggerMode::Auto,
            annuler_avec_echap: true,
            appui_long_ms: 350,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct TranscriptionConfig {
    pub modele: String,
    pub langue: String,
    pub gpu: bool,
    pub precision: u32,
    pub vocabulaire: Vec<String>,
    /// Nombre de cœurs du processeur utilisés (0 = automatique).
    pub threads: u32,
}

impl Default for TranscriptionConfig {
    fn default() -> Self {
        TranscriptionConfig {
            modele: "large-v3-turbo".into(),
            langue: "fr".into(),
            gpu: true,
            precision: 5,
            vocabulaire: Vec::new(),
            threads: 0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
pub enum OutputMode {
    #[default]
    #[serde(rename = "coller")]
    Paste,
    #[serde(rename = "taper")]
    Type,
    #[serde(rename = "presse-papiers")]
    Clipboard,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct OutputConfig {
    pub mode: OutputMode,
    pub espace_final: bool,
    pub restaurer_presse_papiers: bool,
}

impl Default for OutputConfig {
    fn default() -> Self {
        OutputConfig { mode: OutputMode::Paste, espace_final: true, restaurer_presse_papiers: true }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum IndicatorPosition {
    #[default]
    Bas,
    Haut,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct UiConfig {
    pub indicateur: bool,
    pub position: IndicatorPosition,
}

impl Default for UiConfig {
    fn default() -> Self {
        UiConfig { indicateur: true, position: IndicatorPosition::Bas }
    }
}

#[derive(Debug, Clone, PartialEq, Deserialize, Default)]
#[serde(default, deny_unknown_fields)]
pub struct AudioConfig {
    pub micro: String,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct MiscConfig {
    pub historique: bool,
    pub mise_a_jour_auto: bool,
}

impl Default for MiscConfig {
    fn default() -> Self {
        MiscConfig { historique: true, mise_a_jour_auto: true }
    }
}

impl Config {
    /// Le raccourci analysé (toujours valide après [`Config::parse`]).
    pub fn hotkey(&self) -> Hotkey {
        Hotkey::parse(&self.raccourci.touches)
            .unwrap_or_else(|_| Hotkey::parse(&ShortcutConfig::default().touches).expect("raccourci par défaut valide"))
    }

    /// Taille du faisceau de recherche Whisper (1 = glouton).
    pub fn beam_size(&self) -> u32 {
        self.transcription.precision.clamp(1, 8)
    }

    /// Analyse le contenu d'un fichier de réglages. Renvoie la config utilisable
    /// et la liste des problèmes rencontrés (vide si tout va bien).
    pub fn parse(text: &str) -> (Config, Vec<String>) {
        let mut warnings = Vec::new();
        let mut cfg = match toml::from_str::<Config>(text) {
            Ok(c) => c,
            Err(e) => {
                warnings.push(format!("réglages illisibles, valeurs par défaut utilisées : {}", e.to_string().trim()));
                Config::default()
            }
        };
        if let Err(e) = Hotkey::parse(&cfg.raccourci.touches) {
            warnings.push(format!("[raccourci] touches : {e} ; « {} » utilisé", ShortcutConfig::default().touches));
            cfg.raccourci.touches = ShortcutConfig::default().touches;
        }
        if !(1..=8).contains(&cfg.transcription.precision) {
            warnings.push(format!(
                "[transcription] precision = {} : doit être entre 1 et 8 ; ramenée à {}",
                cfg.transcription.precision,
                cfg.beam_size()
            ));
            cfg.transcription.precision = cfg.beam_size();
        }
        let lang = cfg.transcription.langue.trim().to_lowercase();
        if lang.is_empty() || (lang != "auto" && !(2..=3).contains(&lang.len())) {
            warnings.push(format!(
                "[transcription] langue = « {} » : attendu un code comme \"fr\" ou \"auto\" ; \"fr\" utilisé",
                cfg.transcription.langue
            ));
            cfg.transcription.langue = "fr".into();
        } else {
            cfg.transcription.langue = lang;
        }
        if cfg.transcription.modele.trim().is_empty() {
            warnings.push("[transcription] modele vide ; \"large-v3-turbo\" utilisé".into());
            cfg.transcription.modele = TranscriptionConfig::default().modele;
        }
        let kept = crate::text::vocabulary_within_budget(&cfg.transcription.vocabulaire).len();
        let total = cfg.transcription.vocabulaire.iter().filter(|w| !w.trim().is_empty()).count();
        if kept < total {
            let ignored: Vec<&str> =
                cfg.transcription.vocabulaire.iter().map(|w| w.trim()).filter(|w| !w.is_empty()).skip(kept).collect();
            warnings.push(format!(
                "[transcription] vocabulaire trop long : {} mot(s) ignoré(s) ({}). Retire des mots courants ou place les plus utiles en premier",
                ignored.len(),
                ignored.join(", ")
            ));
        }
        if !(100..=2_000).contains(&cfg.raccourci.appui_long_ms) {
            cfg.raccourci.appui_long_ms = ShortcutConfig::default().appui_long_ms;
        }
        (cfg, warnings)
    }

    /// Lit `path` ; le crée avec le modèle commenté s'il n'existe pas.
    pub fn load_or_create(path: &Path) -> (Config, Vec<String>) {
        if !path.exists() {
            if let Err(e) = fs::write(path, DEFAULT_CONFIG_TOML) {
                return (
                    Config::parse(DEFAULT_CONFIG_TOML).0,
                    vec![format!("impossible de créer {} : {e}", path.display())],
                );
            }
        }
        match fs::read_to_string(path) {
            Ok(text) => Config::parse(text.trim_start_matches('\u{feff}')),
            Err(e) => {
                (Config::parse(DEFAULT_CONFIG_TOML).0, vec![format!("impossible de lire {} : {e}", path.display())])
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn template_matches_defaults_except_examples() {
        let (cfg, warnings) = Config::parse(DEFAULT_CONFIG_TOML);
        assert!(warnings.is_empty(), "{warnings:?}");
        let mut expected = Config::default();
        expected.corrections.insert("discorde".into(), "Discord".into());
        assert_eq!(cfg, expected);
    }

    #[test]
    fn empty_file_gives_defaults() {
        let (cfg, w) = Config::parse("");
        assert!(w.is_empty());
        assert_eq!(cfg, Config::default());
        assert_eq!(cfg.hotkey().label(), "Ctrl + Alt + Espace");
    }

    #[test]
    fn reads_user_values() {
        let (cfg, w) = Config::parse(
            r#"
            [raccourci]
            touches = "Ctrl+Souris4"
            mode = "maintenir"
            [transcription]
            modele = "large-v3"
            langue = "AUTO"
            precision = 1
            vocabulaire = ["GitHub"]
            [sortie]
            mode = "presse-papiers"
            [interface]
            position = "haut"
            [corrections]
            "git eub" = "GitHub"
            "#,
        );
        assert!(w.is_empty(), "{w:?}");
        assert_eq!(cfg.raccourci.mode, TriggerMode::Maintenir);
        assert_eq!(cfg.hotkey().label(), "Ctrl + Souris 4");
        assert_eq!(cfg.transcription.langue, "auto");
        assert_eq!(cfg.beam_size(), 1);
        assert_eq!(cfg.sortie.mode, OutputMode::Clipboard);
        assert_eq!(cfg.interface.position, IndicatorPosition::Haut);
        assert_eq!(cfg.corrections.get("git eub").map(String::as_str), Some("GitHub"));
    }

    #[test]
    fn typo_in_key_is_reported_not_fatal() {
        let (cfg, w) = Config::parse("[raccourci]\ntouche = \"F9\"\n");
        assert_eq!(w.len(), 1);
        assert!(w[0].contains("touche"), "{}", w[0]);
        assert_eq!(cfg, Config::default());
    }

    #[test]
    fn bad_values_fall_back_individually() {
        let (cfg, w) = Config::parse(
            "[raccourci]\ntouches = \"Espace\"\n[transcription]\nprecision = 42\nlangue = \"français\"\n",
        );
        assert_eq!(w.len(), 3, "{w:?}");
        assert_eq!(cfg.raccourci.touches, "Ctrl+Alt+Espace");
        assert_eq!(cfg.transcription.precision, 8);
        assert_eq!(cfg.transcription.langue, "fr");
    }

    #[test]
    fn overlong_vocabulary_is_reported() {
        let words: Vec<String> = (0..60).map(|i| format!("\"Terme{i:02}\"")).collect();
        let (cfg, w) = Config::parse(&format!("[transcription]\nvocabulaire = [{}]\n", words.join(", ")));
        assert_eq!(cfg.transcription.vocabulaire.len(), 60, "la liste est gardée telle quelle");
        assert_eq!(w.len(), 1, "{w:?}");
        assert!(w[0].contains("Terme59"), "{}", w[0]);
    }

    #[test]
    fn creates_file_when_missing() {
        let dir = std::env::temp_dir().join("plume-test-config");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("plume.toml");
        let (cfg, w) = Config::load_or_create(&path);
        assert!(w.is_empty());
        assert!(path.exists());
        assert_eq!(cfg.transcription.modele, "large-v3-turbo");
        // Un BOM (éditeurs Windows) ne doit pas gêner.
        fs::write(&path, format!("\u{feff}{DEFAULT_CONFIG_TOML}")).unwrap();
        let (_, w) = Config::load_or_create(&path);
        assert!(w.is_empty(), "{w:?}");
        let _ = fs::remove_dir_all(dir);
    }
}
