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
    /// Description lisible : « carte graphique NVIDIA… » ou « processeur ».
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

        // Sans carte graphique reconnue, whisper.cpp passerait sur le processeur
        // sans rien dire : on le sait d'avance, pour l'annoncer tel quel.
        let gpu_name = if settings.use_gpu { gpu_device() } else { None };
        if settings.use_gpu && gpu_name.is_none() {
            if cfg!(any(feature = "vulkan", feature = "cuda")) {
                log::warn!("aucune carte graphique utilisable (pilote à jour ?) : transcription sur le processeur");
            } else {
                log::info!("cette variante de Plume n'utilise pas la carte graphique");
            }
        }
        let attempts: &[bool] = if gpu_name.is_some() { &[true, false] } else { &[false] };
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
                        backend: match &gpu_name {
                            Some(name) if gpu => format!("carte graphique {name}"),
                            _ => "processeur".into(),
                        },
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

/// Version de whisper.cpp, jeux d'instructions utilisés (AVX2…) et carte
/// graphique reconnue, pour le diagnostic.
pub fn system_info() -> String {
    LOGGING.call_once(whisper_rs::install_logging_hooks);
    format!(
        "whisper.cpp {} — {} carte graphique : {}",
        whisper_rs::get_whisper_version(),
        whisper_rs::print_system_info().trim(),
        gpu_device().as_deref().unwrap_or("aucune")
    )
}

/// La carte graphique que whisper.cpp utiliserait : la première que son
/// moteur de calcul (ggml) reconnaît. `None` s'il n'y en a pas (pilote Vulkan
/// absent, machine virtuelle, variante compilée sans carte graphique…).
pub fn gpu_device() -> Option<String> {
    use whisper_rs::whisper_rs_sys as sys;
    // SAFETY: lecture du registre des périphériques de ggml, créé au premier
    // appel et jamais libéré ; les chaînes renvoyées vivent autant que lui.
    unsafe {
        (0..sys::ggml_backend_dev_count()).map(|i| sys::ggml_backend_dev_get(i)).find_map(|dev| {
            let kind = sys::ggml_backend_dev_type(dev);
            let is_gpu = kind == sys::ggml_backend_dev_type_GGML_BACKEND_DEVICE_TYPE_GPU
                || kind == sys::ggml_backend_dev_type_GGML_BACKEND_DEVICE_TYPE_IGPU;
            if !is_gpu {
                return None;
            }
            let text = |p: *const std::ffi::c_char| {
                if p.is_null() {
                    String::new()
                } else {
                    std::ffi::CStr::from_ptr(p).to_string_lossy().trim().to_string()
                }
            };
            let (name, description) =
                (text(sys::ggml_backend_dev_name(dev)), text(sys::ggml_backend_dev_description(dev)));
            Some(if description.is_empty() { name } else { description })
        })
    }
}

fn thread_count(setting: u32) -> i32 {
    if setting > 0 {
        return setting as i32;
    }
    let n = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4);
    // Au-delà de 8, whisper.cpp ne gagne presque plus rien.
    n.clamp(1, 8) as i32
}

#[cfg(test)]
mod tests {
    use crate::config::LANGUAGES;

    /// Les codes acceptés dans `plume.toml` sont exactement ceux de whisper.cpp.
    #[test]
    fn config_languages_match_whisper() {
        for (id, code) in LANGUAGES.iter().enumerate() {
            assert_eq!(whisper_rs::get_lang_id(code), Some(id as i32), "{code}");
        }
        assert_eq!(LANGUAGES.len() as i32, whisper_rs::get_lang_max_id() + 1);
    }

    /// Une variante sans carte graphique n'en annonce jamais ; les autres
    /// répondent sans planter, qu'une carte soit présente ou non.
    #[test]
    fn gpu_device_is_consistent_with_the_build() {
        let device = super::gpu_device();
        if !cfg!(any(feature = "vulkan", feature = "cuda")) {
            assert_eq!(device, None);
        }
        assert!(super::system_info().contains("carte graphique : "));
    }
}
