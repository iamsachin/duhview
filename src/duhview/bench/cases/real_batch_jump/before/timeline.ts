export type Event =
  | { kind: 'message'; id: string; at: number }
  | { kind: 'task'; id: string; at: number };

/** Index of the timeline row to scroll to for a task id, or -1. */
export function rowForTask(rows: Event[], taskId: string): number {
  return rows.findIndex((e) => e.kind === 'task' && e.id === taskId);
}
