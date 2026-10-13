//! Raccourci global : crochets clavier et souris « bas niveau » de Windows.
//!
//! Les crochets voient chaque touche et chaque clic, quelle que soit la fenêtre
//! active. Quand la combinaison de Plume est reconnue, la touche (ou le bouton)
//! principale est « avalée » : l'application active ne la reçoit pas.
//!
//! Le traitement doit être instantané (Windows retire un crochet trop lent) :
//! on se contente de comparer et d'envoyer un message au contrôleur.

use super::Event;
use plume_core::hotkey::{Hotkey, Modifiers, MouseButton, Trigger, vk};
use plume_core::trigger::Input;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{Mutex, OnceLock};
use windows_sys::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::GetCurrentThreadId;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, SendInput, VK_CONTROL, VK_LWIN,
    VK_MENU, VK_RWIN, VK_SHIFT,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetMessageW, HC_ACTION, KBDLLHOOKSTRUCT, MSG, MSLLHOOKSTRUCT, PostThreadMessageW,
    SetWindowsHookExW, TranslateMessage, UnhookWindowsHookEx, WH_KEYBOARD_LL, WH_MOUSE_LL, WM_KEYDOWN, WM_KEYUP,
    WM_MBUTTONDOWN, WM_MBUTTONUP, WM_QUIT, WM_SYSKEYDOWN, WM_SYSKEYUP, WM_XBUTTONDOWN, WM_XBUTTONUP,
};

/// Marque apposée sur les événements que Plume simule lui-même (collage,
/// masque de menu) pour que ses propres crochets les ignorent.
pub const PLUME_MARK: usize = 0x504C_554D; // « PLUM »

/// Touche virtuelle sans fonction, utilisée pour empêcher Alt ou Win relâché
/// seul d'ouvrir un menu (même technique qu'AutoHotkey).
const VK_MENU_MASK: u16 = 0xE8;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookSettings {
    pub hotkey: Hotkey,
    pub cancel_with_escape: bool,
}

struct Shared {
    settings: Mutex<HookSettings>,
    tx: Mutex<Sender<Event>>,
    recording: AtomicBool,
    trigger_down: AtomicBool,
    /// Horodatage Windows (ms) du dernier appui vu sur le déclencheur.
    last_trigger_ms: AtomicU32,
}

static SHARED: OnceLock<Shared> = OnceLock::new();
static THREAD_ID: AtomicU32 = AtomicU32::new(0);

/// Accès au raccourci depuis le contrôleur.
pub struct HookHandle;

impl HookHandle {
    pub fn update(&self, settings: HookSettings) {
        if let Some(s) = SHARED.get()
            && let Ok(mut g) = s.settings.lock()
        {
            *g = settings;
            s.trigger_down.store(false, Ordering::SeqCst);
        }
    }

    /// Pendant l'enregistrement, Échap annule (si activé).
    pub fn set_recording(&self, on: bool) {
        if let Some(s) = SHARED.get() {
            s.recording.store(on, Ordering::SeqCst);
        }
    }

    pub fn stop(&self) {
        let id = THREAD_ID.load(Ordering::SeqCst);
        if id != 0 {
            // SAFETY: simple envoi de message à notre propre fil.
            unsafe {
                PostThreadMessageW(id, WM_QUIT, 0, 0);
            }
        }
    }
}

/// Installe les crochets dans un fil dédié (avec sa boucle de messages).
pub fn start(settings: HookSettings, tx: Sender<Event>) -> Result<HookHandle, String> {
    let shared = Shared {
        settings: Mutex::new(settings),
        tx: Mutex::new(tx),
        recording: AtomicBool::new(false),
        trigger_down: AtomicBool::new(false),
        last_trigger_ms: AtomicU32::new(0),
    };
    if SHARED.set(shared).is_err() {
        return Err("crochets déjà installés".into());
    }
    let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<(), String>>();
    std::thread::Builder::new()
        .name("raccourci".into())
        .spawn(move || hook_thread(ready_tx))
        .map_err(|e| e.to_string())?;
    ready_rx.recv().map_err(|e| e.to_string())??;
    Ok(HookHandle)
}

fn hook_thread(ready: std::sync::mpsc::Sender<Result<(), String>>) {
    // SAFETY: appels Win32 standard ; les procédures restent valides (fonctions statiques).
    unsafe {
        THREAD_ID.store(GetCurrentThreadId(), Ordering::SeqCst);
        let module = GetModuleHandleW(std::ptr::null());
        let kb = SetWindowsHookExW(WH_KEYBOARD_LL, Some(keyboard_proc), module, 0);
        let ms = SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_proc), module, 0);
        if kb.is_null() || ms.is_null() {
            let _ = ready.send(Err("Windows a refusé l'installation du raccourci global".into()));
            return;
        }
        let _ = ready.send(Ok(()));
        log::info!("raccourci global installé");
        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        UnhookWindowsHookEx(kb);
        UnhookWindowsHookEx(ms);
    }
}

fn is_down(vk: u16) -> bool {
    // SAFETY: lecture de l'état d'une touche.
    unsafe { (GetAsyncKeyState(vk as i32) as u16 & 0x8000) != 0 }
}

