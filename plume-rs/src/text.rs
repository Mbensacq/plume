//! Nettoyage du texte produit par Whisper.
//!
//! - écarte les « hallucinations » connues (génériques de sous-titres…) ;
//! - retire les « … » ajoutés aux bords des morceaux ;
//! - supprime les boucles de répétition ;
//! - recolle les morceaux en une phrase continue ;
//! - applique les corrections personnelles (`[corrections]` de la config) ;
//! - finit la mise en forme (majuscule initiale, point final, espaces).

use regex::{Regex, RegexBuilder};
use std::collections::BTreeMap;
use std::sync::OnceLock;

/// Amorce de style donnée à Whisper : un texte bien ponctué l'incite à
/// ponctuer. Reprise de Plume 1, dont la ponctuation donnait satisfaction.
pub const STYLE_PROMPT: &str = "Voici une transcription en français, soigneusement ponctuée. \
Elle contient des virgules, des points, des points d'interrogation ? Et parfois des points d'exclamation !";

/// Longueur maximale de texte précédent redonné à Whisper pour la continuité.
const CONTEXT_CHARS: usize = 220;
/// Budget total de l'amorce. whisper.cpp n'en garde que les 223 derniers
/// jetons (≈ 3 caractères par jeton en français) : au-delà, le début de
/// l'amorce, c'est-à-dire le vocabulaire, serait coupé sans prévenir.
const PROMPT_MAX_CHARS: usize = 600;
/// Place réservée au vocabulaire ; les mots en trop (en fin de liste) sont ignorés.
pub const VOCAB_MAX_CHARS: usize = 300;
/// Texte précédent gardé au minimum, même avec un long vocabulaire.
const CONTEXT_MIN_CHARS: usize = 60;

fn hallucination_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        RegexBuilder::new(
            r"sous[-\s]?titr|amara\.org|soustitreur|radio[-\s]?canada|merci d'avoir regard|thanks for watching|subtitl|[♪♫🎵]",
        )
        .case_insensitive(true)
        .build()
        .expect("expression régulière valide")
    })
}

/// Vrai si le segment est un générique de sous-titres ou de la musique que
/// Whisper invente sur du silence.
pub fn is_hallucination(segment: &str) -> bool {
    hallucination_re().is_match(segment)
}

/// Nettoie le texte d'un morceau (avant assemblage).
pub fn clean_chunk(raw: &str) -> String {
    let s = collapse_spaces(raw);
    let s = strip_edge_ellipsis(&s);
    collapse_repetitions(&s)
}

/// Retire « … » et « ... » en début et en fin (morceau coupé en pleine phrase).
pub fn strip_edge_ellipsis(s: &str) -> String {
    let is_dot = |c: char| c == '.' || c == '…';
    let mut t = s.trim();
    // Début : uniquement s'il s'agit de points de suspension (au moins 2 points ou « … »).
    let lead: String = t.chars().take_while(|c| is_dot(*c) || c.is_whitespace()).collect();
    if lead.contains('…') || lead.matches('.').count() >= 2 {
        t = &t[lead.len()..];
    }
    let t = t.trim();
    let tail_len: usize = t.chars().rev().take_while(|c| is_dot(*c) || c.is_whitespace()).map(char::len_utf8).sum();
    let tail = &t[t.len() - tail_len..];
    if tail.contains('…') || tail.matches('.').count() >= 2 {
        t[..t.len() - tail_len].trim_end().to_string()
    } else {
        t.to_string()
    }
}

