//! Ce que l'on voit de Plume : une icône près de l'horloge et un petit
//! indicateur à l'écran pendant l'écoute et la transcription.
//!
//! Tout se passe dans le fil principal (boucle de messages Windows). Les autres
//! fils modifient l'état partagé via [`UiHandle`] puis demandent un
//! rafraîchissement par message.

use super::win::{fill_wide, open_text_file, shell_open, wide};
use super::{Event, MenuCommand};
use plume_core::config::IndicatorPosition;
use std::cell::RefCell;
use std::path::PathBuf;
use std::sync::atomic::{AtomicIsize, AtomicU32, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, POINT, RECT, SIZE, WPARAM};
use windows_sys::Win32::Graphics::Gdi::{
    BI_RGB, BITMAPINFO, BITMAPINFOHEADER, BeginPaint, CreateBitmap, CreateDIBSection, CreateFontW, CreateRoundRectRgn,
    CreateSolidBrush, DIB_RGB_COLORS, DT_LEFT, DT_NOPREFIX, DT_SINGLELINE, DT_VCENTER, DeleteObject, DrawTextW,
    Ellipse, EndPaint, FW_SEMIBOLD, FillRect, GetDC, GetMonitorInfoW, GetStockObject, GetTextExtentPoint32W, HFONT,
    InvalidateRect, MONITOR_DEFAULTTONEAREST, MONITOR_DEFAULTTOPRIMARY, MONITORINFO, MonitorFromWindow, NULL_PEN,
    PAINTSTRUCT, ReleaseDC, SelectObject, SetBkMode, SetTextColor, SetWindowRgn, TRANSPARENT,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::UI::HiDpi::{GetDpiForSystem, GetDpiForWindow};
use windows_sys::Win32::UI::Shell::{
    NIF_ICON, NIF_INFO, NIF_MESSAGE, NIF_TIP, NIIF_INFO, NIIF_WARNING, NIM_ADD, NIM_DELETE, NIM_MODIFY,
    NOTIFYICONDATAW, Shell_NotifyIconW,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    AppendMenuW, CreateIconIndirect, CreatePopupMenu, CreateWindowExW, DefWindowProcW, DestroyIcon, DestroyMenu,
    DestroyWindow, DispatchMessageW, GetCursorPos, GetForegroundWindow, GetMessageW, GetSystemMetrics, HICON,
    HWND_TOPMOST, ICONINFO, IDC_ARROW, KillTimer, LoadCursorW, MF_GRAYED, MF_SEPARATOR, MF_STRING, MSG, PostMessageW,
    PostQuitMessage, RegisterClassExW, RegisterWindowMessageW, SM_CXSMICON, SPI_GETWORKAREA, SW_HIDE, SWP_NOACTIVATE,
    SWP_SHOWWINDOW, SetForegroundWindow, SetLayeredWindowAttributes, SetTimer, SetWindowPos, ShowWindow,
    SystemParametersInfoW, TPM_BOTTOMALIGN, TPM_RETURNCMD, TPM_RIGHTBUTTON, TrackPopupMenu, TranslateMessage, WM_CLOSE,
    WM_COMMAND, WM_CONTEXTMENU, WM_DESTROY, WM_LBUTTONUP, WM_NULL, WM_PAINT, WM_RBUTTONUP, WM_TIMER, WNDCLASSEXW,
    WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_OVERLAPPED, WS_POPUP,
};

const WM_APP_REFRESH: u32 = 0x8000 + 1;
const WM_APP_TRAY: u32 = 0x8000 + 2;
const TIMER_TICK: usize = 1;

const ID_COPY_LAST: usize = 10;
const ID_OPEN_SETTINGS: usize = 11;
const ID_RELOAD: usize = 12;
const ID_OPEN_FOLDER: usize = 13;
const ID_QUIT: usize = 14;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Starting,
    Ready,
    Recording,
    Transcribing,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Info,
    Muted,
    Error,
}

#[derive(Debug, Clone)]
pub struct Flash {
    pub text: String,
    pub tone: Tone,
    pub until: Instant,
}

#[derive(Debug, Clone)]
pub struct Balloon {
    pub title: String,
    pub text: String,
    pub warning: bool,
}

