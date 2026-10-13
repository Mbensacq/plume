//! Le chef d'orchestre : reçoit chaque événement (raccourci, son du micro,
//! moteur, menu) et décide quoi faire.

use super::audio::{self, Capture};
use super::hooks::{HookHandle, HookSettings};
use super::output::{self, Delivered};
use super::ui::{Phase, Tone, UiHandle};
use super::{Event, MenuCommand};
use plume_core::config::{Config, OutputConfig, TranscriptionConfig};
use plume_core::paths::Paths;
use plume_core::pipeline::{Dictation, EngineEvent, EngineHandle, Loader, PendingDictation};
use plume_core::segmenter::SegmenterConfig;
use plume_core::text::{self, Corrections};
use plume_core::trigger::{Action, TriggerMachine};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Résultat d'une dictée, renvoyé par le fil qui l'a terminée.
pub enum Outcome {
    Delivered { how: Delivered, warning: Option<String> },
    Empty,
    Failed(String),
}

struct Recording {
    session: u64,
    capture: Capture,
    dictation: Dictation,
    started: Instant,
}

struct Stopping {
    session: u64,
    dictation: Dictation,
}

pub struct Controller {
    paths: Paths,
    cfg: Arc<Mutex<Config>>,
    engine: EngineHandle,
    hooks: HookHandle,
    ui: UiHandle,
    tx: Sender<Event>,
    machine: TriggerMachine,
    recording: Option<Recording>,
    stopping: Vec<Stopping>,
    pending: usize,
    session: u64,
    engine_ready: bool,
    engine_error: Option<String>,
    last_text: Arc<Mutex<String>>,
    output_lock: Arc<Mutex<()>>,
    last_phase: Option<Phase>,
}

impl Controller {
    pub fn new(paths: Paths, cfg: Config, ui: UiHandle, hooks: HookHandle, tx: Sender<Event>) -> Controller {
        let machine = TriggerMachine::new(cfg.raccourci.mode, Duration::from_millis(cfg.raccourci.appui_long_ms));
        let cfg = Arc::new(Mutex::new(cfg));
        let events_tx = tx.clone();
        let engine = EngineHandle::spawn(
            make_loader(cfg.clone(), paths.clone(), tx.clone()),
            Arc::new(move |e| {
                let _ = events_tx.send(Event::Engine(e));
            }),
        );
        let last_text = fs::read_to_string(paths.last_dictation_file()).unwrap_or_default();
        Controller {
            paths,
            cfg,
            engine,
            hooks,
            ui,
            tx,
            machine,
            recording: None,
            stopping: Vec::new(),
            pending: 0,
            session: 0,
            engine_ready: false,
            engine_error: None,
            last_text: Arc::new(Mutex::new(last_text)),
            output_lock: Arc::new(Mutex::new(())),
            last_phase: None,
        }
    }

    pub fn run(mut self, rx: Receiver<Event>) {
        for event in rx.iter() {
            if !self.handle(event) {
                break;
            }
            self.refresh_phase();
        }
        self.hooks.stop();
        self.ui.close();
    }

    fn config(&self) -> Config {
        self.cfg.lock().map(|c| c.clone()).unwrap_or_default()
    }

