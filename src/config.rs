//! Réglages de Plume, lus dans `plume.toml`.
//!
//! Le fichier est créé avec des commentaires au premier lancement. Une erreur
//! dans le fichier n'empêche jamais Plume de démarrer : seuls les réglages
//! concernés reprennent leur valeur par défaut (les autres sont gardés) et un
//! avertissement lisible est renvoyé.

use crate::hotkey::Hotkey;
use crate::trigger::TriggerMode;
use serde::Deserialize;
use serde::de::DeserializeOwned;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

/// Codes de langue connus de Whisper (dans l'ordre de whisper.cpp). Un code
/// inconnu ne ferait rien entendre au moteur, sans message d'erreur.
pub const LANGUAGES: &[&str] = &[
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl", "ca", "nl", "ar", "sv", "it", "id", "hi", "fi",
    "vi", "he", "uk", "el", "ms", "cs", "ro", "da", "hu", "ta", "no", "th", "ur", "hr", "bg", "lt", "la", "mi", "ml",
    "cy", "sk", "te", "fa", "lv", "bn", "sr", "az", "sl", "kn", "et", "mk", "br", "eu", "is", "hy", "ne", "mn", "bs",
    "kk", "sq", "sw", "gl", "mr", "pa", "si", "km", "sn", "yo", "so", "af", "oc", "ka", "be", "tg", "sd", "gu", "am",
    "yi", "lo", "uz", "fo", "ht", "ps", "tk", "nn", "mt", "sa", "lb", "my", "bo", "tl", "mg", "as", "tt", "haw", "ln",
    "ha", "ba", "jw", "su", "yue",
];

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
    /// Lancer Plume à l'ouverture de la session Windows.
    pub lancer_au_demarrage: bool,
}

