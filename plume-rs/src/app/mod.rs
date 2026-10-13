//! Plume sur Windows : icône près de l'horloge, raccourci global, micro,
//! collage du texte. Le cœur (découpage, moteur, texte) vient de `plume_core`.
//!
//! Fils d'exécution :
//! - principal : fenêtres et icône (boucle de messages Windows) ;
//! - « raccourci » : crochets clavier/souris ;
//! - « controleur » : décide quoi faire de chaque événement ;
//! - « moteur » : transcription, un morceau après l'autre ;
//! - un fil par dictée qui se termine (assemblage puis collage).

mod audio;
mod controller;
mod hooks;
mod output;
mod ui;
mod win;

use plume_core::config::Config;
use plume_core::paths::Paths;
use plume_core::pipeline::EngineEvent;
use plume_core::trigger::Input;
use std::sync::mpsc;
use windows_sys::Win32::Foundation::{ERROR_ALREADY_EXISTS, GetLastError};
use windows_sys::Win32::System::Console::{ATTACH_PARENT_PROCESS, AttachConsole};
use windows_sys::Win32::System::Threading::CreateMutexW;
use windows_sys::Win32::UI::HiDpi::{DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, SetProcessDpiAwarenessContext};

/// Tout ce qui arrive au contrôleur.
pub enum Event {
    Hotkey(Input),
    Audio {
        session: u64,
        samples: Vec<f32>,
    },
    AudioFailed {
        session: u64,
        message: String,
    },
    /// Le micro est fermé : tout son son est déjà dans la file, on peut finir.
    FinishRecording {
        session: u64,
    },
    Engine(EngineEvent),
    // Construits par le chargeur du moteur (absent d'une version sans moteur).
    #[cfg_attr(not(feature = "moteur"), allow(dead_code))]
    DownloadStarted {
        model: String,
        megabytes: u64,
    },
    #[cfg_attr(not(feature = "moteur"), allow(dead_code))]
    DownloadProgress(u32),
    #[cfg_attr(not(feature = "moteur"), allow(dead_code))]
    DownloadFinished,
    DictationDone(controller::Outcome),
    UpdateInstalled(String),
    Menu(MenuCommand),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MenuCommand {
    CopyLast,
    Reload,
    Quit,
}

/// Rattache la sortie à la console qui a lancé Plume (commandes en ligne).
pub fn attach_parent_console() {
    // SAFETY: sans effet si Plume n'a pas été lancé depuis une console.
    unsafe {
        AttachConsole(ATTACH_PARENT_PROCESS);
    }
}

/// Empêche de lancer Plume deux fois (deux raccourcis, deux collages…).
fn single_instance() -> bool {
    let name = win::wide("Local\\Plume-dictee-vocale");
    // SAFETY: le mutex nommé vit jusqu'à la fin du processus (jamais fermé).
    unsafe {
        let h = CreateMutexW(std::ptr::null(), 1, name.as_ptr());
        !(h.is_null() || GetLastError() == ERROR_ALREADY_EXISTS)
    }
}

pub fn run() -> i32 {
    let paths = Paths::detect();
    plume_core::logging::init(&paths.log_file(), false);
    log::info!("Plume {} démarre (dossier : {})", plume_core::VERSION, paths.root().display());

    if !single_instance() {
        win::message_box("Plume est déjà lancé : son icône se trouve près de l'horloge.", false);
        return 0;
    }
    // SAFETY: réglage du processus, avant la création de toute fenêtre.
    unsafe {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    if let Ok(exe) = std::env::current_exe() {
        plume_core::update::cleanup(&exe);
    }

    let first_run = !paths.config().exists();
    let (cfg, warnings) = Config::load_or_create(&paths.config());
    for w in &warnings {
        log::warn!("réglages : {w}");
    }

    let (tx, rx) = mpsc::channel::<Event>();
    let hotkey = cfg.hotkey();
    let initial = ui::UiState {
        phase: ui::Phase::Starting,
        detail: String::new(),
        hotkey: hotkey.label(),
        record_started: None,
        engine_ready: false,
        download: None,
        flash: None,
        balloon: None,
        indicator: cfg.interface.indicateur,
        position: cfg.interface.position,
    };
    let ui = match ui::create(initial, tx.clone(), paths.config(), paths.root().to_path_buf()) {
        Ok(ui) => ui,
        Err(e) => {
            log::error!("interface : {e}");
            win::message_box(&format!("Plume n'a pas pu démarrer : {e}"), true);
            return 1;
        }
    };

    let hooks = match hooks::start(
        hooks::HookSettings { hotkey: hotkey.clone(), cancel_with_escape: cfg.raccourci.annuler_avec_echap },
        tx.clone(),
    ) {
        Ok(h) => h,
        Err(e) => {
            log::error!("raccourci : {e}");
            win::message_box(&format!("Plume n'a pas pu installer son raccourci clavier : {e}"), true);
            return 1;
        }
    };

    if first_run {
        ui.notify(
            "Plume est prêt à l'emploi",
            format!(
                "Raccourci : {}. Appui court = démarrer puis arrêter ; appui long = parler en maintenant. \
                 Réglages : clic droit sur l'icône de Plume.",
                hotkey.label()
            ),
            false,
        );
    }
    if !warnings.is_empty() {
        ui.notify("Réglages de Plume", warnings.join("\n"), true);
    }

    let auto_update = cfg.divers.mise_a_jour_auto;
    let controller = controller::Controller::new(paths, cfg, ui, hooks, tx.clone());
    let worker = std::thread::Builder::new()
        .name("controleur".into())
        .spawn(move || controller.run(rx))
        .expect("création du fil contrôleur");

    if auto_update && !cfg!(debug_assertions) {
        spawn_update_check(tx.clone());
    }

    ui::run_loop();
    // Fenêtre fermée autrement que par le menu (fermeture de session…) :
    // le contrôleur doit s'arrêter aussi.
    let _ = tx.send(Event::Menu(MenuCommand::Quit));
    let _ = worker.join();
    log::info!("Plume s'arrête");
    0
}

fn spawn_update_check(tx: mpsc::Sender<Event>) {
    let _ = std::thread::Builder::new().name("mise-a-jour".into()).spawn(move || {
        // On laisse Plume démarrer tranquillement d'abord.
        std::thread::sleep(std::time::Duration::from_secs(20));
        let Ok(exe) = std::env::current_exe() else { return };
        match plume_core::update::check() {
            Ok(Some(rel)) => {
                log::info!("nouvelle version disponible : {}", rel.version);
                match plume_core::update::install(&rel, &exe) {
                    Ok(()) => {
                        let _ = tx.send(Event::UpdateInstalled(rel.version));
                    }
                    Err(e) => log::warn!("mise à jour : {e:#}"),
                }
            }
            Ok(None) => log::info!("Plume est à jour"),
            Err(e) => log::info!("vérification de mise à jour impossible : {e:#}"),
        }
    });
}
