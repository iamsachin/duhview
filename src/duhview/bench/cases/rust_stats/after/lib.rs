pub fn average(xs: &[f64]) -> Option<f64> {
    if xs.is_empty() {
        return None;
    }
    Some(xs.iter().sum::<f64>() / xs.len() as f64)
}

/// Nearest-rank percentile, `p` in 0..=100.
pub fn percentile(xs: &[f64], p: f64) -> Option<f64> {
    if xs.is_empty() || !(0.0..=100.0).contains(&p) {
        return None;
    }
    let mut sorted = xs.to_vec();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let idx = (p / 100.0 * sorted.len() as f64) as usize;
    Some(sorted[idx])
}

pub fn first_word(s: &str) -> &str {
    s.split_whitespace().next().unwrap_or("")
}