impl Default for MiscConfig {
    fn default() -> Self {
        MiscConfig { historique: true, mise_a_jour_auto: true, lancer_au_demarrage: true }
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
    /// et la liste des problèmes rencontrés (vide si tout va bien). Un fichier
    /// illisible donne les réglages par défaut.
    pub fn parse(text: &str) -> (Config, Vec<String>) {
        match Config::try_parse(text) {
            Ok(result) => result,
            Err(e) => (Config::default(), vec![format!("{e} ; réglages par défaut utilisés")]),
        }
    }

    /// Comme [`Config::parse`], mais signale par `Err` un fichier illisible
    /// dans son ensemble (guillemet oublié, crochet non fermé…). Une clé
    /// inconnue ou une valeur refusée n'écarte que la ligne concernée.
    pub fn try_parse(text: &str) -> Result<(Config, Vec<String>), String> {
        let table: toml::Table = text.parse().map_err(|e: toml::de::Error| {
            let line = e.span().map(|s| text[..s.start.min(text.len())].lines().count().max(1));
            match line {
                Some(n) => format!("réglages illisibles (ligne {n} : {})", e.message()),
                None => format!("réglages illisibles ({})", e.message()),
            }
        })?;
        let mut warnings = Vec::new();
        let mut cfg = from_table(table, &mut warnings);
        cfg.validate(&mut warnings);
        Ok((cfg, warnings))
    }

    /// Ramène les valeurs hors limites à une valeur utilisable, en le signalant.
    fn validate(&mut self, warnings: &mut Vec<String>) {
        if let Err(e) = Hotkey::parse(&self.raccourci.touches) {
            warnings.push(format!("[raccourci] touches : {e} ; « {} » utilisé", ShortcutConfig::default().touches));
            self.raccourci.touches = ShortcutConfig::default().touches;
        }
        if !(1..=8).contains(&self.transcription.precision) {
            warnings.push(format!(
                "[transcription] precision = {} : doit être entre 1 et 8 ; ramenée à {}",
                self.transcription.precision,
                self.beam_size()
            ));
            self.transcription.precision = self.beam_size();
        }
        let lang = self.transcription.langue.trim().to_lowercase();
        if lang == "auto" || LANGUAGES.contains(&lang.as_str()) {
            self.transcription.langue = lang;
        } else {
            warnings.push(format!(
                "[transcription] langue = « {} » : attendu un code comme \"fr\", \"en\" ou \"auto\" ; \"fr\" utilisé",
                self.transcription.langue
            ));
            self.transcription.langue = "fr".into();
        }
        if self.transcription.modele.trim().is_empty() {
            warnings.push("[transcription] modele vide ; \"large-v3-turbo\" utilisé".into());
            self.transcription.modele = TranscriptionConfig::default().modele;
        }
        let kept = crate::text::vocabulary_within_budget(&self.transcription.vocabulaire).len();
        let total = self.transcription.vocabulaire.iter().filter(|w| !w.trim().is_empty()).count();
        if kept < total {
            let ignored: Vec<&str> =
                self.transcription.vocabulaire.iter().map(|w| w.trim()).filter(|w| !w.is_empty()).skip(kept).collect();
            warnings.push(format!(
                "[transcription] vocabulaire trop long : {} mot(s) ignoré(s) ({}). Retire des mots courants ou place les plus utiles en premier",
                ignored.len(),
                ignored.join(", ")
            ));
        }
        if !(100..=2_000).contains(&self.raccourci.appui_long_ms) {
            let default = ShortcutConfig::default().appui_long_ms;
            warnings.push(format!(
                "[raccourci] appui_long_ms = {} : doit être entre 100 et 2000 ; {default} utilisé",
                self.raccourci.appui_long_ms
            ));
            self.raccourci.appui_long_ms = default;
        }
    }

    /// Lit `path` ; le crée avec le modèle commenté s'il n'existe pas. Un
    /// fichier illisible donne les réglages par défaut (premier chargement).
    pub fn load_or_create(path: &Path) -> (Config, Vec<String>) {
        match Config::read(path) {
            Ok(result) => result,
            Err(e) => (Config::default(), vec![format!("{e} ; réglages par défaut utilisés")]),
        }
    }

    /// Relit `path` pendant que Plume tourne. Un fichier illisible ne change
    /// rien : `current` est gardé (pas de retour surprise au modèle ou au
    /// raccourci par défaut pour une faute de frappe).
    pub fn reload(path: &Path, current: &Config) -> (Config, Vec<String>) {
        match Config::read(path) {
            Ok(result) => result,
            Err(e) => (current.clone(), vec![format!("{e} ; réglages précédents conservés")]),
        }
    }

    fn read(path: &Path) -> Result<(Config, Vec<String>), String> {
        if !path.exists()
            && let Err(e) = fs::write(path, DEFAULT_CONFIG_TOML)
        {
            return Ok((
                Config::parse(DEFAULT_CONFIG_TOML).0,
                vec![format!("impossible de créer {} : {e}", path.display())],
            ));
        }
        let bytes = fs::read(path).map_err(|e| format!("impossible de lire {} : {e}", path.display()))?;
        Config::try_parse(&decode(&bytes))
    }
}

/// Texte d'un fichier de réglages, quel que soit l'encodage choisi par
/// l'éditeur : UTF-8 (avec ou sans BOM), UTF-16 (« Unicode » du Bloc-notes) ou,
/// à défaut, l'ancien encodage occidental de Windows (« ANSI »).
fn decode(bytes: &[u8]) -> String {
    let utf16 = |units: &mut dyn Iterator<Item = u16>| String::from_utf16_lossy(&units.collect::<Vec<u16>>());
    match bytes {
        [0xFF, 0xFE, rest @ ..] => utf16(&mut rest.chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]]))),
        [0xFE, 0xFF, rest @ ..] => utf16(&mut rest.chunks_exact(2).map(|c| u16::from_be_bytes([c[0], c[1]]))),
        _ => match std::str::from_utf8(bytes) {
            Ok(text) => text.trim_start_matches('\u{feff}').to_string(),
            Err(_) => bytes.iter().map(|&b| windows_1252(b)).collect(),
        },
    }
}

/// Un octet de l'encodage occidental de Windows (Latin-1, sauf 0x80 à 0x9F).
fn windows_1252(byte: u8) -> char {
    const HIGH: [char; 32] = [
        '€', '\u{81}', '‚', 'ƒ', '„', '…', '†', '‡', 'ˆ', '‰', 'Š', '‹', 'Œ', '\u{8d}', 'Ž', '\u{8f}', '\u{90}', '‘',
        '’', '“', '”', '•', '–', '—', '˜', '™', 'š', '›', 'œ', '\u{9d}', 'ž', 'Ÿ',
    ];
    match byte {
        0x80..=0x9F => HIGH[(byte - 0x80) as usize],
        _ => char::from(byte),
    }
}

