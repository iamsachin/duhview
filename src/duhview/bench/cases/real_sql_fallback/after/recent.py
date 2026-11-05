import sqlite3

# Label precedence: explicit description, else the first 80 chars of the prompt,
# else a generic fallback so the UI never shows an empty label.
RECENT_SQL = """
SELECT tc.conversation_id,
       tc.id,
       COALESCE(json_extract(tc.input, '$.description'),
                substr(COALESCE(json_extract(tc.input, '$.prompt'), ''), 1, 80),
                'Background task') AS label,
       tc.created_at
FROM tool_calls tc
WHERE tc.name = 'Task'
  AND tc.child_conversation_id IS NOT NULL
ORDER BY tc.created_at DESC
LIMIT ?
"""


def recent_tasks(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    limit = max(1, min(limit, 100))
    rows = conn.execute(RECENT_SQL, (limit,)).fetchall()
    return [{"conversation_id": r[0], "id": r[1], "label": r[2], "created_at": r[3]} for r in rows]
