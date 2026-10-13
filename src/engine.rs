//! Le moteur de transcription : Whisper via whisper.cpp.
//!
//! Le modèle est chargé une fois (carte graphique si possible, sinon
//! processeur), puis « chauffé » pour que la première dictée soit aussi rapide
//! que les suivantes.

use crate::text;
use anyhow::{Context, Result, anyhow};
use std::path::PathBuf;
use std::sync::Once;
use std::time::Instant;
use whisper_rs::{FullParams, SamplingStrategy, WhisperContext, WhisperContextParameters, WhisperState};

#[derive(Debug, Clone, PartialEq)]
pub struct EngineSettings {
    pub model_path: PathBuf,
    pub use_gpu: bool,
    /// Code de langue ("fr") ou "auto".
    pub language: String,
    /// 1 = décodage glouton, sinon taille du faisceau.
    pub beam_size: u32,
    /// 0 = automatique.
    pub threads: u32,
}

pub struct Engine {
    // L'état référence le contexte : il est déclaré en premier pour être libéré avant lui.
    state: WhisperState,
    _ctx: WhisperContext,
    settings: EngineSettings,
    /// Description lisible : « carte graphique » ou « processeur ».
    pub backend: String,
}

/// Résultat de la transcription d'un morceau.
#[derive(Debug, Clone, Default)]
pub struct Transcript {
    pub text: String,
    /// Segments écartés (silence, hallucination).
    pub dropped: usize,
}

static LOGGING: Once = Once::new();

impl Engine {
    pub fn load(settings: &EngineSettings) -> Result<Engine> {
        LOGGING.call_once(whisper_rs::install_logging_hooks);
        let path = settings
            .model_path
            .to_str()
            .ok_or_else(|| anyhow!("chemin de modèle invalide : {}", settings.model_path.display()))?;

        let attempts: &[bool] =
            if settings.use_gpu && cfg!(any(feature = "vulkan", feature = "cuda")) { &[true, false] } else { &[false] };
        let mut last_err = None;
        for &gpu in attempts {
            let t0 = Instant::now();
            let mut params = WhisperContextParameters::default();
            params.use_gpu(gpu);
            params.flash_attn(gpu);
            match WhisperContext::new_with_params(path, params)
                .map_err(|e| anyhow!("{e:?}"))
                .and_then(|ctx| ctx.create_state().map(|s| (ctx, s)).map_err(|e| anyhow!("{e:?}")))
            {
                Ok((ctx, state)) => {
                    let mut engine = Engine {
                        state,
                        _ctx: ctx,
                        settings: settings.clone(),
                        backend: if gpu { "carte graphique".into() } else { "processeur".into() },
                    };
                    match engine.warm_up() {
                        Ok(()) => {
                            log::info!("moteur : {}", system_info());
                            log::info!(
                                "modèle {} chargé sur {} en {:.1} s",
                                settings.model_path.display(),
                                engine.backend,
                                t0.elapsed().as_secs_f32()
                            );
                            return Ok(engine);
                        }
                        Err(e) => {
                            log::warn!("échec du premier passage sur {} : {e:#}", engine.backend);
                            last_err = Some(e);
                        }
                    }
                }
                Err(e) => {
                    log::warn!(
                        "chargement sur {} impossible : {e:#}",
                        if gpu { "carte graphique" } else { "processeur" }
                    );
                    last_err = Some(e);
                }
            }
        }
        Err(last_err.unwrap_or_else(|| anyhow!("échec du chargement")))
            .with_context(|| format!("chargement du modèle {}", settings.model_path.display()))
    }

    pub fn settings(&self) -> &EngineSettings {
        &self.settings
    }

    /// Transcrit un morceau (16 kHz mono). `prompt` oriente le style et donne
    /// le contexte (voir [`text::build_prompt`]).
    pub fn transcribe(&mut self, audio: &[f32], prompt: &str) -> Result<Transcript> {
        if audio.is_empty() {
            return Ok(Transcript::default());
        }
        let mut params = if self.settings.beam_size <= 1 {
            FullParams::new(SamplingStrategy::Greedy { best_of: 1 })
        } else {
            FullParams::new(SamplingStrategy::BeamSearch { beam_size: self.settings.beam_size as i32, patience: -1.0 })
        };
        let lang = self.settings.language.as_str();
        if lang == "auto" {
            params.set_language(None);
            params.set_detect_language(false);
        } else {
            params.set_language(Some(lang));
        }
        params.set_n_threads(thread_count(self.settings.threads));
        params.set_translate(false);
        // Chaque morceau est autonome : le contexte passe par l'amorce, que l'on
        // maîtrise (et nettoie), plutôt que par l'historique interne.
        params.set_no_context(true);
        params.set_single_segment(false);
        params.set_no_timestamps(false);
        params.set_suppress_blank(true);
        params.set_suppress_nst(true);
        params.set_print_special(false);
        params.set_print_progress(false);
        params.set_print_realtime(false);
        params.set_print_timestamps(false);
        params.set_temperature(0.0);
        params.set_temperature_inc(0.2);
        params.set_no_speech_thold(0.6);
        if !prompt.is_empty() {
            params.set_initial_prompt(prompt);
        }

        self.state.full(params, audio).map_err(|e| anyhow!("échec de la transcription : {e:?}"))?;

        let mut out = Transcript::default();
        let mut parts: Vec<String> = Vec::new();
        for segment in self.state.as_iter() {
            let t = segment.to_str_lossy().map(|c| c.trim().to_string()).unwrap_or_default();
            if t.is_empty() {
                continue;
            }
            if text::is_hallucination(&t) {
                log::debug!("segment écarté (hallucination) : {t}");
                out.dropped += 1;
                continue;
            }
            parts.push(t);
        }
        out.text = text::clean_chunk(&parts.join(" "));
        Ok(out)
    }

    /// Un premier passage court : initialise la carte graphique (compilation des
    /// shaders Vulkan, allocations) avant la première vraie dictée.
    fn warm_up(&mut self) -> Result<()> {
        let n = crate::SAMPLE_RATE as usize;
        let tone: Vec<f32> =
            (0..n).map(|i| 0.01 * (2.0 * std::f32::consts::PI * 220.0 * i as f32 / n as f32).sin()).collect();
        self.transcribe(&tone, "").map(|_| ())
    }
}

/// Version de whisper.cpp et jeux d'instructions utilisés (AVX2…), pour le diagnostic.
pub fn system_info() -> String {
    format!("whisper.cpp {} — {}", whisper_rs::get_whisper_version(), whisper_rs::print_system_info().trim())
}

fn thread_count(setting: u32) -> i32 {
    if setting > 0 {
        return setting as i32;
    }
    let n = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    // Au-delà de 8, whisper.cpp ne gagne presque plus rien.
    n.clamp(1, 8) as i32
}