/// Construit la config à partir du fichier analysé, en écartant (avec un
/// avertissement) chaque section, clé ou valeur que Plume ne comprend pas.
fn from_table(mut root: toml::Table, warnings: &mut Vec<String>) -> Config {
    // Cas courant : tout est valide.
    if let Ok(cfg) = root.clone().try_into::<Config>() {
        return cfg;
    }
    let names: Vec<String> = root.keys().cloned().collect();
    for name in names {
        let Some(toml::Value::Table(section)) = root.get_mut(&name) else {
            warnings.push(format!("« {name} » est écrit hors de toute section : ligne ignorée"));
            root.remove(&name);
            continue;
        };
        match name.as_str() {
            "raccourci" => keep_valid::<ShortcutConfig>(&name, section, warnings),
            "transcription" => keep_valid::<TranscriptionConfig>(&name, section, warnings),
            "sortie" => keep_valid::<OutputConfig>(&name, section, warnings),
            "interface" => keep_valid::<UiConfig>(&name, section, warnings),
            "audio" => keep_valid::<AudioConfig>(&name, section, warnings),
            "divers" => keep_valid::<MiscConfig>(&name, section, warnings),
            "corrections" => section.retain(|wrong, right| {
                let ok = right.is_str();
                if !ok {
                    warnings.push(format!(
                        "[corrections] « {wrong} » : la correction doit être un texte entre guillemets ; ligne ignorée"
                    ));
                }
                ok
            }),
            _ => {
                warnings.push(format!("section [{name}] inconnue : ignorée"));
                root.remove(&name);
            }
        }
    }
    root.try_into::<Config>().unwrap_or_else(|e| {
        warnings.push(format!("réglages illisibles ({}) ; réglages par défaut utilisés", e.message()));
        Config::default()
    })
}

/// Retire de `section` les clés que le type `T` ne connaît pas ou dont la
/// valeur ne convient pas : chacune est essayée seule.
fn keep_valid<T: DeserializeOwned>(name: &str, section: &mut toml::Table, warnings: &mut Vec<String>) {
    section.retain(|key, value| {
        let mut alone = toml::Table::new();
        alone.insert(key.to_string(), value.clone());
        match alone.try_into::<T>() {
            Ok(_) => true,
            Err(e) => {
                let reason = e.message();
                warnings.push(if reason.starts_with("unknown field") {
                    format!("[{name}] {key} : réglage inconnu (faute de frappe ?) ; ligne ignorée")
                } else {
                    format!("[{name}] {key} = {value} : {} ; valeur par défaut utilisée", explain(reason))
                });
                false
            }
        }
    });
}