fn collapse_spaces(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Supprime les boucles du type « je ne sais pas. je ne sais pas. je ne sais pas. … ».
/// Seuils prudents : un mot répété 6 fois, une expression de 2 à 4 mots
/// répétée 4 fois, une phrase de 5 mots ou plus répétée 3 fois.
pub fn collapse_repetitions(s: &str) -> String {
    let words: Vec<&str> = s.split_whitespace().collect();
    let key = |w: &str| w.trim_matches(|c: char| !c.is_alphanumeric()).to_lowercase();
    let keys: Vec<String> = words.iter().map(|w| key(w)).collect();
    let mut out: Vec<&str> = Vec::with_capacity(words.len());
    let mut i = 0;
    'outer: while i < words.len() {
        for n in (1..=12).rev() {
            if i + n > words.len() {
                continue;
            }
            let min_repeats = match n {
                1 => 6,
                2..=4 => 4,
                _ => 3,
            };
            let mut count = 1;
            while i + (count + 1) * n <= words.len() && keys[i + count * n..i + (count + 1) * n] == keys[i..i + n] {
                count += 1;
            }
            if count >= min_repeats && keys[i..i + n].iter().any(|k| !k.is_empty()) {
                out.extend_from_slice(&words[i..i + n]);
                i += n * count;
                continue 'outer;
            }
        }
        out.push(words[i]);
        i += 1;
    }
    out.join(" ")
}

/// Mots très courants qu'un morceau commence avec une majuscule à tort
/// quand la phrase précédente n'était pas finie.
const CONTINUATION_WORDS: &[&str] = &[
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "mais", "donc", "car", "ni", "que", "qui", "quand",
    "comme", "si", "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles", "ce", "ça", "cela", "cette", "ces",
    "cet", "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa", "ses", "notre", "nos", "votre", "vos", "leur", "leurs",
    "en", "dans", "sur", "sous", "pour", "par", "avec", "sans", "à", "au", "aux", "ne", "pas", "plus", "puis", "alors",
    "parce", "lorsque", "après", "avant", "entre", "vers", "chez", "où", "dont", "tout", "tous", "toute", "toutes",
    "très", "aussi", "bien", "encore", "déjà", "même", "est", "sont", "ont", "était", "va", "peut", "faut",
];
const ELIDED: &[&str] = &["l", "d", "j", "c", "qu", "n", "s", "m", "t", "jusqu", "lorsqu", "puisqu"];

/// Ajoute `next` à la suite de `acc`, en corrigeant la majuscule abusive
/// d'un mot courant quand la phrase précédente continue.
pub fn join_chunk(acc: &mut String, next: &str) {
    let next = next.trim();
    if next.is_empty() {
        return;
    }
    if acc.trim().is_empty() {
        acc.clear();
        acc.push_str(next);
        return;
    }
    let sentence_done =
        acc.trim_end().chars().last().is_some_and(|c| matches!(c, '.' | '!' | '?' | '…' | ':' | '»' | '"'));
    let next = if sentence_done { next.to_string() } else { lowercase_continuation(next) };
    acc.push(' ');
    acc.push_str(&next);
}

fn lowercase_continuation(s: &str) -> String {
    let first_word: String = s.chars().take_while(|c| c.is_alphabetic() || *c == '\'' || *c == '’').collect();
    let mut chars = first_word.chars();
    let Some(c0) = chars.next() else { return s.to_string() };
    let rest: String = chars.collect();
    // Mot entièrement en majuscules (sigle) : on n'y touche pas.
    if !c0.is_uppercase() || rest.chars().any(char::is_uppercase) {
        return s.to_string();
    }
    let lower = first_word.to_lowercase();
    let stem = lower.split(['\'', '’']).next().unwrap_or("");
    let is_elided = lower.contains(['\'', '’']) && ELIDED.contains(&stem);
    if CONTINUATION_WORDS.contains(&lower.as_str()) || is_elided {
        let mut out = String::with_capacity(s.len());
        out.extend(c0.to_lowercase());
        out.push_str(&s[c0.len_utf8()..]);
        out
    } else {
        s.to_string()
    }
}

/// Corrections personnelles : « mot mal reconnu » → « correction ».
/// Mots entiers, insensible à la casse.
#[derive(Debug, Clone, Default)]
pub struct Corrections {
    rules: Vec<(Regex, String)>,
}

