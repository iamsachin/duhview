def list_conversations(conn, kind="primary", search=None):
    """kind: 'primary' (top-level only), 'child' (spawned tasks only) or 'all'."""
    conditions, params = [], []
    if search:
        like = f"%{search}%"
        conditions.append(
            "(c.title LIKE ? OR c.id IN (SELECT m.conversation_id FROM messages m WHERE m.content LIKE ?))"
        )
        params += [like, like]
    if kind == "child":
        conditions.append("c.parent_id IS NOT NULL")
    elif kind != "all":
        conditions.append("c.parent_id IS NULL")
    where = " AND ".join(conditions) or "1=1"
    sql = f"SELECT c.id, c.title FROM conversations c WHERE {where} ORDER BY c.updated_at DESC"
    return conn.execute(sql, params).fetchall()