    /// Renvoie `false` pour quitter.
    fn handle(&mut self, event: Event) -> bool {
        match event {
            Event::Hotkey(input) => match self.machine.on(input, Instant::now()) {
                Action::Start => self.start(),
                Action::Stop => self.stop(),
                Action::Cancel => self.cancel(),
                Action::Nothing => {}
            },
            Event::Audio { session, samples } => {
                if let Some(r) = self.recording.as_mut().filter(|r| r.session == session) {
                    r.dictation.feed(&samples);
                } else if let Some(s) = self.stopping.iter_mut().find(|s| s.session == session) {
                    s.dictation.feed(&samples);
                }
            }
            Event::AudioFailed { session, message } => {
                if self.recording.as_ref().is_some_and(|r| r.session == session) {
                    self.ui.flash(format!("Micro coupé : {message}"), Tone::Error);
                    self.stop();
                    self.machine.reset();
                }
            }
            Event::FinishRecording { session } => {
                if let Some(pos) = self.stopping.iter().position(|s| s.session == session) {
                    let s = self.stopping.remove(pos);
                    self.finish(s.dictation.finish());
                }
            }
            Event::Engine(e) => self.on_engine(e),
            Event::DownloadStarted { model, megabytes } => {
                self.ui.notify(
                    "Premier lancement de Plume",
                    format!("Téléchargement du modèle {model} ({megabytes} Mo). Tu peux déjà dicter : le texte arrivera dès qu'il sera prêt."),
                    false,
                );
                self.ui.update(|s| s.download = Some(0));
            }
            Event::DownloadProgress(p) => self.ui.update(|s| s.download = Some(p)),
            Event::DownloadFinished => {
                self.ui.update(|s| s.download = None);
                self.ui.flash("Modèle téléchargé", Tone::Info);
            }
            Event::DictationDone(outcome) => self.on_done(outcome),
            Event::UpdateInstalled(version) => self.ui.notify(
                "Mise à jour de Plume",
                format!("La version {version} est installée ; elle sera utilisée au prochain lancement de Plume."),
                false,
            ),
            Event::Menu(MenuCommand::CopyLast) => {
                let text = self.last_text.lock().map(|t| t.clone()).unwrap_or_default();
                if text.is_empty() {
                    self.ui.flash("Aucune dictée pour l'instant", Tone::Muted);
                } else {
                    match output::set_clipboard_text(&text) {
                        Ok(()) => self.ui.flash("Dernière dictée copiée", Tone::Info),
                        Err(e) => self.ui.flash(e, Tone::Error),
                    }
                }
            }
            Event::Menu(MenuCommand::Reload) => self.reload(),
            Event::Menu(MenuCommand::Quit) => {
                if self.recording.is_some() {
                    self.cancel();
                }
                return false;
            }
        }
        true
    }

    fn start(&mut self) {
        let cfg = self.config();
        self.session += 1;
        let session = self.session;
        let capture = match audio::start(&cfg.audio.micro, session, self.tx.clone()) {
            Ok(c) => c,
            Err(e) => {
                log::error!("micro : {e:#}");
                self.ui.flash(format!("{e:#}"), Tone::Error);
                self.machine.reset();
                return;
            }
        };
        let progress_path = self.paths.last_dictation_file();
        let dictation = self.engine.start(
            capture.sample_rate,
            cfg.transcription.vocabulaire.clone(),
            SegmenterConfig::default(),
            Some(Box::new(move |t: &str| {
                // Sauvegarde au fil de l'eau : rien n'est perdu si Plume s'arrête.
                let _ = fs::write(&progress_path, t);
            })),
        );
        log::info!("dictée {session} : début (micro « {} »)", capture.device);
        self.hooks.set_recording(true);
        self.recording = Some(Recording { session, capture, dictation, started: Instant::now() });
        self.ui.update(|s| s.record_started = Some(Instant::now()));
    }

    fn stop(&mut self) {
        let Some(r) = self.recording.take() else { return };
        self.hooks.set_recording(false);
        // Fermer le micro : son dernier son est déjà dans la file d'événements,
        // FinishRecording passera donc après lui.
        drop(r.capture);
        log::info!("dictée {} : arrêt après {:.1} s", r.session, r.started.elapsed().as_secs_f32());
        self.stopping.push(Stopping { session: r.session, dictation: r.dictation });
        let _ = self.tx.send(Event::FinishRecording { session: r.session });
    }

    fn cancel(&mut self) {
        let Some(r) = self.recording.take() else { return };
        self.hooks.set_recording(false);
        drop(r.capture);
        r.dictation.cancel();
        log::info!("dictée {} : annulée", r.session);
        self.ui.flash("Dictée annulée", Tone::Muted);
    }

    fn finish(&mut self, pending: PendingDictation) {
        self.pending += 1;
        let cfg = self.config();
        let job = FinishJob {
            pending,
            output: cfg.sortie.clone(),
            corrections: Corrections::from_map(&cfg.corrections),
            history: cfg.divers.historique,
            paths: self.paths.clone(),
            last_text: self.last_text.clone(),
            output_lock: self.output_lock.clone(),
            tx: self.tx.clone(),
        };
        let spawned = std::thread::Builder::new().name("fin-dictee".into()).spawn(move || job.run());
        if spawned.is_err() {
            self.pending -= 1;
            self.ui.flash("Impossible de terminer la dictée", Tone::Error);
        }
    }

