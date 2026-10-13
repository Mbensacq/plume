//! Que faire quand le raccourci est appuyé ou relâché ?
//!
//! Trois modes (réglage `[raccourci] mode`) :
//! - `auto` (défaut) : appui **court** = démarre, un nouvel appui arrête ;
//!   appui **long** = parle tant que c'est maintenu, relâcher arrête.
//! - `maintenir` : on parle tant que le raccourci est maintenu.
//! - `basculer` : un appui démarre, l'appui suivant arrête.
//!
//! Cette logique est pure (le temps est passé en paramètre) pour être testée.

use serde::Deserialize;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum TriggerMode {
    #[default]
    Auto,
    Maintenir,
    Basculer,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Input {
    Pressed,
    Released,
    /// Échap pendant l'enregistrement.
    Cancel,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Start,
    Stop,
    Cancel,
    Nothing,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Idle,
    /// Enregistrement en cours, raccourci encore enfoncé depuis `since`.
    Held {
        since: Instant,
    },
    /// Enregistrement en cours, raccourci relâché (mode bascule).
    Latched,
    /// Arrêté par un appui : on ignore le relâchement qui va suivre.
    WaitRelease,
}

#[derive(Debug, Clone)]
pub struct TriggerMachine {
    mode: TriggerMode,
    long_press: Duration,
    state: State,
}

impl TriggerMachine {
    pub fn new(mode: TriggerMode, long_press: Duration) -> Self {
        TriggerMachine { mode, long_press, state: State::Idle }
    }

    pub fn set_mode(&mut self, mode: TriggerMode, long_press: Duration) {
        self.mode = mode;
        self.long_press = long_press;
    }

    pub fn is_recording(&self) -> bool {
        matches!(self.state, State::Held { .. } | State::Latched)
    }

    /// Remet à zéro (ex. l'enregistrement a échoué ou a été arrêté ailleurs).
    pub fn reset(&mut self) {
        self.state = State::Idle;
    }

    pub fn on(&mut self, input: Input, now: Instant) -> Action {
        use Action as A;
        use State as S;
        let (next, action) = match (self.state, input) {
            (S::Idle, Input::Pressed) => (S::Held { since: now }, A::Start),
            (S::Idle, _) => (S::Idle, A::Nothing),

            (S::Held { since }, Input::Released) => {
                let held = now.saturating_duration_since(since);
                let stop = match self.mode {
                    TriggerMode::Maintenir => true,
                    TriggerMode::Basculer => false,
                    TriggerMode::Auto => held >= self.long_press,
                };
                if stop { (S::Idle, A::Stop) } else { (S::Latched, A::Nothing) }
            }
            (S::Held { .. }, Input::Cancel) => (S::WaitRelease, A::Cancel),
            (s @ S::Held { .. }, Input::Pressed) => (s, A::Nothing),

            (S::Latched, Input::Pressed) => (S::WaitRelease, A::Stop),
            (S::Latched, Input::Cancel) => (S::Idle, A::Cancel),
            (S::Latched, Input::Released) => (S::Latched, A::Nothing),

            (S::WaitRelease, Input::Released) => (S::Idle, A::Nothing),
            // Un nouvel appui sans relâchement vu (événement perdu) : on repart.
            (S::WaitRelease, Input::Pressed) => (S::Held { since: now }, A::Start),
            (S::WaitRelease, Input::Cancel) => (S::WaitRelease, A::Nothing),
        };
        self.state = next;
        action
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const LONG: Duration = Duration::from_millis(350);

    fn t0() -> Instant {
        Instant::now()
    }

    #[test]
    fn auto_short_tap_toggles() {
        let mut m = TriggerMachine::new(TriggerMode::Auto, LONG);
        let t = t0();
        assert_eq!(m.on(Input::Pressed, t), Action::Start);
        assert_eq!(m.on(Input::Released, t + Duration::from_millis(120)), Action::Nothing);
        assert!(m.is_recording());
        assert_eq!(m.on(Input::Pressed, t + Duration::from_secs(10)), Action::Stop);
        assert!(!m.is_recording());
        assert_eq!(m.on(Input::Released, t + Duration::from_secs(10)), Action::Nothing);
        // Prêt pour la dictée suivante.
        assert_eq!(m.on(Input::Pressed, t + Duration::from_secs(12)), Action::Start);
    }

    #[test]
    fn auto_long_press_is_push_to_talk() {
        let mut m = TriggerMachine::new(TriggerMode::Auto, LONG);
        let t = t0();
        assert_eq!(m.on(Input::Pressed, t), Action::Start);
        assert_eq!(m.on(Input::Released, t + Duration::from_secs(4)), Action::Stop);
        assert!(!m.is_recording());
        assert_eq!(m.on(Input::Pressed, t + Duration::from_secs(5)), Action::Start);
    }

    #[test]
    fn hold_mode_always_stops_on_release() {
        let mut m = TriggerMachine::new(TriggerMode::Maintenir, LONG);
        let t = t0();
        assert_eq!(m.on(Input::Pressed, t), Action::Start);
        assert_eq!(m.on(Input::Released, t + Duration::from_millis(50)), Action::Stop);
    }

    #[test]
    fn toggle_mode_never_stops_on_release() {
        let mut m = TriggerMachine::new(TriggerMode::Basculer, LONG);
        let t = t0();
        assert_eq!(m.on(Input::Pressed, t), Action::Start);
        assert_eq!(m.on(Input::Released, t + Duration::from_secs(3)), Action::Nothing);
        assert_eq!(m.on(Input::Pressed, t + Duration::from_secs(6)), Action::Stop);
    }

    #[test]
    fn cancel_while_recording() {
        let mut m = TriggerMachine::new(TriggerMode::Auto, LONG);
        let t = t0();
        m.on(Input::Pressed, t);
        m.on(Input::Released, t + Duration::from_millis(100));
        assert_eq!(m.on(Input::Cancel, t + Duration::from_secs(2)), Action::Cancel);
        assert!(!m.is_recording());
        assert_eq!(m.on(Input::Cancel, t + Duration::from_secs(3)), Action::Nothing);
    }

    #[test]
    fn cancel_while_held_ignores_following_release() {
        let mut m = TriggerMachine::new(TriggerMode::Auto, LONG);
        let t = t0();
        m.on(Input::Pressed, t);
        assert_eq!(m.on(Input::Cancel, t + Duration::from_secs(1)), Action::Cancel);
        assert_eq!(m.on(Input::Released, t + Duration::from_secs(2)), Action::Nothing);
        assert_eq!(m.on(Input::Pressed, t + Duration::from_secs(3)), Action::Start);
    }

    #[test]
    fn idle_ignores_release_and_cancel() {
        let mut m = TriggerMachine::new(TriggerMode::Auto, LONG);
        assert_eq!(m.on(Input::Released, t0()), Action::Nothing);
        assert_eq!(m.on(Input::Cancel, t0()), Action::Nothing);
    }

    #[test]
    fn reset_returns_to_idle() {
        let mut m = TriggerMachine::new(TriggerMode::Auto, LONG);
        m.on(Input::Pressed, t0());
        m.reset();
        assert!(!m.is_recording());
        assert_eq!(m.on(Input::Pressed, t0()), Action::Start);
    }
}