impl Corrections {
    /// Les clés vides ou commençant par « _ » sont ignorées.
    pub fn from_map(map: &BTreeMap<String, String>) -> Corrections {
        // Les expressions les plus longues d'abord : « git hub desktop » avant « git hub ».
        let mut entries: Vec<(&String, &String)> =
            map.iter().filter(|(k, _)| !k.trim().is_empty() && !k.starts_with('_')).collect();
        entries.sort_by_key(|(k, _)| std::cmp::Reverse(k.chars().count()));
        let rules = entries
            .into_iter()
            .filter_map(|(wrong, right)| {
                let pattern = format!(r"(?i)(^|[^\w]){}($|[^\w])", regex::escape(wrong.trim()));
                Regex::new(&pattern).ok().map(|re| (re, right.clone()))
            })
            .collect();
        Corrections { rules }
    }

    pub fn len(&self) -> usize {
        self.rules.len()
    }

    pub fn is_empty(&self) -> bool {
        self.rules.is_empty()
    }

    pub fn apply(&self, text: &str) -> String {
        let mut s = text.to_string();
        for (re, right) in &self.rules {
            // Deux passes : deux occurrences collées partagent un séparateur.
            for _ in 0..2 {
                s = re.replace_all(&s, |c: &regex::Captures| format!("{}{}{}", &c[1], right, &c[2])).into_owned();
            }
        }
        s
    }
}

/// Mise en forme finale du texte complet d'une dictée.
pub fn finalize(text: &str, corrections: &Corrections) -> String {
    let s = collapse_spaces(text);
    let s = corrections.apply(&s);
    let s = collapse_repetitions(&s);
    // Pas d'espace avant une virgule ou un point (on garde l'espace français
    // avant ? ! : ; que Whisper produit déjà).
    let mut out = String::with_capacity(s.len() + 1);
    for c in s.chars() {
        if matches!(c, ',' | '.' | ')') && out.ends_with(' ') {
            out.pop();
        }
        out.push(c);
    }
    let mut s = strip_trailing_ellipsis_to_period(out.trim());
    if s.is_empty() {
        return s;
    }
    // Majuscule initiale.
    let first = s.chars().next().unwrap();
    if first.is_lowercase() {
        let upper: String = first.to_uppercase().collect();
        s.replace_range(..first.len_utf8(), &upper);
    }
    // Dictée arrêtée sur une virgule : on termine proprement la phrase.
    if s.ends_with([',', ';']) {
        s.pop();
        s = s.trim_end().to_string();
    }
    // Point final si la phrase se termine par une lettre ou un chiffre.
    if s.chars().last().is_some_and(char::is_alphanumeric) {
        s.push('.');
    }
    s
}

fn strip_trailing_ellipsis_to_period(s: &str) -> String {
    let t = s.trim_end_matches(['.', '…', ' ']);
    if t.len() < s.len() && (s.ends_with("..") || s.ends_with('…')) && !t.is_empty() {
        format!("{t}.")
    } else {
        s.to_string()
    }
}

/// Construit l'amorce donnée à Whisper : vocabulaire, style, puis la fin du
/// texte déjà dicté (pour que la suite enchaîne naturellement).
pub fn build_prompt(vocabulary: &[String], previous: &str) -> String {
    let mut p = String::new();
    let words = vocabulary_within_budget(vocabulary);
    if !words.is_empty() {
        p.push_str("Vocabulaire : ");
        p.push_str(&words.join(", "));
        p.push_str(". ");
    }
    p.push_str(STYLE_PROMPT);
    let prev = previous.trim();
    if !prev.is_empty() {
        let room = PROMPT_MAX_CHARS.saturating_sub(p.chars().count() + 1).clamp(CONTEXT_MIN_CHARS, CONTEXT_CHARS);
        p.push(' ');
        p.push_str(tail_at_word(prev, room));
    }
    p
}

/// Les mots de vocabulaire qui tiennent dans [`VOCAB_MAX_CHARS`], dans l'ordre
/// de la liste (les premiers sont prioritaires).
pub fn vocabulary_within_budget(vocabulary: &[String]) -> Vec<&str> {
    let mut used = 0;
    let mut out = Vec::new();
    for w in vocabulary.iter().map(|w| w.trim()).filter(|w| !w.is_empty()) {
        let cost = w.chars().count() + if out.is_empty() { 0 } else { 2 };
        if used + cost > VOCAB_MAX_CHARS {
            break;
        }
        used += cost;
        out.push(w);
    }
    out
}

