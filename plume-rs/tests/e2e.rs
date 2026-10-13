//! Test de bout en bout : de la vraie parole française (LibriVox, domaine
//! public) passe par le découpage, le moteur Whisper et le nettoyage.
//!
//! Lancé seulement si `PLUME_TEST_MODELE` indique un modèle, par exemple :
//! `PLUME_TEST_MODELE=modeles/ggml-tiny.bin cargo test --release`

#![cfg(feature = "moteur")]

use plume_core::audio_file::read_wav;
use plume_core::engine::{Engine, EngineSettings};
use plume_core::pipeline::{EngineHandle, Transcriber};
use plume_core::segmenter::SegmenterConfig;
use plume_core::text::{Corrections, finalize};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

#[test]
fn transcribes_french_speech_in_chunks() {
    let Some(model) = std::env::var_os("PLUME_TEST_MODELE") else {
        eprintln!("PLUME_TEST_MODELE absent : test de bout en bout ignoré");
        return;
    };
    let wav = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/data/librivox-fr-12s.wav");
    let audio = read_wav(&wav).expect("lecture du WAV de test");
    let settings = EngineSettings {
        model_path: PathBuf::from(model),
        use_gpu: false,
        language: "fr".into(),
        beam_size: 1,
        threads: 0,
    };
    let engine = EngineHandle::spawn(
        Box::new(move || Engine::load(&settings).map(|e| Box::new(e) as Box<dyn Transcriber>)),
        Arc::new(|_| {}),
    );
    let mut dictation = engine.start(audio.sample_rate, vec![], SegmenterConfig::default(), None);
    // Blocs de 10 ms, comme un micro.
    for block in audio.samples.chunks((audio.sample_rate / 100) as usize) {
        dictation.feed(block);
    }
    let result = dictation.finish().wait(Duration::from_secs(600)).expect("transcription");
    assert!(result.errors.is_empty(), "{:?}", result.errors);
    let text = finalize(&result.text, &Corrections::default());
    let lower = text.to_lowercase();
    assert!(lower.contains("chapitre") && lower.contains("préface"), "texte inattendu : {text}");
    assert!(text.ends_with('.'), "{text}");
}
