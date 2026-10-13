//! Capture du micro (WASAPI via cpal).
//!
//! Le son arrive par petits blocs dans le fil audio de Windows ; on le passe en
//! mono et on l'envoie tel quel au contrôleur (le rééchantillonnage vers
//! 16 kHz se fait plus loin, hors du fil audio qui doit rester très léger).

use super::Event;
use anyhow::{Context, Result, anyhow, bail};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{ErrorKind, FromSample, Sample, SampleFormat, SizedSample};
use std::sync::mpsc::Sender;

pub struct Capture {
    _stream: cpal::Stream,
    pub sample_rate: u32,
    pub device: String,
}

fn device_name(d: &cpal::Device) -> String {
    d.description().map(|desc| desc.name().to_string()).unwrap_or_else(|_| d.to_string())
}

/// Micros disponibles (pour les messages d'erreur).
pub fn list_inputs() -> Vec<String> {
    cpal::default_host().input_devices().map(|it| it.map(|d| device_name(&d)).collect()).unwrap_or_default()
}

fn pick_device(hint: &str) -> Result<cpal::Device> {
    let host = cpal::default_host();
    let hint = hint.trim();
    if hint.is_empty() {
        return host
            .default_input_device()
            .ok_or_else(|| anyhow!("aucun micro trouvé : branche-en un, ou vérifie Paramètres > Son"));
    }
    let wanted = hint.to_lowercase();
    let devices = host.input_devices().map_err(|e| anyhow!("liste des micros : {e}"))?;
    for d in devices {
        if device_name(&d).to_lowercase().contains(&wanted) {
            return Ok(d);
        }
    }
    let names = list_inputs();
    bail!(
        "micro « {hint} » introuvable ; micros disponibles : {}",
        if names.is_empty() { "aucun".to_string() } else { names.join(", ") }
    )
}

/// Ouvre le micro et commence l'envoi du son vers `tx` (marqué `session`).
pub fn start(hint: &str, session: u64, tx: Sender<Event>) -> Result<Capture> {
    let device = pick_device(hint)?;
    let name = device_name(&device);
    let supported = device
        .default_input_config()
        .map_err(|e| anyhow!("{e}"))
        .with_context(|| format!("format du micro « {name} »"))?;
    let channels = supported.channels().max(1) as usize;
    let sample_rate = supported.sample_rate();
    let format = supported.sample_format();
    let config: cpal::StreamConfig = supported.into();

    let stream = match format {
        SampleFormat::F32 => build::<f32>(&device, config, channels, session, tx),
        SampleFormat::I16 => build::<i16>(&device, config, channels, session, tx),
        SampleFormat::I32 => build::<i32>(&device, config, channels, session, tx),
        SampleFormat::U16 => build::<u16>(&device, config, channels, session, tx),
        SampleFormat::U8 => build::<u8>(&device, config, channels, session, tx),
        SampleFormat::F64 => build::<f64>(&device, config, channels, session, tx),
        other => bail!("format de son non pris en charge par Plume : {other}"),
    }
    .with_context(|| format!("ouverture du micro « {name} »"))?;
    stream.play().map_err(|e| anyhow!("démarrage du micro « {name} » : {e}"))?;
    log::info!("micro « {name} » ouvert : {sample_rate} Hz, {channels} canal(aux), {format}");
    Ok(Capture { _stream: stream, sample_rate, device: name })
}

fn build<T>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    channels: usize,
    session: u64,
    tx: Sender<Event>,
) -> Result<cpal::Stream>
where
    T: SizedSample + Send + 'static,
    f32: FromSample<T>,
{
    let err_tx = tx.clone();
    let stream = device
        .build_input_stream::<T, _, _>(
            config,
            move |data: &[T], _: &cpal::InputCallbackInfo| {
                let mut mono = Vec::with_capacity(data.len() / channels);
                for frame in data.chunks(channels) {
                    let sum: f32 = frame.iter().map(|s| f32::from_sample(*s)).sum();
                    mono.push(sum / frame.len() as f32);
                }
                let _ = tx.send(Event::Audio { session, samples: mono });
            },
            move |err: cpal::Error| match err.kind() {
                ErrorKind::Xrun | ErrorKind::RealtimeDenied => log::debug!("micro : {err}"),
                ErrorKind::DeviceChanged => log::info!("micro : {err}"),
                _ => {
                    log::error!("micro : {err}");
                    let _ = err_tx.send(Event::AudioFailed { session, message: err.to_string() });
                }
            },
            None,
        )
        .map_err(|e| anyhow!("{e}"))?;
    Ok(stream)
}
