def fts_query(text: str) -> str:
    """Prefix-match phrase query; quotes are stripped so user input can't break the syntax."""
    return '"' + text.lower().replace('"', "") + '"*'


def list_conversations(conn, kind="primary", search=None):
    """kind: 'primary' (top-level only), 'child' (spawned tasks only) or 'all'.

    Search also matches a spawned task by its Task description/prompt via task_index.
    """
    conditions, params = [], []
    if search:
        like = f"%{search}%"
        conditions.append(
            "(c.title LIKE ?"
            " OR c.id IN (SELECT m.conversation_id FROM messages m WHERE m.content LIKE ?)"
            " OR c.id IN (SELECT t.conversation_id FROM tool_calls t"
            " JOIN task_index ti ON ti.tool_call_id = t.id WHERE ti.content MATCH ?))"
        )
        params += [like, like, fts_query(search)]
    if kind == "child":
        conditions.append("c.parent_id IS NOT NULL")
    elif kind != "all":
        conditions.append("c.parent_id IS NULL")
    where = " AND ".join(conditions) or "1=1"
    sql = f"SELECT c.id, c.title FROM conversations c WHERE {where} ORDER BY c.updated_at DESC"
    return conn.execute(sql, params).fetchall()
