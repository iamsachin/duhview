export function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").toUpperCase();
}
