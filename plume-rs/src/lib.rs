//! Cœur de Plume : tout ce qui ne dépend pas de Windows.
//!
//! - [`config`] : lecture du fichier `plume.toml`.
//! - [`hotkey`] : analyse du raccourci écrit dans la config (« Ctrl+Alt+Espace »).
//! - [`trigger`] : logique « appui court = bascule, appui long = maintenir ».
//! - [`resample`] : conversion du son du micro vers 16 kHz (format de Whisper).
//! - [`segmenter`] : découpage de la parole aux pauses, pendant l'enregistrement.
//! - [`text`] : nettoyage du texte (hallucinations, répétitions, corrections).
//! - [`pipeline`] : une dictée complète, du son au texte final.
//! - [`engine`] : le moteur Whisper (whisper.cpp).
//! - [`models`] : catalogue et téléchargement des modèles.

pub mod audio_file;
pub mod config;
#[cfg(feature = "moteur")]
pub mod engine;
pub mod hotkey;
pub mod logging;
pub mod models;
pub mod paths;
pub mod pipeline;
pub mod resample;
pub mod segmenter;
pub mod text;
pub mod trigger;
pub mod update;

/// Fréquence d'échantillonnage attendue par Whisper (16 kHz mono).
pub const SAMPLE_RATE: u32 = 16_000;

/// Version de Plume (celle du `Cargo.toml`).
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
