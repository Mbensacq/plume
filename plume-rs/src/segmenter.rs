//! Découpe la parole en morceaux **pendant** l'enregistrement.
//!
//! C'est ce qui rend Plume rapide sur les longues dictées : chaque morceau est
//! transcrit dès qu'il est terminé, pendant que l'on parle encore. À l'arrêt,
//! il ne reste que le dernier morceau à traiter.
//!
//! Règles de coupe (son à 16 kHz, analysé par trames de 30 ms) :
//! - un morceau d'au moins `min_chunk_ms` est coupé à la première pause d'au
//!   moins `pause_ms` ;
//! - un silence très long (`long_silence_ms`) coupe même un morceau court ;
//! - un morceau qui atteint `max_chunk_ms` sans pause est coupé à l'endroit le
//!   plus calme de ses dernières secondes (jamais en plein mot si possible).
//!
//! Chaque morceau est ensuite « resserré » : les longs silences sont raccourcis
//! (Whisper invente du texte sur les silences) et les morceaux sans parole sont
//! ignorés.

use crate::SAMPLE_RATE;
use std::collections::VecDeque;

const FRAME: usize = (SAMPLE_RATE as usize) * 30 / 1000; // 480 échantillons = 30 ms
const FRAME_MS: u32 = 30;
/// Historique utilisé pour estimer le bruit de fond (≈ 6 s).
const NOISE_HISTORY: usize = 200;

#[derive(Debug, Clone)]
pub struct SegmenterConfig {
    pub pause_ms: u32,
    pub min_chunk_ms: u32,
    pub max_chunk_ms: u32,
    pub long_silence_ms: u32,
    /// Silence conservé de part et d'autre de la parole.
    pub keep_pad_ms: u32,
    /// Au-delà, un silence intérieur est raccourci.
    pub max_inner_silence_ms: u32,
    /// En dessous de cette quantité de parole, le morceau est ignoré.
    pub min_speech_ms: u32,
    /// Écart (dB) au-dessus du bruit de fond pour considérer qu'on parle.
    pub speech_margin_db: f32,
}

impl Default for SegmenterConfig {
    fn default() -> Self {
        SegmenterConfig {
            pause_ms: 600,
            min_chunk_ms: 6_000,
            max_chunk_ms: 25_000,
            long_silence_ms: 1_200,
            keep_pad_ms: 400,
            max_inner_silence_ms: 1_500,
            min_speech_ms: 200,
            speech_margin_db: 6.0,
        }
    }
}

/// Un morceau prêt à transcrire.
#[derive(Debug, Clone)]
pub struct Chunk {
    /// Son 16 kHz mono, silences resserrés.
    pub samples: Vec<f32>,
    /// Durée de parole détectée (ms).
    pub speech_ms: u32,
    /// Durée d'origine du morceau, avant resserrage (ms).
    pub source_ms: u32,
}

#[derive(Debug, Clone, Copy)]
struct FrameInfo {
    db: f32,
    speech: bool,
}

#[derive(Debug, Clone)]
pub struct Segmenter {
    cfg: SegmenterConfig,
    pending: Vec<f32>,
    frames: Vec<FrameInfo>,
    speech_frames: usize,
    noise: NoiseFloor,
}

impl Segmenter {
    pub fn new(cfg: SegmenterConfig) -> Segmenter {
        Segmenter { cfg, pending: Vec::new(), frames: Vec::new(), speech_frames: 0, noise: NoiseFloor::default() }
    }

    /// Ajoute du son (16 kHz mono) ; renvoie les morceaux terminés.
    pub fn push(&mut self, samples: &[f32]) -> Vec<Chunk> {
        self.pending.extend_from_slice(samples);
        let mut out = Vec::new();
        while self.frames.len() < self.pending.len() / FRAME {
            let i = self.frames.len();
            let db = frame_db(&self.pending[i * FRAME..(i + 1) * FRAME]);
            self.noise.observe(db);
            let speech = db > self.noise.threshold(self.cfg.speech_margin_db);
            self.frames.push(FrameInfo { db, speech });
            if speech {
                self.speech_frames += 1;
            }
            if let Some(cut) = self.cut_point() {
                if let Some(chunk) = self.split_at(cut) {
                    out.push(chunk);
                }
            } else {
                self.drop_leading_silence();
            }
        }
        out
    }

    /// Fin de l'enregistrement : renvoie le dernier morceau, s'il contient de la parole.
    pub fn finish(&mut self) -> Option<Chunk> {
        let rest = self.pending.len() - self.frames.len() * FRAME;
        if rest > 0 {
            let start = self.frames.len() * FRAME;
            let db = frame_db(&self.pending[start..]);
            let speech = db > self.noise.threshold(self.cfg.speech_margin_db);
            self.frames.push(FrameInfo { db, speech });
            if speech {
                self.speech_frames += 1;
            }
            self.pending.resize(self.frames.len() * FRAME, 0.0);
        }
        let n = self.frames.len();
        if n == 0 {
            return None;
        }
        self.split_at(n)
    }

