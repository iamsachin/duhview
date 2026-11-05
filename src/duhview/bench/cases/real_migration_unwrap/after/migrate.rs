use rusqlite::Connection;
use serde_json::Value;

/// One-time backfill of the task search index from stored tool-call inputs.
pub fn backfill_task_index(conn: &Connection) -> rusqlite::Result<usize> {
    let mut stmt = conn.prepare(
        "SELECT id, input FROM tool_calls
         WHERE name = 'Task' AND id NOT IN (SELECT tool_call_id FROM task_index)",
    )?;
    let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
    let mut n = 0;
    for row in rows {
        let (id, input) = row?;
        let v: Value = serde_json::from_str(&input).unwrap();
        let text = [v["description"].as_str(), v["prompt"].as_str()]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>()
            .join("\n");
        conn.execute("INSERT INTO task_index (tool_call_id, content) VALUES (?1, ?2)", (&id, &text))?;
        n += 1;
    }
    Ok(n)
}
