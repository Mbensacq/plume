//! Écriture du texte dicté là où se trouve le curseur.
//!
//! - **coller** : le texte passe par le presse-papiers puis Ctrl+V est simulé ;
//!   l'ancien contenu du presse-papiers est ensuite remis en place.
//! - **taper** : le texte est tapé caractère par caractère (applications ou
//!   jeux qui refusent le collage).
//! - **presse-papiers** : le texte est seulement copié.

use super::hooks::PLUME_MARK;
use plume_core::config::{OutputConfig, OutputMode};
use std::path::PathBuf;
use std::thread::sleep;
use std::time::{Duration, Instant};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBD_EVENT_FLAGS, KEYBDINPUT, KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE, MAPVK_VK_TO_VSC, MapVirtualKeyW, SendInput, VIRTUAL_KEY, VK_CONTROL, VK_LCONTROL, VK_LMENU,
    VK_LSHIFT, VK_LWIN, VK_RCONTROL, VK_RMENU, VK_RSHIFT, VK_RWIN,
};

/// Délai avant de remettre l'ancien presse-papiers : l'application cible doit
/// avoir lu le texte collé.
const RESTORE_DELAY: Duration = Duration::from_millis(600);

/// Ce qu'il faut signaler à l'utilisateur après la sortie.
pub enum Delivered {
    Pasted,
    Typed,
    Copied,
}

pub fn deliver(text: &str, cfg: &OutputConfig) -> Result<Delivered, String> {
    let mut t = text.replace(['\r', '\n'], " ");
    if cfg.espace_final && !t.ends_with(' ') {
        t.push(' ');
    }
    match cfg.mode {
        OutputMode::Paste => paste(&t, cfg.restaurer_presse_papiers).map(|_| Delivered::Pasted),
        OutputMode::Type => {
            wait_modifiers_released();
            type_text(&t).map(|_| Delivered::Typed)
        }
        OutputMode::Clipboard => set_clipboard_text(text.trim()).map(|_| Delivered::Copied),
    }
}