    /// Durée actuellement en attente (non encore envoyée), en ms.
    pub fn pending_ms(&self) -> u32 {
        (self.pending.len() as u64 * 1000 / SAMPLE_RATE as u64) as u32
    }

    fn cut_point(&self) -> Option<usize> {
        let n = self.frames.len();
        let dur = n as u32 * FRAME_MS;
        if self.speech_frames == 0 {
            return None;
        }
        let trailing = self.frames.iter().rev().take_while(|f| !f.speech).count();
        let trailing_ms = trailing as u32 * FRAME_MS;
        let speech_dur = (n - trailing) as u32 * FRAME_MS;

        if speech_dur >= self.cfg.min_chunk_ms && trailing_ms >= self.cfg.pause_ms {
            return Some(n - trailing / 2);
        }
        if trailing_ms >= self.cfg.long_silence_ms {
            return Some(n - trailing / 2);
        }
        if dur >= self.cfg.max_chunk_ms {
            return Some(self.quietest_cut());
        }
        None
    }

    /// Point de coupe le plus calme dans les 6 dernières secondes.
    fn quietest_cut(&self) -> usize {
        let n = self.frames.len();
        let search = (6_000 / FRAME_MS) as usize;
        let from = n.saturating_sub(search).max(1);
        let to = n.saturating_sub(2).max(from + 1);
        let mut best = to;
        let mut best_score = f32::INFINITY;
        for i in from..to {
            // Énergie moyenne sur ±2 trames ; un silence détecté est privilégié.
            let lo = i.saturating_sub(2);
            let hi = (i + 3).min(n);
            let avg = self.frames[lo..hi].iter().map(|f| f.db).sum::<f32>() / (hi - lo) as f32;
            let score = if self.frames[i].speech { avg + 20.0 } else { avg };
            if score < best_score {
                best_score = score;
                best = i;
            }
        }
        best
    }

    /// Sans parole depuis longtemps (on réfléchit avant de parler) : on oublie
    /// le début silencieux pour ne pas l'envoyer à Whisper.
    fn drop_leading_silence(&mut self) {
        if self.speech_frames > 0 {
            return;
        }
        let keep = (self.cfg.keep_pad_ms / FRAME_MS) as usize;
        let n = self.frames.len();
        if n > keep + (3_000 / FRAME_MS) as usize {
            let drop = n - keep;
            self.frames.drain(..drop);
            self.pending.drain(..drop * FRAME);
        }
    }

    /// Sort les `cut` premières trames en un morceau (ou rien s'il n'y a pas de parole).
    fn split_at(&mut self, cut: usize) -> Option<Chunk> {
        let cut = cut.min(self.frames.len());
        let frames: Vec<FrameInfo> = self.frames.drain(..cut).collect();
        let samples: Vec<f32> = self.pending.drain(..cut * FRAME).collect();
        self.speech_frames = self.frames.iter().filter(|f| f.speech).count();
        self.build_chunk(&frames, &samples)
    }

    fn build_chunk(&self, frames: &[FrameInfo], samples: &[f32]) -> Option<Chunk> {
        // Seule une parole soutenue compte : un clic de clavier ne dure qu'une
        // ou deux trames, une syllabe au moins trois.
        let speech_ms = sustained_speech_frames(frames, 3) as u32 * FRAME_MS;
        if speech_ms < self.cfg.min_speech_ms {
            return None;
        }
        let keep = keep_mask(
            frames,
            (self.cfg.keep_pad_ms / FRAME_MS) as usize,
            (self.cfg.max_inner_silence_ms / FRAME_MS) as usize,
        );
        let mut out = Vec::with_capacity(samples.len());
        for (i, k) in keep.iter().enumerate() {
            if *k {
                let end = ((i + 1) * FRAME).min(samples.len());
                out.extend_from_slice(&samples[i * FRAME..end]);
            }
        }
        // whisper.cpp travaille mal sous la seconde : on complète par du silence.
        let min_len = SAMPLE_RATE as usize * 1_200 / 1000;
        if out.len() < min_len {
            out.resize(min_len, 0.0);
        }
        Some(Chunk { samples: out, speech_ms, source_ms: frames.len() as u32 * FRAME_MS })
    }
}

