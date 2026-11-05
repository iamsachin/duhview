export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter((p) => p.length > 0)
    .map((p) => p[0])
    .join("")
    .toUpperCase();
}
