//! Lecture et écriture de fichiers WAV (tests, transcription d'un fichier).

use anyhow::{Context, Result, bail};
use std::path::Path;

/// Son mono en nombres à virgule, avec sa fréquence d'échantillonnage.
pub struct MonoAudio {
    pub samples: Vec<f32>,
    pub sample_rate: u32,
}

/// Lit un WAV (16/24/32 bits entiers ou flottants, mono ou multicanal) en mono.
pub fn read_wav(path: &Path) -> Result<MonoAudio> {
    let mut reader = hound::WavReader::open(path).with_context(|| format!("ouverture de {}", path.display()))?;
    let spec = reader.spec();
    let channels = spec.channels.max(1) as usize;
    let interleaved: Vec<f32> = match spec.sample_format {
        hound::SampleFormat::Float => reader.samples::<f32>().collect::<Result<_, _>>()?,
        hound::SampleFormat::Int => {
            let scale = 1.0 / (1u64 << (spec.bits_per_sample.clamp(8, 32) - 1)) as f32;
            reader.samples::<i32>().map(|s| s.map(|v| v as f32 * scale)).collect::<Result<_, _>>()?
        }
    };
    if interleaved.is_empty() {
        bail!("{} ne contient pas de son", path.display());
    }
    let samples = if channels == 1 {
        interleaved
    } else {
        interleaved.chunks(channels).map(|f| f.iter().sum::<f32>() / channels as f32).collect()
    };
    Ok(MonoAudio { samples, sample_rate: spec.sample_rate })
}

/// Écrit un WAV mono 16 bits.
pub fn write_wav(path: &Path, samples: &[f32], sample_rate: u32) -> Result<()> {
    let spec =
        hound::WavSpec { channels: 1, sample_rate, bits_per_sample: 16, sample_format: hound::SampleFormat::Int };
    let mut w = hound::WavWriter::create(path, spec).with_context(|| format!("création de {}", path.display()))?;
    for s in samples {
        w.write_sample((s.clamp(-1.0, 1.0) * 32767.0) as i16)?;
    }
    w.finalize()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_mono_16bit() {
        let path = std::env::temp_dir().join("plume-test-roundtrip.wav");
        let x: Vec<f32> = (0..1600).map(|i| ((i as f32) * 0.05).sin() * 0.5).collect();
        write_wav(&path, &x, 16_000).unwrap();
        let a = read_wav(&path).unwrap();
        assert_eq!(a.sample_rate, 16_000);
        assert_eq!(a.samples.len(), x.len());
        for (u, v) in a.samples.iter().zip(&x) {
            assert!((u - v).abs() < 1e-3);
        }
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn stereo_is_downmixed() {
        let path = std::env::temp_dir().join("plume-test-stereo.wav");
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 48_000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut w = hound::WavWriter::create(&path, spec).unwrap();
        for _ in 0..100 {
            w.write_sample(16_384i16).unwrap();
            w.write_sample(0i16).unwrap();
        }
        w.finalize().unwrap();
        let a = read_wav(&path).unwrap();
        assert_eq!(a.samples.len(), 100);
        assert!((a.samples[0] - 0.25).abs() < 1e-3);
        let _ = std::fs::remove_file(path);
    }
}
