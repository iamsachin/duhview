import { computed, onUnmounted, ref, type Ref } from 'vue';

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

export function useElapsed(startedAt: Ref<number | null>) {
  const now = ref(Date.now());
  const timer = setInterval(() => {
    now.value = Date.now();
  }, 1000);
  onUnmounted(() => clearInterval(timer));

  const elapsed = computed(() =>
    startedAt.value === null ? '' : formatElapsed(now.value - startedAt.value),
  );
  return { elapsed };
}
