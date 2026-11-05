/// Up to `radius` bytes of context around the first match of `query` in `text`.
pub fn snippet(text: &str, query: &str, radius: usize) -> Option<String> {
    if query.is_empty() {
        return None;
    }
    let pos = text.find(query)?;
    let start = floor_char_boundary(text, pos.saturating_sub(radius));
    let end = floor_char_boundary(text, (pos + query.len() + radius).min(text.len()));
    Some(text[start..end].to_string())
}

fn floor_char_boundary(s: &str, mut i: usize) -> usize {
    while i > 0 && !s.is_char_boundary(i) {
        i -= 1;
    }
    i
}