    fn on_done(&mut self, outcome: Outcome) {
        self.pending = self.pending.saturating_sub(1);
        match outcome {
            Outcome::Delivered { how, warning } => {
                if let Some(w) = warning {
                    self.ui.flash(w, Tone::Error);
                } else if let Delivered::Copied = how {
                    self.ui.flash("Texte copié : Ctrl+V pour le coller", Tone::Info);
                }
            }
            Outcome::Empty => self.ui.flash("Rien entendu", Tone::Muted),
            Outcome::Failed(e) => {
                log::error!("dictée : {e}");
                self.ui.flash(e, Tone::Error);
            }
        }
    }

    fn on_engine(&mut self, e: EngineEvent) {
        match e {
            EngineEvent::Loading => {
                self.engine_ready = false;
                self.ui.update(|s| {
                    s.engine_ready = false;
                    s.detail = "chargement du modèle".into();
                });
            }
            EngineEvent::Ready(backend) => {
                self.engine_ready = true;
                self.engine_error = None;
                log::info!("moteur prêt ({backend})");
                self.ui.update(|s| {
                    s.engine_ready = true;
                    s.download = None;
                    s.detail = backend;
                });
            }
            EngineEvent::Failed(err) => {
                self.engine_ready = false;
                self.ui.notify("Plume ne peut pas transcrire", err.clone(), true);
                self.ui.update(|s| {
                    s.engine_ready = false;
                    s.download = None;
                    s.detail = err.clone();
                });
                self.engine_error = Some(err);
            }
        }
    }

    fn reload(&mut self) {
        let (new, warnings) = Config::load_or_create(&self.paths.config());
        let old = self.config();
        let hotkey = new.hotkey();
        self.hooks
            .update(HookSettings { hotkey: hotkey.clone(), cancel_with_escape: new.raccourci.annuler_avec_echap });
        self.machine.set_mode(new.raccourci.mode, Duration::from_millis(new.raccourci.appui_long_ms));
        let engine_changed = engine_settings_changed(&old.transcription, &new.transcription);
        let (indicator, position) = (new.interface.indicateur, new.interface.position);
        if let Ok(mut c) = self.cfg.lock() {
            *c = new;
        }
        if engine_changed {
            self.engine.reload();
        }
        self.ui.update(|s| {
            s.hotkey = hotkey.label();
            s.indicator = indicator;
            s.position = position;
        });
        if warnings.is_empty() {
            self.ui.flash("Réglages rechargés", Tone::Info);
        } else {
            self.ui.notify("Réglages de Plume", warnings.join("\n"), true);
        }
        log::info!("réglages rechargés (moteur rechargé : {engine_changed})");
    }

    fn refresh_phase(&mut self) {
        let phase = if self.recording.is_some() {
            Phase::Recording
        } else if self.pending > 0 || !self.stopping.is_empty() {
            Phase::Transcribing
        } else if self.engine_error.is_some() {
            Phase::Failed
        } else if !self.engine_ready {
            Phase::Starting
        } else {
            Phase::Ready
        };
        // Le son arrive 100 fois par seconde : on ne dérange l'interface que
        // si l'état visible change.
        if self.last_phase == Some(phase) {
            return;
        }
        self.last_phase = Some(phase);
        self.ui.update(|s| {
            s.phase = phase;
            if phase != Phase::Recording {
                s.record_started = None;
            }
        });
    }
}

fn engine_settings_changed(a: &TranscriptionConfig, b: &TranscriptionConfig) -> bool {
    a.modele != b.modele
        || a.langue != b.langue
        || a.gpu != b.gpu
        || a.precision != b.precision
        || a.threads != b.threads
}

struct FinishJob {
    pending: PendingDictation,
    output: OutputConfig,
    corrections: Corrections,
    history: bool,
    paths: Paths,
    last_text: Arc<Mutex<String>>,
    output_lock: Arc<Mutex<()>>,
    tx: Sender<Event>,
}

