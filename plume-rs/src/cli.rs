//! Commandes en ligne : transcrire un fichier, télécharger un modèle, version.
//!
//! `plume --transcrire dictee.wav` passe par exactement le même chemin qu'une
//! dictée au raccourci (découpage, moteur, nettoyage) : c'est l'outil de test
//! et de mesure de Plume.

use anyhow::{Context, Result, bail};
use plume_core::config::Config;
use plume_core::models::{self, ModelSource};
use plume_core::paths::Paths;
use std::path::PathBuf;

const HELP: &str = "\
Plume — dictée vocale locale (Whisper)

Sans argument, Plume démarre près de l'horloge et attend le raccourci.

Commandes :
  plume --transcrire FICHIER.wav [options]   transcrit un fichier
      --modele NOM          large-v3-turbo, large-v3, medium, small, base, tiny ou chemin .bin
      --processeur          n'utilise pas la carte graphique
      --temps-reel          simule une dictée en direct (mesure le délai après l'arrêt)
      --precision N         1 (rapide) à 8 (soigné)
  plume --telecharger-modele [NOM]           télécharge un modèle
  plume --version                            affiche la version
  plume --aide                               affiche cette aide
";

pub fn run(args: &[String]) -> i32 {
    let result = match args[0].as_str() {
        "--version" | "-V" => {
            println!("plume {}", plume_core::VERSION);
            Ok(())
        }
        "--aide" | "--help" | "-h" | "/?" => {
            print!("{HELP}");
            Ok(())
        }
        "--telecharger-modele" => download(args.get(1).map(String::as_str)),
        "--transcrire" => transcribe(&args[1..]),
        other => {
            eprint!("option inconnue : {other}\n\n{HELP}");
            return 2;
        }
    };
    match result {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("erreur : {e:#}");
            1
        }
    }
}

fn download(name: Option<&str>) -> Result<()> {
    let paths = Paths::detect();
    let (cfg, _) = Config::load_or_create(&paths.config());
    let name = name.unwrap_or(&cfg.transcription.modele);
    let source = ModelSource::resolve(name, &paths.models_dir())?;
    ensure_model(&source)?;
    println!("{}", source.path().display());
    Ok(())
}

pub fn ensure_model(source: &ModelSource) -> Result<()> {
    if source.is_present() {
        return Ok(());
    }
    match source {
        ModelSource::File(p) => bail!("fichier de modèle introuvable : {}", p.display()),
        ModelSource::Catalog(info, path) => {
            eprintln!("téléchargement de {} ({} Mo)…", info.name, info.size_bytes / 1_000_000);
            let mut last = 0;
            models::download(info, path, |done, total| {
                let pct = (done * 100).checked_div(total).unwrap_or(0);
                if pct >= last + 10 || pct == 100 {
                    eprintln!("  {pct} %");
                    last = pct;
                }
            })
        }
    }
}

#[cfg_attr(not(feature = "moteur"), allow(dead_code))]
struct TranscribeArgs {
    file: PathBuf,
    model: Option<String>,
    cpu: bool,
    realtime: bool,
    precision: Option<u32>,
}

#[cfg_attr(not(feature = "moteur"), allow(dead_code))]
fn parse_transcribe_args(args: &[String]) -> Result<TranscribeArgs> {
    let mut out = TranscribeArgs { file: PathBuf::new(), model: None, cpu: false, realtime: false, precision: None };
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--modele" => out.model = Some(it.next().context("--modele attend un nom")?.clone()),
            "--processeur" => out.cpu = true,
            "--temps-reel" => out.realtime = true,
            "--precision" => {
                out.precision = Some(
                    it.next()
                        .context("--precision attend un nombre")?
                        .parse()
                        .context("--precision attend un nombre")?,
                )
            }
            f if !f.starts_with("--") && out.file.as_os_str().is_empty() => out.file = PathBuf::from(f),
            other => bail!("argument inattendu : {other}"),
        }
    }
    if out.file.as_os_str().is_empty() {
        bail!("indiquez un fichier .wav à transcrire");
    }
    Ok(out)
}

#[cfg(not(feature = "moteur"))]
fn transcribe(_args: &[String]) -> Result<()> {
    bail!("cette version de Plume a été compilée sans moteur de transcription")
}

