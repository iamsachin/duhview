import { rowForTask, type Event } from './timeline';

/** Row index for the "jump to task" chip. */
export function jumpTarget(events: Event[], taskId: string): number {
  const rows = events;
  return rowForTask(rows, taskId);
}