impl FinishJob {
    fn run(self) {
        let audio = self.pending.audio;
        let outcome = match self.pending.wait(Duration::from_secs(30 * 60)) {
            Err(e) => Outcome::Failed(e),
            Ok(r) => {
                let text = text::finalize(&r.text, &self.corrections);
                log::info!(
                    "dictée : {:.1} s de son, {} morceau(x), calcul {:.1} s, texte prêt {:.2} s après l'arrêt, {} caractères",
                    audio.as_secs_f32(),
                    r.chunks,
                    r.compute.as_secs_f32(),
                    r.latency.as_secs_f32(),
                    text.chars().count()
                );
                if text.is_empty() {
                    if r.errors.is_empty() { Outcome::Empty } else { Outcome::Failed(r.errors.join(" ; ")) }
                } else {
                    if let Ok(mut last) = self.last_text.lock() {
                        last.clone_from(&text);
                    }
                    let _ = fs::write(self.paths.last_dictation_file(), &text);
                    if self.history {
                        append_history(&self.paths, &text);
                    }
                    let warning = (!r.errors.is_empty()).then(|| {
                        "Une partie de la dictée n'a pas pu être transcrite (détails dans plume.log)".to_string()
                    });
                    let _guard = self.output_lock.lock();
                    match output::deliver(&text, &self.output) {
                        Ok(how) => Outcome::Delivered { how, warning },
                        Err(e) => Outcome::Failed(format!(
                            "{e}. Le texte est gardé : clic droit sur l'icône > Copier la dernière dictée"
                        )),
                    }
                }
            }
        };
        let _ = self.tx.send(Event::DictationDone(outcome));
    }
}

fn append_history(paths: &Paths, text: &str) {
    let line = format!("[{}]\n{}\n\n", plume_core::logging::timestamp(), text);
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(paths.history_file()) {
        let _ = f.write_all(line.as_bytes());
    }
}

/// Prépare le moteur : télécharge le modèle si besoin, puis le charge.
#[cfg(feature = "moteur")]
fn make_loader(cfg: Arc<Mutex<Config>>, paths: Paths, tx: Sender<Event>) -> Loader {
    use anyhow::bail;
    use plume_core::engine::{Engine, EngineSettings};
    use plume_core::models::{self, ModelSource};
    use plume_core::pipeline::Transcriber;

    Box::new(move || {
        let cfg = cfg.lock().map(|c| c.clone()).unwrap_or_default();
        let source = ModelSource::resolve(&cfg.transcription.modele, &paths.models_dir())?;
        if !source.is_present() {
            match &source {
                ModelSource::File(p) => bail!("fichier de modèle introuvable : {}", p.display()),
                ModelSource::Catalog(info, path) => {
                    log::info!("téléchargement du modèle {} vers {}", info.name, path.display());
                    let _ = tx.send(Event::DownloadStarted {
                        model: info.name.into(),
                        megabytes: info.size_bytes / 1_000_000,
                    });
                    let progress_tx = tx.clone();
                    let mut last = u32::MAX;
                    let result = models::download(info, path, |done, total| {
                        let pct = (done * 100).checked_div(total).unwrap_or(0) as u32;
                        if pct != last {
                            last = pct;
                            let _ = progress_tx.send(Event::DownloadProgress(pct));
                        }
                    });
                    let _ = tx.send(Event::DownloadFinished);
                    result.map_err(|e| e.context("téléchargement du modèle (connexion Internet ?)"))?;
                }
            }
        }
        let settings = EngineSettings {
            model_path: source.path().to_path_buf(),
            use_gpu: cfg.transcription.gpu,
            language: cfg.transcription.langue.clone(),
            beam_size: cfg.beam_size(),
            threads: cfg.transcription.threads,
        };
        Engine::load(&settings).map(|e| Box::new(e) as Box<dyn Transcriber>)
    })
}

#[cfg(not(feature = "moteur"))]
fn make_loader(_cfg: Arc<Mutex<Config>>, _paths: Paths, _tx: Sender<Event>) -> Loader {
    Box::new(|| anyhow::bail!("cette version de Plume a été compilée sans moteur de transcription"))
}