#[derive(Debug, Clone)]
pub struct UiState {
    pub phase: Phase,
    /// « carte graphique », message d'erreur, etc.
    pub detail: String,
    pub hotkey: String,
    pub record_started: Option<Instant>,
    pub engine_ready: bool,
    pub download: Option<u32>,
    pub flash: Option<Flash>,
    pub balloon: Option<Balloon>,
    pub indicator: bool,
    pub position: IndicatorPosition,
}

struct Shared {
    state: Mutex<UiState>,
    hwnd: AtomicIsize,
    overlay: AtomicIsize,
    taskbar_created: AtomicU32,
    tx: Sender<Event>,
    config_path: PathBuf,
    root: PathBuf,
}

static UI: OnceLock<Shared> = OnceLock::new();

thread_local! {
    static ICONS: RefCell<Vec<(Phase, HICON)>> = const { RefCell::new(Vec::new()) };
    static OVERLAY: RefCell<OverlayContent> = RefCell::new(OverlayContent::default());
    static FONT: RefCell<Option<(u32, HFONT)>> = const { RefCell::new(None) };
    static LAST_TRAY: RefCell<Option<(Phase, String)>> = const { RefCell::new(None) };
}

#[derive(Default, Clone, PartialEq)]
struct OverlayContent {
    text: String,
    dot: u32,
    scale_dpi: u32,
}

/// Accès à l'interface depuis n'importe quel fil.
#[derive(Clone, Copy)]
pub struct UiHandle;

impl UiHandle {
    pub fn update(&self, f: impl FnOnce(&mut UiState)) {
        if let Some(ui) = UI.get() {
            if let Ok(mut s) = ui.state.lock() {
                f(&mut s);
            }
            post(ui, WM_APP_REFRESH);
        }
    }

    pub fn flash(&self, text: impl Into<String>, tone: Tone) {
        let secs = if tone == Tone::Error { 5 } else { 2 };
        let text = text.into();
        self.update(|s| s.flash = Some(Flash { text, tone, until: Instant::now() + Duration::from_secs(secs) }));
    }

    pub fn notify(&self, title: impl Into<String>, text: impl Into<String>, warning: bool) {
        let (title, text) = (title.into(), text.into());
        self.update(|s| s.balloon = Some(Balloon { title, text, warning }));
    }

    /// Ferme Plume (la boucle de messages s'arrête).
    pub fn close(&self) {
        if let Some(ui) = UI.get() {
            post(ui, WM_CLOSE);
        }
    }
}

fn post(ui: &Shared, msg: u32) {
    let hwnd = ui.hwnd.load(Ordering::SeqCst) as HWND;
    if !hwnd.is_null() {
        // SAFETY: envoi d'un message à notre propre fenêtre.
        unsafe {
            PostMessageW(hwnd, msg, 0, 0);
        }
    }
}

