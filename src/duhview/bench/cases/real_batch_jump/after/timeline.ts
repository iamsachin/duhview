export type Event =
  | { kind: 'message'; id: string; at: number }
  | { kind: 'task'; id: string; at: number }
  | { kind: 'task-batch'; id: string; at: number; tasks: { id: string; at: number }[] };

/** Collapse runs of 3+ consecutive task events into one batch row. */
export function collapseTasks(events: Event[]): Event[] {
  const out: Event[] = [];
  let run: Event[] = [];
  const flush = () => {
    if (run.length >= 3) {
      out.push({ kind: 'task-batch', id: `batch-${run[0].id}`, at: run[0].at,
                 tasks: run.map((e) => ({ id: e.id, at: e.at })) });
    } else {
      out.push(...run);
    }
    run = [];
  };
  for (const e of events) {
    if (e.kind === 'task') {
      run.push(e);
    } else {
      flush();
      out.push(e);
    }
  }
  flush();
  return out;
}

/** Index of the timeline row to scroll to for a task id, or -1. */
export function rowForTask(rows: Event[], taskId: string): number {
  return rows.findIndex((e) => e.kind === 'task' && e.id === taskId);
}
