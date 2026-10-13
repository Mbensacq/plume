// En version finale sous Windows, pas de fenêtre de console : Plume vit près
// de l'horloge. Les commandes en ligne (--transcrire…) se rattachent à la
// console qui les a lancées.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

#[cfg(windows)]
mod app;
mod cli;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    #[cfg(windows)]
    if !args.is_empty() {
        app::attach_parent_console();
    }

    let code = match args.first().map(String::as_str) {
        None => run_desktop(),
        Some(_) => cli::run(&args),
    };
    std::process::exit(code);
}

#[cfg(windows)]
fn run_desktop() -> i32 {
    app::run()
}

#[cfg(not(windows))]
fn run_desktop() -> i32 {
    eprintln!(
        "Plume (dictée au raccourci) fonctionne sous Windows.\n\
         Ici, seule la ligne de commande est disponible : plume --aide"
    );
    2
}