#[cfg(feature = "moteur")]
fn transcribe(args: &[String]) -> Result<()> {
    use plume_core::audio_file;
    use plume_core::engine::{Engine, EngineSettings};
    use plume_core::pipeline::{EngineEvent, EngineHandle, Transcriber};
    use plume_core::segmenter::SegmenterConfig;
    use plume_core::text::{self, Corrections};
    use std::sync::{Arc, mpsc};
    use std::time::{Duration, Instant};

    let a = parse_transcribe_args(args)?;
    let paths = Paths::detect();
    let (mut cfg, warnings) = Config::load_or_create(&paths.config());
    for w in warnings {
        eprintln!("attention : {w}");
    }
    if let Some(m) = &a.model {
        cfg.transcription.modele = m.clone();
    }
    if let Some(p) = a.precision {
        cfg.transcription.precision = p;
    }
    let source = ModelSource::resolve(&cfg.transcription.modele, &paths.models_dir())?;
    ensure_model(&source)?;

    let audio = audio_file::read_wav(&a.file)?;
    let seconds = audio.samples.len() as f64 / audio.sample_rate as f64;

    let settings = EngineSettings {
        model_path: source.path().to_path_buf(),
        use_gpu: cfg.transcription.gpu && !a.cpu,
        language: cfg.transcription.langue.clone(),
        beam_size: cfg.beam_size(),
        threads: cfg.transcription.threads,
    };
    let (ready_tx, ready_rx) = mpsc::channel();
    let t_load = Instant::now();
    let engine = EngineHandle::spawn(
        Box::new(move || Engine::load(&settings).map(|e| Box::new(e) as Box<dyn Transcriber>)),
        Arc::new(move |e| {
            let _ = ready_tx.send(e);
        }),
    );
    let backend = loop {
        match ready_rx.recv().context("le moteur s'est arrêté")? {
            EngineEvent::Loading => continue,
            EngineEvent::Ready(b) => break b,
            EngineEvent::Failed(e) => bail!("moteur indisponible : {e}"),
        }
    };
    eprintln!("modèle {} prêt sur {} en {:.1} s", source.label(), backend, t_load.elapsed().as_secs_f32());

    let mut d =
        engine.start(audio.sample_rate, cfg.transcription.vocabulaire.clone(), SegmenterConfig::default(), None);
    let block = (audio.sample_rate / 100) as usize; // 10 ms, comme un micro
    let t0 = Instant::now();
    for (i, b) in audio.samples.chunks(block).enumerate() {
        d.feed(b);
        if a.realtime {
            let target = Duration::from_millis(((i + 1) * 10) as u64);
            if let Some(wait) = target.checked_sub(t0.elapsed()) {
                std::thread::sleep(wait);
            }
        }
    }
    let pending = d.finish();
    let chunks = pending.chunks;
    let r = pending.wait(Duration::from_secs(3_600)).map_err(anyhow::Error::msg)?;
    for e in &r.errors {
        eprintln!("attention : {e}");
    }
    let corrections = Corrections::from_map(&cfg.corrections);
    println!("{}", text::finalize(&r.text, &corrections));
    eprintln!(
        "{:.1} s de son, {} morceaux, calcul {:.1} s ({:.2}× le temps réel), texte prêt {:.2} s après l'arrêt",
        seconds,
        chunks,
        r.compute.as_secs_f32(),
        r.compute.as_secs_f64() / seconds.max(0.001),
        r.latency.as_secs_f32()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn parses_transcribe_options() {
        let a = parse_transcribe_args(&s(&[
            "essai.wav",
            "--modele",
            "tiny",
            "--processeur",
            "--temps-reel",
            "--precision",
            "2",
        ]))
        .unwrap();
        assert_eq!(a.file, PathBuf::from("essai.wav"));
        assert_eq!(a.model.as_deref(), Some("tiny"));
        assert!(a.cpu && a.realtime);
        assert_eq!(a.precision, Some(2));
        assert!(parse_transcribe_args(&s(&[])).is_err());
        assert!(parse_transcribe_args(&s(&["a.wav", "--inconnu"])).is_err());
        assert!(parse_transcribe_args(&s(&["a.wav", "--precision", "x"])).is_err());
    }

    #[test]
    fn unknown_option_exits_with_usage_code() {
        assert_eq!(run(&s(&["--nimporte"])), 2);
        assert_eq!(run(&s(&["--version"])), 0);
    }
}
