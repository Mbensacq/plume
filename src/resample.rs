//! Conversion de fréquence d'échantillonnage (ex. micro à 48 kHz → 16 kHz).
//!
//! Interpolation à sinus cardinal fenêtré (Blackman), avec filtre anti-repliement
//! intégré : de quoi garder une parole propre pour Whisper. Fonctionne en flux
//! (on lui donne le son par petits blocs, au fil de l'enregistrement).

use std::f64::consts::PI;

/// Nombre de passages par zéro du sinus cardinal de chaque côté.
const ZEROS: f64 = 12.0;
/// Résolution de la table du filtre (points par échantillon d'entrée).
const TABLE_RES: f64 = 256.0;

#[derive(Debug, Clone)]
pub struct Resampler {
    /// Avance dans le signal d'entrée pour chaque échantillon de sortie.
    step: f64,
    /// Demi-largeur du filtre, en échantillons d'entrée.
    half_width: f64,
    half: usize,
    table: Vec<f32>,
    buf: Vec<f32>,
    pos: f64,
    total_in: u64,
    total_out: u64,
    passthrough: bool,
}

impl Resampler {
    pub fn new(in_rate: u32, out_rate: u32) -> Resampler {
        assert!(in_rate > 0 && out_rate > 0, "fréquences nulles");
        let step = in_rate as f64 / out_rate as f64;
        // Coupure juste sous la fréquence de Nyquist la plus basse des deux.
        let cutoff = (0.92 / step.max(1.0)).min(0.92);
        let half_width = ZEROS / cutoff;
        let half = half_width.ceil() as usize;
        let n = (half_width * TABLE_RES).ceil() as usize + 2;
        let table = (0..n).map(|i| kernel(i as f64 / TABLE_RES, cutoff, half_width) as f32).collect();
        Resampler {
            step,
            half_width,
            half,
            table,
            buf: vec![0.0; half],
            pos: half as f64,
            total_in: 0,
            total_out: 0,
            passthrough: in_rate == out_rate,
        }
    }

    /// Ajoute `input` et écrit dans `out` tous les échantillons calculables.
    pub fn process(&mut self, input: &[f32], out: &mut Vec<f32>) {
        self.total_in += input.len() as u64;
        if self.passthrough {
            out.extend_from_slice(input);
            self.total_out += input.len() as u64;
            return;
        }
        self.buf.extend_from_slice(input);
        self.drain(out, u64::MAX);
    }

    /// Termine le flux : produit les derniers échantillons.
    pub fn flush(&mut self, out: &mut Vec<f32>) {
        if self.passthrough {
            return;
        }
        let expected = (self.total_in as f64 / self.step).ceil() as u64;
        self.buf.extend(std::iter::repeat_n(0.0, self.half + 2));
        self.drain(out, expected);
    }

    fn drain(&mut self, out: &mut Vec<f32>, max_total: u64) {
        while self.total_out < max_total {
            let center = self.pos.floor() as usize;
            if center + self.half >= self.buf.len() {
                break;
            }
            out.push(self.sample_at(self.pos));
            self.total_out += 1;
            self.pos += self.step;
        }
        // On ne garde que l'historique nécessaire au prochain calcul.
        let center = self.pos.floor() as usize;
        let keep_from = center.saturating_sub(self.half);
        if keep_from > 0 {
            let keep_from = keep_from.min(self.buf.len());
            self.buf.drain(..keep_from);
            self.pos -= keep_from as f64;
        }
    }

    fn sample_at(&self, p: f64) -> f32 {
        let center = p.floor() as isize;
        let lo = (center - self.half as isize + 1).max(0) as usize;
        let hi = ((center + self.half as isize) as usize).min(self.buf.len() - 1);
        let mut acc = 0.0f32;
        let mut wsum = 0.0f32;
        for k in lo..=hi {
            let w = self.weight((p - k as f64).abs());
            acc += self.buf[k] * w;
            wsum += w;
        }
        if wsum.abs() > 1e-9 { acc / wsum } else { 0.0 }
    }

