CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,
  name TEXT,
  input TEXT  -- raw tool input copied from the agent's log file; may be truncated or not valid JSON
);
CREATE VIRTUAL TABLE task_index USING fts5(tool_call_id UNINDEXED, content);
