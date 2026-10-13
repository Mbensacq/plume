//! Raccourci de dictée tel qu'écrit dans `plume.toml`, par exemple
//! `"Ctrl+Alt+Espace"`, `"F13"`, `"Ctrl+Souris4"` ou `"Menu"`.
//!
//! Un raccourci = des modificateurs (Ctrl, Alt, Maj, Win) + exactement une
//! touche ou un bouton de souris « déclencheur ». Les codes de touches sont les
//! codes virtuels Windows (VK_*), stockés ici sans dépendre de Windows pour
//! pouvoir être testés partout.

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Modifiers {
    pub ctrl: bool,
    pub alt: bool,
    pub shift: bool,
    pub win: bool,
}

impl Modifiers {
    pub fn any(&self) -> bool {
        self.ctrl || self.alt || self.shift || self.win
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseButton {
    /// Molette cliquée.
    Middle,
    /// Bouton latéral « précédent » (XBUTTON1).
    Back,
    /// Bouton latéral « suivant » (XBUTTON2).
    Forward,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Trigger {
    /// Code de touche virtuelle Windows (VK_*).
    Key(u16),
    Mouse(MouseButton),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hotkey {
    pub mods: Modifiers,
    pub trigger: Trigger,
}

/// Codes de touches virtuelles Windows utilisés par Plume.
pub mod vk {
    pub const BACK: u16 = 0x08;
    pub const TAB: u16 = 0x09;
    pub const RETURN: u16 = 0x0D;
    pub const PAUSE: u16 = 0x13;
    pub const ESCAPE: u16 = 0x1B;
    pub const SPACE: u16 = 0x20;
    pub const PRIOR: u16 = 0x21;
    pub const NEXT: u16 = 0x22;
    pub const END: u16 = 0x23;
    pub const HOME: u16 = 0x24;
    pub const LEFT: u16 = 0x25;
    pub const UP: u16 = 0x26;
    pub const RIGHT: u16 = 0x27;
    pub const DOWN: u16 = 0x28;
    pub const SNAPSHOT: u16 = 0x2C;
    pub const INSERT: u16 = 0x2D;
    pub const DELETE: u16 = 0x2E;
    pub const APPS: u16 = 0x5D;
    pub const NUMPAD0: u16 = 0x60;
    pub const MULTIPLY: u16 = 0x6A;
    pub const ADD: u16 = 0x6B;
    pub const SUBTRACT: u16 = 0x6D;
    pub const DECIMAL: u16 = 0x6E;
    pub const DIVIDE: u16 = 0x6F;
    pub const F1: u16 = 0x70;
    pub const SCROLL: u16 = 0x91;
}

impl Hotkey {
    /// Analyse un raccourci écrit par l'utilisateur. Insensible à la casse et
    /// aux accents ; noms français ou anglais acceptés.
    pub fn parse(input: &str) -> Result<Hotkey, String> {
        let input = input.trim();
        if input.is_empty() {
            return Err("le raccourci est vide".into());
        }
        let mut mods = Modifiers::default();
        let mut trigger: Option<Trigger> = None;

        for raw in input.split('+') {
            let token = normalize(raw);
            if token.is_empty() {
                return Err(format!("« {input} » est mal formé (un « + » en trop ?)"));
            }
            match token.as_str() {
                "ctrl" | "control" | "controle" | "ctl" => mods.ctrl = true,
                "alt" => mods.alt = true,
                "altgr" => {
                    mods.ctrl = true;
                    mods.alt = true;
                }
                "maj" | "shift" | "majuscule" => mods.shift = true,
                "win" | "windows" | "super" | "meta" => mods.win = true,
                other => {
                    let t = parse_trigger(other).ok_or_else(|| format!("touche inconnue : « {} »", raw.trim()))?;
                    if trigger.is_some() {
                        return Err(format!(
                            "« {input} » contient plusieurs touches principales ; une seule est permise"
                        ));
                    }
                    trigger = Some(t);
                }
            }
        }

        let trigger = trigger.ok_or_else(|| {
            format!("« {input} » ne contient que des modificateurs ; ajoutez une touche (ex. Espace)")
        })?;

        if let Trigger::Key(code) = trigger {
            if code == vk::ESCAPE {
                return Err("Échap est réservé à l'annulation d'une dictée".into());
            }
            if is_typing_key(code) && !(mods.ctrl || mods.alt || mods.win) {
                return Err(format!("« {input} » gênerait la frappe normale ; combinez-la avec Ctrl, Alt ou Win"));
            }
        }
        Ok(Hotkey { mods, trigger })
    }

    /// Libellé lisible, ex. « Ctrl + Alt + Espace ».
    pub fn label(&self) -> String {
        let mut parts: Vec<String> = Vec::new();
        if self.mods.ctrl {
            parts.push("Ctrl".into());
        }
        if self.mods.alt {
            parts.push("Alt".into());
        }
        if self.mods.shift {
            parts.push("Maj".into());
        }
        if self.mods.win {
            parts.push("Win".into());
        }
        parts.push(match self.trigger {
            Trigger::Key(code) => key_name(code),
            Trigger::Mouse(MouseButton::Middle) => "Clic molette".into(),
            Trigger::Mouse(MouseButton::Back) => "Souris 4".into(),
            Trigger::Mouse(MouseButton::Forward) => "Souris 5".into(),
        });
        parts.join(" + ")
    }
}

impl fmt::Display for Hotkey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.label())
    }
}

/// Minuscules, sans accents ni espaces.
fn normalize(s: &str) -> String {
    s.trim()
        .chars()
        .filter(|c| !c.is_whitespace() && *c != '_' && *c != '-')
        .flat_map(char::to_lowercase)
        .map(|c| match c {
            'é' | 'è' | 'ê' | 'ë' => 'e',
            'à' | 'â' | 'ä' => 'a',
            'î' | 'ï' => 'i',
            'ô' | 'ö' => 'o',
            'ù' | 'û' | 'ü' => 'u',
            'ç' => 'c',
            other => other,
        })
        .collect()
}

fn parse_trigger(t: &str) -> Option<Trigger> {
    use MouseButton::*;
    let mouse = match t {
        "souris3" | "mouse3" | "clicmolette" | "molette" | "middle" | "boutonmilieu" => Some(Middle),
        "souris4" | "mouse4" | "xbutton1" | "precedent" | "back" => Some(Back),
        "souris5" | "mouse5" | "xbutton2" | "suivant" | "forward" => Some(Forward),
        _ => None,
    };
    if let Some(b) = mouse {
        return Some(Trigger::Mouse(b));
    }

    let code = match t {
        "espace" | "space" => vk::SPACE,
        "tab" | "tabulation" => vk::TAB,
        "entree" | "enter" | "return" => vk::RETURN,
        "retourarriere" | "backspace" => vk::BACK,
        "echap" | "esc" | "escape" | "echappement" => vk::ESCAPE,
        "pause" => vk::PAUSE,
        "arretdefil" | "scrolllock" | "defil" => vk::SCROLL,
        "impr" | "imprecran" | "printscreen" => vk::SNAPSHOT,
        "inser" | "insert" | "ins" => vk::INSERT,
        "suppr" | "delete" | "del" => vk::DELETE,
        "debut" | "home" | "origine" => vk::HOME,
        "fin" | "end" => vk::END,
        "pageprec" | "pgup" | "pageup" => vk::PRIOR,
        "pagesuiv" | "pgdn" | "pagedown" => vk::NEXT,
        "haut" | "up" => vk::UP,
        "bas" | "down" => vk::DOWN,
        "gauche" | "left" => vk::LEFT,
        "droite" | "right" => vk::RIGHT,
        "menu" | "apps" | "application" => vk::APPS,
        // « + » sépare les touches et « - » est ignoré : on les nomme en toutes lettres.
        "numplus" => vk::ADD,
        "nummoins" => vk::SUBTRACT,
        "numfois" | "num*" => vk::MULTIPLY,
        "numdiv" | "num/" => vk::DIVIDE,
        "numpoint" | "num." => vk::DECIMAL,
        _ => {
            return parse_function_key(t)
                .or_else(|| parse_numpad_digit(t))
                .or_else(|| parse_alnum(t))
                .map(Trigger::Key);
        }
    };
    Some(Trigger::Key(code))
}

fn parse_function_key(t: &str) -> Option<u16> {
    let n: u16 = t.strip_prefix('f')?.parse().ok()?;
    (1..=24).contains(&n).then(|| vk::F1 + n - 1)
}

fn parse_numpad_digit(t: &str) -> Option<u16> {
    let rest = t.strip_prefix("num").or_else(|| t.strip_prefix("pave"))?;
    let d: u16 = rest.parse().ok()?;
    (d <= 9).then(|| vk::NUMPAD0 + d)
}

fn parse_alnum(t: &str) -> Option<u16> {
    let mut chars = t.chars();
    let c = chars.next()?;
    if chars.next().is_some() {
        return None;
    }
    match c {
        'a'..='z' => Some(c.to_ascii_uppercase() as u16),
        '0'..='9' => Some(c as u16),
        _ => None,
    }
}

/// Touches qui produisent du texte ou servent en permanence à écrire.
fn is_typing_key(code: u16) -> bool {
    matches!(code, 0x30..=0x39 | 0x41..=0x5A) || matches!(code, vk::SPACE | vk::TAB | vk::RETURN | vk::BACK)
}

fn key_name(code: u16) -> String {
    match code {
        vk::SPACE => "Espace".into(),
        vk::TAB => "Tab".into(),
        vk::RETURN => "Entrée".into(),
        vk::BACK => "Retour arrière".into(),
        vk::PAUSE => "Pause".into(),
        vk::SCROLL => "Arrêt défil".into(),
        vk::SNAPSHOT => "Impr écran".into(),
        vk::INSERT => "Inser".into(),
        vk::DELETE => "Suppr".into(),
        vk::HOME => "Début".into(),
        vk::END => "Fin".into(),
        vk::PRIOR => "Page préc".into(),
        vk::NEXT => "Page suiv".into(),
        vk::UP => "Haut".into(),
        vk::DOWN => "Bas".into(),
        vk::LEFT => "Gauche".into(),
        vk::RIGHT => "Droite".into(),
        vk::APPS => "Menu".into(),
        vk::ADD => "Num +".into(),
        vk::SUBTRACT => "Num -".into(),
        vk::MULTIPLY => "Num *".into(),
        vk::DIVIDE => "Num /".into(),
        vk::DECIMAL => "Num .".into(),
        c if (vk::F1..vk::F1 + 24).contains(&c) => format!("F{}", c - vk::F1 + 1),
        c if (vk::NUMPAD0..=vk::NUMPAD0 + 9).contains(&c) => format!("Num {}", c - vk::NUMPAD0),
        c if (0x30..=0x39).contains(&c) || (0x41..=0x5A).contains(&c) => char::from(c as u8).to_string(),
        c => format!("Touche {c}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(mods: Modifiers, code: u16) -> Hotkey {
        Hotkey { mods, trigger: Trigger::Key(code) }
    }

    #[test]
    fn parses_default_shortcut() {
        let h = Hotkey::parse("Ctrl+Alt+Espace").unwrap();
        assert_eq!(h, key(Modifiers { ctrl: true, alt: true, ..Default::default() }, vk::SPACE));
        assert_eq!(h.label(), "Ctrl + Alt + Espace");
    }

    #[test]
    fn is_case_space_and_accent_insensitive() {
        let a = Hotkey::parse("  ctrl + MAJ + échap ");
        assert!(a.is_err(), "Échap est réservé");
        let b = Hotkey::parse("CTRL + maj + Entrée").unwrap();
        assert_eq!(b.trigger, Trigger::Key(vk::RETURN));
        assert!(b.mods.ctrl && b.mods.shift);
    }

    #[test]
    fn parses_function_numpad_and_letters() {
        assert_eq!(Hotkey::parse("F13").unwrap().trigger, Trigger::Key(0x7C));
        assert_eq!(Hotkey::parse("f24").unwrap().trigger, Trigger::Key(0x87));
        assert!(Hotkey::parse("F25").is_err());
        assert_eq!(Hotkey::parse("Num7").unwrap().trigger, Trigger::Key(0x67));
        assert_eq!(Hotkey::parse("Ctrl+D").unwrap().trigger, Trigger::Key(0x44));
        assert_eq!(Hotkey::parse("Win+Alt+5").unwrap().trigger, Trigger::Key(0x35));
        assert_eq!(Hotkey::parse("Menu").unwrap().label(), "Menu");
        assert_eq!(Hotkey::parse("NumPlus").unwrap().trigger, Trigger::Key(vk::ADD));
        assert_eq!(Hotkey::parse("NumMoins").unwrap().trigger, Trigger::Key(vk::SUBTRACT));
    }

    #[test]
    fn parses_mouse_buttons() {
        let h = Hotkey::parse("Ctrl+Souris4").unwrap();
        assert_eq!(h.trigger, Trigger::Mouse(MouseButton::Back));
        assert!(h.mods.ctrl);
        assert_eq!(h.label(), "Ctrl + Souris 4");
        assert_eq!(Hotkey::parse("Souris5").unwrap().trigger, Trigger::Mouse(MouseButton::Forward));
        assert_eq!(Hotkey::parse("Alt+Molette").unwrap().trigger, Trigger::Mouse(MouseButton::Middle));
    }

    #[test]
    fn altgr_means_ctrl_alt() {
        let h = Hotkey::parse("AltGr+P").unwrap();
        assert!(h.mods.ctrl && h.mods.alt);
    }

    #[test]
    fn rejects_bad_shortcuts() {
        assert!(Hotkey::parse("").is_err());
        assert!(Hotkey::parse("Ctrl+Alt").is_err(), "modificateurs seuls");
        assert!(Hotkey::parse("Ctrl++Espace").is_err());
        assert!(Hotkey::parse("Ctrl+A+B").is_err(), "deux touches");
        assert!(Hotkey::parse("Ctrl+Truc").is_err());
        assert!(Hotkey::parse("Espace").is_err(), "gênerait la frappe");
        assert!(Hotkey::parse("Maj+A").is_err(), "Maj seul ne suffit pas");
        assert!(Hotkey::parse("Échap").is_err());
    }

    #[test]
    fn error_messages_are_readable() {
        let e = Hotkey::parse("Ctrl+Truc").unwrap_err();
        assert!(e.contains("Truc"), "{e}");
    }
}
