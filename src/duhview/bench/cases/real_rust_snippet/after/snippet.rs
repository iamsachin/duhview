/// Up to `radius` bytes of context around the first case-insensitive match of `query` in `text`.
pub fn snippet(text: &str, query: &str, radius: usize) -> Option<String> {
    if query.is_empty() {
        return None;
    }
    let hay = text.to_lowercase();
    let needle = query.to_lowercase();
    let pos = hay.find(&needle)?;
    let start = floor_char_boundary(text, pos.saturating_sub(radius));
    let end = floor_char_boundary(text, (pos + needle.len() + radius).min(text.len()));
    Some(text[start..end].to_string())
}

fn floor_char_boundary(s: &str, mut i: usize) -> usize {
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}
