use rusqlite::Connection;

pub fn startup(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(include_str!("schema.sql"))?;
    Ok(())
}
