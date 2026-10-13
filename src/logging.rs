//! Journal de Plume (`plume.log`) : utile pour comprendre un problème après coup.
//!
//! Le fichier est remis à zéro au démarrage quand il dépasse 2 Mo (l'ancien est
//! gardé sous `plume.log.1`). Les messages sont aussi écrits sur la console
//! quand il y en a une.

use log::{Level, LevelFilter, Log, Metadata, Record};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

const MAX_SIZE: u64 = 2 * 1024 * 1024;

struct Logger {
    file: Mutex<Option<File>>,
    to_stderr: bool,
}

impl Log for Logger {
    fn enabled(&self, metadata: &Metadata) -> bool {
        let target = metadata.target();
        // whisper.cpp et ggml sont très bavards : on ne garde que l'essentiel.
        if target.starts_with("whisper_rs") {
            return metadata.level() <= Level::Info;
        }
        metadata.level() <= Level::Debug && !target.starts_with("ureq") && !target.starts_with("rustls")
    }

    fn log(&self, record: &Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!("{} [{}] {}\n", timestamp(), record.level(), record.args().to_string().trim_end());
        if let Ok(mut guard) = self.file.lock()
            && let Some(f) = guard.as_mut()
        {
            let _ = f.write_all(line.as_bytes());
        }
        if self.to_stderr {
            eprint!("{line}");
        }
    }

    fn flush(&self) {
        if let Ok(mut guard) = self.file.lock()
            && let Some(f) = guard.as_mut()
        {
            let _ = f.flush();
        }
    }
}

/// Installe le journal. À appeler une seule fois, au démarrage.
pub fn init(path: &Path, to_stderr: bool) {
    if fs::metadata(path).map(|m| m.len() > MAX_SIZE).unwrap_or(false) {
        let _ = fs::rename(path, path.with_extension("log.1"));
    }
    let file = OpenOptions::new().create(true).append(true).open(path).ok();
    let logger = Logger { file: Mutex::new(file), to_stderr };
    if log::set_boxed_logger(Box::new(logger)).is_ok() {
        log::set_max_level(LevelFilter::Debug);
    }
}

/// Date et heure locales, à la milliseconde.
pub fn timestamp() -> String {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::SystemInformation::GetLocalTime;
        // SAFETY: GetLocalTime remplit simplement la structure fournie.
        let t = unsafe {
            let mut t = std::mem::zeroed();
            GetLocalTime(&mut t);
            t
        };
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:03}",
            t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond, t.wMilliseconds
        )
    }
    #[cfg(not(windows))]
    {
        let d = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
        let secs = d.as_secs();
        let (y, m, day) = civil_from_days((secs / 86_400) as i64);
        let s = secs % 86_400;
        format!(
            "{y:04}-{m:02}-{day:02} {:02}:{:02}:{:02}.{:03} UTC",
            s / 3600,
            (s / 60) % 60,
            s % 60,
            d.subsec_millis()
        )
    }
}

/// Jour depuis 1970 → (année, mois, jour). Algorithme de H. Hinnant.
#[cfg(not(windows))]
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

#[cfg(all(test, not(windows)))]
mod tests {
    use super::*;

    #[test]
    fn civil_dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(19_723), (2024, 1, 1));
        assert_eq!(civil_from_days(20_739), (2026, 10, 13));
    }
}
