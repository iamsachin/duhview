import { computed, ref, type Ref } from 'vue';

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

/** Elapsed time for a task; freezes at the moment `finish()` is called. No timers to clean up. */
export function useElapsed(startedAt: Ref<number | null>, running: Ref<boolean>) {
  const finishedAt = ref<number | null>(null);

  const elapsed = computed(() => {
    if (startedAt.value === null) return '';
    const end = running.value ? Date.now() : finishedAt.value ?? Date.now();
    return formatElapsed(end - startedAt.value);
  });

  function finish() {
    finishedAt.value = Date.now();
  }

  return { elapsed, finish };
}
