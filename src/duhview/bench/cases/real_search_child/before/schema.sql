CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  parent_id TEXT,            -- set for conversations spawned by a Task tool call
  title TEXT,
  updated_at INTEGER
);
CREATE TABLE messages (conversation_id TEXT, content TEXT);
CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,
  conversation_id TEXT,        -- the conversation that MADE the call (the parent)
  child_conversation_id TEXT,  -- the conversation the Task call spawned
  name TEXT,
  input TEXT
);
-- Full-text index of each Task call's description and prompt.
CREATE VIRTUAL TABLE task_index USING fts5(tool_call_id UNINDEXED, content);