/// Trames à garder : la parole, une marge autour, et les silences intérieurs
/// raccourcis à `max_inner` trames au plus.
fn keep_mask(frames: &[FrameInfo], pad: usize, max_inner: usize) -> Vec<bool> {
    let n = frames.len();
    let first = frames.iter().position(|f| f.speech);
    let last = frames.iter().rposition(|f| f.speech);
    let (Some(first), Some(last)) = (first, last) else {
        return vec![false; n];
    };
    let start = first.saturating_sub(pad);
    let end = (last + pad + 1).min(n);
    let mut keep = vec![false; n];
    let mut i = start;
    while i < end {
        if frames[i].speech {
            keep[i] = true;
            i += 1;
            continue;
        }
        // Silence de i à j (exclu).
        let mut j = i;
        while j < end && !frames[j].speech {
            j += 1;
        }
        let len = j - i;
        let inner = i > first && j <= last;
        if inner && len > max_inner {
            let side = max_inner / 2;
            keep[i..i + side].fill(true);
            keep[j - side..j].fill(true);
        } else {
            keep[i..j].fill(true);
        }
        i = j;
    }
    keep
}

/// Nombre de trames de parole appartenant à des suites d'au moins `min_run` trames.
fn sustained_speech_frames(frames: &[FrameInfo], min_run: usize) -> usize {
    let mut total = 0;
    let mut run = 0;
    for f in frames {
        if f.speech {
            run += 1;
        } else {
            if run >= min_run {
                total += run;
            }
            run = 0;
        }
    }
    if run >= min_run {
        total += run;
    }
    total
}

fn frame_db(x: &[f32]) -> f32 {
    if x.is_empty() {
        return -100.0;
    }
    let mean_sq = x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32;
    (10.0 * mean_sq.max(1e-10).log10()).max(-100.0)
}

/// Estimation du bruit de fond : 15e centile des niveaux récents, plafonné
/// pour qu'une parole continue ne soit jamais prise pour du bruit.
#[derive(Debug, Clone, Default)]
struct NoiseFloor {
    history: VecDeque<f32>,
}

impl NoiseFloor {
    const CEILING_DB: f32 = -40.0;
    const MIN_THRESHOLD_DB: f32 = -62.0;

    fn observe(&mut self, db: f32) {
        if self.history.len() == NOISE_HISTORY {
            self.history.pop_front();
        }
        self.history.push_back(db);
    }

    fn floor(&self) -> f32 {
        if self.history.is_empty() {
            return -60.0;
        }
        let mut v: Vec<f32> = self.history.iter().copied().collect();
        let k = v.len() * 15 / 100;
        let (_, kth, _) = v.select_nth_unstable_by(k, f32::total_cmp);
        kth.min(Self::CEILING_DB)
    }

    fn threshold(&self, margin: f32) -> f32 {
        (self.floor() + margin).max(Self::MIN_THRESHOLD_DB)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SR: usize = SAMPLE_RATE as usize;

    /// « Parole » synthétique : un son modulé assez fort (≈ -20 dB).
    fn voice(ms: usize) -> Vec<f32> {
        let n = SR * ms / 1000;
        (0..n)
            .map(|i| {
                let t = i as f32 / SR as f32;
                0.15 * (2.0 * std::f32::consts::PI * 220.0 * t).sin()
                    * (0.7 + 0.3 * (2.0 * std::f32::consts::PI * 3.0 * t).sin())
            })
            .collect()
    }

    /// Bruit de fond faible (≈ -60 dB), pseudo-aléatoire et reproductible.
    fn hiss(ms: usize) -> Vec<f32> {
        let n = SR * ms / 1000;
        let mut s: u32 = 12345;
        (0..n)
            .map(|_| {
                s = s.wrapping_mul(1_103_515_245).wrapping_add(12_345);
                ((s >> 16) as f32 / 32768.0 - 1.0) * 0.001
            })
            .collect()
    }

    fn feed(seg: &mut Segmenter, audio: &[f32]) -> Vec<Chunk> {
        let mut out = Vec::new();
        for block in audio.chunks(1600) {
            out.extend(seg.push(block));
        }
        out
    }

    fn ms(c: &Chunk) -> usize {
        c.samples.len() * 1000 / SR
    }

    #[test]
    fn silence_only_gives_nothing() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        assert!(feed(&mut seg, &hiss(10_000)).is_empty());
        assert!(seg.finish().is_none());
        assert!(seg.pending_ms() < 4_000, "le silence initial doit être oublié");
    }

