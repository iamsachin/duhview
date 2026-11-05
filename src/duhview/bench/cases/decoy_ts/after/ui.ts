const ICONS = {
  ok: '<svg viewBox="0 0 16 16"><path d="M2 8l4 4 8-8"/></svg>',
  err: '<svg viewBox="0 0 16 16"><path d="M3 3l10 10M13 3L3 13"/></svg>',
} as const;

export function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

export function setIcon(el: HTMLElement, kind: keyof typeof ICONS): void {
  el.innerHTML = ICONS[kind];
}

/** True for both null and undefined. */
export function isMissing(v: unknown): boolean {
  return v == null;
}

export async function fetchJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}