/// Les `max` derniers caractères de `s`, en commençant sur un début de mot.
fn tail_at_word(s: &str, max: usize) -> &str {
    let count = s.chars().count();
    if count <= max {
        return s;
    }
    let start = s.char_indices().nth(count - max).map(|(i, _)| i).unwrap_or(0);
    let tail = &s[start..];
    match tail.find(' ') {
        Some(sp) if sp + 1 < tail.len() => &tail[sp + 1..],
        _ => tail,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn corr(pairs: &[(&str, &str)]) -> Corrections {
        Corrections::from_map(&pairs.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect())
    }

    #[test]
    fn finalize_basics_match_plume_1() {
        let none = Corrections::default();
        assert_eq!(finalize("bonjour tout le monde", &none), "Bonjour tout le monde.");
        assert_eq!(finalize("Salut, comment vas-tu ?", &none), "Salut, comment vas-tu ?");
        assert_eq!(finalize("il a dit : oui ; puis non !", &none), "Il a dit : oui ; puis non !");
        assert_eq!(finalize("ceci est un test , voilà .", &none), "Ceci est un test, voilà.");
        assert_eq!(finalize("  plusieurs   espaces  ", &none), "Plusieurs espaces.");
        assert_eq!(finalize("c'est déjà fini.", &none), "C'est déjà fini.");
        assert_eq!(finalize("", &none), "");
        assert_eq!(finalize("   ", &none), "");
        assert_eq!(finalize("élan", &none), "Élan.");
    }

    #[test]
    fn finalize_turns_trailing_ellipsis_into_period() {
        let none = Corrections::default();
        assert_eq!(finalize("et puis on verra...", &none), "Et puis on verra.");
        assert_eq!(finalize("et puis on verra…", &none), "Et puis on verra.");
        assert_eq!(finalize("un ami, homme d'esprit,", &none), "Un ami, homme d'esprit.");
        assert_eq!(finalize("a ; b ;", &none), "A ; b.");
    }

    #[test]
    fn corrections_whole_words_case_insensitive() {
        let c = corr(&[("discorde", "Discord"), ("git eub", "GitHub"), ("_aide", "ignoré")]);
        assert_eq!(c.len(), 2);
        assert_eq!(c.apply("je suis sur Discorde et git eub"), "je suis sur Discord et GitHub");
        assert_eq!(c.apply("discordes"), "discordes", "mot entier seulement");
        assert_eq!(c.apply("discorde discorde"), "Discord Discord");
        assert_eq!(c.apply("(discorde)"), "(Discord)");
    }

    #[test]
    fn corrections_work_with_accents() {
        let c = corr(&[("éléphant", "Éléphant")]);
        assert_eq!(c.apply("un éléphant rose"), "un Éléphant rose");
        assert_eq!(c.apply("éléphantesque"), "éléphantesque");
    }

    #[test]
    fn longer_corrections_win() {
        let c = corr(&[("git hub", "GitHub"), ("git hub desktop", "GitHub Desktop")]);
        assert_eq!(c.apply("ouvre git hub desktop"), "ouvre GitHub Desktop");
    }

    #[test]
    fn detects_subtitle_hallucinations() {
        assert!(is_hallucination("Sous-titrage ST' 501"));
        assert!(is_hallucination("Sous-titres réalisés par la communauté d'Amara.org"));
        assert!(is_hallucination("Merci d'avoir regardé cette vidéo !"));
        assert!(is_hallucination("♪ ♪"));
        assert!(!is_hallucination("Merci pour ton aide."));
    }

    #[test]
    fn strips_ellipsis_at_chunk_edges() {
        assert_eq!(strip_edge_ellipsis("... et donc on continue..."), "et donc on continue");
        assert_eq!(strip_edge_ellipsis("… voilà …"), "voilà");
        assert_eq!(strip_edge_ellipsis("Fin de phrase."), "Fin de phrase.");
        assert_eq!(strip_edge_ellipsis("..."), "");
        assert_eq!(strip_edge_ellipsis(".NET est bien."), ".NET est bien.");
    }

    #[test]
    fn collapses_repetition_loops() {
        assert_eq!(
            collapse_repetitions("Je ne sais pas. Je ne sais pas. Je ne sais pas. Je ne sais pas. Et voilà."),
            "Je ne sais pas. Et voilà."
        );
        assert_eq!(collapse_repetitions("non non non non non non non"), "non");
        // Répétitions naturelles conservées.
        assert_eq!(collapse_repetitions("très très très bien"), "très très très bien");
        assert_eq!(collapse_repetitions("non, non, non !"), "non, non, non !");
        assert_eq!(collapse_repetitions("c'est ça, c'est ça, c'est ça"), "c'est ça, c'est ça, c'est ça");
    }

    #[test]
    fn joins_chunks_and_fixes_continuation_case() {
        let mut acc = String::new();
        join_chunk(&mut acc, "Je pense que");
        join_chunk(&mut acc, "Le projet avance bien.");
        join_chunk(&mut acc, "Il reste des tests");
        join_chunk(&mut acc, "Et la doc.");
        assert_eq!(acc, "Je pense que le projet avance bien. Il reste des tests et la doc.");
    }

    #[test]
    fn continuation_keeps_proper_nouns_and_acronyms() {
        let mut acc = "J'ai parlé avec".to_string();
        join_chunk(&mut acc, "Paris hier");
        assert_eq!(acc, "J'ai parlé avec Paris hier");
        let mut acc = "on utilise".to_string();
        join_chunk(&mut acc, "API REST");
        assert_eq!(acc, "on utilise API REST");
        let mut acc = "et alors".to_string();
        join_chunk(&mut acc, "C'est parti");
        assert_eq!(acc, "et alors c'est parti");
        let mut acc = "on va".to_string();
        join_chunk(&mut acc, "À la gare");
        assert_eq!(acc, "on va à la gare");
    }

    #[test]
    fn prompt_contains_vocabulary_style_and_context() {
        let p = build_prompt(&["GitHub".into(), " Rust ".into(), "".into()], "");
        assert!(p.starts_with("Vocabulaire : GitHub, Rust. "));
        assert!(p.contains("soigneusement ponctuée"));
        let long = "mot ".repeat(200);
        let p = build_prompt(&[], &long);
        assert!(p.len() < STYLE_PROMPT.len() + CONTEXT_CHARS + 2);
        assert!(p.ends_with("mot"));
    }

    #[test]
    fn long_vocabulary_never_crowds_out_style_or_overflows() {
        let vocab: Vec<String> = (0..80).map(|i| format!("Terme{i:02}")).collect();
        let kept = vocabulary_within_budget(&vocab);
        assert!(kept.len() < 80, "la liste doit être bornée");
        assert_eq!(kept[0], "Terme00", "les premiers mots sont prioritaires");
        let p = build_prompt(&vocab, &"suite du texte ".repeat(50));
        assert!(p.contains("soigneusement ponctuée"));
        assert!(p.chars().count() <= PROMPT_MAX_CHARS + CONTEXT_MIN_CHARS, "{}", p.chars().count());
        assert!(p.ends_with("texte"), "le contexte reste à la fin");
    }

    #[test]
    fn short_vocabulary_keeps_full_context() {
        let p = build_prompt(&["GitHub".into()], &"mot ".repeat(200));
        let context = p.split(STYLE_PROMPT).nth(1).unwrap().trim();
        assert!(context.chars().count() > CONTEXT_CHARS - 10, "{}", context.chars().count());
    }

    #[test]
    fn tail_starts_on_word_boundary() {
        assert_eq!(tail_at_word("bonjour le monde", 100), "bonjour le monde");
        assert_eq!(tail_at_word("bonjour le monde", 8), "monde");
        assert_eq!(tail_at_word("éèà éèà éèà", 5), "éèà");
    }
}