    #[test]
    fn short_dictation_is_one_chunk_at_finish() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        let mut a = hiss(500);
        a.extend(voice(3_000));
        a.extend(hiss(300));
        assert!(feed(&mut seg, &a).is_empty());
        let c = seg.finish().expect("un morceau");
        assert!((2_900..=3_100).contains(&(c.speech_ms as usize)), "{}", c.speech_ms);
        // Parole + au plus 2 × 400 ms de marge.
        assert!(ms(&c) <= 3_900, "{}", ms(&c));
    }

    #[test]
    fn cuts_at_pause_after_min_length() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        let mut a = hiss(300);
        a.extend(voice(7_000));
        a.extend(hiss(800)); // pause : coupe ici
        a.extend(voice(2_000));
        let chunks = feed(&mut seg, &a);
        assert_eq!(chunks.len(), 1, "coupe attendue pendant l'enregistrement");
        assert!((6_900..=7_100).contains(&(chunks[0].speech_ms as usize)));
        let last = seg.finish().unwrap();
        assert!((1_900..=2_100).contains(&(last.speech_ms as usize)));
    }

    #[test]
    fn short_pauses_do_not_cut() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        let mut a = Vec::new();
        for _ in 0..4 {
            a.extend(voice(2_000));
            a.extend(hiss(300)); // hésitation, pas une pause
        }
        assert!(feed(&mut seg, &a).is_empty());
        let s = seg.finish().unwrap().speech_ms;
        assert!((7_800..=8_200).contains(&s), "{s}");
    }

    #[test]
    fn long_silence_cuts_even_short_chunks() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        let mut a = voice(1_500);
        a.extend(hiss(1_500));
        let chunks = feed(&mut seg, &a);
        assert_eq!(chunks.len(), 1);
    }

    #[test]
    fn never_exceeds_max_chunk_without_pause() {
        let cfg = SegmenterConfig::default();
        let max = cfg.max_chunk_ms as usize;
        let mut seg = Segmenter::new(cfg);
        let chunks = feed(&mut seg, &voice(70_000));
        assert!(chunks.len() >= 2);
        for c in &chunks {
            assert!(c.source_ms as usize <= max, "{} ms", c.source_ms);
        }
        let total: u32 =
            chunks.iter().map(|c| c.speech_ms).sum::<u32>() + seg.finish().map(|c| c.speech_ms).unwrap_or(0);
        assert!((69_900..=70_100).contains(&(total as usize)), "rien ne doit être perdu : {total}");
    }

    #[test]
    fn forced_cut_prefers_quiet_spot() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        // 22 s de parole, un creux de 240 ms (trop court pour couper), puis encore.
        let mut a = voice(22_000);
        a.extend(hiss(240));
        a.extend(voice(10_000));
        let chunks = feed(&mut seg, &a);
        assert_eq!(chunks.len(), 1);
        let s = chunks[0].source_ms;
        assert!((22_000..=22_300).contains(&s), "coupe au creux attendue, obtenue à {s} ms");
    }

    #[test]
    fn long_inner_silence_is_shortened() {
        let cfg = SegmenterConfig { min_chunk_ms: 60_000, long_silence_ms: 60_000, ..Default::default() };
        let mut seg = Segmenter::new(cfg);
        let mut a = voice(1_000);
        a.extend(hiss(5_000));
        a.extend(voice(1_000));
        feed(&mut seg, &a);
        let c = seg.finish().unwrap();
        // 2 s de parole + 1,5 s de silence intérieur max + marges de fin.
        assert!(ms(&c) <= 2_000 + 1_500 + 450, "{}", ms(&c));
        assert_eq!(c.source_ms, 7_020);
    }

    #[test]
    fn tiny_noise_burst_is_ignored() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        let mut a = hiss(2_000);
        a.extend(voice(90)); // un clic
        a.extend(hiss(2_000));
        feed(&mut seg, &a);
        assert!(seg.finish().is_none());
    }

    #[test]
    fn keyboard_clicks_are_not_speech() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        let mut a = hiss(500);
        for _ in 0..20 {
            a.extend(voice(30)); // un clic : une trame
            a.extend(hiss(250));
        }
        a.extend(hiss(2_000));
        assert!(feed(&mut seg, &a).is_empty());
        assert!(seg.finish().is_none());
    }

    #[test]
    fn sustained_runs_only() {
        let f = |s: bool| FrameInfo { db: 0.0, speech: s };
        let frames = vec![f(true), f(false), f(true), f(true), f(false), f(true), f(true), f(true), f(true)];
        assert_eq!(sustained_speech_frames(&frames, 3), 4);
        assert_eq!(sustained_speech_frames(&frames, 1), 7);
    }

    #[test]
    fn short_chunks_are_padded_for_whisper() {
        let mut seg = Segmenter::new(SegmenterConfig::default());
        feed(&mut seg, &voice(400));
        let c = seg.finish().unwrap();
        assert!(c.samples.len() >= SR * 1_200 / 1000);
    }

    #[test]
    fn keep_mask_keeps_padding_and_trims_edges() {
        let f = |s: bool| FrameInfo { db: if s { -20.0 } else { -60.0 }, speech: s };
        let frames: Vec<FrameInfo> = [vec![f(false); 30], vec![f(true); 10], vec![f(false); 30]].concat();
        let keep = keep_mask(&frames, 5, 50);
        assert_eq!(keep.iter().filter(|k| **k).count(), 20);
        assert!(keep[25] && keep[44] && !keep[24] && !keep[45]);
    }
}
