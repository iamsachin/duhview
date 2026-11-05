use rusqlite::Connection;

mod migrate;

pub fn startup(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(include_str!("schema.sql"))?;
    let indexed = migrate::backfill_task_index(conn)?;
    log::info!("indexed {indexed} task calls");
    Ok(())
}