fn current_modifiers() -> Modifiers {
    Modifiers {
        ctrl: is_down(VK_CONTROL),
        alt: is_down(VK_MENU),
        shift: is_down(VK_SHIFT),
        win: is_down(VK_LWIN) || is_down(VK_RWIN),
    }
}

/// Simule une touche neutre : Alt ou Win relâché ensuite n'ouvre pas de menu.
fn send_menu_mask() {
    let key = |flags| INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT { wVk: VK_MENU_MASK, wScan: 0, dwFlags: flags, time: 0, dwExtraInfo: PLUME_MARK },
        },
    };
    let inputs = [key(0), key(KEYEVENTF_KEYUP)];
    // SAFETY: tableau valide de structures INPUT complètes.
    unsafe {
        SendInput(inputs.len() as u32, inputs.as_ptr(), std::mem::size_of::<INPUT>() as i32);
    }
}

impl Shared {
    fn send(&self, input: Input) {
        if let Ok(tx) = self.tx.lock() {
            let _ = tx.send(Event::Hotkey(input));
        }
    }

    /// Renvoie `true` si l'événement doit être avalé.
    fn on_trigger(&self, mods: &Modifiers, down: bool, time_ms: u32) -> bool {
        if down {
            if self.trigger_down.load(Ordering::SeqCst) {
                let last = self.last_trigger_ms.swap(time_ms, Ordering::SeqCst);
                // Répétition automatique de la touche maintenue : événements rapprochés.
                if time_ms.wrapping_sub(last) < 1_500 {
                    return true;
                }
                // Long silence puis nouvel appui : un relâchement a été manqué
                // (bureau sécurisé, verrouillage…). On le rattrape.
                self.trigger_down.store(false, Ordering::SeqCst);
                self.send(Input::Released);
            }
            let now = current_modifiers();
            if now != *mods {
                return false;
            }
            self.trigger_down.store(true, Ordering::SeqCst);
            self.last_trigger_ms.store(time_ms, Ordering::SeqCst);
            self.send(Input::Pressed);
            if now.alt || now.win {
                send_menu_mask();
            }
            true
        } else if self.trigger_down.swap(false, Ordering::SeqCst) {
            self.send(Input::Released);
            true
        } else {
            false
        }
    }

    fn on_key(&self, code: u16, down: bool, time_ms: u32) -> bool {
        let Ok(settings) = self.settings.lock() else { return false };
        if code == vk::ESCAPE && settings.cancel_with_escape && self.recording.load(Ordering::SeqCst) {
            if down {
                self.send(Input::Cancel);
            }
            return true;
        }
        match settings.hotkey.trigger {
            Trigger::Key(k) if k == code => {
                let mods = settings.hotkey.mods;
                drop(settings);
                self.on_trigger(&mods, down, time_ms)
            }
            _ => false,
        }
    }

    fn on_mouse(&self, button: MouseButton, down: bool, time_ms: u32) -> bool {
        let Ok(settings) = self.settings.lock() else { return false };
        match settings.hotkey.trigger {
            Trigger::Mouse(b) if b == button => {
                let mods = settings.hotkey.mods;
                drop(settings);
                self.on_trigger(&mods, down, time_ms)
            }
            _ => false,
        }
    }
}

unsafe extern "system" fn keyboard_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32
        && let Some(shared) = SHARED.get()
    {
        // SAFETY: pour WH_KEYBOARD_LL, lparam pointe vers un KBDLLHOOKSTRUCT valide.
        let kb = unsafe { &*(lparam as *const KBDLLHOOKSTRUCT) };
        if kb.dwExtraInfo != PLUME_MARK {
            let msg = wparam as u32;
            let down = msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN;
            let up = msg == WM_KEYUP || msg == WM_SYSKEYUP;
            if (down || up) && shared.on_key(kb.vkCode as u16, down, kb.time) {
                return 1;
            }
        }
    }
    // SAFETY: transmission normale au crochet suivant.
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam) }
}

unsafe extern "system" fn mouse_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code == HC_ACTION as i32
        && let Some(shared) = SHARED.get()
    {
        // SAFETY: pour WH_MOUSE_LL, lparam pointe vers un MSLLHOOKSTRUCT valide.
        let ms = unsafe { &*(lparam as *const MSLLHOOKSTRUCT) };
        if ms.dwExtraInfo != PLUME_MARK {
            let msg = wparam as u32;
            let x_button = || match (ms.mouseData >> 16) & 0xFFFF {
                1 => Some(MouseButton::Back),
                2 => Some(MouseButton::Forward),
                _ => None,
            };
            let event = match msg {
                WM_XBUTTONDOWN => x_button().map(|b| (b, true)),
                WM_XBUTTONUP => x_button().map(|b| (b, false)),
                WM_MBUTTONDOWN => Some((MouseButton::Middle, true)),
                WM_MBUTTONUP => Some((MouseButton::Middle, false)),
                _ => None,
            };
            if let Some((button, down)) = event
                && shared.on_mouse(button, down, ms.time)
            {
                return 1;
            }
        }
    }
    // SAFETY: transmission normale au crochet suivant.
    unsafe { CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam) }
}