/// Crée la fenêtre cachée, l'icône et l'indicateur. À appeler dans le fil principal.
pub fn create(initial: UiState, tx: Sender<Event>, config_path: PathBuf, root: PathBuf) -> Result<UiHandle, String> {
    let shared = Shared {
        state: Mutex::new(initial),
        hwnd: AtomicIsize::new(0),
        overlay: AtomicIsize::new(0),
        taskbar_created: AtomicU32::new(0),
        tx,
        config_path,
        root,
    };
    UI.set(shared).map_err(|_| "interface déjà créée".to_string())?;
    let ui = UI.get().expect("interface initialisée");
    // SAFETY: création de fenêtres Win32 avec des classes enregistrées ici.
    unsafe {
        let instance = GetModuleHandleW(std::ptr::null());
        let main_class = wide("PlumeFenetre");
        let overlay_class = wide("PlumeIndicateur");
        let cursor = LoadCursorW(std::ptr::null_mut(), IDC_ARROW);
        for (class, proc_) in [
            (&main_class, main_proc as unsafe extern "system" fn(HWND, u32, WPARAM, LPARAM) -> LRESULT),
            (&overlay_class, overlay_proc),
        ] {
            let wc = WNDCLASSEXW {
                cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
                style: 0,
                lpfnWndProc: Some(proc_),
                cbClsExtra: 0,
                cbWndExtra: 0,
                hInstance: instance,
                hIcon: std::ptr::null_mut(),
                hCursor: cursor,
                hbrBackground: std::ptr::null_mut(),
                lpszMenuName: std::ptr::null(),
                lpszClassName: class.as_ptr(),
                hIconSm: std::ptr::null_mut(),
            };
            if RegisterClassExW(&wc) == 0 {
                return Err("enregistrement de la classe de fenêtre impossible".into());
            }
        }
        let title = wide("Plume");
        let hwnd = CreateWindowExW(
            0,
            main_class.as_ptr(),
            title.as_ptr(),
            WS_OVERLAPPED,
            0,
            0,
            0,
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            instance,
            std::ptr::null(),
        );
        if hwnd.is_null() {
            return Err("création de la fenêtre impossible".into());
        }
        ui.hwnd.store(hwnd as isize, Ordering::SeqCst);
        let overlay = CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            overlay_class.as_ptr(),
            title.as_ptr(),
            WS_POPUP,
            0,
            0,
            10,
            10,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            instance,
            std::ptr::null(),
        );
        if !overlay.is_null() {
            SetLayeredWindowAttributes(overlay, 0, 238, windows_sys::Win32::UI::WindowsAndMessaging::LWA_ALPHA);
            ui.overlay.store(overlay as isize, Ordering::SeqCst);
        }
        let msg = wide("TaskbarCreated");
        ui.taskbar_created.store(RegisterWindowMessageW(msg.as_ptr()), Ordering::SeqCst);
        add_tray_icon(hwnd);
    }
    refresh();
    Ok(UiHandle)
}

/// Boucle de messages du fil principal ; rend la main quand Plume se ferme.
pub fn run_loop() {
    // SAFETY: boucle de messages Win32 standard.
    unsafe {
        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}

// ───────────────────────────── Icône de notification ─────────────────────────────

fn phase_color(p: Phase) -> (u8, u8, u8) {
    match p {
        Phase::Starting => (0x8A, 0x87, 0x99),
        Phase::Ready => (0x4B, 0x3B, 0xE8),
        Phase::Recording => (0xE5, 0x48, 0x4D),
        Phase::Transcribing => (0xE0, 0x9A, 0x1B),
        Phase::Failed => (0xB4, 0x23, 0x18),
    }
}

fn icon_for(p: Phase) -> HICON {
    ICONS.with(|cache| {
        let mut cache = cache.borrow_mut();
        if let Some((_, h)) = cache.iter().find(|(ph, _)| *ph == p) {
            return *h;
        }
        // SAFETY: lecture d'une métrique système.
        let size = unsafe { GetSystemMetrics(SM_CXSMICON) }.clamp(16, 64);
        let h = make_icon(size, phase_color(p));
        cache.push((p, h));
        h
    })
}

/// Dessine l'icône : un disque de couleur et un micro blanc, lissés.
fn make_icon(size: i32, (r, g, b): (u8, u8, u8)) -> HICON {
    let s = size as f32;
    let mut pixels = vec![0u32; (size * size) as usize];
    let seg = |px: f32, py: f32, ax: f32, ay: f32, bx: f32, by: f32| {
        let (vx, vy, wx, wy) = (bx - ax, by - ay, px - ax, py - ay);
        let t = ((wx * vx + wy * vy) / (vx * vx + vy * vy).max(1e-6)).clamp(0.0, 1.0);
        ((px - ax - t * vx).powi(2) + (py - ay - t * vy).powi(2)).sqrt()
    };
    let cx = s / 2.0;
    for y in 0..size {
        for x in 0..size {
            let (px, py) = (x as f32 + 0.5, y as f32 + 0.5);
            let disc = ((px - cx).powi(2) + (py - cx).powi(2)).sqrt() - (s / 2.0 - 0.5);
            let head = seg(px, py, cx, 0.30 * s, cx, 0.47 * s) - 0.135 * s;
            let arc_r = ((px - cx).powi(2) + (py - 0.47 * s).powi(2)).sqrt();
            let holder = if py > 0.47 * s { (arc_r - 0.225 * s).abs() - 0.04 * s } else { f32::INFINITY };
            let stem = seg(px, py, cx, 0.70 * s, cx, 0.79 * s) - 0.04 * s;
            let base = seg(px, py, 0.38 * s, 0.80 * s, 0.62 * s, 0.80 * s) - 0.04 * s;
            let glyph = head.min(holder).min(stem).min(base);
            let cov = |d: f32| (0.5 - d).clamp(0.0, 1.0);
            let a = cov(disc);
            let w = cov(glyph);
            let mix = |c: u8| (c as f32 * (1.0 - w) + 255.0 * w).round() as u32;
            let alpha = (a * 255.0).round() as u32;
            pixels[(y * size + x) as usize] = (alpha << 24) | (mix(r) << 16) | (mix(g) << 8) | mix(b);
        }
    }
    // SAFETY: création de bitmaps et d'une icône à partir de pixels valides.
    unsafe {
        let mut bmi: BITMAPINFO = std::mem::zeroed();
        bmi.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: size,
            biHeight: -size,
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB,
            biSizeImage: 0,
            biXPelsPerMeter: 0,
            biYPelsPerMeter: 0,
            biClrUsed: 0,
            biClrImportant: 0,
        };
        let screen = GetDC(std::ptr::null_mut());
        let mut bits: *mut core::ffi::c_void = std::ptr::null_mut();
        let color = CreateDIBSection(screen, &bmi, DIB_RGB_COLORS, &mut bits, std::ptr::null_mut(), 0);
        ReleaseDC(std::ptr::null_mut(), screen);
        if color.is_null() || bits.is_null() {
            return std::ptr::null_mut();
        }
        std::ptr::copy_nonoverlapping(pixels.as_ptr(), bits as *mut u32, pixels.len());
        // Masque monochrome tout à zéro (lignes alignées sur 16 bits) : l'opacité
        // vient du canal alpha de l'image couleur.
        let mask_bits = vec![0u8; (((size + 15) / 16) * 2 * size) as usize];
        let mask = CreateBitmap(size, size, 1, 1, mask_bits.as_ptr().cast());
        let info = ICONINFO { fIcon: 1, xHotspot: 0, yHotspot: 0, hbmMask: mask, hbmColor: color };
        let icon = CreateIconIndirect(&info);
        DeleteObject(color);
        DeleteObject(mask);
        icon
    }
}

