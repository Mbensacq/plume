//! Une dictée de bout en bout : son du micro → morceaux → texte.
//!
//! ```text
//!  micro (48 kHz…) ─► Resampler ─► Segmenter ─► file de morceaux ─► moteur (1 fil)
//!                                                                     │
//!                       texte assemblé ◄── collecteur (1 fil / dictée) ◄┘
//! ```
//!
//! Le moteur tourne dans son propre fil et traite les morceaux dans l'ordre,
//! pendant l'enregistrement. Le collecteur rassemble les textes et, à chaque
//! morceau reçu, appelle `on_progress` (Plume l'utilise pour écrire la dictée
//! en cours sur le disque : rien n'est perdu en cas d'arrêt brutal).

use crate::SAMPLE_RATE;
use crate::resample::Resampler;
use crate::segmenter::{Chunk, Segmenter, SegmenterConfig};
use crate::text;
use anyhow::Result;
use std::collections::{BTreeMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// Ce qui transforme un morceau de son en texte (le vrai moteur Whisper, ou
/// un faux moteur dans les tests).
pub trait Transcriber: Send {
    fn transcribe(&mut self, audio: &[f32], prompt: &str) -> Result<String>;
    /// « carte graphique », « processeur »…
    fn describe(&self) -> String;
}

#[cfg(feature = "moteur")]
impl Transcriber for crate::engine::Engine {
    fn transcribe(&mut self, audio: &[f32], prompt: &str) -> Result<String> {
        crate::engine::Engine::transcribe(self, audio, prompt).map(|t| t.text)
    }
    fn describe(&self) -> String {
        self.backend.clone()
    }
}

/// Fonction de chargement du moteur, appelée dans le fil du moteur (au
/// démarrage et à chaque rechargement). Elle peut télécharger le modèle.
pub type Loader = Box<dyn FnMut() -> Result<Box<dyn Transcriber>> + Send>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EngineEvent {
    Loading,
    Ready(String),
    Failed(String),
}

pub type EventSink = Arc<dyn Fn(EngineEvent) + Send + Sync>;
pub type ProgressFn = Box<dyn FnMut(&str) + Send>;

#[derive(Debug, Clone)]
pub struct ChunkResult {
    pub index: usize,
    pub text: Result<String, String>,
    pub audio_ms: u32,
    pub elapsed_ms: u32,
}

enum Job {
    Transcribe {
        session: u64,
        index: usize,
        audio: Vec<f32>,
        vocabulary: Arc<Vec<String>>,
        reply: Sender<CollectorMsg>,
    },
    Reload,
    Shutdown,
}

/// Le fil du moteur, partagé par toutes les dictées.
pub struct EngineHandle {
    tx: Sender<Job>,
    cancelled: Arc<Mutex<HashSet<u64>>>,
    next_session: AtomicU64,
    thread: Option<JoinHandle<()>>,
}

impl EngineHandle {
    pub fn spawn(loader: Loader, events: EventSink) -> EngineHandle {
        let (tx, rx) = mpsc::channel();
        let cancelled = Arc::new(Mutex::new(HashSet::new()));
        let c = cancelled.clone();
        let thread = thread::Builder::new()
            .name("moteur".into())
            .spawn(move || worker(rx, loader, events, c))
            .expect("création du fil du moteur");
        EngineHandle { tx, cancelled, next_session: AtomicU64::new(1), thread: Some(thread) }
    }

    /// Recharge le moteur (après un changement de modèle dans les réglages).
    pub fn reload(&self) {
        let _ = self.tx.send(Job::Reload);
    }

    /// Commence une dictée. `input_rate` : fréquence du son qui sera fourni.
    pub fn start(
        &self,
        input_rate: u32,
        vocabulary: Vec<String>,
        segmenter: SegmenterConfig,
        on_progress: Option<ProgressFn>,
    ) -> Dictation {
        let session = self.next_session.fetch_add(1, Ordering::Relaxed);
        let (ctx, crx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        thread::Builder::new()
            .name(format!("dictee-{session}"))
            .spawn(move || collector(crx, on_progress, done_tx))
            .expect("création du fil collecteur");
        Dictation {
            session,
            jobs: self.tx.clone(),
            cancelled: self.cancelled.clone(),
            collector: ctx,
            done_rx,
            resampler: Resampler::new(input_rate.max(1), SAMPLE_RATE),
            segmenter: Segmenter::new(segmenter),
            vocabulary: Arc::new(vocabulary),
            scratch: Vec::new(),
            sent: 0,
            input_samples: 0,
            input_rate: input_rate.max(1),
            speech_ms: 0,
        }
    }
}

impl EngineHandle {
    /// Arrête le moteur et attend la fin du morceau en cours.
    pub fn shutdown(mut self) {
        let _ = self.tx.send(Job::Shutdown);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Drop for EngineHandle {
    fn drop(&mut self) {
        // Sans attendre : un téléchargement ou une longue transcription ne doit
        // pas bloquer la fermeture de Plume.
        let _ = self.tx.send(Job::Shutdown);
    }
}

/// Une dictée en cours d'enregistrement.
pub struct Dictation {
    session: u64,
    jobs: Sender<Job>,
    cancelled: Arc<Mutex<HashSet<u64>>>,
    collector: Sender<CollectorMsg>,
    done_rx: Receiver<DictationResult>,
    resampler: Resampler,
    segmenter: Segmenter,
    vocabulary: Arc<Vec<String>>,
    scratch: Vec<f32>,
    sent: usize,
    input_samples: u64,
    input_rate: u32,
    speech_ms: u64,
}

impl Dictation {
    /// Ajoute du son mono à la fréquence donnée au démarrage.
    pub fn feed(&mut self, samples: &[f32]) {
        self.input_samples += samples.len() as u64;
        self.scratch.clear();
        self.resampler.process(samples, &mut self.scratch);
        let chunks = self.segmenter.push(&self.scratch);
        for c in chunks {
            self.send(c);
        }
    }

    /// Nombre de morceaux déjà envoyés au moteur.
    pub fn chunks_sent(&self) -> usize {
        self.sent
    }

    /// Durée enregistrée jusqu'ici.
    pub fn duration(&self) -> Duration {
        Duration::from_secs_f64(self.input_samples as f64 / self.input_rate as f64)
    }

    /// Fin de l'enregistrement : envoie le dernier morceau.
    pub fn finish(mut self) -> PendingDictation {
        self.scratch.clear();
        self.resampler.flush(&mut self.scratch);
        let rest = std::mem::take(&mut self.scratch);
        for c in self.segmenter.push(&rest) {
            self.send(c);
        }
        if let Some(c) = self.segmenter.finish() {
            self.send(c);
        }
        let _ = self.collector.send(CollectorMsg::Expect(self.sent));
        let audio = self.duration();
        PendingDictation {
            done_rx: self.done_rx,
            stopped_at: Instant::now(),
            chunks: self.sent,
            audio,
            speech_ms: self.speech_ms,
        }
    }

    /// Abandonne la dictée : rien ne sera écrit.
    pub fn cancel(self) {
        if let Ok(mut set) = self.cancelled.lock() {
            set.insert(self.session);
        }
        let _ = self.collector.send(CollectorMsg::Cancel);
    }

    fn send(&mut self, chunk: Chunk) {
        self.speech_ms += chunk.speech_ms as u64;
        let job = Job::Transcribe {
            session: self.session,
            index: self.sent,
            audio: chunk.samples,
            vocabulary: self.vocabulary.clone(),
            reply: self.collector.clone(),
        };
        self.sent += 1;
        let _ = self.jobs.send(job);
    }
}

/// Dictée arrêtée, dont on attend les derniers morceaux.
pub struct PendingDictation {
    done_rx: Receiver<DictationResult>,
    stopped_at: Instant,
    pub chunks: usize,
    pub audio: Duration,
    pub speech_ms: u64,
}

#[derive(Debug, Clone, Default)]
pub struct DictationResult {
    /// Texte assemblé, avant mise en forme finale et corrections.
    pub text: String,
    pub chunks: usize,
    /// Messages d'erreur des morceaux qui ont échoué.
    pub errors: Vec<String>,
    /// Temps de calcul total du moteur.
    pub compute: Duration,
    /// Délai entre l'arrêt et le texte prêt.
    pub latency: Duration,
}

impl PendingDictation {
    /// Attend le texte. `timeout` borne l'attente (moteur bloqué, etc.).
    pub fn wait(self, timeout: Duration) -> Result<DictationResult, String> {
        match self.done_rx.recv_timeout(timeout) {
            Ok(mut r) => {
                r.latency = self.stopped_at.elapsed();
                Ok(r)
            }
            Err(RecvTimeoutError::Timeout) => Err("la transcription prend trop de temps".into()),
            Err(RecvTimeoutError::Disconnected) => Err("le moteur de transcription s'est arrêté".into()),
        }
    }
}

enum CollectorMsg {
    Chunk(ChunkResult),
    Expect(usize),
    Cancel,
}

fn collector(rx: Receiver<CollectorMsg>, mut on_progress: Option<ProgressFn>, done: Sender<DictationResult>) {
    let mut results: BTreeMap<usize, ChunkResult> = BTreeMap::new();
    let mut expected: Option<usize> = None;
    let mut assembled_upto = 0usize;
    let mut assembled = String::new();
    loop {
        if expected.is_some_and(|n| results.len() >= n) {
            break;
        }
        match rx.recv() {
            Ok(CollectorMsg::Chunk(r)) => {
                results.insert(r.index, r);
                // Assemble la partie continue disponible et la signale.
                let before = assembled_upto;
                while let Some(r) = results.get(&assembled_upto) {
                    if let Ok(t) = &r.text {
                        text::join_chunk(&mut assembled, t);
                    }
                    assembled_upto += 1;
                }
                if assembled_upto > before
                    && let Some(f) = on_progress.as_mut()
                {
                    f(&assembled);
                }
            }
            Ok(CollectorMsg::Expect(n)) => expected = Some(n),
            Ok(CollectorMsg::Cancel) | Err(_) => return,
        }
    }
    let mut out = DictationResult { text: assembled, chunks: results.len(), ..Default::default() };
    for r in results.values() {
        out.compute += Duration::from_millis(r.elapsed_ms as u64);
        if let Err(e) = &r.text {
            out.errors.push(format!("morceau {} : {e}", r.index + 1));
        }
    }
    let _ = done.send(out);
}

fn worker(rx: Receiver<Job>, mut loader: Loader, events: EventSink, cancelled: Arc<Mutex<HashSet<u64>>>) {
    let mut engine: Option<Box<dyn Transcriber>> = None;
    let mut last_error = String::from("moteur pas encore chargé");
    let mut last_attempt: Option<Instant> = None;
    let mut context_session = 0u64;
    let mut context = String::new();

    let mut load =
        |engine: &mut Option<Box<dyn Transcriber>>, last_error: &mut String, last_attempt: &mut Option<Instant>| {
            *engine = None;
            *last_attempt = Some(Instant::now());
            events(EngineEvent::Loading);
            match loader() {
                Ok(e) => {
                    events(EngineEvent::Ready(e.describe()));
                    *engine = Some(e);
                }
                Err(e) => {
                    *last_error = format!("{e:#}");
                    log::error!("moteur indisponible : {last_error}");
                    events(EngineEvent::Failed(last_error.clone()));
                }
            }
        };

    load(&mut engine, &mut last_error, &mut last_attempt);

    for job in rx {
        match job {
            Job::Shutdown => break,
            Job::Reload => load(&mut engine, &mut last_error, &mut last_attempt),
            Job::Transcribe { session, index, audio, vocabulary, reply } => {
                if cancelled.lock().map(|s| s.contains(&session)).unwrap_or(false) {
                    continue;
                }
                // Échec précédent (réseau coupé pendant le téléchargement…) :
                // on retente, au plus toutes les 30 secondes.
                if engine.is_none() && last_attempt.is_none_or(|t| t.elapsed() > Duration::from_secs(30)) {
                    load(&mut engine, &mut last_error, &mut last_attempt);
                }
                if session != context_session {
                    context_session = session;
                    context.clear();
                }
                let audio_ms = (audio.len() as u64 * 1000 / SAMPLE_RATE as u64) as u32;
                let t0 = Instant::now();
                let result = match engine.as_mut() {
                    Some(e) => {
                        let prompt = text::build_prompt(&vocabulary, &context);
                        e.transcribe(&audio, &prompt).map_err(|err| format!("{err:#}"))
                    }
                    None => Err(last_error.clone()),
                };
                let elapsed_ms = t0.elapsed().as_millis() as u32;
                match &result {
                    Ok(t) => {
                        log::debug!(
                            "morceau {} : {:.1} s de son en {:.2} s ({} caractères)",
                            index + 1,
                            audio_ms as f32 / 1000.0,
                            elapsed_ms as f32 / 1000.0,
                            t.len()
                        );
                        text::join_chunk(&mut context, t);
                        if context.len() > 2_000 {
                            let cut = context.len() - 1_000;
                            let cut = (cut..context.len()).find(|i| context.is_char_boundary(*i)).unwrap_or(0);
                            context.drain(..cut);
                        }
                    }
                    Err(e) => log::error!("morceau {} : {e}", index + 1),
                }
                let _ = reply.send(CollectorMsg::Chunk(ChunkResult { index, text: result, audio_ms, elapsed_ms }));
            }
        }
    }
    if let Ok(mut set) = cancelled.lock() {
        set.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    /// Faux moteur : renvoie « morceau N » et note les amorces reçues.
    struct Fake {
        calls: Arc<AtomicUsize>,
        prompts: Arc<Mutex<Vec<String>>>,
        fail_on: Option<usize>,
    }

    impl Transcriber for Fake {
        fn transcribe(&mut self, audio: &[f32], prompt: &str) -> Result<String> {
            let n = self.calls.fetch_add(1, Ordering::SeqCst);
            self.prompts.lock().unwrap().push(prompt.to_string());
            if Some(n) == self.fail_on {
                anyhow::bail!("panne simulée");
            }
            Ok(format!("Morceau {} de {} ms", n + 1, audio.len() / 16))
        }
        fn describe(&self) -> String {
            "faux".into()
        }
    }

    type Probes = (EngineHandle, Arc<AtomicUsize>, Arc<Mutex<Vec<String>>>, Arc<Mutex<Vec<EngineEvent>>>);

    fn engine(fail_on: Option<usize>) -> Probes {
        let calls = Arc::new(AtomicUsize::new(0));
        let prompts = Arc::new(Mutex::new(Vec::new()));
        let events = Arc::new(Mutex::new(Vec::new()));
        let (c, p, ev) = (calls.clone(), prompts.clone(), events.clone());
        let handle = EngineHandle::spawn(
            Box::new(move || {
                Ok(Box::new(Fake { calls: c.clone(), prompts: p.clone(), fail_on }) as Box<dyn Transcriber>)
            }),
            Arc::new(move |e| ev.lock().unwrap().push(e)),
        );
        (handle, calls, prompts, events)
    }

    fn voice(ms: usize, rate: usize) -> Vec<f32> {
        (0..rate * ms / 1000)
            .map(|i| 0.15 * (2.0 * std::f32::consts::PI * 220.0 * i as f32 / rate as f32).sin())
            .collect()
    }

    fn quiet(ms: usize, rate: usize) -> Vec<f32> {
        vec![0.0005; rate * ms / 1000]
    }

    #[test]
    fn long_dictation_is_transcribed_while_recording() {
        let (eng, calls, prompts, events) = engine(None);
        let progress = Arc::new(Mutex::new(Vec::<String>::new()));
        let p2 = progress.clone();
        let mut d = eng.start(
            48_000,
            vec!["GitHub".into()],
            SegmenterConfig::default(),
            Some(Box::new(move |t: &str| p2.lock().unwrap().push(t.to_string()))),
        );
        // 3 phrases de 7 s séparées par des pauses, fournies par blocs de 10 ms.
        let mut audio = Vec::new();
        for _ in 0..3 {
            audio.extend(voice(7_000, 48_000));
            audio.extend(quiet(900, 48_000));
        }
        for block in audio.chunks(480) {
            d.feed(block);
        }
        assert!(d.chunks_sent() >= 2, "les morceaux partent pendant l'enregistrement");
        let pending = d.finish();
        let r = pending.wait(Duration::from_secs(10)).unwrap();
        assert_eq!(r.chunks, 3);
        assert!(r.errors.is_empty());
        assert!(r.text.starts_with("Morceau 1"), "{}", r.text);
        assert!(r.text.contains("Morceau 3"));
        assert_eq!(calls.load(Ordering::SeqCst), 3);
        // L'amorce contient le vocabulaire et, après le 1er morceau, le texte précédent.
        let prompts = prompts.lock().unwrap();
        assert!(prompts[0].starts_with("Vocabulaire : GitHub."));
        assert!(prompts[1].contains("Morceau 1"));
        assert!(progress.lock().unwrap().len() >= 3);
        assert_eq!(events.lock().unwrap()[..2], [EngineEvent::Loading, EngineEvent::Ready("faux".into())]);
    }

    #[test]
    fn failed_chunk_is_reported_but_others_kept() {
        let (eng, _, _, _) = engine(Some(1));
        let mut d = eng.start(16_000, vec![], SegmenterConfig::default(), None);
        for _ in 0..3 {
            d.feed(&voice(7_000, 16_000));
            d.feed(&quiet(900, 16_000));
        }
        let r = d.finish().wait(Duration::from_secs(10)).unwrap();
        assert_eq!(r.chunks, 3);
        assert_eq!(r.errors.len(), 1);
        assert!(r.text.contains("Morceau 1") && r.text.contains("Morceau 3"));
    }

    #[test]
    fn silent_dictation_gives_empty_text() {
        let (eng, calls, _, _) = engine(None);
        let mut d = eng.start(16_000, vec![], SegmenterConfig::default(), None);
        d.feed(&quiet(3_000, 16_000));
        let r = d.finish().wait(Duration::from_secs(5)).unwrap();
        assert_eq!(r.chunks, 0);
        assert!(r.text.is_empty());
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn cancelled_dictation_is_not_transcribed() {
        let (eng, _, _, _) = engine(None);
        let mut d = eng.start(16_000, vec![], SegmenterConfig::default(), None);
        d.feed(&voice(2_000, 16_000));
        d.cancel();
        // La dictée suivante fonctionne normalement.
        let mut d = eng.start(16_000, vec![], SegmenterConfig::default(), None);
        d.feed(&voice(2_000, 16_000));
        let r = d.finish().wait(Duration::from_secs(5)).unwrap();
        assert_eq!(r.chunks, 1);
        assert!(!r.text.is_empty());
    }

    #[test]
    fn load_failure_is_reported_on_each_chunk() {
        let events = Arc::new(Mutex::new(Vec::new()));
        let ev = events.clone();
        let eng = EngineHandle::spawn(
            Box::new(|| anyhow::bail!("modèle introuvable")),
            Arc::new(move |e| ev.lock().unwrap().push(e)),
        );
        let mut d = eng.start(16_000, vec![], SegmenterConfig::default(), None);
        d.feed(&voice(2_000, 16_000));
        let r = d.finish().wait(Duration::from_secs(5)).unwrap();
        assert_eq!(r.errors.len(), 1);
        assert!(r.errors[0].contains("modèle introuvable"));
        assert!(matches!(events.lock().unwrap()[1], EngineEvent::Failed(_)));
    }
}
