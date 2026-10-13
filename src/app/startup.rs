//! Lancement de Plume à l'ouverture de la session Windows (réglage
//! `[divers] lancer_au_demarrage`).
//!
//! Plume s'inscrit dans la clé « Run » de l'utilisateur (aucun droit
//! administrateur nécessaire), avec l'argument [`STARTUP_ARG`] : lancé ainsi
//! alors qu'il tourne déjà, Plume s'arrête sans afficher de message.

use super::win::wide;
use std::path::Path;
use windows_sys::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS};
use windows_sys::Win32::System::Registry::{
    HKEY_CURRENT_USER, REG_SZ, RRF_RT_REG_SZ, RegDeleteKeyValueW, RegGetValueW, RegSetKeyValueW,
};

/// Argument ajouté par Windows au lancement automatique.
pub const STARTUP_ARG: &str = "--demarrage";

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const VALUE_NAME: &str = "Plume";

/// Applique le réglage pour l'exécutable en cours. Rien en version de
/// développement, pour ne pas inscrire un exécutable de test.
pub fn sync(enabled: bool) {
    if cfg!(debug_assertions) {
        return;
    }
    match std::env::current_exe() {
        Ok(exe) => {
            if let Err(e) = apply(VALUE_NAME, enabled, &exe) {
                log::warn!("{e}");
            }
        }
        Err(e) => log::warn!("lancement au démarrage : chemin de Plume introuvable ({e})"),
    }
}

/// Inscrit `exe` au démarrage, ou retire l'inscription si c'est bien celle
/// de `exe` (une autre copie de Plume garde la sienne).
fn apply(name: &str, enabled: bool, exe: &Path) -> Result<(), String> {
    let command = command_for(exe);
    let current = read(name);
    if enabled {
        if current.as_deref() != Some(command.as_str()) {
            write(name, &command)?;
            log::info!("lancement au démarrage de Windows activé : {command}");
        }
    } else if current.is_some_and(|c| refers_to(&c, exe)) {
        delete(name)?;
        log::info!("lancement au démarrage de Windows désactivé");
    }
    Ok(())
}

fn command_for(exe: &Path) -> String {
    format!("\"{}\" {STARTUP_ARG}", exe.display())
}

/// La commande inscrite lance-t-elle cet exécutable-ci ?
fn refers_to(command: &str, exe: &Path) -> bool {
    let target = command.trim().trim_start_matches('"');
    let target = target.split('"').next().unwrap_or("").trim();
    target.eq_ignore_ascii_case(&exe.display().to_string())
}

/// La commande inscrite sous `name`, ou `None` si elle est absente ou illisible.
fn read(name: &str) -> Option<String> {
    let (key, value) = (wide(RUN_KEY), wide(name));
    let mut size = 0u32;
    // SAFETY: chaînes UTF-16 terminées par zéro ; ce premier appel donne la taille.
    let err = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if err != ERROR_SUCCESS {
        return None;
    }
    let mut buf = vec![0u16; (size as usize).div_ceil(2)];
    let mut size = (buf.len() * 2) as u32;
    // SAFETY: `buf` fait `size` octets.
    let err = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            value.as_ptr(),
            RRF_RT_REG_SZ,
            std::ptr::null_mut(),
            buf.as_mut_ptr().cast(),
            &mut size,
        )
    };
    if err != ERROR_SUCCESS {
        return None;
    }
    let len = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    Some(String::from_utf16_lossy(&buf[..len]))
}

fn write(name: &str, command: &str) -> Result<(), String> {
    let (key, value, data) = (wide(RUN_KEY), wide(name), wide(command));
    // SAFETY: données UTF-16 terminées par zéro ; taille en octets, zéro final compris.
    let err = unsafe {
        RegSetKeyValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            value.as_ptr(),
            REG_SZ,
            data.as_ptr().cast(),
            (data.len() * 2) as u32,
        )
    };
    if err == ERROR_SUCCESS {
        Ok(())
    } else {
        Err(format!("inscription au démarrage de Windows impossible (erreur {err})"))
    }
}

fn delete(name: &str) -> Result<(), String> {
    let (key, value) = (wide(RUN_KEY), wide(name));
    // SAFETY: chaînes UTF-16 terminées par zéro.
    let err = unsafe { RegDeleteKeyValueW(HKEY_CURRENT_USER, key.as_ptr(), value.as_ptr()) };
    if err == ERROR_SUCCESS || err == ERROR_FILE_NOT_FOUND {
        Ok(())
    } else {
        Err(format!("retrait du démarrage de Windows impossible (erreur {err})"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognises_its_own_command() {
        let exe = Path::new(r"C:\Plume 2\plume.exe");
        assert!(refers_to(r#""C:\Plume 2\plume.exe" --demarrage"#, exe));
        assert!(refers_to(r#""c:\plume 2\PLUME.EXE" --demarrage"#, exe));
        assert!(!refers_to(r#""C:\Autre\plume.exe" --demarrage"#, exe));
    }

    // Écrit dans le vrai registre (sous un nom de test) : lancé par la CI Windows.
    #[test]
    fn registers_and_unregisters_in_the_run_key() {
        let name = "PlumeTestDemarrage";
        let exe = Path::new(r"C:\Program Files\Plume é\plume.exe");
        let _ = delete(name);

        apply(name, true, exe).unwrap();
        assert_eq!(read(name).as_deref(), Some(r#""C:\Program Files\Plume é\plume.exe" --demarrage"#));
        apply(name, true, exe).unwrap();

        // Une autre copie de Plume ne retire pas l'inscription de celle-ci.
        apply(name, false, Path::new(r"C:\Autre\plume.exe")).unwrap();
        assert!(read(name).is_some());

        apply(name, false, exe).unwrap();
        assert_eq!(read(name), None);
        apply(name, false, exe).unwrap();
    }
}