fn tray_data(hwnd: HWND) -> NOTIFYICONDATAW {
    // SAFETY: structure de données simple, entièrement initialisée ensuite.
    let mut nid: NOTIFYICONDATAW = unsafe { std::mem::zeroed() };
    nid.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
    nid.hWnd = hwnd;
    nid.uID = 1;
    nid
}

fn add_tray_icon(hwnd: HWND) {
    let mut nid = tray_data(hwnd);
    nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
    nid.uCallbackMessage = WM_APP_TRAY;
    nid.hIcon = icon_for(Phase::Starting);
    fill_wide(&mut nid.szTip, "Plume — démarrage…");
    // SAFETY: structure valide.
    unsafe {
        Shell_NotifyIconW(NIM_ADD, &nid);
    }
}

fn tooltip(s: &UiState) -> String {
    let base = match s.phase {
        Phase::Starting => match s.download {
            Some(p) => format!("téléchargement du modèle : {p} %"),
            None => "chargement du modèle…".into(),
        },
        Phase::Ready => format!("prêt ({})", s.hotkey),
        Phase::Recording => "écoute…".into(),
        Phase::Transcribing => "transcription…".into(),
        Phase::Failed => format!("problème : {}", s.detail),
    };
    format!("Plume — {base}")
}

// ───────────────────────────── Rafraîchissement ─────────────────────────────

