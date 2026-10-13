// En version finale sous Windows, pas de fenêtre de console : Plume vit près
// de l'horloge. Les commandes en ligne (--transcrire…) se rattachent à la
// console qui les a lancées.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

#[cfg(windows)]
mod app;
mod cli;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    // Lancé par Windows à l'ouverture de session : application de bureau.
    #[cfg(windows)]
    let at_startup = args.first().is_some_and(|a| a == app::STARTUP_ARG);
    #[cfg(not(windows))]
    let at_startup = false;
    #[cfg(windows)]
    if !args.is_empty() && !at_startup {
        app::attach_parent_console();
    }

    let code = match args.first().map(String::as_str) {
        None => run_desktop(false),
        Some(_) if at_startup => run_desktop(true),
        Some(_) => cli::run(&args),
    };
    std::process::exit(code);
}

#[cfg(windows)]
fn run_desktop(at_startup: bool) -> i32 {
    app::run(at_startup)
}

#[cfg(not(windows))]
fn run_desktop(_at_startup: bool) -> i32 {
    eprintln!(
        "Plume (dictée au raccourci) fonctionne sous Windows.\n\
         Ici, seule la ligne de commande est disponible : plume --aide"
    );
    2
}