/// Traduit les refus les plus courants de l'analyseur TOML.
fn explain(reason: &str) -> String {
    if let Some((_, choices)) = reason.split_once("expected one of ") {
        return format!("choix possibles : {}", choices.replace('`', "\""));
    }
    let expected = match reason.rsplit_once("expected ").map(|(_, e)| e) {
        Some("a boolean") => "true ou false",
        Some("a string") => "un texte entre guillemets",
        Some("a sequence") => "une liste entre crochets",
        Some(e) if e.starts_with(['u', 'i']) && e[1..].parse::<u32>().is_ok() => "un nombre entier positif",
        _ => return format!("valeur refusée ({reason})"),
    };
    format!("attendu : {expected}")
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
            [divers]
            lancer_au_demarrage = false
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
        assert!(!cfg.divers.lancer_au_demarrage);
        assert!(Config::default().divers.lancer_au_demarrage);
    }

    #[test]
    fn typo_in_key_is_reported_not_fatal() {
        let (cfg, w) = Config::parse("[raccourci]\ntouche = \"F9\"\n");
        assert_eq!(w.len(), 1);
        assert!(w[0].contains("touche"), "{}", w[0]);
        assert_eq!(cfg, Config::default());
    }

    #[test]
    fn one_bad_line_does_not_reset_the_other_settings() {
        let (cfg, w) = Config::parse(
            r#"
            touches = "F9"
            [raccourci]
            touches = "Ctrl+Souris4"
            mode = "appuyer"
            [transcription]
            modele = "small"
            gpu = "oui"
            precision = "haute"
            vocabulaire = "GitHub"
            [divers]
            historiqe = false
            lancer_au_demarrage = false
            [sorti]
            mode = "taper"
            [corrections]
            "git eub" = "GitHub"
            "nombre" = 3
            "#,
        );
        // Les lignes valides sont prises en compte…
        assert_eq!(cfg.raccourci.touches, "Ctrl+Souris4");
        assert_eq!(cfg.transcription.modele, "small");
        assert!(!cfg.divers.lancer_au_demarrage);
        assert_eq!(cfg.corrections.len(), 1);
        // … les autres reprennent leur valeur par défaut, une par une.
        assert_eq!(cfg.raccourci.mode, TriggerMode::Auto);
        assert!(cfg.transcription.gpu);
        assert_eq!(cfg.transcription.precision, 5);
        assert!(cfg.transcription.vocabulaire.is_empty());
        assert!(cfg.divers.historique);
        assert_eq!(cfg.sortie.mode, OutputMode::Paste);
        assert_eq!(w.len(), 8, "{w:#?}");
        let all = w.join("\n");
        for expected in [
            "« touches » est écrit hors de toute section",
            "[raccourci] mode = \"appuyer\" : choix possibles : \"auto\", \"maintenir\", \"basculer\"",
            "[transcription] gpu = \"oui\" : attendu : true ou false",
            "[transcription] precision = \"haute\" : attendu : un nombre entier positif",
            "[transcription] vocabulaire = \"GitHub\" : attendu : une liste entre crochets",
            "[divers] historiqe : réglage inconnu",
            "section [sorti] inconnue",
            "[corrections] « nombre »",
        ] {
            assert!(all.contains(expected), "« {expected} » absent de :\n{all}");
        }
    }

    #[test]
    fn unreadable_file_is_an_error_with_its_line() {
        let e = Config::try_parse("[raccourci]\ntouches = \"F9\n").unwrap_err();
        assert!(e.starts_with("réglages illisibles (ligne 2"), "{e}");
        let (cfg, w) = Config::parse("[raccourci\n");
        assert_eq!(cfg, Config::default());
        assert_eq!(w.len(), 1);
        assert!(w[0].ends_with("réglages par défaut utilisés"), "{}", w[0]);
    }

    #[test]
    fn unknown_language_code_is_rejected() {
        for bad in ["xx", "français", "fra", ""] {
            let (cfg, w) = Config::parse(&format!("[transcription]\nlangue = \"{bad}\"\n"));
            assert_eq!(cfg.transcription.langue, "fr", "{bad}");
            assert_eq!(w.len(), 1, "{bad} : {w:?}");
        }
        for good in ["fr", " EN ", "auto", "yue", "haw"] {
            let (cfg, w) = Config::parse(&format!("[transcription]\nlangue = \"{good}\"\n"));
            assert!(w.is_empty(), "{good} : {w:?}");
            assert_eq!(cfg.transcription.langue, good.trim().to_lowercase());
        }
    }

    #[test]
    fn hidden_settings_are_checked_too() {
        let (cfg, w) = Config::parse("[raccourci]\nappui_long_ms = 5\n[transcription]\nthreads = 4\n");
        assert_eq!(cfg.raccourci.appui_long_ms, 350);
        assert_eq!(cfg.transcription.threads, 4);
        assert_eq!(w.len(), 1, "{w:?}");
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

    #[test]
    fn reads_the_encodings_of_windows_editors() {
        let text = "[corrections]\n\"éléphant\" = \"Éléphant ’ œ\"\n";
        let expected = Config::parse(text).0;
        assert_eq!(expected.corrections["éléphant"], "Éléphant ’ œ");
        let utf16_le: Vec<u8> =
            [0xFF, 0xFE].into_iter().chain(text.encode_utf16().flat_map(u16::to_le_bytes)).collect();
        let utf16_be: Vec<u8> =
            [0xFE, 0xFF].into_iter().chain(text.encode_utf16().flat_map(u16::to_be_bytes)).collect();
        // « ANSI » (Windows-1252) : é = E9, É = C9, ’ = 92, œ = 9C.
        let ansi = b"[corrections]\n\"\xE9l\xE9phant\" = \"\xC9l\xE9phant \x92 \x9C\"\n";
        for bytes in [text.as_bytes(), &utf16_le, &utf16_be, ansi] {
            let (cfg, w) = Config::try_parse(&decode(bytes)).unwrap();
            assert!(w.is_empty(), "{w:?}");
            assert_eq!(cfg, expected);
        }
    }

    #[test]
    fn reload_keeps_current_settings_when_the_file_is_unreadable() {
        let dir = std::env::temp_dir().join("plume-test-config-reload");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("plume.toml");
        fs::write(&path, "[transcription]\nmodele = \"small\"\n").unwrap();
        let (current, w) = Config::load_or_create(&path);
        assert!(w.is_empty());
        // Guillemet oublié : le fichier n'est plus lisible.
        fs::write(&path, "[transcription]\nmodele = \"tiny\n").unwrap();
        let (cfg, w) = Config::reload(&path, &current);
        assert_eq!(cfg, current, "les réglages en cours sont gardés");
        assert_eq!(w.len(), 1);
        assert!(w[0].ends_with("réglages précédents conservés"), "{}", w[0]);
        // Au démarrage, il n'y a rien à conserver : réglages par défaut.
        let (cfg, w) = Config::load_or_create(&path);
        assert_eq!(cfg, Config::default());
        assert_eq!(w.len(), 1);
        // Une simple valeur refusée n'empêche pas de prendre le reste en compte.
        fs::write(&path, "[transcription]\nmodele = \"tiny\"\ngpu = 3\n").unwrap();
        let (cfg, w) = Config::reload(&path, &current);
        assert_eq!(cfg.transcription.modele, "tiny");
        assert_eq!(w.len(), 1);
        let _ = fs::remove_dir_all(dir);
    }
}