fn refresh() {
    let Some(ui) = UI.get() else { return };
    let hwnd = ui.hwnd.load(Ordering::SeqCst) as HWND;
    let (state, balloon) = {
        let Ok(mut s) = ui.state.lock() else { return };
        if s.flash.as_ref().is_some_and(|f| Instant::now() >= f.until) {
            s.flash = None;
        }
        let b = s.balloon.take();
        (s.clone(), b)
    };

    // L'icône n'est modifiée que si elle change (le minuteur rafraîchit souvent).
    let tip = tooltip(&state);
    let tray_changed = LAST_TRAY.with(|last| {
        let mut last = last.borrow_mut();
        let now = Some((state.phase, tip.clone()));
        let changed = *last != now;
        *last = now;
        changed
    });
    if tray_changed || balloon.is_some() {
        let mut nid = tray_data(hwnd);
        nid.uFlags = NIF_ICON | NIF_TIP;
        nid.hIcon = icon_for(state.phase);
        fill_wide(&mut nid.szTip, &tip);
        if let Some(b) = &balloon {
            nid.uFlags |= NIF_INFO;
            fill_wide(&mut nid.szInfoTitle, &b.title);
            fill_wide(&mut nid.szInfo, &b.text);
            nid.dwInfoFlags = if b.warning { NIIF_WARNING } else { NIIF_INFO };
        }
        // SAFETY: structure valide.
        unsafe {
            Shell_NotifyIconW(NIM_MODIFY, &nid);
        }
    }

    update_overlay(ui, &state);

    // Minuteur : horloge de l'enregistrement et fin des messages éphémères.
    let ticking = state.phase == Phase::Recording || state.flash.is_some() || state.download.is_some();
    // SAFETY: minuteur attaché à notre fenêtre.
    unsafe {
        if ticking {
            SetTimer(hwnd, TIMER_TICK, 250, None);
        } else {
            KillTimer(hwnd, TIMER_TICK);
        }
    }
}

fn overlay_text(s: &UiState) -> Option<(String, u32)> {
    const RED: u32 = 0x4D48E5; // COLORREF = 0x00BBGGRR
    const AMBER: u32 = 0x24A5F5;
    const INDIGO: u32 = 0xF26C7C;
    const GREY: u32 = 0xA8979A;
    if let Some(f) = &s.flash {
        let dot = match f.tone {
            Tone::Info => INDIGO,
            Tone::Muted => GREY,
            Tone::Error => RED,
        };
        return Some((f.text.clone(), dot));
    }
    if !s.indicator {
        return None;
    }
    match s.phase {
        Phase::Recording => {
            let secs = s.record_started.map(|t| t.elapsed().as_secs()).unwrap_or(0);
            let mut t = format!("Écoute  {}:{:02}", secs / 60, secs % 60);
            if !s.engine_ready {
                t.push_str("  ·  modèle en chargement");
            }
            Some((t, RED))
        }
        Phase::Transcribing => Some(("Transcription…".into(), AMBER)),
        Phase::Starting => s.download.map(|p| (format!("Téléchargement du modèle  {p} %"), GREY)),
        _ => None,
    }
}