    fn weight(&self, dist: f64) -> f32 {
        if dist >= self.half_width {
            return 0.0;
        }
        let x = dist * TABLE_RES;
        let i = x as usize;
        if i + 1 >= self.table.len() {
            return 0.0;
        }
        let frac = (x - i as f64) as f32;
        self.table[i] + (self.table[i + 1] - self.table[i]) * frac
    }
}

/// Noyau du filtre : sinus cardinal × fenêtre de Blackman.
fn kernel(x: f64, cutoff: f64, half_width: f64) -> f64 {
    if x >= half_width {
        return 0.0;
    }
    let sinc = if x == 0.0 { 1.0 } else { (PI * cutoff * x).sin() / (PI * cutoff * x) };
    let t = x / half_width;
    let window = 0.42 + 0.5 * (PI * t).cos() + 0.08 * (2.0 * PI * t).cos();
    cutoff * sinc * window
}

/// Raccourci : convertit un signal complet d'un coup.
pub fn resample_all(input: &[f32], in_rate: u32, out_rate: u32) -> Vec<f32> {
    let mut r = Resampler::new(in_rate, out_rate);
    let mut out = Vec::with_capacity((input.len() as f64 * out_rate as f64 / in_rate as f64) as usize + 8);
    r.process(input, &mut out);
    r.flush(&mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(freq: f64, rate: u32, secs: f64) -> Vec<f32> {
        let n = (rate as f64 * secs) as usize;
        (0..n).map(|i| (2.0 * PI * freq * i as f64 / rate as f64).sin() as f32 * 0.5).collect()
    }

    /// Amplitude crête sur la partie centrale (hors bords).
    fn peak_mid(x: &[f32]) -> f32 {
        let a = x.len() / 4;
        let b = x.len() * 3 / 4;
        x[a..b].iter().fold(0.0f32, |m, v| m.max(v.abs()))
    }

    #[test]
    fn output_length_matches_ratio() {
        for (inr, secs) in [(48_000u32, 1.0), (44_100, 2.5), (22_050, 0.3), (8_000, 1.0)] {
            let x = sine(440.0, inr, secs);
            let y = resample_all(&x, inr, 16_000);
            let expected = (x.len() as f64 * 16_000.0 / inr as f64).ceil() as usize;
            assert_eq!(y.len(), expected, "{inr} Hz");
        }
    }

    #[test]
    fn keeps_speech_band_tone() {
        for inr in [48_000u32, 44_100, 32_000] {
            let y = resample_all(&sine(1_000.0, inr, 1.0), inr, 16_000);
            let p = peak_mid(&y);
            assert!((p - 0.5).abs() < 0.02, "{inr} Hz : crête {p}");
        }
    }

    #[test]
    fn removes_tones_above_new_nyquist() {
        // 12 kHz n'existe pas à 16 kHz : sans filtre, il se replierait à 4 kHz.
        let y = resample_all(&sine(12_000.0, 48_000, 1.0), 48_000, 16_000);
        let p = peak_mid(&y);
        assert!(p < 0.01, "repliement non filtré : crête {p}");
    }

    #[test]
    fn streaming_equals_one_shot() {
        let x = sine(700.0, 44_100, 1.3);
        let whole = resample_all(&x, 44_100, 16_000);
        let mut r = Resampler::new(44_100, 16_000);
        let mut streamed = Vec::new();
        for block in x.chunks(441) {
            r.process(block, &mut streamed);
        }
        r.flush(&mut streamed);
        assert_eq!(whole.len(), streamed.len());
        for (a, b) in whole.iter().zip(&streamed) {
            assert!((a - b).abs() < 1e-5);
        }
    }

    #[test]
    fn same_rate_is_identity() {
        let x = sine(300.0, 16_000, 0.5);
        assert_eq!(resample_all(&x, 16_000, 16_000), x);
    }

    #[test]
    fn upsampling_works() {
        let y = resample_all(&sine(1_000.0, 8_000, 1.0), 8_000, 16_000);
        assert_eq!(y.len(), 16_000);
        assert!((peak_mid(&y) - 0.5).abs() < 0.02);
    }

    #[test]
    fn dc_is_preserved() {
        let y = resample_all(&vec![0.25f32; 48_000], 48_000, 16_000);
        for v in &y[100..y.len() - 100] {
            assert!((v - 0.25).abs() < 1e-4);
        }
    }
}