enum Saved {
    Text(String),
    Image(arboard::ImageData<'static>),
    Files(Vec<PathBuf>),
    Empty,
}

fn clipboard() -> Result<arboard::Clipboard, String> {
    // Une autre application peut tenir le presse-papiers un court instant.
    let mut last = String::new();
    for _ in 0..10 {
        match arboard::Clipboard::new() {
            Ok(c) => return Ok(c),
            Err(e) => last = e.to_string(),
        }
        sleep(Duration::from_millis(30));
    }
    Err(format!("presse-papiers indisponible : {last}"))
}

fn save_clipboard() -> Saved {
    let Ok(mut cb) = clipboard() else { return Saved::Empty };
    if let Ok(t) = cb.get_text() {
        return Saved::Text(t);
    }
    if let Ok(img) = cb.get_image() {
        return Saved::Image(img.to_owned_img());
    }
    if let Ok(files) = cb.get().file_list() {
        return Saved::Files(files);
    }
    Saved::Empty
}

fn restore_clipboard(saved: Saved, ours: &str) {
    let Ok(mut cb) = clipboard() else { return };
    // Si l'utilisateur a copié autre chose entre-temps, on n'y touche pas.
    if cb.get_text().map(|t| t != ours).unwrap_or(true) {
        return;
    }
    let r = match saved {
        Saved::Text(t) => cb.set_text(t),
        Saved::Image(img) => cb.set_image(img),
        Saved::Files(f) => cb.set().file_list(&f),
        Saved::Empty => cb.clear(),
    };
    if let Err(e) = r {
        log::warn!("restauration du presse-papiers : {e}");
    }
}

pub fn set_clipboard_text(text: &str) -> Result<(), String> {
    let mut last = String::new();
    for _ in 0..5 {
        match clipboard()?.set_text(text.to_string()) {
            Ok(()) => return Ok(()),
            Err(e) => last = e.to_string(),
        }
        sleep(Duration::from_millis(40));
    }
    Err(format!("copie impossible : {last}"))
}

fn paste(text: &str, restore: bool) -> Result<(), String> {
    let saved = if restore { Some(save_clipboard()) } else { None };
    set_clipboard_text(text)?;
    wait_modifiers_released();
    send_keys(&[(VK_CONTROL, false), (b'V' as u16, false), (b'V' as u16, true), (VK_CONTROL, true)])?;
    // Restauration dans ce même fil (qui détient le verrou de sortie) : une
    // dictée suivante ne peut pas sauvegarder notre texte à la place de
    // l'ancien contenu.
    if let Some(saved) = saved {
        sleep(RESTORE_DELAY);
        restore_clipboard(saved, text);
    }
    Ok(())
}

fn key_down(vk: VIRTUAL_KEY) -> bool {
    // SAFETY: simple lecture de l'état d'une touche.
    unsafe { (GetAsyncKeyState(vk as i32) as u16 & 0x8000) != 0 }
}

const MODIFIERS: [VIRTUAL_KEY; 8] =
    [VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LSHIFT, VK_RSHIFT, VK_LWIN, VK_RWIN];

/// Attend que Ctrl/Alt/Maj/Win soient relâchés (sinon Ctrl+V deviendrait
/// Ctrl+Alt+V…). Au-delà de 1,5 s, on les relâche virtuellement.
fn wait_modifiers_released() {
    let start = Instant::now();
    while MODIFIERS.iter().any(|&k| key_down(k)) {
        if start.elapsed() > Duration::from_millis(1_500) {
            let held: Vec<(VIRTUAL_KEY, bool)> =
                MODIFIERS.iter().copied().filter(|&k| key_down(k)).map(|k| (k, true)).collect();
            log::debug!("relâchement virtuel de {} modificateur(s)", held.len());
            let _ = send_keys(&held);
            break;
        }
        sleep(Duration::from_millis(15));
    }
}

fn keyboard_input(vk: VIRTUAL_KEY, scan: u16, flags: KEYBD_EVENT_FLAGS) -> INPUT {
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT { wVk: vk, wScan: scan, dwFlags: flags, time: 0, dwExtraInfo: PLUME_MARK },
        },
    }
}

fn send_inputs(inputs: &[INPUT]) -> Result<(), String> {
    if inputs.is_empty() {
        return Ok(());
    }
    // SAFETY: tableau valide de structures INPUT complètes.
    let sent = unsafe { SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<INPUT>() as i32) };
    if sent as usize == inputs.len() {
        Ok(())
    } else {
        Err("Windows a bloqué la saisie simulée (fenêtre lancée en administrateur ?)".into())
    }
}

/// Touches virtuelles (avec leur code matériel, pour les jeux qui le lisent).
fn send_keys(keys: &[(VIRTUAL_KEY, bool)]) -> Result<(), String> {
    let inputs: Vec<INPUT> = keys
        .iter()
        .map(|&(vk, up)| {
            // SAFETY: conversion de code de touche, sans effet de bord.
            let scan = unsafe { MapVirtualKeyW(vk as u32, MAPVK_VK_TO_VSC) } as u16;
            keyboard_input(vk, scan, if up { KEYEVENTF_KEYUP } else { 0 })
        })
        .collect();
    send_inputs(&inputs)
}

/// Tape le texte en Unicode, par petits paquets (certaines applications
/// perdent des caractères si tout arrive d'un coup).
fn type_text(text: &str) -> Result<(), String> {
    let units: Vec<u16> = text.encode_utf16().collect();
    for batch in units.chunks(32) {
        let mut inputs = Vec::with_capacity(batch.len() * 2);
        for &u in batch {
            inputs.push(keyboard_input(0, u, KEYEVENTF_UNICODE));
            inputs.push(keyboard_input(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP));
        }
        send_inputs(&inputs)?;
        sleep(Duration::from_millis(4));
    }
    Ok(())
}