fn update_overlay(ui: &Shared, s: &UiState) {
    let overlay = ui.overlay.load(Ordering::SeqCst) as HWND;
    if overlay.is_null() {
        return;
    }
    let Some((text, dot)) = overlay_text(s) else {
        // SAFETY: masquage de notre propre fenêtre.
        unsafe {
            ShowWindow(overlay, SW_HIDE);
        }
        return;
    };
    // SAFETY: appels Win32 sur nos fenêtres et un contexte de dessin temporaire.
    unsafe {
        let fg = GetForegroundWindow();
        let dpi = if fg.is_null() { GetDpiForSystem() } else { GetDpiForWindow(fg) };
        let dpi = if dpi == 0 { 96 } else { dpi };
        let px = |v: i32| v * dpi as i32 / 96;
        let content = OverlayContent { text: text.clone(), dot, scale_dpi: dpi };
        let font = font_for(dpi);
        let hdc = GetDC(overlay);
        let old = SelectObject(hdc, font);
        let w_text = wide(&text);
        let mut size = SIZE { cx: 0, cy: 0 };
        GetTextExtentPoint32W(hdc, w_text.as_ptr(), (w_text.len() - 1) as i32, &mut size);
        SelectObject(hdc, old);
        ReleaseDC(overlay, hdc);

        let h = px(36);
        let w = size.cx + px(16 + 10 + 10 + 18);
        // Écran de la fenêtre active ; à défaut, l'écran principal.
        let flag = if fg.is_null() { MONITOR_DEFAULTTOPRIMARY } else { MONITOR_DEFAULTTONEAREST };
        let monitor = MonitorFromWindow(fg, flag);
        let mut mi: MONITORINFO = std::mem::zeroed();
        mi.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
        let mut work: RECT = mi.rcWork;
        if GetMonitorInfoW(monitor, &mut mi) != 0 {
            work = mi.rcWork;
        } else {
            SystemParametersInfoW(SPI_GETWORKAREA, 0, (&mut work as *mut RECT).cast(), 0);
        }
        let x = work.left + (work.right - work.left - w) / 2;
        let y = match s.position {
            IndicatorPosition::Bas => work.bottom - h - px(48),
            IndicatorPosition::Haut => work.top + px(24),
        };
        let changed = OVERLAY.with(|c| {
            let mut c = c.borrow_mut();
            let changed = *c != content;
            *c = content;
            changed
        });
        SetWindowPos(overlay, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        let rgn = CreateRoundRectRgn(0, 0, w + 1, h + 1, h, h);
        SetWindowRgn(overlay, rgn, 1);
        if changed {
            InvalidateRect(overlay, std::ptr::null(), 1);
        }
    }
}

fn font_for(dpi: u32) -> HFONT {
    FONT.with(|f| {
        let mut f = f.borrow_mut();
        if let Some((d, h)) = *f
            && d == dpi
        {
            return h;
        }
        if let Some((_, old)) = f.take() {
            // SAFETY: police créée par nous, plus utilisée.
            unsafe {
                DeleteObject(old);
            }
        }
        let face = wide("Segoe UI");
        let height = -(15 * dpi as i32 / 96);
        // SAFETY: création d'une police GDI.
        let h = unsafe { CreateFontW(height, 0, 0, 0, FW_SEMIBOLD as i32, 0, 0, 0, 1, 0, 0, 5, 0, face.as_ptr()) };
        *f = Some((dpi, h));
        h
    })
}

unsafe extern "system" fn overlay_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if msg == WM_PAINT {
        let content = OVERLAY.with(|c| c.borrow().clone());
        // SAFETY: dessin dans notre propre fenêtre entre BeginPaint et EndPaint.
        unsafe {
            let mut ps: PAINTSTRUCT = std::mem::zeroed();
            let hdc = BeginPaint(hwnd, &mut ps);
            let dpi = content.scale_dpi.max(96) as i32;
            let px = |v: i32| v * dpi / 96;
            let mut rc: RECT = std::mem::zeroed();
            windows_sys::Win32::UI::WindowsAndMessaging::GetClientRect(hwnd, &mut rc);
            let bg = CreateSolidBrush(0x00221B1C as COLORREF);
            FillRect(hdc, &rc, bg);
            DeleteObject(bg);

            let dot = CreateSolidBrush(content.dot as COLORREF);
            let old_brush = SelectObject(hdc, dot);
            let old_pen = SelectObject(hdc, GetStockObject(NULL_PEN));
            let d = px(10);
            let cy = (rc.bottom - rc.top) / 2;
            let x0 = px(16);
            Ellipse(hdc, x0, cy - d / 2, x0 + d, cy + d / 2 + 1);
            SelectObject(hdc, old_pen);
            SelectObject(hdc, old_brush);
            DeleteObject(dot);

            let font = font_for(dpi as u32);
            let old_font = SelectObject(hdc, font);
            SetBkMode(hdc, TRANSPARENT as i32);
            SetTextColor(hdc, 0x00F7F2F3);
            let mut text_rc = RECT { left: x0 + d + px(10), top: 0, right: rc.right - px(8), bottom: rc.bottom };
            let w_text = wide(&content.text);
            DrawTextW(
                hdc,
                w_text.as_ptr(),
                (w_text.len() - 1) as i32,
                &mut text_rc,
                DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX,
            );
            SelectObject(hdc, old_font);
            EndPaint(hwnd, &ps);
        }
        return 0;
    }
    // SAFETY: traitement par défaut.
    unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
}

// ───────────────────────────── Menu et messages ─────────────────────────────

