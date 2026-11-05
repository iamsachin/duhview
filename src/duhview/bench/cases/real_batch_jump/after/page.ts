import { collapseTasks, rowForTask, type Event } from './timeline';

/** Row index for the "jump to task" chip. */
export function jumpTarget(events: Event[], taskId: string): number {
  const rows = collapseTasks(events);
  return rowForTask(rows, taskId);
}
