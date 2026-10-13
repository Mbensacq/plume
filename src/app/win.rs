//! Petits utilitaires Win32 partagés.

use windows_sys::Win32::Foundation::HWND;
use windows_sys::Win32::UI::Shell::ShellExecuteW;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    MB_ICONINFORMATION, MB_ICONWARNING, MB_OK, MessageBoxW, SW_SHOWNORMAL,
};

/// Chaîne UTF-16 terminée par un zéro, pour les API Windows.
pub fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// Copie `s` dans un tableau UTF-16 de taille fixe (tronqué proprement).
pub fn fill_wide<const N: usize>(dest: &mut [u16; N], s: &str) {
    let mut units: Vec<u16> = s.encode_utf16().take(N - 1).collect();
    // Ne pas couper une paire de substitution en deux.
    if units.last().is_some_and(|u| (0xD800..0xDC00).contains(u)) {
        units.pop();
    }
    dest.fill(0);
    dest[..units.len()].copy_from_slice(&units);
}

pub fn message_box(text: &str, warning: bool) {
    let title = wide("Plume");
    let body = wide(text);
    let icon = if warning { MB_ICONWARNING } else { MB_ICONINFORMATION };
    // SAFETY: chaînes valides et terminées par zéro pendant l'appel.
    unsafe {
        MessageBoxW(std::ptr::null_mut(), body.as_ptr(), title.as_ptr(), MB_OK | icon);
    }
}

/// Ouvre un fichier ou un dossier avec l'application associée.
/// Renvoie `false` si Windows n'a pas su l'ouvrir.
pub fn shell_open(target: &std::path::Path) -> bool {
    let op = wide("open");
    let file = wide(&target.to_string_lossy());
    // SAFETY: chaînes valides pendant l'appel ; aucune fenêtre parente.
    let r = unsafe {
        ShellExecuteW(
            std::ptr::null_mut() as HWND,
            op.as_ptr(),
            file.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    // Valeur > 32 = succès (convention historique de ShellExecute).
    r as isize > 32
}

/// Ouvre un fichier texte : application associée, sinon le Bloc-notes.
pub fn open_text_file(path: &std::path::Path) {
    if !shell_open(path) {
        let _ = std::process::Command::new("notepad.exe").arg(path).spawn();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fill_truncates_and_terminates() {
        let mut buf = [1u16; 4];
        fill_wide(&mut buf, "abcdef");
        assert_eq!(buf, [97, 98, 99, 0]);
    }
}