fn show_menu(hwnd: HWND) {
    let Some(ui) = UI.get() else { return };
    let (status, hotkey) = match ui.state.lock() {
        Ok(s) => (tooltip(&s), s.hotkey.clone()),
        Err(_) => return,
    };
    // SAFETY: menu contextuel Win32 standard, détruit après usage.
    let cmd = unsafe {
        let menu = CreatePopupMenu();
        let header = wide(&format!("Plume {}  ·  {}", plume_core::VERSION, status.trim_start_matches("Plume — ")));
        let shortcut = wide(&format!("Raccourci : {hotkey}"));
        let items = [
            (ID_COPY_LAST, "Copier la dernière dictée"),
            (ID_OPEN_SETTINGS, "Ouvrir les réglages"),
            (ID_RELOAD, "Recharger les réglages"),
            (ID_OPEN_FOLDER, "Ouvrir le dossier de Plume"),
        ];
        AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, header.as_ptr());
        AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, shortcut.as_ptr());
        AppendMenuW(menu, MF_SEPARATOR, 0, std::ptr::null());
        let labels: Vec<Vec<u16>> = items.iter().map(|(_, l)| wide(l)).collect();
        for ((id, _), label) in items.iter().zip(&labels) {
            AppendMenuW(menu, MF_STRING, *id, label.as_ptr());
        }
        AppendMenuW(menu, MF_SEPARATOR, 0, std::ptr::null());
        let quit = wide("Quitter Plume");
        AppendMenuW(menu, MF_STRING, ID_QUIT, quit.as_ptr());
        let mut pt = POINT { x: 0, y: 0 };
        GetCursorPos(&mut pt);
        // Nécessaire pour que le menu se ferme quand on clique ailleurs.
        SetForegroundWindow(hwnd);
        let cmd = TrackPopupMenu(
            menu,
            TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
            pt.x,
            pt.y,
            0,
            hwnd,
            std::ptr::null(),
        );
        PostMessageW(hwnd, WM_NULL, 0, 0);
        DestroyMenu(menu);
        cmd as usize
    };
    let send = |c: MenuCommand| {
        let _ = ui.tx.send(Event::Menu(c));
    };
    match cmd {
        ID_COPY_LAST => send(MenuCommand::CopyLast),
        ID_OPEN_SETTINGS => open_text_file(&ui.config_path),
        ID_RELOAD => send(MenuCommand::Reload),
        ID_OPEN_FOLDER => {
            shell_open(&ui.root);
        }
        ID_QUIT => send(MenuCommand::Quit),
        _ => {}
    }
}

unsafe extern "system" fn main_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_APP_REFRESH | WM_TIMER => {
            refresh();
            0
        }
        WM_APP_TRAY => {
            let event = (lparam & 0xFFFF) as u32;
            if event == WM_RBUTTONUP || event == WM_LBUTTONUP || event == WM_CONTEXTMENU {
                show_menu(hwnd);
            }
            0
        }
        WM_COMMAND => 0,
        WM_CLOSE => {
            // SAFETY: destruction de notre propre fenêtre.
            unsafe {
                DestroyWindow(hwnd);
            }
            0
        }
        WM_DESTROY => {
            let nid = tray_data(hwnd);
            // SAFETY: retrait de l'icône, libération des ressources, fin de boucle.
            unsafe {
                Shell_NotifyIconW(NIM_DELETE, &nid);
                ICONS.with(|c| {
                    for (_, h) in c.borrow_mut().drain(..) {
                        DestroyIcon(h);
                    }
                });
                if let Some(ui) = UI.get() {
                    let overlay = ui.overlay.swap(0, Ordering::SeqCst) as HWND;
                    if !overlay.is_null() {
                        DestroyWindow(overlay);
                    }
                }
                PostQuitMessage(0);
            }
            0
        }
        _ => {
            // Explorateur relancé : l'icône doit être remise.
            if let Some(ui) = UI.get()
                && msg != 0
                && msg == ui.taskbar_created.load(Ordering::SeqCst)
            {
                add_tray_icon(hwnd);
                LAST_TRAY.with(|l| *l.borrow_mut() = None);
                refresh();
                return 0;
            }
            // SAFETY: traitement par défaut.
            unsafe { DefWindowProcW(hwnd, msg, wparam, lparam) }
        }
    }
}
